"""Facebook Graph API ile video Reels olarak yükler — anlık veya zamanlanmış."""
import logging
import os
import requests
from config import FB_DESCRIPTION_TEMPLATE

logger = logging.getLogger(__name__)

GRAPH_URL       = "https://graph.facebook.com/v19.0"
GRAPH_VIDEO_URL = "https://graph-video.facebook.com/v19.0"


def _upload_video(
    page: dict,
    video_path: str,
    title: str,
    youtube_url: str,
    scheduled_ts: int | None = None,
) -> str | None:
    """
    Videoyu Facebook Reels olarak yükler.
    1. video_reels endpoint ile upload session aç
    2. Binary olarak yükle
    3. Finish ile yayınla
    """
    page_id = page["page_id"]
    token   = page["access_token"]
    clean_title = title.replace("#shorts", "").replace("#Shorts", "").replace("#SHORTS", "").strip()
    description = FB_DESCRIPTION_TEMPLATE.format(title=clean_title, url=youtube_url)
    file_size   = os.path.getsize(video_path)

    # ── 1. Upload session başlat ──────────────────────────────────────────────
    try:
        init_resp = requests.post(
            f"{GRAPH_URL}/{page_id}/video_reels",
            data={
                "access_token":  token,
                "upload_phase":  "start",
                "file_size":     file_size,
            },
            timeout=30,
        )
        init_data = init_resp.json()
        if "video_id" not in init_data or "upload_url" not in init_data:
            logger.error(f"[{page_id}] Reels start hatası: {init_data}")
            return None
        video_id   = init_data["video_id"]
        upload_url = init_data["upload_url"]
    except Exception as e:
        logger.error(f"[{page_id}] Reels start isteği hatası: {e}")
        return None

    # ── 2. Videoyu yükle ─────────────────────────────────────────────────────
    try:
        with open(video_path, "rb") as f:
            upload_resp = requests.post(
                upload_url,
                headers={
                    "Authorization":        f"OAuth {token}",
                    "offset":               "0",
                    "file_size":            str(file_size),
                },
                data=f,
                timeout=300,
            )
        upload_data = upload_resp.json()
        if not upload_data.get("success"):
            logger.error(f"[{page_id}] Reels upload hatası: {upload_data}")
            return None
    except Exception as e:
        logger.error(f"[{page_id}] Reels upload isteği hatası: {e}")
        return None

    # ── 3. Finish — yayınla ──────────────────────────────────────────────────
    try:
        finish_data = {
            "access_token":  token,
            "upload_phase":  "finish",
            "video_id":      video_id,
            "title":         clean_title,
            "description":   description,
            "video_state":   "PUBLISHED",
        }
        if scheduled_ts:
            finish_data["video_state"]            = "SCHEDULED"
            finish_data["scheduled_publish_time"] = str(scheduled_ts)

        finish_resp = requests.post(
            f"{GRAPH_URL}/{page_id}/video_reels",
            data=finish_data,
            timeout=60,
        )
        finish_result = finish_resp.json()

        if finish_result.get("success"):
            mode = f"zamanlandı (ts={scheduled_ts})" if scheduled_ts else "Reels olarak yayınlandı"
            logger.info(f"[{page_id}] Başarıyla {mode}. Video ID: {video_id}")
            return video_id
        else:
            logger.error(f"[{page_id}] Reels finish hatası: {finish_result}")
            return None

    except Exception as e:
        logger.error(f"[{page_id}] Reels finish isteği hatası: {e}")
        return None


def upload_video_instant(page: dict, video_path: str, title: str, url: str) -> str | None:
    """Anlık Reels paylaşımı. Başarılıysa video ID döndürür, hata varsa None."""
    return _upload_video(page, video_path, title, url, scheduled_ts=None)


def upload_video_scheduled(
    page: dict, video_path: str, title: str, url: str, scheduled_ts: int
) -> str | None:
    """Zamanlanmış Reels paylaşımı. Başarılıysa video ID döndürür, hata varsa None."""
    return _upload_video(page, video_path, title, url, scheduled_ts=scheduled_ts)


def like_post_as_page(page: dict, post_id: str, user_token: str = "") -> bool | str:
    """
    Bir page'in (page token'ı ile) verilen post'u beğenmesini sağlar.
    `post_id` Graph API formatında olmalı: ya '{pageid}_{postid}' ya da düz id.
    Başarılıysa True, başarısızsa hata stringi döner.
    """
    import config as _cfg
    page_id = page["page_id"]
    token   = page.get("access_token", "") or user_token or _cfg.FB_USER_ACCESS_TOKEN

    if not post_id:
        return "post_id yok"

    try:
        response = requests.post(
            f"{GRAPH_URL}/{post_id}/likes",
            params={"access_token": token},
            timeout=20,
        )
        data = response.json()

        if response.status_code == 200 and data.get("success", False):
            logger.info(f"[{page_id}] Like başarılı: {post_id}")
            return True

        # Bazı API versiyonlarında 'success' alanı dönmez ama 200 + boş hata yeterli
        if response.status_code == 200 and "error" not in data:
            logger.info(f"[{page_id}] Like başarılı (success alanı yok): {post_id}")
            return True

        err = data.get("error", data)
        logger.error(f"[{page_id}] Like hatası: {err}")
        return str(err)

    except requests.RequestException as e:
        logger.error(f"[{page_id}] Like istek hatası: {e}")
        return str(e)


def _is_local_path(s: str) -> bool:
    """URL değil, local dosya yolu mu?"""
    return not (s.startswith("http://") or s.startswith("https://"))


def _upload_single_photo(page_id: str, token: str, photo: str,
                         caption: str = "", published: bool = True) -> tuple[bool, dict]:
    """Tek foto yükler. URL veya local path kabul eder. (ok, data) döner."""
    data = {"access_token": token, "published": "true" if published else "false"}
    if caption:
        data["caption"] = caption

    try:
        if _is_local_path(photo):
            with open(photo, "rb") as f:
                resp = requests.post(
                    f"{GRAPH_URL}/{page_id}/photos",
                    data=data,
                    files={"source": f},
                    timeout=120,
                )
        else:
            data["url"] = photo
            resp = requests.post(
                f"{GRAPH_URL}/{page_id}/photos",
                data=data,
                timeout=60,
            )
        rj = resp.json()
        if resp.status_code == 200 and ("id" in rj or "post_id" in rj):
            return True, rj
        return False, rj
    except (requests.RequestException, OSError) as e:
        return False, {"error": str(e)}


def upload_photo_to_page(page: dict, photo_urls: list[str],
                         caption: str = "", user_token: str = "") -> bool | str:
    """
    FB sayfasına foto post'u yükler.
    photo_urls listesi URL veya local file path içerebilir (karışık olabilir).
    - Tek foto: doğrudan /photos endpoint'i ile published=true
    - Çoklu foto: önce her birini published=false yükle, sonra /feed'e attached_media ile post et
    Başarılıysa True, hata varsa hata stringi döner.
    """
    import config as _cfg
    page_id = page["page_id"]
    token   = page.get("access_token", "") or user_token or _cfg.FB_USER_ACCESS_TOKEN

    if not photo_urls:
        return "Foto yok"

    # Tek foto — doğrudan yayınla
    if len(photo_urls) == 1:
        ok, data = _upload_single_photo(page_id, token, photo_urls[0],
                                        caption=caption, published=True)
        if ok:
            logger.info(f"[{page_id}] Foto yüklendi: {data.get('post_id') or data.get('id')}")
            return True
        err = data.get("error", data)
        logger.error(f"[{page_id}] Foto yükleme hatası: {err}")
        return str(err)

    # Çoklu foto — önce unpublished yükle, sonra feed'e attach et
    media_fbids = []
    for purl in photo_urls:
        ok, data = _upload_single_photo(page_id, token, purl, published=False)
        if ok and "id" in data:
            media_fbids.append(data["id"])
        else:
            err = data.get("error", data)
            logger.error(f"[{page_id}] Çoklu foto upload hatası: {err}")
            return f"Foto upload hatası: {err}"

    # Tüm fotoları içeren post'u yayınla
    try:
        feed_data = {
            "access_token": token,
            "message":      caption,
        }
        # FB attached_media[N]={"media_fbid": X} formatında bekliyor
        for i, fbid in enumerate(media_fbids):
            feed_data[f"attached_media[{i}]"] = '{"media_fbid":"' + fbid + '"}'

        resp = requests.post(
            f"{GRAPH_URL}/{page_id}/feed",
            data=feed_data,
            timeout=60,
        )
        data = resp.json()
        if resp.status_code == 200 and "id" in data:
            logger.info(f"[{page_id}] Çoklu foto post'u yayınlandı: {data['id']}")
            return True
        err = data.get("error", data)
        logger.error(f"[{page_id}] Feed post hatası: {err}")
        return str(err)
    except requests.RequestException as e:
        return str(e)


def repost_to_page(page: dict, post_url: str, description: str = "",
                   user_token: str = "") -> bool:
    """
    Mevcut bir FB postunu başka bir sayfada repost eder (link paylaşımı).
    """
    import config as _cfg
    page_id = page["page_id"]
    token   = page.get("access_token", "") or user_token or _cfg.FB_USER_ACCESS_TOKEN

    params = {
        "access_token": token,
        "link":         post_url,
        "message":      description,
    }

    try:
        response = requests.post(
            f"{GRAPH_URL}/{page_id}/feed",
            params=params,
            timeout=30,
        )
        data = response.json()

        if response.status_code == 200 and "id" in data:
            logger.info(f"[{page_id}] Repost başarılı. Post ID: {data['id']}")
            return True
        else:
            err = data.get("error", data)
            logger.error(f"[{page_id}] Repost hatası: {err}")
            return str(err)

    except requests.RequestException as e:
        logger.error(f"[{page_id}] Repost istek hatası: {e}")
        return False
