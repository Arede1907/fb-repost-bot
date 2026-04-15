"""
Flask Web UI — YouTube Shorts → Facebook Bot
Terminal versiyonu (main.py) bozulmadan çalışmaya devam eder.
Başlatmak için: python app.py
"""
import json
import os
import queue
import random
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta

from flask import (Flask, Response, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from functools import wraps

import config
from config import is_quiet_hours
import youtube_monitor as ym_module
from facebook_poster import upload_video_instant, repost_to_page, upload_photo_to_page
from twitter_fetcher import fetch_tweet
import video_frames
from startup import (get_last_channel, resolve_fb_page,
                     resolve_youtube_channel, save_last_channel, update_env)
from state import get_shared_ids, mark_shared
from token_manager import get_page_token, refresh_all_page_tokens
from video_downloader import delete_video, download_video
from youtube_monitor import fetch_shorts, verify_and_get_turkish_title
from fb_monitor import FBRepostBot

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "fbbot-flask-secret-change-me")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == config.UI_USERNAME and password == config.UI_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error="Kullanıcı adı veya şifre hatalı.")
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ─── Global state (tek kullanıcı) ─────────────────────────────────────────────
_videos: list[dict] = []
_channel_info: dict = {}
_progress: queue.Queue = queue.Queue()
_active_page_ids: list[str] = []
_operations: list[dict] = []          # Bu oturumdaki tüm işlemler
_repost_bots: dict[str, FBRepostBot] = {}   # bot_id → bot

# ─── Repost bot persistence ───────────────────────────────────────────────────
REPOST_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "repost_bots_state.json")
_state_lock = threading.Lock()


def _save_repost_bots_state() -> None:
    """Tüm repost bot durumlarını disk'e yazar (atomik)."""
    try:
        with _state_lock:
            data = {
                "version": 1,
                "saved_at": datetime.now().isoformat(),
                "bots": [b.to_state_dict() for b in _repost_bots.values()],
            }
            tmp = REPOST_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, REPOST_STATE_FILE)
    except Exception as e:
        print(f"[repost-state] save failed: {e}")


def _load_repost_bots_state() -> None:
    """Disk'ten repost botlarını restore eder.
    Önceden çalışıyor olanlar tekrar başlatılır; durmuş olanlar 'finished'
    olarak listede gösterilmek üzere yüklenir."""
    if not os.path.exists(REPOST_STATE_FILE):
        return
    try:
        with open(REPOST_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[repost-state] load failed: {e}")
        return

    restored_running  = 0
    restored_finished = 0
    for bot_data in data.get("bots", []):
        bot = FBRepostBot.from_state_dict(bot_data, config.FB_PAGES)
        if bot is None:
            print(f"[repost-state] bot atlandı (sayfa bulunamadı): "
                  f"{bot_data.get('bot_id')}")
            continue

        was_running = bool(bot_data.get("running", False))
        if was_running:
            # Bot crash'ten önce çalışıyordu — aynı şekilde tekrar başlat
            bot.finished = False
            bot.start()
            restored_running += 1
        else:
            # Bot durmuş halde kaydedilmişti — finished olarak listede tut
            bot.finished = True
            bot.running  = False
            restored_finished += 1

        _repost_bots[bot.bot_id] = bot

    if restored_running or restored_finished:
        print(f"[repost-state] {restored_running} bot tekrar başlatıldı, "
              f"{restored_finished} bot 'bitti' olarak yüklendi")


def _periodic_save_loop():
    """Arka plan thread'i — her 30 saniyede bir state'i diske yazar.
    Bu sayede crash anında en fazla 30s'lik seen_ids/queue kaybı olur."""
    while True:
        try:
            time.sleep(30)
            if _repost_bots:
                _save_repost_bots_state()
        except Exception as e:
            print(f"[repost-state] periodic save error: {e}")


def push(msg_type: str, text: str = "", **extra):
    """SSE kuyruğuna mesaj ekle."""
    _progress.put({"type": msg_type, "text": text, **extra})


# ─── Yardımcılar ──────────────────────────────────────────────────────────────

def format_views(n) -> str:
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_date(raw: str) -> str:
    if not raw or len(raw) != 8:
        return "–"
    return f"{raw[6:]}/{raw[4:6]}/{raw[:4]}"


def get_active_pages() -> list[dict]:
    if not _active_page_ids:
        return config.FB_PAGES
    return [p for p in config.FB_PAGES if p["page_id"] in _active_page_ids]


def get_yesterday_str() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


def enrich_videos(video_list: list[dict], shared_ids: set) -> list[dict]:
    """Video listesine rank, str alanları ve shared bayrağı ekler."""
    sorted_v = sorted(video_list, key=lambda x: x.get("view_count") or 0, reverse=True)
    for i, v in enumerate(sorted_v):
        v["rank"] = i + 1
        v["views_str"] = format_views(v.get("view_count"))
        v["date_str"] = format_date(v.get("upload_date", ""))
        v["shared"] = v["id"] in shared_ids
    return sorted_v


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    last = get_last_channel()
    return render_template(
        "index.html",
        channel=last,
        pages=config.FB_PAGES,
        active_page_ids=_active_page_ids,
        operations=list(reversed(_operations)),
    )


@app.route("/api/operations")
@login_required
def api_operations():
    """Ana sayfa için işlem listesi — JS polling ile kullanılır.
    ?batch=<id> ile sadece o batch'e ait işlemler döner."""
    batch = request.args.get("batch")
    ops = _operations
    if batch:
        ops = [o for o in ops if o.get("batch_id") == batch]
    else:
        ops = list(reversed(ops))
    return jsonify(ops)


@app.route("/select-pages", methods=["POST"])
@login_required
def select_pages():
    global _active_page_ids
    _active_page_ids = request.form.getlist("page_ids")
    return redirect(url_for("videos"))


@app.route("/videos")
@login_required
def videos():
    active_pages = get_active_pages()
    active_page_ids = [p["page_id"] for p in active_pages]
    shared_ids = get_shared_ids(config.YOUTUBE_CHANNEL_ID, active_page_ids)

    sorted_videos = enrich_videos(list(_videos), shared_ids)
    yesterday = get_yesterday_str()
    yesterday_videos = [v for v in sorted_videos if v.get("upload_date") == yesterday]

    return render_template(
        "videos.html",
        videos=sorted_videos,
        yesterday_videos=yesterday_videos,
        channel_info=_channel_info,
        loaded=bool(_videos),
        active_pages=active_pages,
    )


@app.route("/fetch-videos", methods=["POST"])
@login_required
def fetch_videos():
    """Arka planda video listesi çeker."""
    global _videos, _channel_info

    def _do():
        global _videos, _channel_info
        push("info", "Shorts listesi yükleniyor, lütfen bekleyin...")
        try:
            ch_info, vids = fetch_shorts(limit=200)
            _channel_info = ch_info
            _videos = vids
            save_last_channel(config.YOUTUBE_CHANNEL_ID, ch_info.get("name", "?"))
            push("done", f"{len(vids)} video yüklendi.", redirect="/videos")
        except Exception as e:
            push("error", f"Hata: {e}")

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/schedule", methods=["POST"])
@login_required
def schedule_page():
    """Seçilen videolar için sayfa-sayfa zaman ayarlama ekranı."""
    video_ids = request.form.getlist("video_ids")
    page_ids  = request.form.getlist("page_ids")

    if not video_ids:
        flash("En az bir video seçmelisin.", "warning")
        return redirect(url_for("videos"))

    video_map = {v["id"]: v for v in _videos}
    active_pages = get_active_pages()
    shared_ids = get_shared_ids(
        config.YOUTUBE_CHANNEL_ID,
        [p["page_id"] for p in active_pages],
    )
    selected = enrich_videos(
        [video_map[vid] for vid in video_ids if vid in video_map],
        shared_ids,
    )

    return render_template(
        "schedule.html",
        videos=selected,
        page_ids=page_ids,
        active_pages=active_pages,
        all_pages=config.FB_PAGES,   # repost sayfa seçimi için tüm sayfalar
    )


@app.route("/share", methods=["POST"])
@login_required
def share():
    """
    Seçilen videoları indir ve paylaş.
    Beklenen JSON:
      { "videos": [{"id": "...", "time_str": "HH:MM"|null}, ...],
        "page_ids": ["..."] }
    """
    if is_quiet_hours():
        return jsonify({"status": "error",
                        "msg": f"Gece modu aktif ({config.QUIET_HOURS_START}-{config.QUIET_HOURS_END}). "
                               f"Bu saatler arasında paylaşım yapılamaz."}), 403

    data = request.get_json()
    video_entries = data.get("videos", [])
    page_ids = data.get("page_ids", [])

    pages = [p for p in config.FB_PAGES if p["page_id"] in page_ids] if page_ids else config.FB_PAGES
    channel_id = config.YOUTUBE_CHANNEL_ID
    batch_id = uuid.uuid4().hex[:8]   # bu paylaşım grubunun kimliği

    def _do():
        for entry in video_entries:
            vid_id   = entry.get("id")
            time_str = entry.get("time_str") or None

            video = next((v for v in _videos if v["id"] == vid_id), None)
            if not video:
                continue

            # İşlemi kaydet
            op = {
                "batch_id":   batch_id,
                "title":      video["title"],
                "video_url":  video["url"],
                "pages":      [p.get("name", p["page_id"]) for p in pages],
                "status":     "uploading",
                "time_str":   time_str,
                "created_at": datetime.now().strftime("%H:%M"),
                "result_at":  None,
                "error":      None,
                "post_url":   None,   # ilk başarılı yüklemenin FB URL'si (repost için)
            }
            _operations.append(op)

            push("info", f"⏳ İşleniyor: {video['title']}")

            # Türkçe başlık + kanal doğrulama
            valid, tr_title = verify_and_get_turkish_title(video["id"])
            if not valid:
                op["status"] = "error"
                op["error"]  = "Kanal uyuşmazlığı"
                op["result_at"] = datetime.now().strftime("%H:%M")
                push("error", f"✗ Kanal uyuşmazlığı: {video['title']}")
                continue
            if tr_title:
                video = {**video, "title": tr_title}
                op["title"] = tr_title

            push("info", f"⬇ İndiriliyor: {video['title']}")
            file_path = download_video(video["url"], video["id"])
            if not file_path:
                op["status"] = "error"
                op["error"]  = "İndirme başarısız"
                op["result_at"] = datetime.now().strftime("%H:%M")
                push("error", f"✗ İndirme başarısız: {video['title']}")
                continue

            if time_str:
                # Zamanlanmış
                h, m = map(int, time_str.split(":"))
                dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if dt <= datetime.now():
                    dt += timedelta(days=1)
                delay = (dt - datetime.now()).total_seconds()
                op["status"]    = "scheduled"
                op["result_at"] = dt.strftime("%d/%m %H:%M")
                push("scheduled", f"🕐 {dt.strftime('%d/%m %H:%M')}'de yüklenecek: {video['title']}")

                def _delayed(v=video, fp=file_path, ps=pages, cid=channel_id, _op=op):
                    ok = False
                    for page in ps:
                        post_id = upload_video_instant(page, fp, v["title"], v["url"])
                        if post_id:
                            mark_shared(v["id"], v["title"], cid, page["page_id"])
                            ok = True
                            if not _op["post_url"]:
                                _op["post_url"] = f"https://www.facebook.com/{page['page_id']}/videos/{post_id}"
                            push("success", f"✓ [{page.get('name', page['page_id'])}] {v['title']}")
                        else:
                            push("error", f"✗ [{page.get('name', page['page_id'])}] {v['title']}")
                    delete_video(fp)
                    _op["status"]    = "success" if ok else "error"
                    _op["result_at"] = datetime.now().strftime("%H:%M")

                t = threading.Timer(delay, _delayed)
                t.daemon = True
                t.start()
            else:
                # Anlık
                success = False
                for page in pages:
                    push("info", f"⬆ Yükleniyor → {page.get('name', page['page_id'])}")
                    post_id = upload_video_instant(page, file_path, video["title"], video["url"])
                    if post_id:
                        mark_shared(video["id"], video["title"], channel_id, page["page_id"])
                        success = True
                        if not op["post_url"]:
                            op["post_url"] = f"https://www.facebook.com/{page['page_id']}/videos/{post_id}"
                        push("success", f"✓ [{page.get('name', page['page_id'])}] {video['title']}")
                    else:
                        push("error", f"✗ [{page.get('name', page['page_id'])}] {video['title']}")
                delete_video(file_path)
                op["status"]    = "success" if success else "error"
                op["result_at"] = datetime.now().strftime("%H:%M")

        push("done", "✅ Tüm işlemler tamamlandı!", batch_id=batch_id)

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/repost", methods=["POST"])
@login_required
def repost():
    """
    Paylaşılan videoları diğer sayfalarda repost eder.
    JSON: { "batch_id": "...", "page_ids": [...], "max_minutes": 120 }
    """
    if is_quiet_hours():
        return jsonify({"status": "error",
                        "msg": f"Gece modu aktif ({config.QUIET_HOURS_START}-{config.QUIET_HOURS_END}). "
                               f"Bu saatler arasında repost yapılamaz."}), 403

    data        = request.get_json()
    batch_id    = data.get("batch_id")
    page_ids    = data.get("page_ids", [])
    max_minutes = int(data.get("max_minutes", 60))

    pages = [p for p in config.FB_PAGES if p["page_id"] in page_ids]
    if not pages:
        return jsonify({"status": "error", "msg": "Sayfa seçilmedi"}), 400

    # Bu batch'e ait başarıyla paylaşılmış işlemler
    ops = [o for o in _operations if o.get("batch_id") == batch_id and o.get("post_url")]

    def _do():
        for op in ops:
            post_url    = op["post_url"]
            description = config.FB_DESCRIPTION_TEMPLATE.format(
                title=op["title"], url=op.get("video_url", "")
            )
            delay = random.randint(0, max_minutes * 60)
            repost_dt = datetime.now() + timedelta(seconds=delay)
            push("scheduled", f"🔁 {op['title']} → {repost_dt.strftime('%d/%m %H:%M')}'de repost")

            def _delayed(url=post_url, desc=description, ps=pages, title=op["title"]):
                for page in ps:
                    ok = repost_to_page(page, url, desc)
                    icon = "✓" if ok else "✗"
                    status = "success" if ok else "error"
                    push(status, f"{icon} Repost [{page.get('name', page['page_id'])}] {title}")

            t = threading.Timer(delay, _delayed)
            t.daemon = True
            t.start()

        push("done", "✅ Repost zamanlamaları ayarlandı!")

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/stream")
@login_required
def stream():
    """SSE — gerçek zamanlı ilerleme."""
    def generate():
        while True:
            try:
                msg = _progress.get(timeout=25)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/tw-to-fb", methods=["GET"])
@login_required
def tw_to_fb_page():
    return render_template("tw_to_fb.html", pages=config.FB_PAGES)


@app.route("/api/tw-preview", methods=["POST"])
@login_required
def api_tw_preview():
    """Tweet'i önizler — text + foto URL'lerini döner."""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Link boş"}), 400
    result = fetch_tweet(url)
    return jsonify(result)


@app.route("/api/tw-share", methods=["POST"])
@login_required
def api_tw_share():
    """Tweet'i belirtilen FB sayfalarına foto post olarak yükler."""
    if is_quiet_hours():
        return jsonify({"status": "error",
                        "msg": f"Gece modu aktif ({config.QUIET_HOURS_START}-{config.QUIET_HOURS_END}). "
                               f"Bu saatler arasında paylaşım yapılamaz."}), 403

    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    page_ids = data.get("page_ids") or []
    custom_caption = (data.get("caption") or "").strip()  # Opsiyonel; boşsa tweet text'i

    if not url:
        return jsonify({"status": "error", "msg": "Link boş"}), 400
    if not page_ids:
        return jsonify({"status": "error", "msg": "En az bir sayfa seç"}), 400

    pages = [p for p in config.FB_PAGES if p["page_id"] in page_ids]
    if not pages:
        return jsonify({"status": "error", "msg": "Geçerli sayfa bulunamadı"}), 400

    def _do():
        push("info", f"⏳ Tweet alınıyor: {url}")
        tweet = fetch_tweet(url)
        if not tweet.get("ok"):
            push("error", f"✗ Tweet alınamadı: {tweet.get('error')}")
            return

        if tweet.get("has_video"):
            push("error", "✗ Bu tweet video içeriyor — şu an sadece fotolu tweet'ler destekleniyor.")
            return
        if not tweet.get("photos"):
            push("error", "✗ Bu tweet'te foto bulunamadı.")
            return

        caption = custom_caption or tweet.get("text", "")
        photos  = tweet["photos"]
        push("info", f"📷 {len(photos)} foto bulundu, paylaşım başlıyor...")

        for page in pages:
            page_name = page.get("name", page["page_id"])
            push("info", f"⬆ Yükleniyor → {page_name}")
            result = upload_photo_to_page(page, photos, caption=caption)
            if result is True:
                push("success", f"✓ [{page_name}] Paylaşıldı")
            else:
                detail = f": {result}" if isinstance(result, str) else ""
                push("error", f"✗ [{page_name}] hata{detail}")

        push("done", "✅ TW → FB paylaşımı tamamlandı!")

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/tw-extract-frames", methods=["POST"])
@login_required
def api_tw_extract_frames():
    """Video tweet'inden frame'leri çıkarır. body: {url, count}"""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    try:
        count = int(data.get("count") or 8)
    except (TypeError, ValueError):
        count = 8
    count = max(1, min(count, 12))

    if not url:
        return jsonify({"ok": False, "error": "Link boş"}), 400

    def _do_and_return():
        push("info", f"⏳ Tweet alınıyor (frame için): {url}")
        tweet = fetch_tweet(url)
        if not tweet.get("ok"):
            return {"ok": False, "error": tweet.get("error", "Tweet alınamadı")}
        if not tweet.get("has_video") or not tweet.get("video_url"):
            return {"ok": False, "error": "Bu tweet video içermiyor"}

        push("info", f"🎞 Video indiriliyor & {count} frame çıkarılıyor...")
        result = video_frames.extract_frames(tweet["video_url"], count=count)
        if not result.get("ok"):
            push("error", f"✗ Frame çıkarma hatası: {result.get('error')}")
            return result

        sid = result["session_id"]
        urls = [f"/frames/{sid}/{name}" for name in result["frames"]]
        push("success", f"✓ {len(urls)} frame hazır.")

        # Background cleanup of old sessions
        try:
            video_frames.cleanup_old_sessions()
        except Exception:
            pass

        return {
            "ok": True,
            "session_id": sid,
            "frames": urls,
            "duration": result.get("duration", 0),
            "tweet_text": tweet.get("text", ""),
            "author": tweet.get("author", ""),
        }

    out = _do_and_return()
    return jsonify(out)


@app.route("/frames/<session_id>/<filename>")
@login_required
def serve_frame(session_id, filename):
    p = video_frames.get_frame_path(session_id, filename)
    if p is None:
        return "Not Found", 404
    return send_file(str(p), mimetype="image/jpeg")


@app.route("/api/tw-share-frames", methods=["POST"])
@login_required
def api_tw_share_frames():
    """Seçili frame'leri FB sayfalarına foto post olarak yükler."""
    if is_quiet_hours():
        return jsonify({"status": "error",
                        "msg": f"Gece modu aktif ({config.QUIET_HOURS_START}-{config.QUIET_HOURS_END}). "
                               f"Bu saatler arasında paylaşım yapılamaz."}), 403

    data = request.get_json() or {}
    session_id = (data.get("session_id") or "").strip()
    frame_indices = data.get("frame_indices") or []
    page_ids = data.get("page_ids") or []
    caption = (data.get("caption") or "").strip()

    sess_dir = video_frames.get_session_dir(session_id)
    if sess_dir is None:
        return jsonify({"status": "error", "msg": "Geçersiz veya süresi dolmuş frame oturumu"}), 400
    if not frame_indices:
        return jsonify({"status": "error", "msg": "En az bir frame seç"}), 400
    if len(frame_indices) > 10:
        return jsonify({"status": "error", "msg": "FB tek post'ta en fazla 10 foto kabul ediyor"}), 400
    if not page_ids:
        return jsonify({"status": "error", "msg": "En az bir sayfa seç"}), 400

    # Frame index'leri local path'lere çevir
    frame_paths = []
    for idx in frame_indices:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        p = sess_dir / f"frame_{i}.jpg"
        if p.exists():
            frame_paths.append(str(p))

    if not frame_paths:
        return jsonify({"status": "error", "msg": "Seçili frame'ler bulunamadı"}), 400

    pages = [p for p in config.FB_PAGES if p["page_id"] in page_ids]
    if not pages:
        return jsonify({"status": "error", "msg": "Geçerli sayfa bulunamadı"}), 400

    def _do():
        push("info", f"📷 {len(frame_paths)} frame ile paylaşım başlıyor...")
        for page in pages:
            page_name = page.get("name", page["page_id"])
            push("info", f"⬆ Yükleniyor → {page_name}")
            result = upload_photo_to_page(page, frame_paths, caption=caption)
            if result is True:
                push("success", f"✓ [{page_name}] Paylaşıldı")
            else:
                detail = f": {result}" if isinstance(result, str) else ""
                push("error", f"✗ [{page_name}] hata{detail}")

        push("done", "✅ Frame paylaşımı tamamlandı!")

        # Cleanup
        try:
            video_frames.cleanup_session(session_id)
            push("info", "🧹 Geçici frame'ler silindi.")
        except Exception as e:
            push("warn", f"Cleanup hatası: {e}")

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/repost-bots")
@login_required
def repost_bots_page():
    return render_template(
        "repost_bots.html",
        pages=config.FB_PAGES,
        bots=list(_repost_bots.values()),
    )


@app.route("/repost-bots/start", methods=["POST"])
@login_required
def start_repost_bot():
    source_page_id = request.form.get("source_page_id", "").strip()
    target_ids     = request.form.getlist("target_page_ids")

    if not source_page_id:
        flash("Kaynak sayfa seçmelisin.", "danger")
        return redirect(url_for("repost_bots_page"))

    # Kaynak sayfayı config'den bul (token dahil)
    source_page = next((p for p in config.FB_PAGES if p["page_id"] == source_page_id), None)
    if not source_page:
        flash("Kaynak sayfa bulunamadı.", "danger")
        return redirect(url_for("repost_bots_page"))

    # Hedef sayfalar — kaynakla aynı olanı otomatik çıkar
    target_pages = [p for p in config.FB_PAGES
                    if p["page_id"] in target_ids and p["page_id"] != source_page_id]

    if not target_pages:
        flash("En az bir hedef sayfa seçmelisin (kaynak sayfadan farklı).", "danger")
        return redirect(url_for("repost_bots_page"))

    bot = FBRepostBot(source_page, target_pages)
    bot.start()
    _repost_bots[bot.bot_id] = bot
    _save_repost_bots_state()

    flash(f"✓ Bot başlatıldı: {source_page.get('name', source_page_id)}", "success")
    return redirect(url_for("repost_bots_page"))


@app.route("/repost-bots/stop/<bot_id>", methods=["POST"])
@login_required
def stop_repost_bot(bot_id):
    if bot_id in _repost_bots:
        _repost_bots[bot_id].stop()
        _save_repost_bots_state()
        flash("Bot durduruldu.", "warning")
    return redirect(url_for("repost_bots_page"))


@app.route("/repost-bots/delete/<bot_id>", methods=["POST"])
@login_required
def delete_repost_bot(bot_id):
    if bot_id in _repost_bots:
        _repost_bots[bot_id].stop()
        del _repost_bots[bot_id]
        _save_repost_bots_state()
        flash("Bot silindi.", "success")
    return redirect(url_for("repost_bots_page"))


@app.route("/api/debug-scrape/<page_id>")
@login_required
def debug_scrape(page_id):
    """Sayfanın ham HTML'ini döner — regex düzeltmek için."""
    import config as _cfg
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }
    cookies = {}
    if _cfg.FB_COOKIE_C_USER and _cfg.FB_COOKIE_XS:
        cookies = {"c_user": _cfg.FB_COOKIE_C_USER, "xs": _cfg.FB_COOKIE_XS}
    import requests as _req
    resp = _req.get(f"https://mbasic.facebook.com/{page_id}", headers=headers,
                    cookies=cookies, timeout=20, allow_redirects=True)
    return resp.text, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/repost-bots")
@login_required
def api_repost_bots():
    return jsonify([b.to_dict() for b in _repost_bots.values()])


@app.route("/api/resolve-fb-page")
@login_required
def api_resolve_fb_page():
    """Kaynak FB sayfa arama endpoint'i (JS tarafından çağrılır)."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Boş sorgu"}), 400
    if not config.FB_USER_ACCESS_TOKEN:
        return jsonify({"error": "FB_USER_ACCESS_TOKEN tanımlı değil. Ayarlar'dan token ekle."}), 400
    info, err = resolve_fb_page(query, config.FB_USER_ACCESS_TOKEN)
    if info:
        return jsonify(info)
    return jsonify({
        "error": "Sayfa bulunamadı",
        "detail": err,
    }), 404


@app.route("/api/repost-bots/<bot_id>/log")
@login_required
def api_bot_log(bot_id):
    if bot_id not in _repost_bots:
        return jsonify([])
    return jsonify(_repost_bots[bot_id].get_log(50))


@app.route("/api/repost-activity-log")
@login_required
def api_repost_activity_log():
    """repost_activity.log dosyasını döndür (son N satır)."""
    n = request.args.get("n", 500, type=int)
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "repost_activity.log")
    if not os.path.exists(log_path):
        return jsonify({"lines": [], "total": 0})
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        total = len(all_lines)
        lines = [l.rstrip() for l in all_lines[-n:]]
        return jsonify({"lines": lines, "total": total})
    except Exception as e:
        return jsonify({"lines": [f"Hata: {e}"], "total": 0})


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", pages=config.FB_PAGES,
                           channel_id=config.YOUTUBE_CHANNEL_ID,
                           fb_c_user=config.FB_COOKIE_C_USER,
                           fb_xs=config.FB_COOKIE_XS)


@app.route("/settings/fb-cookies", methods=["POST"])
@login_required
def save_fb_cookies():
    c_user = request.form.get("c_user", "").strip()
    xs     = request.form.get("xs", "").strip()
    if not c_user or not xs:
        flash("c_user ve xs alanları boş olamaz.", "danger")
        return redirect(url_for("settings"))
    config.FB_COOKIE_C_USER = c_user
    config.FB_COOKIE_XS     = xs
    update_env("FB_COOKIE_C_USER", c_user)
    update_env("FB_COOKIE_XS", xs)
    flash("✓ Cookie'ler kaydedildi.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/channel", methods=["POST"])
@login_required
def change_channel():
    global _videos, _channel_info
    query = request.form.get("channel_query", "").strip()
    if not query:
        flash("Kanal adı boş olamaz.", "danger")
        return redirect(url_for("settings"))

    channel = resolve_youtube_channel(query)
    if not channel:
        flash("Kanal bulunamadı. URL veya @kullanıcıadını kontrol et.", "danger")
        return redirect(url_for("settings"))

    config.YOUTUBE_CHANNEL_ID = channel["id"]
    ym_module.SHORTS_URL = f"https://www.youtube.com/channel/{channel['id']}/shorts"
    update_env("YOUTUBE_CHANNEL_ID", channel["id"])
    save_last_channel(channel["id"], channel["name"])
    _videos = []
    _channel_info = {}

    flash(f"✓ Kanal değiştirildi: {channel['name']}", "success")
    return redirect(url_for("settings"))


@app.route("/settings/page/add", methods=["POST"])
@login_required
def add_page():
    query = request.form.get("page_query", "").strip()
    if not query:
        flash("Sayfa adı boş olamaz.", "danger")
        return redirect(url_for("settings"))

    if not config.FB_USER_ACCESS_TOKEN:
        flash("FB_USER_ACCESS_TOKEN .env dosyasında tanımlı değil.", "danger")
        return redirect(url_for("settings"))

    page_info, resolve_err = resolve_fb_page(query, config.FB_USER_ACCESS_TOKEN)
    if not page_info:
        flash(f"Sayfa bulunamadı: {resolve_err}", "danger")
        return redirect(url_for("settings"))

    page_id = page_info["page_id"]
    if any(p["page_id"] == page_id for p in config.FB_PAGES):
        flash("Bu sayfa zaten kayıtlı.", "warning")
        return redirect(url_for("settings"))

    token, _ = get_page_token(page_id, config.FB_USER_ACCESS_TOKEN)
    if not token:
        token = "MANUAL_TOKEN_REQUIRED"

    config.FB_PAGES.append({
        "page_id": page_id,
        "access_token": token,
        "name": page_info["name"],
    })
    pages_str = ",".join(f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES)
    update_env("FB_PAGES", pages_str)

    flash(f"✓ {page_info['name']} eklendi.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/page/remove", methods=["POST"])
@login_required
def remove_page():
    page_id = request.form.get("page_id")
    config.FB_PAGES = [p for p in config.FB_PAGES if p["page_id"] != page_id]
    pages_str = ",".join(f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES)
    update_env("FB_PAGES", pages_str)
    flash("Sayfa silindi.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/user-token", methods=["POST"])
@login_required
def update_user_token():
    new_token = request.form.get("user_token", "").strip()
    if not new_token:
        flash("Yeni token boş olamaz. Sadece sayfa tokenlarını yenilemek için 'Sayfa Tokenlarını Yenile' butonunu kullan.", "warning")
        return redirect(url_for("settings"))

    config.FB_USER_ACCESS_TOKEN = new_token
    update_env("FB_USER_ACCESS_TOKEN", new_token)

    # Hemen page token'larını yenile
    if config.FB_PAGES:
        config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, new_token)
        failed = [p for p in config.FB_PAGES if p.get("static_token")]
        if failed:
            names = ", ".join(p.get("name", p["page_id"]) for p in failed)
            flash(f"Token güncellendi ama şu sayfalar yenilenemedi: {names}", "warning")
        else:
            flash("✓ Token güncellendi ve tüm sayfa token'ları yenilendi.", "success")
    else:
        flash("✓ Token güncellendi.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/tokens/refresh", methods=["POST"])
@login_required
def refresh_tokens():
    if not config.FB_USER_ACCESS_TOKEN:
        flash("FB_USER_ACCESS_TOKEN tanımlı değil.", "danger")
        return redirect(url_for("settings"))
    config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, config.FB_USER_ACCESS_TOKEN)
    flash("✓ Token'lar yenilendi.", "success")
    return redirect(url_for("settings"))


if __name__ == "__main__":
    if config.FB_USER_ACCESS_TOKEN and config.FB_PAGES:
        print("Sayfa token'ları yenileniyor...")
        config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, config.FB_USER_ACCESS_TOKEN)
        # Yenilenen token'ları .env'e kaydet
        pages_str = ",".join(
            f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES
        )
        update_env("FB_PAGES", pages_str)
        ok  = [p for p in config.FB_PAGES if not p.get("static_token")]
        bad = [p for p in config.FB_PAGES if p.get("static_token")]
        if ok:
            print(f"✓ {len(ok)} sayfa token'ı yenilendi.")
        if bad:
            print(f"⚠ {len(bad)} sayfa yenilenemedi: "
                  f"{', '.join(p.get('name', p['page_id']) for p in bad)}")
    # Repost botlarını disk'ten restore et
    _load_repost_bots_state()
    threading.Thread(target=_periodic_save_loop, daemon=True).start()

    port = int(os.getenv("PORT", 5000))

    # Self-signed SSL: ssl/cert.pem ve ssl/key.pem varsa HTTPS'e geç
    _ssl_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssl")
    _cert     = os.path.join(_ssl_dir, "cert.pem")
    _key      = os.path.join(_ssl_dir, "key.pem")
    if os.path.exists(_cert) and os.path.exists(_key):
        print(f"Bot UI çalışıyor → https://0.0.0.0:{port} (self-signed SSL)")
        app.run(debug=False, threaded=True, host="0.0.0.0", port=port,
                ssl_context=(_cert, _key))
    else:
        print(f"Bot UI çalışıyor → http://0.0.0.0:{port}")
        app.run(debug=False, threaded=True, host="0.0.0.0", port=port)
