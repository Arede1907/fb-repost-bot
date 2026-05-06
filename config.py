import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


def get_fb_pages() -> list[dict]:
    """FB_PAGES env değişkenini parse eder. Format: PAGE_ID:TOKEN,PAGE_ID:TOKEN"""
    raw = os.getenv("FB_PAGES", "").strip()
    if not raw:
        return []
    pages = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        page_id, token = entry.split(":", 1)
        pages.append({"page_id": page_id.strip(), "access_token": token.strip()})
    return pages


def enrich_page_names(pages: list[dict], user_token: str) -> list[dict]:
    """
    /me/accounts API'sinden sayfa isimlerini çekip pages listesine ekler.
    İsim zaten varsa dokunmaz.
    """
    if not pages or not user_token:
        return pages
    try:
        import requests
        resp = requests.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": user_token, "limit": 100},
            timeout=10,
        )
        data = resp.json()
        name_map = {p["id"]: p["name"] for p in data.get("data", [])}
        for page in pages:
            if not page.get("name"):
                page["name"] = name_map.get(page["page_id"], page["page_id"])
    except Exception:
        pass
    return pages


YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")
FB_USER_ACCESS_TOKEN = os.getenv("FB_USER_ACCESS_TOKEN", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))

FB_PAGES = get_fb_pages()
FB_POST_MODE = os.getenv("FB_POST_MODE", "instant")  # "instant" veya "scheduled"
FB_SCHEDULE_DELAY_MINUTES = int(os.getenv("FB_SCHEDULE_DELAY_MINUTES", "60"))
# \n ifadesini gerçek satır sonu olarak işle
FB_DESCRIPTION_TEMPLATE = os.getenv(
    "FB_DESCRIPTION_TEMPLATE", "{title}"
).replace("\\n", "\n")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Web UI giriş bilgileri
UI_USERNAME = os.getenv("UI_USERNAME", "admin")
UI_PASSWORD = os.getenv("UI_PASSWORD", "admin")

STATE_FILE = "./state.json"

# Facebook oturum cookie'leri (Repost Bot scraping için)
FB_COOKIE_C_USER = os.getenv("FB_COOKIE_C_USER", "")
FB_COOKIE_XS     = os.getenv("FB_COOKIE_XS", "")

# Gece modu — bu saatler arasında tüm işlemler duraklar (HH:MM formatı)
QUIET_HOURS_START = os.getenv("QUIET_HOURS_START", "")  # ör: "01:30"
QUIET_HOURS_END   = os.getenv("QUIET_HOURS_END", "")    # ör: "08:30"

# Twitter API credentials (OAuth 1.0a — developer.twitter.com)
TW_API_KEY             = os.getenv("TW_API_KEY", "")
TW_API_SECRET          = os.getenv("TW_API_SECRET", "")
TW_ACCESS_TOKEN        = os.getenv("TW_ACCESS_TOKEN", "")
TW_ACCESS_TOKEN_SECRET = os.getenv("TW_ACCESS_TOKEN_SECRET", "")


def is_quiet_hours() -> bool:
    """Şu an quiet hours (gece modu) içinde mi?"""
    if not QUIET_HOURS_START or not QUIET_HOURS_END:
        return False
    try:
        now = datetime.now().time()
        start = datetime.strptime(QUIET_HOURS_START, "%H:%M").time()
        end   = datetime.strptime(QUIET_HOURS_END, "%H:%M").time()
        if start <= end:
            return start <= now <= end
        else:
            # Gece yarısını geçen aralık (ör: 23:00 - 07:00)
            return now >= start or now <= end
    except ValueError:
        return False
