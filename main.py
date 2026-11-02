import logging
import os
import re
import time
import uuid
import hashlib
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("omnidrop")

# ---------- Caminhos ----------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ---------- RapidAPI (Auto / Download All in One - coder2077) ----------
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "instagram-downloader-download-instagram-videos-stories1.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"
HTTP_TIMEOUT = 40  # a API espera até 30s na plataforma de origem

app = FastAPI(title="OmniDrop API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utilitários ----------
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return name[:100] or "video"

def safe_unlink(path: Path):
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("Não consegui remover %s: %s", path, e)

def cleanup_job(job_prefix: str):
    for f in DOWNLOAD_DIR.glob(f"{job_prefix}*"):
        safe_unlink(f)

def api_headers():
    if not RAPIDAPI_KEY:
        raise HTTPException(status_code=500, detail="RAPIDAPI_KEY não configurada no servidor.")
    return {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# ---------- Cache curto (evita pagar 2x pelo mesmo link) ----------
_CACHE = {}
CACHE_TTL = 240  # segundos

def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()

def cache_put(url: str, value: dict):
    _CACHE[_cache_key(url)] = (time.time(), value)

def cache_get(url: str):
    item = _CACHE.get(_cache_key(url))
    if item and (time.time() - item[0]) < CACHE_TTL:
        return item[1]
    return None

# ---------- Chamadas à RapidAPI ----------
def fetch_social(url: str) -> dict:
    """GET /fetch -> Instagram, TikTok, Facebook, Pinterest, X."""
    try:
        r = requests.get(f"{RAPIDAPI_BASE}/fetch", headers=api_headers(),
                         params={"url": url}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"A API de download não respondeu: {e}")
    if r.status_code == 422:
        raise HTTPException(status_code=400, detail="Link inválido ou ausente.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Limite de créditos ou requisições atingido. Tente mais tarde.")
    try:
        data = r.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Resposta inválida da API de download.")
    if not data.get("ok", False):
        raise HTTPException(status_code=400,
                            detail=data.get("detail") or "Post privado, apagado ou plataforma não suportada.")
    return data

def download_to_file(download_url: str, job_id: str, ext: str) -> Path:
    """Baixa o arquivo para o disco (garante Content-Length p/ progresso real)."""
    path = DOWNLOAD_DIR / f"{job_id}.{ext}"
    try:
        with requests.get(download_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        safe_unlink(path)
        raise HTTPException(status_code=502, detail=f"Falha ao baixar o arquivo: {e}")
    if not path.exists() or path.stat().st_size == 0:
        safe_unlink(path)
        raise HTTPException(status_code=500, detail="O arquivo baixado veio vazio.")
    return path

def social_to_info(data: dict) -> dict:
    """Normaliza o JSON do /fetch para o formato que o frontend já entende."""
    typ = data.get("type")
    formats = []
    if typ == "album":
        for i, item in enumerate(data.get("items", [])):
            ext = "mp4" if item.get("type") == "video" else "jpg"
            formats.append({
                "format_id": str(i),
                "resolution": f"{'Vídeo' if item.get('type') == 'video' else 'Foto'} {i + 1}",
                "ext": ext,
            })
    else:
        ext = "mp4" if typ == "video" else ("gif" if typ == "gif" else "jpg")
        w, h = data.get("width"), data.get("height")
        res = f"{w}x{h}" if w and h else "Qualidade original"
        formats.append({"format_id": "0", "resolution": res, "ext": ext})

    return {
        "title": (data.get("caption") or "Post sem título")[:100],
        "thumbnail": data.get("thumbnail_url") or "",
        "duration": data.get("duration") or 0,
        "uploader": data.get("source") or "Rede social",
        "formats": formats,
        "music_url": data.get("music_url"),
    }

# ---------- Rotas ----------
@app.get("/")
def root():
    return FileResponse(BASE_DIR / "index.html")

@app.post("/api/info")
def get_info(data: dict):
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")
    log.info("INFO social: %s", url)
    raw = fetch_social(url)
    cache_put(url, raw)
    return social_to_info(raw)

@app.post("/api/download")
def download(data: dict):
    url = (data.get("url") or "").strip()
    format_id = str(data.get("format_id") or "0")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    raw = cache_get(url) or fetch_social(url)
    cache_put(url, raw)
    typ = raw.get("type")

    if typ == "album":
        idx = int(format_id) if format_id.isdigit() else 0
        items = raw.get("items", [])
        if not (0 <= idx < len(items)):
            raise HTTPException(status_code=400, detail="Formato inválido para este álbum.")
        item = items[idx]
        dl_url = item.get("download_url")
        ext = "mp4" if item.get("type") == "video" else "jpg"
    else:
        dl_url = raw.get("download_url")
        ext = "mp4" if typ == "video" else ("gif" if typ == "gif" else "jpg")

    if not dl_url:
        raise HTTPException(status_code=400, detail="Este post não tem arquivo para download.")

    job_id = uuid.uuid4().hex
    log.info("DOWNLOAD social job=%s type=%s ext=%s", job_id, typ, ext)
    path = download_to_file(dl_url, job_id, ext)

    title = sanitize_filename(raw.get("caption") or "video")
    download_name = f"{title}.{ext}"
    media_type = {"mp4": "video/mp4", "jpg": "image/jpeg", "gif": "image/gif"}.get(ext, "application/octet-stream")

    return FileResponse(
        path=path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        background=BackgroundTask(cleanup_job, job_id),
    )

@app.post("/api/download-audio")
def download_audio(data: dict):
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    raw = cache_get(url) or fetch_social(url)
    music = raw.get("music_url") or (raw.get("music") or {}).get("download_url")
    if not music:
        raise HTTPException(status_code=400,
                            detail="Esta plataforma não oferece áudio separado. Use MP3 no YouTube ou no TikTok.")

    ext = "mp3" if ".mp3" in music.split("?")[0].lower() else "m4a"
    job_id = uuid.uuid4().hex
    log.info("DOWNLOAD audio social job=%s", job_id)
    path = download_to_file(music, job_id, ext)

    title = sanitize_filename(raw.get("caption") or "audio")
    download_name = f"{title}.{ext}"
    media_type = "audio/mpeg" if ext == "mp3" else "audio/mp4"

    return FileResponse(
        path=path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        background=BackgroundTask(cleanup_job, job_id),
    )

# ---------- YouTube (mesma API) ----------
FIXED_YT_FORMATS = ["2160p", "1440p", "1080p", "720p", "480p", "360p"]

@app.post("/api/youtube-info")
def youtube_info(data: dict):
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")
    log.info("INFO youtube: %s", url)

    info = None
    try:
        r = requests.get(f"{RAPIDAPI_BASE}/youtube/info", headers=api_headers(),
                         params={"url": url}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            j = r.json()
            if j.get("ok", True) is not False:
                info = j
    except (requests.RequestException, ValueError):
        info = None

    if info:
        title = info.get("title") or "Vídeo do YouTube"
        uploader = info.get("author") or info.get("uploader") or info.get("channel") or "YouTube"
        thumb = info.get("thumbnail_url") or info.get("thumbnail") or ""
        if not thumb and isinstance(info.get("thumbnails"), list) and info["thumbnails"]:
            thumb = info["thumbnails"][-1].get("url", "")
        duration = info.get("duration") or 0

        formats, seen = [], set()
        for f in (info.get("formats") or info.get("videos") or []):
            q = f.get("quality") or f.get("format") or f.get("label") or f.get("resolution")
            if not q or q in seen or q in ("audio", "mp3", "m4a"):
                continue
            seen.add(q)
            formats.append({"format_id": q, "resolution": q, "ext": "mp4"})
        if not formats:
            formats = [{"format_id": q, "resolution": q, "ext": "mp4"} for q in FIXED_YT_FORMATS]

        return {"title": title, "thumbnail": thumb, "duration": duration,
                "uploader": uploader, "formats": formats}

    # Fallback gratuito: oEmbed do YouTube (título, autor, thumbnail)
    title, uploader, thumb = "Vídeo do YouTube", "YouTube", ""
    try:
        oe = requests.get("https://www.youtube.com/oembed",
                          params={"url": url, "format": "json"}, timeout=10).json()
        title = oe.get("title") or title
        uploader = oe.get("author_name") or uploader
        thumb = oe.get("thumbnail_url") or thumb
    except (requests.RequestException, ValueError):
        pass

    return {"title": title, "thumbnail": thumb, "duration": 0, "uploader": uploader,
            "formats": [{"format_id": q, "resolution": q, "ext": "mp4"} for q in FIXED_YT_FORMATS]}

@app.post("/api/youtube-download")
def youtube_download(data: dict):
    url = (data.get("url") or "").strip()
    fmt = str(data.get("format_id") or data.get("format") or "1080p")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    log.info("DOWNLOAD youtube: %s formato=%s", url, fmt)
    try:
        r = requests.post(f"{RAPIDAPI_BASE}/youtube/download",
                          headers={**api_headers(), "Content-Type": "application/json"},
                          json={"url": url, "format": fmt}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"A API de download não respondeu: {e}")

    try:
        j = r.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Resposta inválida da API de download.")
    if not j.get("ok", False):
        raise HTTPException(status_code=400, detail=j.get("detail") or "Falha no download do YouTube.")

    dl_url = j.get("download_url")
    if not dl_url:
        raise HTTPException(status_code=500, detail="A API não retornou a URL do arquivo.")

    ext = "mp3" if fmt == "mp3" else "mp4"
    job_id = uuid.uuid4().hex
    path = download_to_file(dl_url, job_id, ext)

    title = sanitize_filename(j.get("title") or "video")
    download_name = f"{title}.{ext}"
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    return FileResponse(
        path=path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        background=BackgroundTask(cleanup_job, job_id),
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)