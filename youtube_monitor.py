"""yt-dlp kullanarak YouTube kanalının Shorts videolarını çeker."""
import logging
import concurrent.futures
import yt_dlp
import config as _cfg

logger = logging.getLogger(__name__)

# Dinamik — app.py/onapp.py tarafından kanal değişince güncellenir
SHORTS_URL = f"https://www.youtube.com/channel/{_cfg.YOUTUBE_CHANNEL_ID}/shorts"

# Geriye dönük uyumluluk için alias
YOUTUBE_CHANNEL_ID = _cfg.YOUTUBE_CHANNEL_ID


def _raw_fetch(ydl_opts: dict, url: str) -> dict | None:
    """Tek bir yt-dlp fetch işlemi. Thread içinde çalışır."""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Fetch hatası: {e}")
        return None


def fetch_shorts(limit: int = 100) -> tuple[dict, list[dict]]:
    """
    Kanalın Shorts videolarını çeker.
    İKİ paralel fetch: biri lang=tr (Türkçe başlık), biri varsayılan (doğru izlenme).
    Sonuçlar video ID'sine göre birleştirilir.
    Döner: (kanal_bilgisi, video_listesi)
    """
    base = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1-{limit}",
        "ignoreerrors": True,
    }
    opts_tr = {**base, "extractor_args": {"youtube": {"lang": ["tr"]}}}
    opts_en = {**base}

    # İki fetch'i paralel başlat
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_tr = ex.submit(_raw_fetch, opts_tr, SHORTS_URL)
        fut_en = ex.submit(_raw_fetch, opts_en, SHORTS_URL)
        info_tr = fut_tr.result()
        info_en = fut_en.result()

    # Kanal bilgisi: en az biri başarılıysa al
    info_base = info_tr or info_en
    if not info_base:
        return {"name": "Bilinmiyor", "url": SHORTS_URL}, []

    channel_info = {
        "name": info_base.get("channel") or info_base.get("uploader") or "Bilinmiyor",
        "url": info_base.get("channel_url") or info_base.get("webpage_url") or SHORTS_URL,
    }

    # id → view_count haritası: lang=tr'siz fetch'ten (doğru sayılar)
    view_map: dict[str, int | None] = {}
    if info_en and "entries" in info_en:
        for e in info_en["entries"]:
            if e and e.get("id"):
                view_map[e["id"]] = e.get("view_count")

    # Başlık haritası: lang=tr fetch'ten
    title_map: dict[str, str] = {}
    if info_tr and "entries" in info_tr:
        for e in info_tr["entries"]:
            if e and e.get("id") and e.get("title"):
                title_map[e["id"]] = e["title"]

    # Birleştir: TR başlık + doğru izlenme
    # Kaynak olarak TR listesini kullan (sıralama ve upload_date için);
    # TR fetch yoksa EN listesine geri dön
    source_info = info_tr if (info_tr and "entries" in info_tr) else info_en
    if not source_info or "entries" not in source_info:
        logger.warning("Shorts listesi boş geldi.")
        return channel_info, []

    videos = []
    for entry in source_info.get("entries", []):
        if not entry or not entry.get("id"):
            continue
        vid_id = entry["id"]
        title = title_map.get(vid_id) or entry.get("title", "")
        view_count = view_map.get(vid_id)  # EN fetch'ten → doğru
        videos.append({
            "id":          vid_id,
            "title":       title,
            "url":         f"https://www.youtube.com/shorts/{vid_id}",
            "view_count":  view_count,
            "upload_date": entry.get("upload_date", ""),
        })

    logger.info(f"{len(videos)} short bulundu")
    return channel_info, videos


def verify_and_get_turkish_title(video_id: str) -> tuple[bool, str | None]:
    """
    Seçilen videonun kanala ait olup olmadığını doğrular VE Türkçe başlığı getirir.
    Döner: (kanal_uyumlu_mu, türkçe_başlık_veya_None)

    extractor_args lang=tr sadece bu tek-video çekiminde kullanılır;
    izlenme sayısı bu noktada önemsiz olduğundan sayı bozulması sorun yaratmaz.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"lang": ["tr"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            detail = ydl.extract_info(
                f"https://www.youtube.com/shorts/{video_id}", download=False
            )

        vid_channel_id = detail.get("channel_id", "")
        if vid_channel_id and vid_channel_id != YOUTUBE_CHANNEL_ID:
            logger.warning(
                f"Kanal uyuşmazlığı: video={vid_channel_id}, beklenen={YOUTUBE_CHANNEL_ID}"
            )
            return False, None

        turkish_title = detail.get("title") or None
        return True, turkish_title

    except Exception as e:
        logger.debug(f"Doğrulama/başlık hatası ({video_id}): {e}")
        return True, None  # Hata varsa devam et, başlık None
