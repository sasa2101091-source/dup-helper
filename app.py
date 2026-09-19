import os, json, tempfile, subprocess, itertools, traceback, asyncio
from pathlib import Path
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional

SECRET = os.environ.get("DUP_HELPER_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
LIB_PATH = Path(os.environ.get("DUP_LIB_PATH", "/tmp/dup_lib.json"))
LIB_MAX = 400

app = FastAPI()
_tg = None
_tg_lock = asyncio.Lock()


class Item(BaseModel):
    key: str = ""
    file_id: str = ""
    mid: int = 0
    dur: int = 0
    ch: str = ""
    label: str = ""


class KnownFp(BaseModel):
    key: str = ""
    fp: str = ""
    mid: int = 0
    dur: int = 0
    ch: str = ""
    label: str = ""


class ScanIn(BaseModel):
    items: List[Item] = []
    known: Optional[List[KnownFp]] = None
    chat_id: str = ""
    start_mid: int = 0


def fpcalc(path: str):
    r = subprocess.run(
        ["fpcalc", "-json", "-length", "90", path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "fpcalc failed")[-200:])
    data = json.loads(r.stdout)
    return data.get("fingerprint") or "", float(data.get("duration") or 0)


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    step, size = 24, 48
    if len(a) < size or len(b) < size:
        return 1.0 if a[:20] == b[:20] else 0.0
    best = 0.0
    blocks_a = [a[i:i + size] for i in range(0, min(len(a) - size, 1200), step)]
    for blk in blocks_a[:50]:
        if blk in b:
            best = max(best, 0.92)
        else:
            hit = sum(1 for i in range(0, len(b) - size, step) if b[i:i + size][:12] == blk[:12])
            if hit:
                best = max(best, 0.7)
    return best


def load_lib():
    try:
        if LIB_PATH.exists():
            data = json.loads(LIB_PATH.read_text())
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_lib(rows):
    try:
        if len(rows) > LIB_MAX:
            rows = rows[-LIB_MAX:]
        LIB_PATH.write_text(json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass


def extract_audio(src: Path, dst: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", "90", "-f", "wav", str(dst)],
        check=True, capture_output=True, timeout=90
    )


def row_of(item, fp, dur):
    return {
        "key": item.key or (str(item.ch) + ":" + str(item.mid)),
        "fp": fp,
        "dur": dur,
        "mid": item.mid,
        "ch": item.ch,
        "label": item.label,
    }


def is_video_msg(m):
    if not m or getattr(m, "empty", False):
        return False
    if getattr(m, "video", None) or getattr(m, "animation", None) or getattr(m, "video_note", None):
        return True
    doc = getattr(m, "document", None)
    if doc and str(getattr(doc, "mime_type", "") or "").startswith("video"):
        return True
    return False


def short_err(e):
    s = str(e) or e.__class__.__name__
    s = s.replace("\n", " ")
    return s[:180]


async def tg_client():
    global _tg
    async with _tg_lock:
        if _tg is not None and getattr(_tg, "is_connected", False):
            return _tg
        if not (API_ID and API_HASH and BOT_TOKEN):
            raise RuntimeError("חסר TELEGRAM_API_ID או TELEGRAM_API_HASH")
        from pyrogram import Client
        if _tg is None:
            _tg = Client(
                "duphelper",
                api_id=int(API_ID),
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                in_memory=True,
                no_updates=True,
            )
        try:
            await _tg.start()
        except Exception as e:
            msg = str(e).lower()
            if "already" in msg or "started" in msg:
                return _tg
            _tg = None
            raise
        return _tg


async def download_small(file_id: str, dest: Path):
    async with httpx.AsyncClient(timeout=60) as c:
        g = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
        js = g.json()
        if not js.get("ok"):
            raise RuntimeError(str(js.get("description") or js))
        path = js["result"]["file_path"]
        r = await c.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}")
        r.raise_for_status()
        dest.write_bytes(r.content)


async def pyro_download(m_or_id, dest: Path):
    client = await tg_client()
    folder = dest.parent
    folder.mkdir(parents=True, exist_ok=True)
    out = await client.download_media(m_or_id, file_name=str(folder) + "/")
    if not out:
        raise RuntimeError("download empty")
    p = Path(out)
    if p.resolve() != dest.resolve():
        dest.write_bytes(p.read_bytes())
        try:
            p.unlink()
        except Exception:
            pass


async def download_media_obj(file_id: str, chat_id: str, mid: int, dest: Path):
    if file_id:
        try:
            await download_small(file_id, dest)
            return
        except Exception:
            try:
                await pyro_download(file_id, dest)
                return
            except Exception:
                pass
    if not chat_id or not mid:
        raise RuntimeError("אין סרטון להורדה")
    client = await tg_client()
    m = await client.get_messages(int(chat_id), int(mid))
    if not is_video_msg(m):
        raise RuntimeError("not-video")
    await pyro_download(m, dest)


async def collect_from_chat(chat_id: str, start_mid: int, limit_videos: int = 3):
    client = await tg_client()
    ch = int(str(chat_id).strip())
    start = int(start_mid or 0)
    if start < 1:
        for mid in (8000, 4000, 2000, 800, 200, 80, 20):
            try:
                m = await client.get_messages(ch, mid)
                if m and not getattr(m, "empty", False):
                    start = mid
                    break
            except Exception:
                continue
    if start < 1:
        return [], 0, "לא מצאתי הודעות בעמוד. הבוט חייב להיות מנהל שם."
    items = []
    last_seen = start
    note = ""
    for mid in range(start, max(0, start - 25), -1):
        last_seen = mid - 1
        try:
            m = await client.get_messages(ch, mid)
        except Exception as e:
            note = short_err(e)
            continue
        if not is_video_msg(m):
            continue
        items.append(Item(
            key=str(chat_id) + ":" + str(mid),
            file_id="",
            mid=mid,
            dur=int(getattr(getattr(m, "video", None), "duration", 0) or 0),
            ch=str(chat_id),
            label="",
        ))
        if len(items) >= limit_videos:
            break
    return items, (last_seen if last_seen > 0 else 0), note


def pair_rows(all_rows, fresh_keys):
    pairs = []
    for x, y in itertools.combinations(all_rows, 2):
        if x["key"] == y["key"]:
            continue
        if x["key"] not in fresh_keys and y["key"] not in fresh_keys:
            continue
        sc = similar(x["fp"], y["fp"])
        if sc < 0.68:
            continue
        pairs.append({
            "score": round(sc, 3),
            "a": {"key": x["key"], "mid": x.get("mid") or 0, "ch": x.get("ch") or "", "label": x.get("label") or "", "dur": x.get("dur") or 0},
            "b": {"key": y["key"], "mid": y.get("mid") or 0, "ch": y.get("ch") or "", "label": y.get("label") or "", "dur": y.get("dur") or 0},
        })
    pairs.sort(key=lambda p: -p["score"])
    return pairs[:8]


@app.get("/health")
def health():
    return {"ok": True, "lib": len(load_lib()), "strong": bool(API_ID and API_HASH and BOT_TOKEN)}


@app.post("/scan")
async def scan(body: ScanIn, x_dup_secret: Optional[str] = Header(None)):
    if SECRET and x_dup_secret != SECRET:
        raise HTTPException(401, "bad secret")
    try:
        lib = load_lib()
        known = list(body.known or [])
        by_key = {}
        for r in lib:
            k = str(r.get("key") or "")
            if k and r.get("fp"):
                by_key[k] = r
        for k in known:
            if k.key and k.fp:
                by_key[k.key] = row_of(k, k.fp, k.dur)

        items = list(body.items or [])
        next_cur = int(body.start_mid or 0)
        note = ""
        chat_id = str(body.chat_id or "").strip()
        if chat_id:
            try:
                found, next_cur, note = await collect_from_chat(chat_id, int(body.start_mid or 0), 3)
                if found:
                    items = found
            except Exception as e:
                note = "קריאת עמוד: " + short_err(e)

        usable = [it for it in items if (it.file_id or (it.ch and it.mid))]
        if len(usable) < 1:
            return {
                "ok": True,
                "pairs": [],
                "checked": 0,
                "lib": len(by_key),
                "fps": [],
                "next_mid": next_cur,
                "error": note or "אין סרטונים בסיבוב",
                "note": note or "אין סרטונים בסיבוב",
            }

        fresh = []
        errs = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for it in usable[:3]:
                raw = td / (str(it.mid or "x") + ".bin")
                wav = td / (str(it.mid or "x") + ".wav")
                try:
                    await download_media_obj(it.file_id or "", it.ch or chat_id, it.mid, raw)
                    if not raw.exists() or raw.stat().st_size < 1000:
                        raise RuntimeError("קובץ ריק")
                    extract_audio(raw, wav)
                    try:
                        raw.unlink()
                    except Exception:
                        pass
                    fp, dur = fpcalc(str(wav))
                    row = row_of(it, fp, dur)
                    fresh.append(row)
                    by_key[row["key"]] = row
                except Exception as e:
                    err = short_err(e)
                    if "not-video" in err:
                        continue
                    errs.append(err)
                    fresh.append({"key": it.key, "fp": "", "dur": 0, "mid": it.mid, "ch": it.ch, "label": it.label, "err": err})

        all_rows = [by_key[k] for k in by_key if by_key[k].get("fp")]
        fresh_keys = set(x["key"] for x in fresh if x.get("fp"))
        pairs = pair_rows(all_rows, fresh_keys)
        save_lib(all_rows)
        fps_out = [{"key": r["key"], "fp": r["fp"], "mid": r.get("mid") or 0, "ch": r.get("ch") or "", "label": r.get("label") or "", "dur": r.get("dur") or 0} for r in fresh if r.get("fp")]
        out = {
            "ok": True,
            "pairs": pairs,
            "checked": len(fresh),
            "lib": len(all_rows),
            "fps": fps_out,
            "next_mid": next_cur,
        }
        if not fps_out and errs:
            out["error"] = errs[0]
            out["note"] = errs[0]
        return out
    except Exception as e:
        return {
            "ok": False,
            "error": short_err(e),
            "detail": traceback.format_exc()[-400:],
            "pairs": [],
            "checked": 0,
            "lib": 0,
            "fps": [],
        }
