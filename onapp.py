"""
Flask Web UI — YouTube Shorts → Facebook Bot
Terminal versiyonu (main.py) bozulmadan çalışmaya devam eder.
Başlatmak için: python app.py
"""
import heapq
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
from tw_monitor import TWLikeBot
import discovery

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "fbbot-flask-secret-change-me")
app.config["SESSION_COOKIE_SECURE"]   = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
_limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")


@app.after_request
def _no_cache_html(response):
    """HTML cevaplarda tarayıcı cache'ini kapat — yeni tema deploy'ları
    hemen yansısın. JSON / static asset'ler etkilenmez."""
    ct = response.headers.get("Content-Type", "")
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
    return response


# /newtema'dan gelen form POST'ları yine /newtema'da kalsın diye Referer'a göre
# redirect hedefini değiştiren küçük yardımcı.
_NEWTEMA_REDIRECT_MAP = {
    "settings":         "newtema_settings",
    "repost_bots_page": "newtema_repost_bots",
    "tw_bot_page":      "newtema_tw_bot_page",
    "tw_videos_page":   "newtema_tw_videos_page",
    "index":            "newtema_index",
}


def _smart_redirect(endpoint, **kwargs):
    """Eğer istek /newtema sayfalarından geldiyse, oraya geri yönlendir."""
    ref = (request.referrer or "")
    if "/newtema" in ref and endpoint in _NEWTEMA_REDIRECT_MAP:
        endpoint = _NEWTEMA_REDIRECT_MAP[endpoint]
    return redirect(url_for(endpoint, **kwargs))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
@_limiter.limit("5 per 15 minutes", methods=["POST"], error_message="Çok fazla deneme. 15 dakika bekleyin.")
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == config.UI_USERNAME and password == config.UI_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("newtema_index"))
        return render_template("newtema/login.html", error="Kullanıcı adı veya şifre hatalı.")
    if session.get("logged_in"):
        return redirect(url_for("newtema_index"))
    return render_template("newtema/login.html", error=None)


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
_tw_bots: dict[str, TWLikeBot] = {}         # bot_id → TWLikeBot

# ─── TW Like bot persistence ─────────────────────────────────────────────────
TW_BOT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "tw_bot_state.json")


def _save_tw_bot_state() -> None:
    if not _tw_bots:
        if os.path.exists(TW_BOT_STATE_FILE):
            try:
                os.remove(TW_BOT_STATE_FILE)
            except Exception:
                pass
        return
    try:
        data = {"version": 2, "saved_at": datetime.now().isoformat(),
                "bots": [b.to_state_dict() for b in _tw_bots.values()]}
        tmp = TW_BOT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TW_BOT_STATE_FILE)
    except Exception as e:
        print(f"[tw-state] save failed: {e}")


def _load_tw_bot_state() -> None:
    if not os.path.exists(TW_BOT_STATE_FILE):
        return
    try:
        with open(TW_BOT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version", 1)
        if version == 1:
            # Eski tek-bot formatı — geriye dönük uyumluluk
            bots_data = [data["bot"]] if data.get("bot") else []
        else:
            bots_data = data.get("bots", [])
        for bot_data in bots_data:
            bot = TWLikeBot.from_state_dict(bot_data, config.FB_PAGES)
            if bot is None:
                print(f"[tw-state] bot atlandı (sayfalar bulunamadı): {bot_data.get('bot_id')}")
                continue
            if bot_data.get("running"):
                bot.start()
                print(f"[tw-state] TW Like Bot {bot.bot_id} yeniden başlatıldı")
            else:
                bot.finished = True
            _tw_bots[bot.bot_id] = bot
    except Exception as e:
        print(f"[tw-state] load failed: {e}")


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
            if _tw_bots:
                _save_tw_bot_state()
        except Exception as e:
            print(f"[periodic-save] error: {e}")


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
    # Yeni tema yayında — eski oturum sahipleri de /newtema'ya gitsin
    return redirect(url_for("newtema_index"))



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
        "newtema/videos.html",
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
        "newtema/schedule.html",
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
    return redirect(url_for("newtema_repost_bots"))


@app.route("/repost-bots/start", methods=["POST"])
@login_required
def start_repost_bot():
    source_page_id = request.form.get("source_page_id", "").strip()
    target_ids     = request.form.getlist("target_page_ids")

    if not source_page_id:
        flash("Kaynak sayfa seçmelisin.", "danger")
        return _smart_redirect("repost_bots_page")

    # Kaynak sayfayı config'den bul (token dahil)
    source_page = next((p for p in config.FB_PAGES if p["page_id"] == source_page_id), None)
    if not source_page:
        flash("Kaynak sayfa bulunamadı.", "danger")
        return _smart_redirect("repost_bots_page")

    # Hedef sayfalar — kaynakla aynı olanı otomatik çıkar
    target_pages = [p for p in config.FB_PAGES
                    if p["page_id"] in target_ids and p["page_id"] != source_page_id]

    if not target_pages:
        flash("En az bir hedef sayfa seçmelisin (kaynak sayfadan farklı).", "danger")
        return _smart_redirect("repost_bots_page")

    bot = FBRepostBot(source_page, target_pages)
    bot.start()
    _repost_bots[bot.bot_id] = bot
    _save_repost_bots_state()

    flash(f"✓ Bot başlatıldı: {source_page.get('name', source_page_id)}", "success")
    return _smart_redirect("repost_bots_page")


@app.route("/repost-bots/stop/<bot_id>", methods=["POST"])
@login_required
def stop_repost_bot(bot_id):
    if bot_id in _repost_bots:
        _repost_bots[bot_id].stop()
        _save_repost_bots_state()
        flash("Bot durduruldu.", "warning")
    return _smart_redirect("repost_bots_page")


@app.route("/repost-bots/delete/<bot_id>", methods=["POST"])
@login_required
def delete_repost_bot(bot_id):
    if bot_id in _repost_bots:
        _repost_bots[bot_id].stop()
        del _repost_bots[bot_id]
        _save_repost_bots_state()
        flash("Bot silindi.", "success")
    return _smart_redirect("repost_bots_page")


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


# ─── TW Like Bot routes ───────────────────────────────────────────────────────

@app.route("/tw-bot")
@login_required
def tw_bot_page():
    return redirect(url_for("newtema_tw_bot_page"))


@app.route("/tw-bot/start", methods=["POST"])
@login_required
def start_tw_bot():
    instant_ids = request.form.getlist("instant_page_ids")
    queued_ids  = request.form.getlist("queued_page_ids")
    prime_ids   = request.form.getlist("prime_page_ids")
    bot_name    = request.form.get("bot_name", "").strip()

    if not instant_ids and not queued_ids and not prime_ids:
        flash("En az bir sayfa seçmelisin.", "danger")
        return _smart_redirect("tw_bot_page")

    instant_pages = [p for p in config.FB_PAGES if p["page_id"] in instant_ids]
    queued_pages  = [p for p in config.FB_PAGES if p["page_id"] in queued_ids]
    prime_pages   = [p for p in config.FB_PAGES if p["page_id"] in prime_ids]

    try:
        interval_min = int(request.form.get("check_interval", 15))
        interval_min = interval_min if interval_min in (1, 3, 5, 10, 15, 30) else 15
    except (TypeError, ValueError):
        interval_min = 15

    try:
        tw_account = int(request.form.get("tw_account", 1))
        tw_account = tw_account if tw_account in (1, 2, 3) else 1
    except (TypeError, ValueError):
        tw_account = 1

    watermark_icon  = request.form.get("watermark_icon", "").strip()
    auto_post_video = request.form.get("auto_post_video") == "on"

    bot = TWLikeBot(instant_pages, queued_pages, prime_pages,
                    check_interval=interval_min * 60,
                    bot_name=bot_name,
                    tw_account=tw_account,
                    watermark_icon=watermark_icon,
                    auto_post_video=auto_post_video)
    bot.start()
    _tw_bots[bot.bot_id] = bot
    _save_tw_bot_state()
    flash("✓ TW Like Bot başlatıldı.", "success")
    return _smart_redirect("tw_bot_page")


@app.route("/tw-bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop_tw_bot(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot and bot.running:
        bot.stop()
        _save_tw_bot_state()
        flash("Bot durduruldu.", "warning")
    return _smart_redirect("tw_bot_page")


@app.route("/tw-bot/<bot_id>/check-now", methods=["POST"])
@login_required
def tw_bot_check_now(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot and bot.running:
        bot.check_now()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "Bot çalışmıyor"}), 400


@app.route("/tw-bot/<bot_id>/delete", methods=["POST"])
@login_required
def delete_tw_bot(bot_id):
    bot = _tw_bots.pop(bot_id, None)
    if bot:
        bot.stop()
        _save_tw_bot_state()
        flash("Bot silindi.", "success")
    return _smart_redirect("tw_bot_page")


@app.route("/api/tw-bot/list")
@login_required
def api_tw_bot_list():
    return jsonify([b.to_dict() for b in _tw_bots.values()])


@app.route("/api/tw-bot/<bot_id>/status")
@login_required
def api_tw_bot_status(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"exists": False})
    return jsonify({"exists": True, **bot.to_dict()})


@app.route("/api/tw-bot/<bot_id>/log")
@login_required
def api_tw_bot_log(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify([])
    n = request.args.get("n", 100, type=int)
    return jsonify(bot.get_log(n))


@app.route("/api/tw-bot/<bot_id>/queue")
@login_required
def api_tw_bot_queue(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify([])
    items = []
    for task in sorted(bot._queue):   # heap sıralı kopyası
        items.append({
            "run_at":    task.run_at,
            "run_str":   datetime.fromtimestamp(task.run_at).strftime("%d.%m %H:%M"),
            "page":      task.page.get("name", task.page.get("page_id", "?")),
            "tweet_url": task.tweet_url,
            "text":      task.text[:120] if task.text else "",
            "photos":        len(task.photos),
            "is_video_post": task.is_video_post,
            "task_id":       task.task_id,
        })
    history = list(reversed(bot._posted_history[-10:]))  # en yeni önce
    return jsonify({"queue": items, "history": history})


@app.route("/api/tw-bot/<bot_id>/history-likes", methods=["GET"])
@login_required
def api_tw_bot_history_likes(bot_id):
    """Son paylaşılanların FB like sayılarını döndürür."""
    import re, requests as _req
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({}), 404

    # Botun tüm sayfalarından page_id → token haritası
    all_pages = bot.instant_pages + bot.queued_pages + bot.prime_pages
    token_map = {p["page_id"]: p["access_token"] for p in all_pages if p.get("access_token")}

    result = {}
    for item in bot._posted_history:
        post_url = item.get("post_url", "")
        if not post_url:
            continue

        # URL'den page_id ve post raw_id çıkar
        # Örnek: https://www.facebook.com/356043057851504/posts/123456
        m = re.search(r'facebook\.com/(\d+)/(?:posts|videos)/(\d+)', post_url)
        if not m:
            continue
        url_page_id = m.group(1)
        raw_id      = m.group(2)

        page_id = item.get("page_id", "") or url_page_id
        token   = item.get("page_token", "") or token_map.get(page_id, "") or token_map.get(url_page_id, "")
        if not token:
            continue

        object_id = f"{page_id}_{raw_id}"

        try:
            r = _req.get(
                f"https://graph.facebook.com/v19.0/{object_id}",
                params={"fields": "likes.summary(true)", "access_token": token},
                timeout=8,
            )
            data = r.json()
            count = data.get("likes", {}).get("summary", {}).get("total_count", 0)
            result[post_url] = count
        except Exception:
            result[post_url] = None

    return jsonify(result)


@app.route("/api/tw-bot/<bot_id>/watermark", methods=["PATCH"])
@login_required
def api_tw_bot_set_watermark(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    icon = (request.get_json(silent=True) or {}).get("icon", "").strip()
    bot.watermark_icon = icon
    _save_tw_bot_state()
    return jsonify({"ok": True, "watermark_icon": icon})


@app.route("/api/tw-bot/<bot_id>/rename", methods=["PATCH"])
@login_required
def api_tw_bot_rename(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if name:
        bot.bot_name = name
        _save_tw_bot_state()
    return jsonify({"ok": True, "bot_name": bot.bot_name})


@app.route("/api/tw-bot/<bot_id>/auto-video", methods=["PATCH"])
@login_required
def api_tw_bot_set_auto_video(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    enabled = (request.get_json(silent=True) or {}).get("enabled", False)
    bot.auto_post_video = bool(enabled)
    _save_tw_bot_state()
    return jsonify({"ok": True, "auto_post_video": bot.auto_post_video})


@app.route("/api/tw-bot/<bot_id>/queue/<task_id>/run", methods=["POST"])
@login_required
def api_tw_bot_queue_run(bot_id, task_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    ok = bot.run_queue_item(task_id)
    return jsonify({"ok": ok})


@app.route("/api/tw-bot/<bot_id>/queue/<task_id>", methods=["DELETE"])
@login_required
def api_tw_bot_queue_cancel(bot_id, task_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    ok = bot.remove_queue_item(task_id)
    return jsonify({"ok": ok})


@app.route("/api/tw-bot/<bot_id>/queue/<task_id>/reschedule", methods=["POST"])
@login_required
def api_tw_bot_queue_reschedule(bot_id, task_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    result = bot.reschedule_queue_item(task_id)
    if result is None:
        return jsonify({"ok": False, "msg": "Görev bulunamadı"}), 404
    _save_tw_bot_state()
    return jsonify({"ok": True, **result})


@app.route("/api/tw-bot/<bot_id>/queue/swap", methods=["POST"])
@login_required
def api_tw_bot_queue_swap(bot_id):
    """İki kuyruktaki görevin run_at (slot) zamanlarını değiştirir."""
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False, "msg": "Bot bulunamadı"}), 404
    body = request.get_json(silent=True) or {}
    id_a = body.get("task_id_a")
    id_b = body.get("task_id_b")
    if not id_a or not id_b:
        return jsonify({"ok": False, "msg": "task_id_a ve task_id_b gerekli"}), 400
    task_a = next((t for t in bot._queue if t.task_id == id_a), None)
    task_b = next((t for t in bot._queue if t.task_id == id_b), None)
    if task_a is None or task_b is None:
        return jsonify({"ok": False, "msg": "Görev bulunamadı"}), 404
    task_a.run_at, task_b.run_at = task_b.run_at, task_a.run_at
    heapq.heapify(bot._queue)
    _save_tw_bot_state()
    return jsonify({"ok": True})


@app.route("/api/tw-bot/<bot_id>/restart", methods=["POST"])
@login_required
def api_tw_bot_restart(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False}), 404
    bot.restart()
    _save_tw_bot_state()
    return jsonify({"ok": True})


@app.route("/api/tw-bot/<bot_id>/sleep", methods=["POST"])
@login_required
def api_tw_bot_sleep(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False}), 404
    until_ts = request.json.get("until_ts") if request.is_json else None
    if not until_ts:
        return jsonify({"ok": False, "error": "until_ts gerekli"}), 400
    bot.sleep_until(int(until_ts))
    _save_tw_bot_state()
    return jsonify({"ok": True})


@app.route("/api/tw-bot/<bot_id>/set-account", methods=["POST"])
@login_required
def api_tw_bot_set_account(bot_id):
    bot = _tw_bots.get(bot_id)
    if bot is None:
        return jsonify({"ok": False}), 404
    account = int(request.json.get("account", 1)) if request.is_json else 1
    bot.set_account(account)
    _save_tw_bot_state()
    return jsonify({"ok": True})


@app.route("/tw-videos")
@login_required
def tw_videos_page():
    return redirect(url_for("newtema_tw_videos_page"))


@app.route("/api/tw-videos")
@login_required
def api_tw_videos():
    result = []
    for bot in _tw_bots.values():
        result.extend(bot._pending_videos)
    return jsonify(result)


@app.route("/api/tw-videos/<video_id>/dismiss", methods=["POST"])
@login_required
def api_tw_video_dismiss(video_id):
    for bot in _tw_bots.values():
        bot.dismiss_video(video_id)
    return jsonify({"ok": True})


def _fetch_tw_username(access_token: str, access_token_secret: str) -> str:
    """Twitter API'den kullanıcı adı çeker (sadece cache boşken çağrılır)."""
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=config.TW_API_KEY,
            consumer_secret=config.TW_API_SECRET,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        me = client.get_me(user_auth=True)
        return f"@{me.data.username}" if me and me.data else ""
    except Exception:
        return ""


def _cache_tw_account_names():
    """Uygulama başlarken arka planda bir kez çalışır.
    İsimler zaten .env'de varsa API'ye dokunmaz."""
    if not config.TW_API_KEY or not config.TW_API_SECRET:
        return
    changed = False
    if not config.TW_ACCOUNT1_NAME and config.TW_ACCESS_TOKEN:
        name = _fetch_tw_username(config.TW_ACCESS_TOKEN, config.TW_ACCESS_TOKEN_SECRET)
        if name:
            config.TW_ACCOUNT1_NAME = name
            update_env("TW_ACCOUNT1_NAME", name)
            changed = True
    if not config.TW_ACCOUNT2_NAME and config.TW2_ACCESS_TOKEN:
        name = _fetch_tw_username(config.TW2_ACCESS_TOKEN, config.TW2_ACCESS_TOKEN_SECRET)
        if name:
            config.TW_ACCOUNT2_NAME = name
            update_env("TW_ACCOUNT2_NAME", name)
            changed = True
    if not config.TW_ACCOUNT3_NAME and config.TW3_ACCESS_TOKEN:
        name = _fetch_tw_username(config.TW3_ACCESS_TOKEN, config.TW3_ACCESS_TOKEN_SECRET)
        if name:
            config.TW_ACCOUNT3_NAME = name
            update_env("TW_ACCOUNT3_NAME", name)
            changed = True
    if changed:
        print(f"[tw-cache] Hesap adları kaydedildi: "
              f"{config.TW_ACCOUNT1_NAME} / {config.TW_ACCOUNT2_NAME} / {config.TW_ACCOUNT3_NAME}")


@app.route("/api/icons")
@login_required
def api_icons():
    from watermark import list_icons
    return jsonify(list_icons())


@app.route("/api/tw-credentials-check")
@login_required
def api_tw_credentials_check():
    missing = [k for k, v in {
        "TW_API_KEY":             config.TW_API_KEY,
        "TW_API_SECRET":          config.TW_API_SECRET,
        "TW_ACCESS_TOKEN":        config.TW_ACCESS_TOKEN,
        "TW_ACCESS_TOKEN_SECRET": config.TW_ACCESS_TOKEN_SECRET,
    }.items() if not v]
    account2_ready = bool(config.TW2_ACCESS_TOKEN and config.TW2_ACCESS_TOKEN_SECRET)
    account3_ready = bool(config.TW3_ACCESS_TOKEN and config.TW3_ACCESS_TOKEN_SECRET)
    return jsonify({
        "ok":            len(missing) == 0,
        "missing":       missing,
        "account1_name": config.TW_ACCOUNT1_NAME,
        "account2_ready": account2_ready,
        "account2_name": config.TW_ACCOUNT2_NAME,
        "account3_ready": account3_ready,
        "account3_name": config.TW_ACCOUNT3_NAME,
    })


@app.route("/api/tw-activity-log")
@login_required
def api_tw_activity_log():
    """tw_activity.log dosyasını döndür (son N satır)."""
    n = request.args.get("n", 500, type=int)
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tw_activity.log")
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
    return redirect(url_for("newtema_settings"))


@app.route("/settings/fb-cookies", methods=["POST"])
@login_required
def save_fb_cookies():
    c_user = request.form.get("c_user", "").strip()
    xs     = request.form.get("xs", "").strip()
    if not c_user or not xs:
        flash("c_user ve xs alanları boş olamaz.", "danger")
        return _smart_redirect("settings")
    config.FB_COOKIE_C_USER = c_user
    config.FB_COOKIE_XS     = xs
    update_env("FB_COOKIE_C_USER", c_user)
    update_env("FB_COOKIE_XS", xs)
    flash("✓ Cookie'ler kaydedildi.", "success")
    return _smart_redirect("settings")


@app.route("/settings/channel", methods=["POST"])
@login_required
def change_channel():
    global _videos, _channel_info
    query = request.form.get("channel_query", "").strip()
    if not query:
        flash("Kanal adı boş olamaz.", "danger")
        return _smart_redirect("settings")

    channel = resolve_youtube_channel(query)
    if not channel:
        flash("Kanal bulunamadı. URL veya @kullanıcıadını kontrol et.", "danger")
        return _smart_redirect("settings")

    config.YOUTUBE_CHANNEL_ID = channel["id"]
    ym_module.SHORTS_URL = f"https://www.youtube.com/channel/{channel['id']}/shorts"
    update_env("YOUTUBE_CHANNEL_ID", channel["id"])
    save_last_channel(channel["id"], channel["name"])
    _videos = []
    _channel_info = {}

    flash(f"✓ Kanal değiştirildi: {channel['name']}", "success")
    return _smart_redirect("settings")


@app.route("/settings/page/add", methods=["POST"])
@login_required
def add_page():
    query = request.form.get("page_query", "").strip()
    if not query:
        flash("Sayfa adı boş olamaz.", "danger")
        return _smart_redirect("settings")

    if not config.FB_USER_ACCESS_TOKEN:
        flash("FB_USER_ACCESS_TOKEN .env dosyasında tanımlı değil.", "danger")
        return _smart_redirect("settings")

    page_info, resolve_err = resolve_fb_page(query, config.FB_USER_ACCESS_TOKEN)
    if not page_info:
        flash(f"Sayfa bulunamadı: {resolve_err}", "danger")
        return _smart_redirect("settings")

    page_id = page_info["page_id"]
    if any(p["page_id"] == page_id for p in config.FB_PAGES):
        flash("Bu sayfa zaten kayıtlı.", "warning")
        return _smart_redirect("settings")

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
    return _smart_redirect("settings")


@app.route("/settings/page/remove", methods=["POST"])
@login_required
def remove_page():
    page_id = request.form.get("page_id")
    config.FB_PAGES = [p for p in config.FB_PAGES if p["page_id"] != page_id]
    pages_str = ",".join(f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES)
    update_env("FB_PAGES", pages_str)
    flash("Sayfa silindi.", "success")
    return _smart_redirect("settings")


@app.route("/settings/user-token", methods=["POST"])
@login_required
def update_user_token():
    new_token = request.form.get("user_token", "").strip()
    if not new_token:
        flash("Yeni token boş olamaz. Sadece sayfa tokenlarını yenilemek için 'Sayfa Tokenlarını Yenile' butonunu kullan.", "warning")
        return _smart_redirect("settings")

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

    return _smart_redirect("settings")


@app.route("/settings/tokens/refresh", methods=["POST"])
@login_required
def refresh_tokens():
    if not config.FB_USER_ACCESS_TOKEN:
        flash("FB_USER_ACCESS_TOKEN tanımlı değil.", "danger")
        return _smart_redirect("settings")
    config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, config.FB_USER_ACCESS_TOKEN)
    flash("✓ Token'lar yenilendi.", "success")
    return _smart_redirect("settings")


@app.route("/settings/tw2-tokens", methods=["POST"])
@login_required
def save_tw2_tokens():
    at  = request.form.get("tw2_access_token", "").strip()
    ats = request.form.get("tw2_access_token_secret", "").strip()
    if not at or not ats:
        flash("Her iki alan da doldurulmalıdır.", "danger")
        return _smart_redirect("settings")
    update_env("TW2_ACCESS_TOKEN", at)
    update_env("TW2_ACCESS_TOKEN_SECRET", ats)
    config.TW2_ACCESS_TOKEN        = at
    config.TW2_ACCESS_TOKEN_SECRET = ats
    flash("✓ Hesap 2 token'ları kaydedildi.", "success")
    return _smart_redirect("settings")


# ─── /newtema — Yeni tema önizleme (BahoAgent) ───────────────────────────────
# Eski /, /videos, /repost-bots, /settings vb. route'lar olduğu gibi çalışmaya
# devam eder. Yeni tema yayına alınınca template'leri ana yola taşıyacağız.

def _newtema_dashboard_stats() -> dict:
    repost_running = sum(1 for b in _repost_bots.values() if b.running)
    repost_total   = sum(int(getattr(b, "posts_reposted", 0)) for b in _repost_bots.values())

    tw_running        = any(b.running for b in _tw_bots.values())
    tw_target_pages   = sum(len(b.instant_pages) + len(b.queued_pages) for b in _tw_bots.values())
    tw_likes          = sum(b.tweets_posted for b in _tw_bots.values())
    _all_pending      = [v for b in _tw_bots.values() for v in b._pending_videos]
    tw_videos_total   = len(_all_pending)
    tw_videos_pending = tw_videos_total

    return {
        "repost_running":    repost_running,
        "repost_total":      repost_total,
        "tw_running":        tw_running,
        "tw_handles":        tw_target_pages,
        "tw_likes":          tw_likes,
        "tw_videos_total":   tw_videos_total,
        "tw_videos_pending": tw_videos_pending,
    }


@app.route("/newtema")
@login_required
def newtema_index():
    return render_template(
        "newtema/index.html",
        channel=get_last_channel(),
        pages=config.FB_PAGES,
        active_page_ids=_active_page_ids,
        stats=_newtema_dashboard_stats(),
    )


@app.route("/api/newtema/dashboard")
@login_required
def api_newtema_dashboard():
    return jsonify(_newtema_dashboard_stats())


@app.route("/newtema/settings")
@login_required
def newtema_settings():
    return render_template(
        "newtema/settings.html",
        pages=config.FB_PAGES,
        channel_id=config.YOUTUBE_CHANNEL_ID,
        fb_c_user=config.FB_COOKIE_C_USER,
        fb_xs=config.FB_COOKIE_XS,
        tw2_ready=bool(config.TW2_ACCESS_TOKEN and config.TW2_ACCESS_TOKEN_SECRET),
    )


@app.route("/newtema/repost-bots")
@login_required
def newtema_repost_bots():
    bots = list(_repost_bots.values())
    bots_json = {b.bot_id: b.to_dict() for b in bots}
    return render_template(
        "newtema/repost_bots.html",
        bots=bots,
        pages=config.FB_PAGES,
        bots_json=json.dumps(bots_json, ensure_ascii=False),
    )


@app.route("/newtema/tw-bot")
@login_required
def newtema_tw_bot_page():
    bots = list(_tw_bots.values())
    bots_json = [b.to_dict() for b in bots]
    return render_template(
        "newtema/tw_bot.html",
        pages=config.FB_PAGES,
        bots=bots,
        bots_json=json.dumps(bots_json, ensure_ascii=False),
    )


@app.route("/newtema/tw-videos")
@login_required
def newtema_tw_videos_page():
    pending = [v for b in _tw_bots.values() for v in b._pending_videos]
    return render_template(
        "newtema/tw_videos.html",
        pages=config.FB_PAGES,
        pending=pending,
    )


# ─── İçerik Keşif (Discovery / Apify) ─────────────────────────────────────────

@app.route("/newtema/kesfet")
@login_required
def newtema_kesfet():
    status  = request.args.get("status", "pending")
    sort_by = request.args.get("sort", "score")
    items   = discovery.list_items(status=status, sort_by=sort_by, limit=300)
    return render_template(
        "newtema/kesfet.html",
        items=items,
        counts=discovery.counts(),
        active_status=status,
        active_sort=sort_by,
    )


@app.route("/api/discovery/list")
@login_required
def api_discovery_list():
    status  = request.args.get("status", "pending")
    sort_by = request.args.get("sort", "score")
    return jsonify({
        "items":  discovery.list_items(status=status, sort_by=sort_by, limit=300),
        "counts": discovery.counts(),
    })


@app.route("/api/discovery/<content_id>/status", methods=["POST"])
@login_required
def api_discovery_set_status(content_id):
    data    = request.get_json(silent=True) or {}
    status  = (data.get("status") or request.form.get("status") or "").strip()
    note    = (data.get("note")   or request.form.get("note")   or "").strip()
    ok      = discovery.set_status(content_id, status, note=note)
    return jsonify({"ok": ok})


@app.route("/api/discovery/<content_id>", methods=["DELETE"])
@login_required
def api_discovery_delete(content_id):
    return jsonify({"ok": discovery.delete_item(content_id)})


# ─── Aday Bul (Candidates) ───────────────────────────────────────────────────

@app.route("/newtema/kesfet/aday-bul")
@login_required
def newtema_aday_bul():
    status        = request.args.get("status", "pending")
    sort_by       = request.args.get("sort", "followers")
    try:
        min_followers = int(request.args.get("min_followers") or 0)
    except Exception:
        min_followers = 0
    verified_only = request.args.get("verified") == "1"
    has_bio       = request.args.get("with_bio") == "1"
    category_q    = (request.args.get("cat") or "").strip()

    return render_template(
        "newtema/aday_bul.html",
        items=discovery.list_candidates(
            status=status, sort_by=sort_by,
            min_followers=min_followers, verified_only=verified_only,
            has_bio=has_bio, category_q=category_q, limit=300,
        ),
        counts=discovery.candidates_counts(),
        active_status=status,
        active_sort=sort_by,
        filters={
            "min_followers": min_followers,
            "verified":      verified_only,
            "with_bio":      has_bio,
            "cat":           category_q,
        },
    )


@app.route("/api/discovery/search-candidates", methods=["POST"])
@login_required
def api_search_candidates():
    data      = request.get_json(silent=True) or {}
    keywords  = data.get("keywords") or []
    locations = data.get("locations") or []
    max_items = int(data.get("max_items") or 50)
    if not isinstance(keywords, list) or not keywords:
        return jsonify({"ok": False, "error": "keywords boş"}), 400
    return jsonify(discovery.trigger_candidate_search(keywords, locations, max_items))


@app.route("/api/discovery/candidates/<cid>/status", methods=["POST"])
@login_required
def api_candidate_status(cid):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    return jsonify({"ok": discovery.set_candidate_status(cid, status)})


@app.route("/api/discovery/candidates-webhook", methods=["POST"])
def api_candidates_webhook():
    """search-scraper webhook'u — aday sayfaları işler."""
    expected = os.getenv("DISCOVERY_WEBHOOK_TOKEN", "")
    given    = request.args.get("token", "") or request.headers.get("X-Webhook-Token", "")
    if not expected or given != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    result = discovery.ingest_candidates(payload)
    if not result.get("ok") and result.get("reason") == "dataset_polling_required":
        dataset_id = result.get("dataset_id")
        token      = os.getenv("APIFY_API_TOKEN", "")
        if dataset_id and token:
            items = discovery.fetch_apify_dataset(dataset_id, token)
            if items:
                result = discovery.ingest_candidates(items)
                result["dataset_id"] = dataset_id

    print(f"[discovery] candidates webhook result: {result}")
    return jsonify(result)


# ─── Alternatif Scraper (yashodhank/actor-facebook-scraper) ──────────────────

@app.route("/newtema/kesfet/alternatif")
@login_required
def newtema_alt_scraper():
    return render_template("newtema/alt_scraper.html")


@app.route("/api/discovery/alt-scraper/run", methods=["POST"])
@login_required
def api_alt_scraper_run():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    max_posts = int(data.get("max_posts") or 20)
    if not urls:
        return jsonify({"ok": False, "error": "urls boş"}), 400
    return jsonify(discovery.trigger_alt_scraper(urls, max_posts))


@app.route("/api/discovery/alt-scraper/status/<run_id>")
@login_required
def api_alt_scraper_status(run_id):
    return jsonify(discovery.get_alt_run_status(run_id))


@app.route("/api/discovery/webhook", methods=["POST"])
def api_discovery_webhook():
    """Apify → BahoAgent webhook endpoint.
    Auth: ?token=... query param ile (env DISCOVERY_WEBHOOK_TOKEN).
    Apify default webhook sadece run-meta gönderir; biz APIFY_API_TOKEN
    ile dataset'i çekip işliyoruz.
    """
    expected = os.getenv("DISCOVERY_WEBHOOK_TOKEN", "")
    given    = request.args.get("token", "") or request.headers.get("X-Webhook-Token", "")
    if not expected or given != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    # 1) Direkt ingest dene
    result = discovery.ingest_apify_payload(payload)

    # 2) Eğer payload sadece run meta ise dataset'i polling ile çek
    if not result.get("ok") and result.get("reason") == "dataset_polling_required":
        dataset_id = result.get("dataset_id")
        api_token  = os.getenv("APIFY_API_TOKEN", "")
        if dataset_id and api_token:
            items = discovery.fetch_apify_dataset(dataset_id, api_token)
            if items:
                result = discovery.ingest_apify_payload(items)
                result["dataset_id"] = dataset_id
            else:
                result = {"ok": False, "reason": "dataset_empty",
                          "dataset_id": dataset_id}
        else:
            result = {"ok": False, "reason": "missing_apify_token"}

    print(f"[discovery] webhook result: {result}")
    return jsonify(result)


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
    # TW Like Bot'u disk'ten restore et
    _load_tw_bot_state()
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
