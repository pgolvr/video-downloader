import logging
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("downloader")

# ---------- Caminhos ----------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

import platform
if platform.system() == "Windows":
    FFMPEG_DIR = str(BASE_DIR)
else:
    FFMPEG_DIR = "/usr/bin"

app = FastAPI(title="API de Download de Vídeos", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Verificação do FFmpeg na inicialização ----------
@app.on_event("startup")
def check_ffmpeg():
    ffmpeg = BASE_DIR / "ffmpeg.exe"
    ffprobe = BASE_DIR / "ffprobe.exe"
    log.info("BASE_DIR      = %s", BASE_DIR)
    log.info("DOWNLOAD_DIR  = %s", DOWNLOAD_DIR)
    log.info("ffmpeg.exe    existe? %s", ffmpeg.exists())
    log.info("ffprobe.exe   existe? %s", ffprobe.exists())
    log.info("yt-dlp        = %s", yt_dlp.version.__version__)
    if ffmpeg.exists():
        try:
            out = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, timeout=10)
            log.info("ffmpeg -version: %s", out.stdout.splitlines()[0] if out.stdout else out.stderr)
        except Exception as e:
            log.error("Falha ao executar ffmpeg.exe: %s", e)
    else:
        log.error("ffmpeg.exe NÃO encontrado em %s", BASE_DIR)

# ---------- Utilitários ----------
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return name[:100] or "video"

def safe_unlink(path: Path):
    try:
        path.unlink(missing_ok=True)
        log.debug("Arquivo removido: %s", path)
    except Exception as e:
        log.warning("Não consegui remover %s: %s", path, e)

def cleanup_job(job_prefix: str):
    """Remove SÓ os arquivos deste download (inclui .part, .mp3, etc.)."""
    for f in DOWNLOAD_DIR.glob(f"{job_prefix}*"):
        safe_unlink(f)

class YDLLogger:
    def debug(self, msg): log.debug("yt-dlp: %s", msg)
    def info(self, msg): log.info("yt-dlp: %s", msg)
    def warning(self, msg): log.warning("yt-dlp: %s", msg)
    def error(self, msg): log.error("yt-dlp: %s", msg)

# ---------- Opções para evitar bloqueios do YouTube ----------
COMMON_YDL_OPTS = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "referer": "https://www.youtube.com/",
}

# ---------- Rotas ----------
@app.get("/")
def root():
    return FileResponse(BASE_DIR / "index.html")

@app.post("/api/info")
def get_video_info(data: dict):
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    log.info("INFO: %s", url)
    try:
        ydl_opts = {
            "quiet": True, 
            "no_warnings": True, 
            "noplaylist": True,
            **COMMON_YDL_OPTS
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.exception("Erro em /api/info")
        raise HTTPException(status_code=400, detail=str(e))

    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") not in (None, "none") and f.get("ext") in ("mp4", "webm") and f.get("height"):
            formats.append({
                "format_id": str(f["format_id"]),
                "height": f["height"],
                "resolution": f.get("resolution") or f"{f.get('width')}x{f['height']}",
                "ext": f["ext"],
            })

    by_height = {}
    for f in formats:
        by_height[f["height"]] = f
    unique_formats = sorted(by_height.values(), key=lambda x: x["height"], reverse=True)

    return {
        "title": info.get("title", "Vídeo sem título"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Desconhecido"),
        "formats": unique_formats,
    }

@app.post("/api/download")
def download_video(data: dict):
    url = data.get("url")
    format_id = data.get("format_id")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    job_id = uuid.uuid4().hex
    outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    if format_id:
        fmt = f"{format_id}+ba/{format_id}/bv*+ba/b"
    else:
        fmt = "bv*+ba/b"

    log.info("DOWNLOAD job=%s url=%s format=%s", job_id, url, fmt)

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "ffmpeg_location": FFMPEG_DIR,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "logger": YDLLogger(),
        "quiet": False,
        "no_warnings": False,
        **COMMON_YDL_OPTS
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        log.exception("Erro no yt-dlp durante o download")
        cleanup_job(job_id)
        raise HTTPException(status_code=500, detail=f"Falha no download: {e}")

    final_path = None
    for d in info.get("requested_downloads") or []:
        p = d.get("filepath")
        if p and Path(p).exists():
            final_path = Path(p)
            break

    if final_path is None:
        candidates = [
            f for f in DOWNLOAD_DIR.glob(f"{job_id}.*")
            if f.suffix.lower() in (".mp4", ".mkv", ".webm") and not re.search(r"\.f\d+\.", f.name)
        ]
        if candidates:
            final_path = max(candidates, key=lambda f: f.stat().st_size)

    log.info("Arquivos do job na pasta: %s", [f.name for f in DOWNLOAD_DIR.glob(f"{job_id}*")])

    if final_path is None or not final_path.exists():
        log.error("Arquivo final não encontrado para o job %s", job_id)
        cleanup_job(job_id)
        raise HTTPException(status_code=500, detail="O download terminou mas o arquivo final não foi gerado.")

    size_mb = final_path.stat().st_size / 1024 / 1024
    log.info("✅ Arquivo final gerado: %s (%.1f MB)", final_path.name, size_mb)

    title = sanitize_filename(info.get("title", "video"))
    download_name = f"{title}{final_path.suffix}"
    media_type = "video/mp4" if final_path.suffix == ".mp4" else "application/octet-stream"

    return FileResponse(
        path=final_path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        background=BackgroundTask(cleanup_job, job_id),
    )

@app.post("/api/download-audio")
def download_audio(data: dict):
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")

    job_id = uuid.uuid4().hex
    outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    log.info("DOWNLOAD AUDIO job=%s url=%s", job_id, url)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "ffmpeg_location": FFMPEG_DIR,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": True,
        "logger": YDLLogger(),
        "quiet": False,
        "no_warnings": False,
        **COMMON_YDL_OPTS
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        log.exception("Erro no yt-dlp durante o download de áudio")
        cleanup_job(job_id)
        raise HTTPException(status_code=500, detail=f"Falha no download de áudio: {e}")

    # Procurar explicitamente pelo arquivo .mp3 gerado com este job_id
    mp3_candidates = list(DOWNLOAD_DIR.glob(f"{job_id}.mp3"))
    if mp3_candidates:
        final_path = mp3_candidates[0]
    else:
        # Fallback: procurar qualquer arquivo do job
        candidates = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if candidates:
            final_path = max(candidates, key=lambda f: f.stat().st_size)

    if final_path is None or not final_path.exists():
        log.error("Arquivo final de áudio não encontrado para o job %s", job_id)
        cleanup_job(job_id)
        raise HTTPException(status_code=500, detail="O download de áudio terminou mas o arquivo final não foi gerado.")

    size_mb = final_path.stat().st_size / 1024 / 1024
    log.info("✅ Arquivo de áudio final gerado: %s (%.1f MB)", final_path.name, size_mb)

    title = sanitize_filename(info.get("title", "audio"))
    download_name = f"{title}.mp3"
    
    return FileResponse(
        path=final_path,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        background=BackgroundTask(cleanup_job, job_id),
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)