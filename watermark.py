"""
Fotoğraf üzerine PNG ikon uygular — tam merkez, saydamlık korunur.
Kullanım:
  processed, tmp_files = apply_watermarks(photo_sources, icon_name)
  # ... yükle ...
  cleanup_temps(tmp_files)
"""
import logging
import os
import tempfile
from io import BytesIO

import requests as _http
from PIL import Image

logger = logging.getLogger(__name__)

ICONS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_RATIO   = 0.22   # ikon genişliği = fotoğraf genişliğinin %22'si
ICON_OPACITY = 0.80   # ikon saydamlığı


def list_icons() -> list[str]:
    """icons/ klasöründeki PNG/JPG dosya adlarını döndürür."""
    if not os.path.isdir(ICONS_DIR):
        return []
    return sorted(
        f for f in os.listdir(ICONS_DIR)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    )


def _load_icon(icon_name: str) -> Image.Image | None:
    path = os.path.join(ICONS_DIR, icon_name)
    if not os.path.exists(path):
        logger.warning(f"[watermark] İkon bulunamadı: {path}")
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception as e:
        logger.warning(f"[watermark] İkon açılamadı: {e}")
        return None


def _open_photo(source: str) -> Image.Image | None:
    """URL veya local path'ten RGBA image açar."""
    try:
        if source.startswith("http"):
            resp = _http.get(source, timeout=20)
            resp.raise_for_status()
            return Image.open(BytesIO(resp.content)).convert("RGBA")
        else:
            return Image.open(source).convert("RGBA")
    except Exception as e:
        logger.warning(f"[watermark] Foto açılamadı ({source[:60]}): {e}")
        return None


def apply_watermark(photo_source: str, icon_name: str) -> str | None:
    """
    Tek fotoğrafa watermark uygular.
    Döndürür: geçici JPEG dosya yolu, hata varsa None.
    """
    icon = _load_icon(icon_name)
    if icon is None:
        return None

    img = _open_photo(photo_source)
    if img is None:
        return None

    # İkonu oran bazlı boyutlandır
    iw = max(50, int(img.width * ICON_RATIO))
    ih = max(1,  int(iw * icon.height / icon.width))
    icon_r = icon.resize((iw, ih), Image.LANCZOS)

    # Opacity uygula
    r, g, b, a = icon_r.split()
    a = a.point(lambda v: int(v * ICON_OPACITY))
    icon_r = Image.merge("RGBA", (r, g, b, a))

    # Tam merkeze yerleştir
    x = (img.width  - iw) // 2
    y = (img.height - ih) // 2

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay.paste(icon_r, (x, y), icon_r)
    result = Image.alpha_composite(img, overlay).convert("RGB")

    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=ICONS_DIR + "/../tmp_wm")
        result.save(tmp.name, "JPEG", quality=92)
        return tmp.name
    except Exception:
        # tmp_wm klasörü yoksa sistem tmp'sine yaz
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        result.save(tmp.name, "JPEG", quality=92)
        return tmp.name


def apply_watermarks(photo_sources: list[str], icon_name: str) -> tuple[list[str], list[str]]:
    """
    Birden fazla fotoğrafa watermark uygular.
    Döndürür: (processed_paths, temp_files_to_cleanup)
    Watermark başarısız olan fotoğraf orijinal haliyle kalır (upload devam eder).
    """
    processed  = []
    temp_files = []
    for src in photo_sources:
        out = apply_watermark(src, icon_name)
        if out:
            processed.append(out)
            temp_files.append(out)
        else:
            logger.warning(f"[watermark] Atlandı (orijinal kullanılacak): {src[:60]}")
            processed.append(src)
    return processed, temp_files


def apply_video_watermark(video_path: str, icon_name: str) -> str:
    """
    Video üzerine watermark ekler, yeni geçici dosya yolu döndürür.
    Hata olursa orijinal video_path döner (upload devam eder).
    ffmpeg zaten kurulu olmalı.
    """
    icon_path = os.path.join(ICONS_DIR, icon_name)
    if not os.path.exists(icon_path):
        logger.warning(f"[watermark] Video için ikon bulunamadı: {icon_path}")
        return video_path

    import tempfile, subprocess
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        out_path = tmp.name

        # overlay=W/2-w/2:H/2-h/2 → tam merkez
        # scale=iw*0.22:-1 → video genişliğinin %22'si, oran korunur
        # format=rgba → şeffaflık desteği
        filter_complex = (
            f"[1:v]scale=iw*0.22:-1,format=rgba,"
            f"colorchannelmixer=aa={ICON_OPACITY}[wm];"
            f"[0:v][wm]overlay=W/2-w/2:H/2-h/2"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", icon_path,
            "-filter_complex", filter_complex,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-loglevel", "error",
            out_path,
        ]
        ret = subprocess.run(cmd, timeout=300)
        if ret.returncode == 0 and os.path.getsize(out_path) > 0:
            logger.info(f"[watermark] Video watermark uygulandı: {out_path}")
            return out_path
        else:
            logger.warning("[watermark] ffmpeg video watermark başarısız, orijinal kullanılıyor")
            try:
                os.remove(out_path)
            except Exception:
                pass
            return video_path
    except Exception as e:
        logger.warning(f"[watermark] Video watermark hatası: {e}")
        return video_path


def cleanup_temps(temp_files: list[str]) -> None:
    for path in temp_files:
        try:
            os.remove(path)
        except Exception:
            pass
