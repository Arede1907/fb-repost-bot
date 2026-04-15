"""
Twitter (veya başka kaynak) videosundan ffmpeg ile kareler çıkarır.
Kareler downloads/frames/<session_id>/ altında tutulur, paylaşımdan sonra silinir.
"""
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
FRAMES_ROOT = BASE_DIR / "downloads" / "frames"
FRAMES_ROOT.mkdir(parents=True, exist_ok=True)


def _download_video(video_url: str, dest: Path) -> bool:
    """
    Twitter video CDN (video.twimg.com) public — proxy gerekmiyor, doğrudan indir.
    Proxy ~12x yavaşlatıyor (test: 0.19s vs 2.3s).
    Direct fail olursa fallback olarak proxy'ye düş.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    proxy_url = os.getenv("PROXY_URL", "").strip()

    def _try(proxies):
        try:
            with requests.get(video_url, headers=headers, proxies=proxies,
                              stream=True, timeout=120) as r:
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            f.write(chunk)
            ok = dest.exists() and dest.stat().st_size > 0
            return ok, "ok" if ok else "empty"
        except requests.RequestException as e:
            return False, str(e)

    # 1) Önce direct
    ok, err = _try(None)
    if ok:
        return True
    logger.warning(f"Direct video download başarısız ({err}), proxy ile deneniyor...")

    # 2) Fallback: proxy
    if proxy_url:
        ok, err = _try({"http": proxy_url, "https": proxy_url})
        if ok:
            return True
        logger.error(f"Proxy ile de indirilemedi: {err}")
    else:
        logger.error(f"Direct download fail, proxy yok: {err}")
    return False


def _probe_duration(video_path: Path) -> float:
    """Video süresini saniye olarak döndürür. Hata varsa 0."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError) as e:
        logger.error(f"ffprobe hatası: {e}")
        return 0.0


def extract_frames(video_url: str, count: int = 8) -> dict:
    """
    Videoyu indirir, eşit aralıklı `count` kare çıkarır.
    Döner:
      {
        "ok": True/False,
        "error": "...",
        "session_id": "<uuid>",
        "frames": ["frame_0.jpg", "frame_1.jpg", ...],  # sadece dosya adları
        "duration": float,
      }
    """
    count = max(1, min(int(count), 20))
    session_id = uuid.uuid4().hex[:12]
    sess_dir = FRAMES_ROOT / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)

    video_path = sess_dir / "video.mp4"

    if not _download_video(video_url, video_path):
        cleanup_session(session_id)
        return {"ok": False, "error": "Video indirilemedi"}

    duration = _probe_duration(video_path)
    if duration <= 0:
        cleanup_session(session_id)
        return {"ok": False, "error": "Video süresi okunamadı"}

    # Süreyi count+2 parçaya böl, ilk/son segmenti atla (intro/outro skip)
    segments = count + 2
    seg_len = duration / segments
    timestamps = [seg_len * (i + 1) for i in range(count + 1)][:count]
    if not timestamps:
        timestamps = [duration / 2]

    frames: list[str] = []
    for i, ts in enumerate(timestamps):
        out_name = f"frame_{i}.jpg"
        out_path = sess_dir / out_name
        try:
            # -ss input öncesi = fast seek
            proc = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video_path),
                 "-vframes", "1", "-q:v", "2", str(out_path)],
                capture_output=True, timeout=60,
            )
            if out_path.exists() and out_path.stat().st_size > 0:
                frames.append(out_name)
            else:
                logger.warning(f"Frame {i} çıkarılamadı: {proc.stderr[:200]!r}")
        except subprocess.SubprocessError as e:
            logger.error(f"ffmpeg frame {i} hatası: {e}")

    if not frames:
        cleanup_session(session_id)
        return {"ok": False, "error": "Hiç frame çıkarılamadı"}

    # video.mp4'ü artık silebiliriz
    try:
        video_path.unlink()
    except OSError:
        pass

    return {
        "ok": True,
        "session_id": session_id,
        "frames": frames,
        "duration": duration,
    }


_SAFE_SID_RE = re.compile(r"^[a-f0-9]{6,32}$")
_SAFE_NAME_RE = re.compile(r"^frame_\d{1,3}\.jpg$")


def get_frame_path(session_id: str, filename: str) -> Path | None:
    """Güvenli bir şekilde frame dosya yolunu döndürür (path traversal koruma)."""
    if not _SAFE_SID_RE.match(session_id or ""):
        return None
    if not _SAFE_NAME_RE.match(filename or ""):
        return None
    p = FRAMES_ROOT / session_id / filename
    if not p.exists():
        return None
    return p


def get_session_dir(session_id: str) -> Path | None:
    if not _SAFE_SID_RE.match(session_id or ""):
        return None
    p = FRAMES_ROOT / session_id
    if not p.is_dir():
        return None
    return p


def cleanup_session(session_id: str) -> None:
    if not _SAFE_SID_RE.match(session_id or ""):
        return
    p = FRAMES_ROOT / session_id
    if p.is_dir():
        try:
            shutil.rmtree(p)
        except OSError as e:
            logger.warning(f"Cleanup hatası ({session_id}): {e}")


def cleanup_old_sessions(max_age_seconds: int = 3600) -> None:
    """1 saatten eski session klasörlerini sil."""
    now = time.time()
    if not FRAMES_ROOT.is_dir():
        return
    for child in FRAMES_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            age = now - child.stat().st_mtime
            if age > max_age_seconds:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass
