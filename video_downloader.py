"""yt-dlp + proxy kullanarak YouTube videosunu indirir."""
import logging
import os
import yt_dlp
from config import DOWNLOAD_DIR

logger = logging.getLogger(__name__)


def download_video(video_url: str, video_id: str) -> str | None:
    """
    Videoyu indirir, dosya yolunu döndürür.
    Hata durumunda None döner.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    output_template = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

    ydl_opts = {
        "outtmpl":            output_template,
        "format":             "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet":              True,
        "no_warnings":        False,
        "js_runtimes":        {"node": {"path": "/usr/bin/node"}},
        "remote_components":  ["ejs:github"],
        # Farklı player client'ları dene — 'web' default'u bot detection tetikliyor
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "web_safari", "mweb"],
            }
        },
        # Residential proxy için optimize
        "concurrent_fragment_downloads": 5,   # paralel fragment = 3-5x hız
        "socket_timeout":                60,  # default 20s, proxy yavaş olduğu için artır
        "retries":                       10,  # info fetch retry
        "fragment_retries":              10,  # fragment download retry
        "http_chunk_size":               10485760,  # 10 MB chunk = daha az request
    }

    proxy = os.getenv("PROXY_URL", "")
    if proxy:
        ydl_opts["proxy"] = proxy
        logger.info(f"Proxy kullanılıyor: {proxy[:30]}...")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Video indiriliyor: {video_url}")
            ydl.download([video_url])

        raw_path = None
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(video_id) and fname.endswith(".mp4"):
                raw_path = os.path.join(DOWNLOAD_DIR, fname)
                break

        if not raw_path:
            logger.error("İndirme tamamlandı ama dosya bulunamadı.")
            return None

        # FB için H.264/AAC re-encode — işleme hatalarını önler
        final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}_final.mp4")
        ret = os.system(
            f'ffmpeg -y -i "{raw_path}" '
            f'-c:v libx264 -preset fast -crf 23 '
            f'-c:a aac -b:a 128k '
            f'-movflags +faststart '
            f'"{final_path}" -loglevel error'
        )
        if ret == 0 and os.path.exists(final_path):
            os.remove(raw_path)
            size_mb = os.path.getsize(final_path) / 1024 / 1024
            logger.info(f"Re-encode tamamlandı: {final_path} ({size_mb:.1f} MB)")
            return final_path
        else:
            logger.warning("ffmpeg başarısız, ham dosya kullanılıyor")
            return raw_path

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"İndirme hatası: {e}")
        return None


def delete_video(file_path: str) -> None:
    """Geçici dosyayı siler."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Silindi: {file_path}")
    except OSError as e:
        logger.warning(f"Dosya silinirken hata: {e}")
