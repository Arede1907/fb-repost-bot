"""
User Access Token kullanarak Page Access Token'ları otomatik yeniler.
Page token'lar User token geçerli olduğu sürece çalışır.
"""
import logging
import os
import requests

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v19.0"


def get_page_token(page_id: str, user_access_token: str) -> str | None:
    """
    User Access Token kullanarak Page Access Token alır.
    Page token, user token geçerli olduğu sürece çalışır.
    """
    url = f"{GRAPH_URL}/{page_id}"
    params = {
        "fields": "access_token,name",
        "access_token": user_access_token,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if "access_token" in data:
            logger.info(f"[{page_id}] Page token yenilendi. Sayfa: {data.get('name', '')}")
            return data["access_token"], data.get("name", page_id)
        else:
            logger.error(f"[{page_id}] Page token alınamadı: {data}")
            return None, page_id

    except requests.RequestException as e:
        logger.error(f"[{page_id}] Token yenileme hatası: {e}")
        return None


def refresh_all_page_tokens(pages: list[dict], user_access_token: str) -> list[dict]:
    """
    Tüm sayfalar için Page Access Token ve sayfa adını yeniler.
    Token alınamazsa (erişim yok / başka hesabın sayfası) mevcut token korunur.
    """
    refreshed = []
    for page in pages:
        new_token, page_name = get_page_token(page["page_id"], user_access_token)
        if new_token:
            refreshed.append({
                "page_id": page["page_id"],
                "access_token": new_token,
                "name": page_name,
            })
        else:
            # Token yenilenemedi — statik token kullan, sayfa adını ID olarak göster
            existing_name = page.get("name", page["page_id"])
            logger.warning(f"[{page['page_id']}] Token yenilenemedi, statik token korunuyor.")
            refreshed.append({**page, "name": existing_name, "static_token": True})
    return refreshed
