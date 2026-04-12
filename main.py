"""
YouTube Shorts → Facebook İnteraktif Bot
Kullanım: python main.py
"""
import logging
import os
import random
import sys
import threading
import time as time_module
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.prompt import Prompt

import config
from youtube_monitor import fetch_shorts, verify_and_get_turkish_title
from token_manager import refresh_all_page_tokens
from startup import show_startup_menu, save_last_channel
from video_downloader import download_video, delete_video
from facebook_poster import upload_video_instant, upload_video_scheduled, repost_to_page
from state import get_shared_ids, mark_shared
from fb_monitor import FBRepostBot

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("main")

console = Console()


# ─────────────────────────────────────────────
# Yardımcı fonksiyonlar
# ─────────────────────────────────────────────

def format_views(n) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_date(raw: str) -> str:
    """YYYYMMDD → DD/MM/YYYY"""
    if not raw or len(raw) != 8:
        return "-"
    return f"{raw[6:]}/{raw[4:6]}/{raw[:4]}"


def get_yesterday_str() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


def time_str_to_datetime(time_str: str) -> datetime:
    """HH:MM → bugün için datetime. Geçmişse kullanıcıya sorar."""
    now = datetime.now()
    h, m = map(int, time_str.split(":"))
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)

    if dt <= now:
        ans = Prompt.ask(
            f"  [yellow]⚠ {time_str} saati geçmiş, yarına planlanacak. Devam edilsin mi?[/yellow] "
            "([green]e[/green]/[red]h[/red])",
            default="e",
        )
        if ans.strip().lower() in ("e", "evet"):
            dt += timedelta(days=1)
        else:
            # Yeni saat iste
            return None
    return dt


def time_str_to_timestamp(time_str: str):
    """HH:MM → (unix_timestamp, datetime)."""
    dt = time_str_to_datetime(time_str)
    if dt is None:
        return None, None
    return int(dt.timestamp()), dt


# ─────────────────────────────────────────────
# Listeleri göster
# ─────────────────────────────────────────────

def display_lists(videos: list, shared_ids: set) -> list:
    """Listeleri gösterir. İzlenmeye göre sıralı listeyi döndürür."""
    yesterday = get_yesterday_str()

    # İzlenmeye göre sıralı ana liste (1'den itibaren numaralandırılır)
    sorted_videos = sorted(
        videos,
        key=lambda x: x.get("view_count") or 0,
        reverse=True,
    )

    # ── Dün yayınlananlar ──
    console.print("\n[bold cyan]📅  Dün Yayınlanan Shorts[/bold cyan]")
    yesterday_found = False
    for rank, v in enumerate(sorted_videos, 1):
        if v.get("upload_date") == yesterday:
            yesterday_found = True
            label = f"  {rank:>3}.  {v['title']}"
            if v["id"] in shared_ids:
                console.print(Text(label, style="strike dim green"))
            else:
                console.print(f"  [bold]{rank:>3}.[/bold]  {v['title']}")

    if not yesterday_found:
        console.print("  [dim]Dün yayınlanan short bulunamadı.[/dim]")

    # ── Tüm Shorts — izlenmeye göre (ilk 30) ──
    console.print("\n[bold cyan]📊  En Çok İzlenen 30 Short[/bold cyan]"
                  f"  [dim](toplam {len(sorted_videos)} short)[/dim]")

    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Başlık", min_width=45)
    table.add_column("İzlenme", justify="right", width=10)
    table.add_column("Tarih", width=12)

    for rank, v in enumerate(sorted_videos[:30], 1):
        shared = v["id"] in shared_ids
        style = "strike dim green" if shared else ""
        table.add_row(
            Text(str(rank), style=style),
            Text(v["title"], style=style),
            Text(format_views(v.get("view_count")), style=style),
            Text(format_date(v.get("upload_date", "")), style=style),
        )

    console.print(table)
    return sorted_videos


# ─────────────────────────────────────────────
# Kullanıcı giriş fonksiyonları
# ─────────────────────────────────────────────

def ask_target_pages(pages: list[dict]) -> list[dict]:
    """Hangi sayfalara paylaşım yapılacağını kullanıcıya sorar."""
    if not pages:
        console.print("[red]Config'de tanımlı FB sayfası yok.[/red]")
        return []

    console.print("\n[bold]📄 Paylaşım yapılacak sayfalar:[/bold]")
    for i, page in enumerate(pages, 1):
        name = page.get("name", page["page_id"])
        static = " [dim](statik token)[/dim]" if page.get("static_token") else ""
        console.print(f"  [bold]{i}.[/bold]  {name}{static}")

    console.print("  [bold]0.[/bold]  Tümü")

    while True:
        raw = Prompt.ask(
            "Sayfa numaralarını gir [bold](boşlukla ayır)[/bold] veya [bold]0[/bold] tümü için"
        ).strip()
        try:
            if raw == "0":
                return pages
            nums = list(dict.fromkeys(int(x) for x in raw.split()))
            selected = [pages[n - 1] for n in nums if 1 <= n <= len(pages)]
            if selected:
                return selected
            console.print("[red]  Geçersiz seçim.[/red]")
        except (ValueError, IndexError):
            console.print("[red]  Geçersiz giriş.[/red]")


def ask_selections(total: int) -> list[int]:
    while True:
        raw = Prompt.ask(
            "\n[bold yellow]Paylaşmak istediğin numaraları gir[/bold yellow] "
            "(boşlukla ayır, örn: [cyan]1 3 5[/cyan])"
        )
        try:
            nums = list(dict.fromkeys(int(x) for x in raw.split()))  # tekrar yok, sıra korunur
            valid = [n for n in nums if 1 <= n <= total]
            if valid:
                return valid
            console.print("[red]  Geçersiz numaralar. Tekrar dene.[/red]")
        except ValueError:
            console.print("[red]  Sadece sayı gir.[/red]")


def ask_time() -> str | None:
    """HH:MM döner. 0 girilirse None (anlık)."""
    while True:
        raw = Prompt.ask(
            "  Paylaşım saati [bold](HH:MM)[/bold] ya da [bold]0[/bold] anlık için"
        ).strip()
        if raw == "0":
            return None
        try:
            parts = raw.split(":")
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
            console.print("[red]  0-23 saat, 0-59 dakika gir.[/red]")
        except Exception:
            console.print("[red]  Format: HH:MM veya 0[/red]")


# ─────────────────────────────────────────────
# Video işleme
# ─────────────────────────────────────────────

scheduled_timers: list[threading.Timer] = []


def _upload_job(video: dict, file_path: str, pages: list[dict], posted_info: dict, channel_id: str = None) -> None:
    """Zamanlı iş: belirlenen saatte çalışır, upload eder, dosyayı siler."""
    console.print(f"\n[bold green]⏰ Zamanlı yükleme başlıyor:[/bold green] {video['title']}")
    for page in pages:
        post_id = upload_video_instant(page, file_path, video["title"], video["url"])
        icon = "[green]✓[/green]" if post_id else "[red]✗[/red]"
        console.print(f"  {icon} [{page.get('name', page['page_id'])}]")
        if post_id:
            post_url = f"https://www.facebook.com/{page['page_id']}/videos/{post_id}"
            posted_info["post_url"] = post_url
            posted_info["source_page"] = page["page_id"]
            mark_shared(video["id"], video["title"], channel_id, page["page_id"])
    delete_video(file_path)


def schedule_local(video: dict, file_path: str, pages: list[dict], scheduled_dt: datetime, posted_info: dict, channel_id: str = None) -> None:
    """Videoyu local timer ile verilen saatte upload eder."""
    delay = (scheduled_dt - datetime.now()).total_seconds()
    if delay < 0:
        delay = 0
    timer = threading.Timer(delay, _upload_job, args=[video, file_path, pages, posted_info, channel_id])
    timer.daemon = True
    timer.start()
    scheduled_timers.append(timer)
    console.print(f"  [green]✓[/green] {scheduled_dt.strftime('%d/%m %H:%M')}'de yüklenecek — program açık kalsın.")


def process_videos(videos: list, selected_nums: list[int], pages: list[dict] = None, channel_id: str = None) -> list[dict]:
    """Seçilen videoları indir ve FB'ye paylaş/planla. Repost için bilgi döndürür."""
    if pages is None:
        pages = config.FB_PAGES
    if channel_id is None:
        channel_id = config.YOUTUBE_CHANNEL_ID
    posted = []

    for num in selected_nums:
        video = videos[num - 1]
        console.print(
            f"\n[bold yellow]━━━  #{num}  {video['title']}  ━━━[/bold yellow]"
        )
        console.print(f"  [dim]İzlenme: {format_views(video.get('view_count'))} | "
                      f"Tarih: {format_date(video.get('upload_date', ''))}[/dim]")

        # Saat sor — geçmiş saat girilirse tekrar sor
        scheduled_dt = None
        while True:
            time_str = ask_time()
            if time_str is None:
                break  # anlık
            _, scheduled_dt = time_str_to_timestamp(time_str)
            if scheduled_dt is not None:
                break  # geçerli saat
            # None ise kullanıcı "h" dedi, tekrar sor

        # Kanal doğrulama + Türkçe başlık çekme (tek sorguda ikisi birden)
        console.print("  [dim]Kanal doğrulanıyor, Türkçe başlık alınıyor...[/dim]")
        valid, turkish_title = verify_and_get_turkish_title(video["id"])
        if not valid:
            console.print("  [red]✗ Bu video bu kanala ait değil, atlanıyor.[/red]")
            continue
        if turkish_title:
            if turkish_title != video["title"]:
                console.print(f"  [dim]Başlık (TR): {turkish_title}[/dim]")
            video = {**video, "title": turkish_title}  # Türkçe başlıkla güncelle

        console.print("  [cyan]İndiriliyor...[/cyan]")
        file_path = download_video(video["url"], video["id"])
        if not file_path:
            console.print("  [red]✗ İndirme başarısız, atlanıyor.[/red]")
            continue

        if time_str is None:
            # Anlık yükleme
            success_count = 0
            post_url = None
            source_page = None
            for page in pages:
                post_id = upload_video_instant(page, file_path, video["title"], video["url"])
                icon = "[green]✓[/green]" if post_id else "[red]✗[/red]"
                page_label = page.get("name", page["page_id"])
                console.print(f"  {icon} [{page_label}] → anlık")
                if post_id:
                    success_count += 1
                    post_url = f"https://www.facebook.com/{page['page_id']}/videos/{post_id}"
                    source_page = page["page_id"]
                    # (YouTube kanalı, FB sayfası) ikilisine özel kayıt
                    mark_shared(video["id"], video["title"], channel_id, page["page_id"])
            delete_video(file_path)
            if success_count > 0:
                posted.append({
                    "video": video,
                    "time_str": None,
                    "post_url": post_url,
                    "source_page": source_page,
                })
        else:
            # Yerel zamanlama — FB API değil, program içinde timer
            posted_info = {"video": video, "time_str": time_str, "post_url": None, "source_page": None}
            schedule_local(video, file_path, pages, scheduled_dt, posted_info, channel_id)
            posted.append(posted_info)

    return posted


def wait_for_scheduled_jobs() -> None:
    """Bekleyen zamanlı işler varsa program açık tutar."""
    active = [t for t in scheduled_timers if t.is_alive()]
    if not active:
        return
    console.print(f"\n[yellow]⏳ {len(active)} zamanlı paylaşım bekleniyor. Program açık kalıyor...[/yellow]")
    console.print("[dim]Çıkmak için Ctrl+C[/dim]")
    try:
        while any(t.is_alive() for t in scheduled_timers):
            time_module.sleep(10)
    except KeyboardInterrupt:
        console.print("\n[red]Zamanlı paylaşımlar iptal edildi.[/red]")


# ─────────────────────────────────────────────
# Repost
# ─────────────────────────────────────────────

def _do_repost(post_url: str, description: str, pages: list[dict]) -> None:
    """Zamanlı repost işi."""
    console.print(f"\n[bold green]⏰ Repost yapılıyor...[/bold green]")
    for page in pages:
        ok = repost_to_page(page, post_url, description)
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"  {icon} [{page.get('name', page['page_id'])}]")


def handle_repost(posted: list[dict]) -> None:
    if not posted:
        return

    ans = Prompt.ask(
        "\n[bold]Bu paylaşımları farklı hesaplardan repost etmek ister misiniz?[/bold] "
        "([green]e[/green]/[red]h[/red])",
        default="h",
    )
    if ans.strip().lower() not in ("e", "evet"):
        return

    pages = config.FB_PAGES
    if not pages:
        console.print("[red]Config'de tanımlı FB sayfası yok.[/red]")
        return

    # Sayfa listesi
    console.print("\n[bold]Repost yapılacak sayfalar:[/bold]")
    for i, page in enumerate(pages, 1):
        console.print(f"  [bold]{i}.[/bold]  {page.get('name', page['page_id'])}")

    while True:
        raw = Prompt.ask("Sayfa numaralarını gir (boşlukla ayır)")
        try:
            nums = [int(x) for x in raw.split()]
            selected_pages = [pages[n - 1] for n in nums if 1 <= n <= len(pages)]
            if selected_pages:
                break
            console.print("[red]  Geçersiz seçim.[/red]")
        except Exception:
            console.print("[red]  Geçersiz giriş.[/red]")

    # Repost aralığı — dakika cinsinden
    while True:
        raw = Prompt.ask("Repost aralığı [bold](dakika olarak, örn: 120)[/bold]").strip()
        try:
            minutes = int(raw)
            if minutes > 0:
                break
            console.print("[red]  Pozitif bir sayı gir.[/red]")
        except ValueError:
            console.print("[red]  Geçersiz giriş.[/red]")

    max_seconds = minutes * 60

    for info in posted:
        video = info["video"]
        post_url = info.get("post_url")

        console.print(f"\n[bold]{video['title']}[/bold]")

        if not post_url:
            console.print("  [yellow]⚠ Post URL henüz hazır değil (zamanlı yükleme bekleniyor), repost atlanıyor.[/yellow]")
            continue

        random_seconds = random.randint(0, max_seconds)
        repost_dt = datetime.now() + timedelta(seconds=random_seconds)
        console.print(f"  Repost zamanı: [cyan]{repost_dt.strftime('%d/%m/%Y %H:%M')}[/cyan]")

        description = config.FB_DESCRIPTION_TEMPLATE.format(
            title=video["title"], url=video["url"]
        )
        delay = random_seconds
        timer = threading.Timer(delay, _do_repost, args=[post_url, description, selected_pages])
        timer.daemon = True
        timer.start()
        scheduled_timers.append(timer)
        console.print(f"  [green]✓[/green] Planlandı — program açık kalsın.")


# ─────────────────────────────────────────────
# Ana akış (tek oturum)
# ─────────────────────────────────────────────

def run_session() -> None:
    """Tek bir oturumu çalıştırır."""

    # Başlangıç menüsü
    show_startup_menu()

    # Page token'ları User token ile otomatik yenile
    if config.FB_USER_ACCESS_TOKEN:
        console.print("\n[cyan]Facebook token'ları yenileniyor...[/cyan]")
        config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, config.FB_USER_ACCESS_TOKEN)
    else:
        console.print("[yellow]⚠ FB_USER_ACCESS_TOKEN tanımlı değil.[/yellow]")

    # Kullanılabilir sayfaları göster ve hangileri seçilecek sor
    active_pages = ask_target_pages(config.FB_PAGES)
    if not active_pages:
        console.print("[red]Hiç sayfa seçilmedi.[/red]")
        return

    console.print("\n[cyan]Shorts yükleniyor...[/cyan]")
    channel_info, videos = fetch_shorts(limit=200)

    # Son kullanılan kanalı kaydet
    save_last_channel(config.YOUTUBE_CHANNEL_ID, channel_info.get("name", "?"))

    if not videos:
        console.print("[red]Short bulunamadı veya kanal okunamadı.[/red]")
        return

    # Üstü çizili listesi: sadece (bu kanal → bu sayfalar) kombinasyonuna özel
    active_page_ids = [p["page_id"] for p in active_pages]
    shared_ids = get_shared_ids(config.YOUTUBE_CHANNEL_ID, active_page_ids)

    os.system("cls" if os.name == "nt" else "clear")
    console.print(
        f"[bold]📺 Kaynak Kanal:[/bold] [green]{channel_info['name']}[/green]"
        f"  [dim]{channel_info['url']}[/dim]"
    )
    sorted_videos = display_lists(videos, shared_ids)

    selected_nums = ask_selections(len(sorted_videos))
    posted = process_videos(sorted_videos, selected_nums, active_pages, config.YOUTUBE_CHANNEL_ID)
    handle_repost(posted)

    wait_for_scheduled_jobs()
    console.print("\n[bold green]✓ Tüm işlemler tamamlandı![/bold green]")


def wait_for_key() -> str:
    """Bir tuşa basılmasını bekler. Basılan tuşu döndürür."""
    console.print(
        "\n[dim]Yeni işlem için bir tuşa basın, çıkmak için [bold]Q[/bold]...[/dim]"
    )
    if os.name == "nt":
        import msvcrt
        return msvcrt.getch().decode("utf-8", errors="ignore").lower()
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ─────────────────────────────────────────────
# FB Repost Bot — terminal yönetimi
# ─────────────────────────────────────────────

_active_bots: list[FBRepostBot] = []


def _bot_start_wizard() -> None:
    """Yeni bot kurulum sihirbazı."""
    from startup import resolve_fb_page
    console.print("\n[bold]Yeni Repost Botu Kur[/bold]  [dim](0 → Geri)[/dim]")

    query = Prompt.ask("  Kaynak FB sayfa URL'si veya adı").strip()
    if query == "0":
        return

    if not config.FB_USER_ACCESS_TOKEN:
        console.print("[red]FB_USER_ACCESS_TOKEN tanımlı değil.[/red]")
        return

    console.print("  [cyan]Sayfa aranıyor...[/cyan]")
    page_info, resolve_err = resolve_fb_page(query, config.FB_USER_ACCESS_TOKEN)
    if not page_info:
        console.print(f"  [red]✗ Sayfa bulunamadı:[/red] {resolve_err}")
        return

    console.print(f"  [green]✓[/green] {page_info['name']} [dim]({page_info['page_id']})[/dim]")

    # Kaynak token — yönetilen sayfa mı yoksa user token mu?
    source_token = next(
        (p["access_token"] for p in config.FB_PAGES if p["page_id"] == page_info["page_id"]),
        config.FB_USER_ACCESS_TOKEN,
    )

    # Hedef sayfalar
    if not config.FB_PAGES:
        console.print("[red]Hedef sayfa tanımlı değil.[/red]")
        return

    console.print("\n  [bold]Hedef sayfalar:[/bold]")
    for i, p in enumerate(config.FB_PAGES, 1):
        console.print(f"  [bold]{i}.[/bold]  {p.get('name', p['page_id'])}")
    console.print("  [bold]0.[/bold]  Tümü")

    raw = Prompt.ask("  Numara gir (boşlukla ayır) veya 0").strip()
    if raw == "0":
        target_pages = list(config.FB_PAGES)
    else:
        try:
            nums = [int(x) for x in raw.split()]
            target_pages = [config.FB_PAGES[n - 1] for n in nums if 1 <= n <= len(config.FB_PAGES)]
        except Exception:
            console.print("[red]Geçersiz giriş.[/red]")
            return

    if not target_pages:
        console.print("[red]Hedef sayfa seçilmedi.[/red]")
        return

    bot = FBRepostBot(page_info["name"], page_info["page_id"], source_token, target_pages)
    bot.start()
    _active_bots.append(bot)
    console.print(f"\n  [green]✓ Bot başlatıldı:[/green] {page_info['name']} → "
                  f"{', '.join(p.get('name', p['page_id']) for p in target_pages)}")


def run_repost_bot_menu() -> None:
    """Terminal'den repost bot yönetimi."""
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        console.print(Panel("[bold magenta]FB Repost Bot Yönetimi[/bold magenta]", expand=False))

        if _active_bots:
            from rich.table import Table
            t = Table(box=None, show_header=True, header_style="bold magenta", padding=(0, 1))
            t.add_column("#", width=3)
            t.add_column("Kaynak Sayfa")
            t.add_column("Hedef")
            t.add_column("Durum", width=8)
            t.add_column("Repost", width=7, justify="right")
            t.add_column("Son Kontrol", width=12)
            t.add_column("Sonraki", width=12)
            for i, bot in enumerate(_active_bots, 1):
                status = "[green]Aktif[/green]" if bot.running else "[red]Durdu[/red]"
                targets = ", ".join(p.get("name", p["page_id"]) for p in bot.target_pages)
                t.add_row(str(i), bot.source_name, targets, status,
                          str(bot.posts_reposted), bot.last_check or "–", bot.next_check_str)
            console.print(t)
        else:
            console.print("[dim]  Aktif bot yok.[/dim]")

        console.print(
            "\n  [bold]N.[/bold]  Yeni bot başlat\n"
            + ("  [bold]D.[/bold]  Bot durdur\n"
               "  [bold]L.[/bold]  Bot logunu gör\n" if _active_bots else "")
            + "  [bold]0.[/bold]  Ana menüye dön [dim](botlar arka planda çalışmaya devam eder)[/dim]"
        )

        choice = Prompt.ask("Seçim").strip().lower()

        if choice == "0":
            break

        elif choice == "n":
            _bot_start_wizard()
            Prompt.ask("\n  [dim]Devam için Enter[/dim]")

        elif choice == "d" and _active_bots:
            console.print("\n  [bold]Durdurmak istediğin bot:[/bold]")
            for i, bot in enumerate(_active_bots, 1):
                console.print(f"  {i}. {bot.source_name}")
            raw = Prompt.ask("  Numara (0=iptal)").strip()
            if raw != "0":
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(_active_bots):
                        _active_bots[idx].stop()
                        console.print(f"  [yellow]Bot durduruldu.[/yellow]")
                except Exception:
                    pass
            Prompt.ask("\n  [dim]Devam için Enter[/dim]")

        elif choice == "l" and _active_bots:
            console.print("\n  [bold]Hangi botun logu:[/bold]")
            for i, bot in enumerate(_active_bots, 1):
                console.print(f"  {i}. {bot.source_name}")
            raw = Prompt.ask("  Numara (0=iptal)").strip()
            if raw != "0":
                try:
                    idx = int(raw) - 1
                    bot = _active_bots[idx]
                    console.print(f"\n[bold]{bot.source_name}[/bold] son 20 kayıt:")
                    for entry in bot.get_log(20):
                        color = {"success": "green", "error": "red",
                                 "warn": "yellow"}.get(entry["level"], "cyan")
                        console.print(f"  [dim]{entry['time']}[/dim] [{color}]{entry['text']}[/{color}]")
                except Exception:
                    pass
            Prompt.ask("\n  [dim]Devam için Enter[/dim]")


def main() -> None:
    """Ana döngü — Q basılana kadar oturumları tekrarlar."""
    while True:
        try:
            mode = show_startup_menu()
            if mode == "repost_bot":
                run_repost_bot_menu()
                continue   # Bot menüsünden döndükten sonra tekrar ana menü
            else:
                run_session()
        except KeyboardInterrupt:
            console.print("\n[yellow]İşlem iptal edildi.[/yellow]")

        key = wait_for_key()
        if key == "q":
            console.print("\n[bold]Görüşürüz! 👋[/bold]\n")
            break

        os.system("cls" if os.name == "nt" else "clear")


if __name__ == "__main__":
    main()
