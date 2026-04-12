"""
Program başlangıç menüsü.
- Son kullanılan YouTube kanalını göster
- FB hesabı ekle / YouTube kanalı değiştir / devam et seçenekleri
"""
import os
import re
import logging
import requests
import yt_dlp

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

import config
from state import load_state, save_state
from token_manager import get_page_token

logger = logging.getLogger(__name__)
console = Console()


# ─────────────────────────────────────────────
# Son kullanılan kanal (state.json)
# ─────────────────────────────────────────────

def get_last_channel() -> dict:
    state = load_state()
    return state.get("last_channel", {"id": config.YOUTUBE_CHANNEL_ID, "name": "?"})


def save_last_channel(channel_id: str, channel_name: str) -> None:
    state = load_state()
    state["last_channel"] = {"id": channel_id, "name": channel_name}
    save_state(state)


# ─────────────────────────────────────────────
# .env güncelleme
# ─────────────────────────────────────────────

def update_env(key: str, value: str) -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# ─────────────────────────────────────────────
# YouTube kanal ID bul
# ─────────────────────────────────────────────

def resolve_youtube_channel(query: str) -> dict | None:
    """
    Kullanıcı adı, @handle veya URL'den YouTube kanal bilgisini çözer.
    Döner: {"id": "UC...", "name": "Kanal Adı"} veya None
    """
    query = query.strip()

    # URL değilse @ veya /channel/ formatına çevir
    if not query.startswith("http"):
        if not query.startswith("@"):
            query = f"@{query}"
        url = f"https://www.youtube.com/{query}"
    else:
        url = query

    # /videos, /shorts gibi suffixleri temizle
    url = re.sub(r"/(videos|shorts|about|community).*$", "", url.rstrip("/"))

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": "1",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        channel_id = info.get("channel_id") or info.get("uploader_id")
        channel_name = info.get("channel") or info.get("uploader") or query

        if channel_id:
            return {"id": channel_id, "name": channel_name}
        return None

    except Exception as e:
        logger.error(f"YouTube kanal çözümleme hatası: {e}")
        return None


# ─────────────────────────────────────────────
# Facebook sayfa ID bul
# ─────────────────────────────────────────────

def resolve_fb_page(query: str, user_token: str) -> tuple[dict | None, str]:
    """
    Facebook URL, sayfa adı veya numeric ID'den page_id ve name çözer.
    Returns: (result_dict_or_None, error_message)
    """
    import re as _re
    query = query.strip().rstrip("/")

    # ── Slug / ID çıkar ───────────────────────────────────────────────────
    if "facebook.com/" in query:
        after = query.split("facebook.com/")[-1].split("?")[0].strip("/")
        if after.startswith("profile.php"):
            import urllib.parse as _up
            qs = _up.parse_qs(query.split("?")[-1])
            page_slug = qs.get("id", [after])[0]
        else:
            page_slug = after
    else:
        page_slug = query.strip()

    # Boşluk veya özel karakter içeriyorsa geçersiz
    if not page_slug:
        return None, "Geçersiz URL veya sayfa adı."

    is_numeric = _re.fullmatch(r"\d+", page_slug) is not None
    errors = []

    def _get(url, params):
        resp = requests.get(url, params=params, timeout=15)
        return resp.json()

    # ── Yöntem 1: Numeric ID → direkt node sorgusu ────────────────────────
    if is_numeric:
        try:
            data = _get(
                f"https://graph.facebook.com/v19.0/{page_slug}",
                {"fields": "id,name", "access_token": user_token},
            )
            if "id" in data:
                return {"page_id": data["id"], "name": data.get("name", page_slug)}, ""
            err = data.get("error", {})
            errors.append(f"ID lookup: {err.get('message', str(data))}")
        except Exception as e:
            errors.append(f"ID lookup exception: {e}")

    # ── Yöntem 2: Slug → direkt node sorgusu ─────────────────────────────
    if not is_numeric:
        try:
            data = _get(
                f"https://graph.facebook.com/v19.0/{page_slug}",
                {"fields": "id,name", "access_token": user_token},
            )
            if "id" in data:
                return {"page_id": data["id"], "name": data.get("name", page_slug)}, ""
            err = data.get("error", {})
            errors.append(f"Slug lookup: {err.get('message', str(data))}")
        except Exception as e:
            errors.append(f"Slug lookup exception: {e}")

    # ── Yöntem 3: /me/accounts → yönetilen sayfalarda ara ────────────────
    try:
        data = _get(
            "https://graph.facebook.com/v19.0/me/accounts",
            {"access_token": user_token, "limit": 100},
        )
        for page in data.get("data", []):
            if (page.get("id") == page_slug or
                    page.get("name", "").lower() == page_slug.lower()):
                return {"page_id": page["id"], "name": page.get("name", page_slug)}, ""
        errors.append(f"/me/accounts: eşleşme yok ({len(data.get('data',[]))} sayfa)")
    except Exception as e:
        errors.append(f"/me/accounts exception: {e}")

    err_summary = " | ".join(errors)
    logger.error(f"resolve_fb_page başarısız [{page_slug}]: {err_summary}")
    return None, err_summary


# ─────────────────────────────────────────────
# Seçenek 1: Yeni FB hesabı ekle
# ─────────────────────────────────────────────

def add_facebook_page() -> bool:
    """False dönerse geri gidildi."""
    if not config.FB_USER_ACCESS_TOKEN:
        console.print("[red]FB_USER_ACCESS_TOKEN tanımlı değil. .env dosyasına ekle.[/red]")
        return False

    console.print("  [dim]Geri dönmek için 0 girin.[/dim]")
    query = Prompt.ask("  Facebook sayfa URL'si veya sayfa adı").strip()
    if query == "0":
        return False

    console.print("  [cyan]Sayfa bilgisi aranıyor...[/cyan]")
    page_info, resolve_err = resolve_fb_page(query, config.FB_USER_ACCESS_TOKEN)
    if not page_info:
        console.print(f"  [red]✗ Sayfa bulunamadı:[/red] {resolve_err}")
        return True

    page_id = page_info["page_id"]
    page_name = page_info["name"]
    console.print(f"  [green]✓ Bulundu:[/green] {page_name} ({page_id})")

    existing_ids = [p["page_id"] for p in config.FB_PAGES]
    if page_id in existing_ids:
        console.print("  [yellow]Bu sayfa zaten kayıtlı.[/yellow]")
        return True

    token, _ = get_page_token(page_id, config.FB_USER_ACCESS_TOKEN)
    if not token:
        console.print("  [yellow]⚠ Token alınamadı — sayfa eklendi ama token yok.[/yellow]")
        token = "MANUAL_TOKEN_REQUIRED"

    config.FB_PAGES.append({"page_id": page_id, "access_token": token, "name": page_name})
    pages_str = ",".join(f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES)
    update_env("FB_PAGES", pages_str)
    console.print(f"  [green]✓ {page_name} eklendi ve .env güncellendi.[/green]")
    return True


# ─────────────────────────────────────────────
# Seçenek 2: YouTube kanalını değiştir
# ─────────────────────────────────────────────

def change_youtube_channel() -> bool:
    """False dönerse geri gidildi."""
    console.print("  [dim]Geri dönmek için 0 girin.[/dim]")
    query = Prompt.ask(
        "  YouTube kanal URL'si, @kullanıcıadı veya kanal adı"
    ).strip()
    if query == "0":
        return False

    console.print("  [cyan]Kanal aranıyor...[/cyan]")
    channel = resolve_youtube_channel(query)
    if not channel:
        console.print("  [red]✗ Kanal bulunamadı.[/red]")
        return True

    console.print(f"  [green]✓ Bulundu:[/green] {channel['name']} ({channel['id']})")

    config.YOUTUBE_CHANNEL_ID = channel["id"]
    update_env("YOUTUBE_CHANNEL_ID", channel["id"])

    import youtube_monitor
    youtube_monitor.SHORTS_URL = f"https://www.youtube.com/channel/{channel['id']}/shorts"

    save_last_channel(channel["id"], channel["name"])
    console.print(f"  [green]✓ Kanal değiştirildi: {channel['name']}[/green]")
    return True


# ─────────────────────────────────────────────
# Seçenek 4: Facebook sayfası sil
# ─────────────────────────────────────────────

def remove_facebook_page() -> bool:
    """False dönerse geri gidildi."""
    if not config.FB_PAGES:
        console.print("  [yellow]Kayıtlı FB sayfası yok.[/yellow]")
        return False

    # İsimler yoksa user token ile çek
    if config.FB_USER_ACCESS_TOKEN:
        for page in config.FB_PAGES:
            if not page.get("name"):
                _, fetched_name = get_page_token(page["page_id"], config.FB_USER_ACCESS_TOKEN)
                page["name"] = fetched_name or page["page_id"]

    console.print("\n  [bold]Kayıtlı sayfalar:[/bold]")
    for i, page in enumerate(config.FB_PAGES, 1):
        name = page.get("name", page["page_id"])
        console.print(f"  [bold]{i}.[/bold]  {name}  [dim]({page['page_id']})[/dim]")
    console.print("  [dim]Geri dönmek için 0 girin.[/dim]")

    while True:
        raw = Prompt.ask("  Silmek istediğin sayfa numarası").strip()
        if raw == "0":
            return False
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(config.FB_PAGES):
                removed = config.FB_PAGES.pop(idx)
                name = removed.get("name", removed["page_id"])

                pages_str = ",".join(
                    f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES
                )
                update_env("FB_PAGES", pages_str)
                console.print(f"  [green]✓ {name} silindi.[/green]")
                return True
            console.print("  [red]Geçersiz numara.[/red]")
        except ValueError:
            console.print("  [red]Geçersiz giriş.[/red]")


# ─────────────────────────────────────────────
# Ana başlangıç menüsü
# ─────────────────────────────────────────────

def show_startup_menu() -> str:
    """'session' veya 'repost_bot' döner."""
    """Başlangıç menüsünü gösterir. 'session' veya 'repost_bot' döner."""
    last = get_last_channel()

    if last.get("name") == "?" and config.YOUTUBE_CHANNEL_ID:
        last = {"id": config.YOUTUBE_CHANNEL_ID, "name": "?"}

    console.print(
        Panel(
            "[bold green]YouTube Shorts → Facebook Bot[/bold green]",
            subtitle=f"[dim]Son kullanılan: [cyan]{last['name']}[/cyan][/dim]",
            expand=False,
        )
    )

    console.print(
        f"\n  [bold]1.[/bold]  [cyan]{last['name']}[/cyan] ile devam et\n"
        f"  [bold]2.[/bold]  YouTube kanalını değiştir\n"
        f"  [bold]3.[/bold]  Yeni Facebook sayfa ekle"
        f"  [dim](Business Manager'da sayfayı portföye eklemeyi unutmayın)[/dim]\n"
        f"  [bold]4.[/bold]  Facebook sayfa sil\n"
        f"  [bold]5.[/bold]  [magenta]FB Repost Bot[/magenta]"
        f"  [dim](FB sayfası izle → otomatik repost)[/dim]\n"
    )

    while True:
        choice = Prompt.ask("Seçim", choices=["1", "2", "3", "4", "5"], default="1")

        if choice == "1":
            return "session"
        elif choice == "5":
            return "repost_bot"
        elif choice == "2":
            result = change_youtube_channel()
            if result:
                return "session"
        elif choice == "3":
            result = add_facebook_page()
            if result:
                return "session"
        elif choice == "4":
            result = remove_facebook_page()
            if result:
                return "session"
