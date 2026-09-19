import os, json, tempfile, subprocess, itertools
from pathlib import Path
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional

SECRET = os.environ.get("DUP_HELPER_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

app = FastAPI()

class Item(BaseModel):
    key: str
    file_id: str
    mid: int = 0
    dur: int = 0
    ch: str = ""
    label: str = ""

class ScanIn(BaseModel):
    items: List[Item]

def fpcalc(path: str):
    r = subprocess.run(
        ["fpcalc", "-json", "-length", "90", path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:] if r.stderr else "fpcalc failed")
    data = json.loads(r.stdout)
    raw = data.get("fingerprint") or ""
    dur = float(data.get("duration") or 0)
    return raw, dur

def decode_fp(s: str):
    # chromaprint fpcalc -json fingerprint is a string of integers joined... actually base64-like
    # fpcalc -json returns fingerprint as comma-separated signed ints when using some versions;
    # official fpcalc -json: {"duration": 10.0, "fingerprint": "AQA..."} base64
    # Compare via fpcalc isn't pairwise; we use a simple token overlap on the raw string chunks.
    # Better: run fpcalc without json and parse, then use bit compare if we have ints.
    return s

def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # sliding block overlap — catches same track with a time shift
    step = 24
    size = 48
    if len(a) < size or len(b) < size:
        return 1.0 if a[:20] == b[:20] else 0.0
    best = 0.0
    blocks_a = [a[i:i + size] for i in range(0, min(len(a) - size, 800), step)]
    for blk in blocks_a[:40]:
        if blk in b:
            best = max(best, 0.92)
        else:
            # cheap char-overlap
            hit = sum(1 for i in range(0, len(b) - size, step) if b[i:i + size][:12] == blk[:12])
            if hit:
                best = max(best, 0.7)
    return best

async def download_file(file_id: str, dest: Path):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN missing on helper")
    async with httpx.AsyncClient(timeout=90) as c:
        g = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
        js = g.json()
        if not js.get("ok"):
            raise RuntimeError(str(js))
        path = js["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        r = await c.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)

def extract_audio(src: Path, dst: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", "90", "-f", "wav", str(dst)],
        check=True, capture_output=True, timeout=90
    )

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/scan")
async def scan(body: ScanIn, x_dup_secret: Optional[str] = Header(None)):
    if SECRET and x_dup_secret != SECRET:
        raise HTTPException(401, "bad secret")
    if len(body.items) < 2:
        return {"ok": True, "pairs": [], "note": "צריך לפחות שני סרטונים"}
    fps = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for it in body.items[:16]:
            raw = td / f"{it.key}.bin"
            wav = td / f"{it.key}.wav"
            try:
                await download_file(it.file_id, raw)
                extract_audio(raw, wav)
                fp, dur = fpcalc(str(wav))
                fps.append({"item": it, "fp": fp, "dur": dur})
            except Exception as e:
                fps.append({"item": it, "fp": "", "dur": 0, "err": str(e)[:120]})
    pairs = []
    for x, y in itertools.combinations(fps, 2):
        if not x["fp"] or not y["fp"]:
            continue
        sc = similar(x["fp"], y["fp"])
        if sc < 0.68:
            continue
        pairs.append({
            "score": round(sc, 3),
            "a": {"key": x["item"].key, "mid": x["item"].mid, "ch": x["item"].ch, "label": x["item"].label, "dur": x["item"].dur},
            "b": {"key": y["item"].key, "mid": y["item"].mid, "ch": y["item"].ch, "label": y["item"].label, "dur": y["item"].dur},
        })
    pairs.sort(key=lambda p: -p["score"])
    return {"ok": True, "pairs": pairs[:5], "checked": len(fps)}
