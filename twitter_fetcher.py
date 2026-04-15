"""
Twitter/X public tweet bilgilerini çeker — login gerektirmez.
Twitter Syndication API kullanır (embed widget'ının çağırdığı public endpoint).
"""
import logging
import math
import os
import re
import requests

logger = logging.getLogger(__name__)

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

_TWEET_ID_RE = re.compile(
    r"(?:twitter\.com|x\.com)/[^/]+/status(?:es)?/(\d+)",
    re.IGNORECASE,
)


def extract_tweet_id(url: str) -> str | None:
    """Tweet URL'inden ID'yi çıkarır."""
    if not url:
        return None
    m = _TWEET_ID_RE.search(url.strip())
    return m.group(1) if m else None


def _compute_token(tweet_id: str) -> str:
    r"""Syndication API'nin beklediği token'ı hesaplar.
    Twitter'ın embed widget'ı şu formülü kullanıyor:
        token = ((id / 1e15) * pi).toString(36).replace(/(0+|\.)/g, '')
    """
    n = (int(tweet_id) / 1e15) * math.pi
    # Python'da float -> base36 dönüşümü manuel
    int_part = int(n)
    frac_part = n - int_part

    def _to_base36(num: int) -> str:
        if num == 0:
            return "0"
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while num > 0:
            out = digits[num % 36] + out
            num //= 36
        return out

    s_int = _to_base36(int_part)

    # Fraksiyonel kısmı base36'ya çevir (ilk 12 basamak yeterli)
    s_frac = ""
    for _ in range(12):
        frac_part *= 36
        d = int(frac_part)
        s_frac += "0123456789abcdefghijklmnopqrstuvwxyz"[d]
        frac_part -= d

    raw = f"{s_int}.{s_frac}"
    # 0+ ve . karakterlerini sil
    return re.sub(r"(0+|\.)", "", raw)


def fetch_tweet(url_or_id: str) -> dict:
    """
    Tweet bilgisini çeker.
    Döner:
      {
        "ok": True/False,
        "error": "...",  # ok=False ise hata mesajı
        "text": "tweet metni",
        "photos": ["url1", "url2", ...],
        "author": "kullanıcı adı",
        "has_video": True/False,
        "id": "tweet_id",
      }
    """
    tweet_id = extract_tweet_id(url_or_id) or (url_or_id if url_or_id.isdigit() else None)
    if not tweet_id:
        return {"ok": False, "error": "Geçerli bir tweet linki/ID değil"}

    token = _compute_token(tweet_id)
    params = {
        "id": tweet_id,
        "token": token,
        "lang": "en",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    proxies = None
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    try:
        resp = requests.get(SYNDICATION_URL, params=params, headers=headers,
                            proxies=proxies, timeout=20)
        if resp.status_code == 404:
            return {"ok": False, "error": "Tweet bulunamadı (silinmiş veya private olabilir)"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"API hatası: HTTP {resp.status_code}"}
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Tweet fetch hatası: {e}")
        return {"ok": False, "error": f"Bağlantı hatası: {e}"}
    except ValueError as e:
        return {"ok": False, "error": f"JSON parse hatası: {e}"}

    # Tweet metni
    text = data.get("text", "") or ""

    # Yazar
    user = data.get("user") or {}
    author = user.get("name") or user.get("screen_name") or ""
    screen_name = user.get("screen_name") or ""
    avatar_url = user.get("profile_image_url_https") or ""
    verified = bool(user.get("verified") or user.get("is_blue_verified"))
    created_at = data.get("created_at") or ""

    # Fotolar — syndication 'photos' alanı liste döndürür (her biri {url, width, height})
    photos_raw = data.get("photos") or []
    photos = []
    for p in photos_raw:
        if isinstance(p, dict) and p.get("url"):
            photos.append(p["url"])

    # Video / GIF kontrolü — mediaDetails altında type='video' olabilir
    has_video = False
    video_url = None
    video_duration_ms = 0
    for media in (data.get("mediaDetails") or []):
        if media.get("type") in ("video", "animated_gif"):
            has_video = True
            vinfo = media.get("video_info") or {}
            video_duration_ms = vinfo.get("duration_millis") or 0
            # En yüksek bitrate'li MP4 variant'ı seç
            best = None
            best_br = -1
            for v in (vinfo.get("variants") or []):
                if v.get("content_type") != "video/mp4":
                    continue
                br = v.get("bitrate") or 0
                if br > best_br:
                    best_br = br
                    best = v.get("url")
            if best:
                video_url = best
            break

    # t.co kısa linkleri tweet text'in sonunda kaldıysa temizle (medya linki)
    text = re.sub(r"\s*https?://t\.co/\S+\s*$", "", text).strip()

    return {
        "ok": True,
        "id": tweet_id,
        "text": text,
        "photos": photos,
        "author": author,
        "screen_name": screen_name,
        "avatar_url": avatar_url,
        "verified": verified,
        "created_at": created_at,
        "has_video": has_video,
        "video_url": video_url,
        "video_duration_ms": video_duration_ms,
    }
