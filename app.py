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
SESSION_STR = os.environ.get("TELEGRAM_SESSION", "").strip()
SESSION_FILE = Path("/tmp/dup_user.session.txt")
LIB_PATH = Path(os.environ.get("DUP_LIB_PATH", "/tmp/dup_lib.json"))
LIB_MAX = 400

app = FastAPI()
_tg = None
_tg_lock = asyncio.Lock()
_auth_client = None
_auth_hash = ""
_auth_phone = ""


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


class AuthPhone(BaseModel):
    phone: str


class AuthCode(BaseModel):
    phone: str = ""
    code: str
    password: str = ""


def check_secret(x_dup_secret: Optional[str]):
    if SECRET and x_dup_secret != SECRET:
        raise HTTPException(401, "bad secret")


def saved_session():
    if SESSION_STR:
        return SESSION_STR
    try:
        if SESSION_FILE.exists():
            s = SESSION_FILE.read_text().strip()
            if s:
                return s
    except Exception:
        pass
    return ""


def store_session(s: str):
    try:
        SESSION_FILE.write_text(s)
    except Exception:
        pass


def fpcalc(path: str):
    r = subprocess.run(
        ["fpcalc", "-json", "-length", "90", path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "fpcalc failed")[-200:])
    data = json.loads(r.stdout)
    fp = data.get("fingerprint") or ""
    if not fp:
        raise RuntimeError("אין טביעת סאונד")
    return fp, float(data.get("duration") or 0)


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
        LIB_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIB_PATH.write_text(json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass


def extract_audio(src: Path, dst: Path):
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", "90", "-f", "wav", str(dst)],
        capture_output=True, timeout=90
    )
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 100:
        raise RuntimeError("ffmpeg: " + ((r.stderr or b"").decode("utf-8", "ignore")[-120:] or "נכשל"))


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


def new_user_client(session=""):
    from pyrogram import Client
    kw = dict(
        name="dupuser",
        api_id=int(API_ID),
        api_hash=API_HASH,
        in_memory=True,
        no_updates=True,
    )
    if session:
        kw["session_string"] = session
    return Client(**kw)


async def tg_client():
    global _tg
    async with _tg_lock:
        if _tg is not None and getattr(_tg, "is_connected", False):
            return _tg
        if not (API_ID and API_HASH):
            raise RuntimeError("חסר TELEGRAM_API_ID או TELEGRAM_API_HASH")
        sess = saved_session()
        if not sess:
            raise RuntimeError("חסר חיבור משתמש. צריך טלפון וקוד חד־פעמי.")
        _tg = new_user_client(sess)
        try:
            await _tg.start()
        except Exception:
            _tg = None
            raise
        me = await _tg.get_me()
        if getattr(me, "is_bot", False):
            await _tg.stop()
            _tg = None
            raise RuntimeError("מחובר כבוט במקום כמשתמש")
        return _tg


async def pyro_download(m_or_id, dest: Path):
    client = await tg_client()
    folder = dest.parent
    folder.mkdir(parents=True, exist_ok=True)
    out = await client.download_media(m_or_id, file_name=str(folder) + "/")
    if not out:
        raise RuntimeError("הורדה ריקה")
    p = Path(out)
    dest.write_bytes(p.read_bytes())
    if p.resolve() != dest.resolve():
        try:
            p.unlink()
        except Exception:
            pass


async def download_media_obj(file_id: str, chat_id: str, mid: int, dest: Path):
    last = None
    if file_id:
        try:
            await pyro_download(file_id, dest)
            if dest.exists() and dest.stat().st_size >= 1000:
                return
        except Exception as e:
            last = e
    if not chat_id or not mid:
        raise last or RuntimeError("אין סרטון להורדה")
    client = await tg_client()
    m = await client.get_messages(int(chat_id), int(mid))
    if not is_video_msg(m):
        raise RuntimeError("not-video")
    await pyro_download(m, dest)
    if not dest.exists() or dest.stat().st_size < 1000:
        raise last or RuntimeError("הורדה נכשלה")


async def collect_from_chat(chat_id: str, start_mid: int, skip_keys, need: int = 3):
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
        return [], 0, "לא מצאתי הודעות בעמוד. המשתמש חייב להיות מנהל שם."
    items = []
    last_seen = start
    note = ""
    seen_video = 0
    for mid in range(start, max(0, start - 80), -1):
        last_seen = mid - 1
        try:
            m = await client.get_messages(ch, mid)
        except Exception as e:
            note = short_err(e)
            continue
        if not is_video_msg(m):
            continue
        seen_video += 1
        key = str(chat_id) + ":" + str(mid)
        if key in skip_keys:
            continue
        items.append(Item(
            key=key,
            file_id="",
            mid=mid,
            dur=int(getattr(getattr(m, "video", None), "duration", 0) or 0),
            ch=str(chat_id),
            label="",
        ))
        if len(items) >= need:
            break
    if not items and seen_video:
        note = "מצאתי סרטונים אבל כולם כבר נשמעו"
    elif not items:
        note = note or "לא מצאתי סרטון בטווח הזה"
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
    return {
        "ok": True,
        "lib": len(load_lib()),
        "strong": bool(API_ID and API_HASH),
        "user": bool(saved_session()),
    }


@app.post("/auth/start")
async def auth_start(body: AuthPhone, x_dup_secret: Optional[str] = Header(None)):
    global _auth_client, _auth_hash, _auth_phone
    check_secret(x_dup_secret)
    if not (API_ID and API_HASH):
        return {"ok": False, "error": "חסר API_ID"}
    phone = (body.phone or "").replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    if _auth_client is not None:
        try:
            await _auth_client.disconnect()
        except Exception:
            pass
        _auth_client = None
    c = new_user_client()
    await c.connect()
    sent = await c.send_code(phone)
    _auth_client = c
    _auth_hash = sent.phone_code_hash
    _auth_phone = phone
    return {"ok": True, "phone": phone, "note": "נשלח קוד לטלגרם. שלח את הקוד."}


@app.post("/auth/confirm")
async def auth_confirm(body: AuthCode, x_dup_secret: Optional[str] = Header(None)):
    global _tg, _auth_client, _auth_hash, _auth_phone
    check_secret(x_dup_secret)
    phone = (body.phone or _auth_phone or "").replace(" ", "")
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    if _auth_client is None or not _auth_hash:
        return {"ok": False, "error": "אין בקשת קוד פתוחה. שלח טלפון קודם."}
    from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
    try:
        try:
            await _auth_client.sign_in(phone or _auth_phone, _auth_hash, body.code.strip())
        except SessionPasswordNeeded:
            if not body.password:
                return {"ok": False, "need_password": True, "error": "יש סיסמת שני שלבים. שלח גם אותה."}
            await _auth_client.check_password(body.password)
        ss = await _auth_client.export_session_string()
        store_session(ss)
        try:
            await _auth_client.disconnect()
        except Exception:
            pass
        _auth_client = None
        _tg = None
        me = None
        c = new_user_client(ss)
        await c.start()
        me = await c.get_me()
        await c.stop()
        return {
            "ok": True,
            "session": ss,
            "user": (me.first_name if me else "") + ((" @" + me.username) if me and me.username else ""),
            "id": me.id if me else 0,
        }
    except PhoneCodeInvalid:
        return {"ok": False, "error": "קוד שגוי"}
    except PhoneCodeExpired:
        return {"ok": False, "error": "הקוד פג. שלח טלפון שוב."}
    except Exception as e:
        return {"ok": False, "error": short_err(e)}


@app.post("/scan")
async def scan(body: ScanIn, x_dup_secret: Optional[str] = Header(None)):
    check_secret(x_dup_secret)
    try:
        if not saved_session():
            return {
                "ok": False,
                "error": "חסר חיבור משתמש. צריך טלפון וקוד.",
                "pairs": [],
                "checked": 0,
                "lib": 0,
                "fps": [],
            }
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

        chat_id = str(body.chat_id or "").strip()
        next_cur = int(body.start_mid or 0)
        note = ""
        items = []
        if chat_id:
            try:
                items, next_cur, note = await collect_from_chat(chat_id, int(body.start_mid or 0), set(by_key.keys()), 3)
            except Exception as e:
                note = "קריאת עמוד: " + short_err(e)
        if not items:
            items = [it for it in (body.items or []) if it.key not in by_key and (it.file_id or (it.ch and it.mid))]

        if len(items) < 1:
            return {
                "ok": False,
                "error": note or "אין סרטונים חדשים לשמוע",
                "pairs": [],
                "checked": 0,
                "lib": len(by_key),
                "fps": [],
                "next_mid": next_cur,
            }

        heard = []
        errs = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for it in items[:3]:
                raw = td / (str(it.mid or "x") + ".bin")
                wav = td / (str(it.mid or "x") + ".wav")
                try:
                    await download_media_obj(it.file_id or "", it.ch or chat_id, it.mid, raw)
                    if not raw.exists() or raw.stat().st_size < 1000:
                        raise RuntimeError("קובץ ריק אחרי הורדה")
                    extract_audio(raw, wav)
                    try:
                        raw.unlink()
                    except Exception:
                        pass
                    fp, dur = fpcalc(str(wav))
                    row = row_of(it, fp, dur)
                    heard.append(row)
                    by_key[row["key"]] = row
                except Exception as e:
                    err = short_err(e)
                    if "not-video" in err:
                        continue
                    errs.append(err)

        all_rows = [by_key[k] for k in by_key if by_key[k].get("fp")]
        fresh_keys = set(x["key"] for x in heard if x.get("fp"))
        pairs = pair_rows(all_rows, fresh_keys)
        save_lib(all_rows)
        if not heard:
            return {
                "ok": False,
                "error": (errs[0] if errs else note) or "ראיתי סרטון אבל לא הצלחתי לקחת אותו",
                "pairs": [],
                "checked": 0,
                "lib": len(all_rows),
                "fps": [],
                "next_mid": next_cur,
            }
        return {
            "ok": True,
            "pairs": pairs,
            "checked": len(heard),
            "lib": len(all_rows),
            "fps": [{"key": r["key"], "fp": r["fp"], "mid": r.get("mid") or 0, "ch": r.get("ch") or "", "label": r.get("label") or "", "dur": r.get("dur") or 0} for r in heard],
            "next_mid": next_cur,
        }
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
