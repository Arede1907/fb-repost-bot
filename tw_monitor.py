"""
Twitter Like Bot
- instant_pages: yeni like bulununca anında paylaşır (aralarında 30sn bekleme)
- queued_pages:  her gönderi +5dk arayla planlanır
- Videolu tweet'ler → _pending_videos kuyruğuna alınır, kullanıcı frame seçip paylaşır
"""
import heapq
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import requests as _http

logger = logging.getLogger(__name__)

_log_dir = os.path.dirname(os.path.abspath(__file__))
_tw_log_file = os.path.join(_log_dir, "tw_activity.log")
_file_handler = logging.FileHandler(_tw_log_file, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_file_handler)
logger.setLevel(logging.INFO)

CHECK_INTERVAL   = 15 * 60
POST_SPACING     =  5 * 60
INSTANT_SPACING  = 30        # anında sayfalar arası bekleme (saniye)


def _post_text_to_fb(page: dict, message: str) -> bool:
    try:
        resp = _http.post(
            f"https://graph.facebook.com/v19.0/{page['page_id']}/feed",
            data={"message": message, "access_token": page["access_token"]},
            timeout=20,
        )
        data = resp.json()
        return resp.status_code == 200 and "id" in data
    except Exception:
        return False


@dataclass(order=True)
class _PostTask:
    run_at:    int
    tweet_url: str       = field(compare=False)
    text:      str       = field(compare=False)
    photos:    list[str] = field(compare=False)
    page:      dict      = field(compare=False)


class TWLikeBot:
    def __init__(self,
                 instant_pages:  list[dict],
                 queued_pages:   list[dict],
                 check_interval: int = CHECK_INTERVAL):
        self.bot_id         = uuid.uuid4().hex[:8]
        self.instant_pages  = instant_pages
        self.queued_pages   = queued_pages
        self.check_interval = check_interval

        self.running        = False
        self.finished       = False
        self.started_at     = None
        self.last_check     = None
        self._next_check_ts = 0
        self._next_slot_ts  = 0

        self.tweets_posted  = 0
        self.errors         = 0

        self._log_entries    : list[dict]      = []
        self._thread         = None
        self._seen_ids       : set[str]        = set()
        self._initialized    = False
        self._start_ts       = 0
        self._tw_user_id     : str | None      = None
        self._queue          : list[_PostTask] = []
        self._pending_videos : list[dict]      = []  # frame hazır, kullanıcı onayı bekliyor

    # ── Arayüz ──────────────────────────────────────────────────────────────

    def start(self):
        self.running       = True
        self._start_ts     = int(datetime.now().timestamp())
        self.started_at    = datetime.now().strftime("%H:%M:%S")
        self._next_slot_ts = int(datetime.now().timestamp())
        instant_names = [p.get("name", p["page_id"]) for p in self.instant_pages]
        queued_names  = [p.get("name", p["page_id"]) for p in self.queued_pages]
        self._log("info",
                  f"TW Like Bot başlatıldı — "
                  f"anında: {instant_names} | kuyruk: {queued_names} | "
                  f"kontrol: {self.check_interval // 60}dk")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._log("warn", f"Bot durduruldu. ({len(self._queue)} görev kuyrukta, "
                          f"{len(self._pending_videos)} video bekliyor)")

    def check_now(self):
        if not self.running:
            return
        self._next_check_ts = int(datetime.now().timestamp()) + self.check_interval
        threading.Thread(target=self._check, daemon=True).start()

    def dismiss_video(self, video_id: str):
        self._pending_videos = [v for v in self._pending_videos if v["id"] != video_id]

    @property
    def next_check_str(self) -> str:
        if not self._next_check_ts or not self.running:
            return "–"
        return datetime.fromtimestamp(self._next_check_ts).strftime("%H:%M:%S")

    @property
    def pending_tasks(self) -> int:
        return len(self._queue)

    @property
    def all_pages(self) -> list[dict]:
        return self.instant_pages + self.queued_pages

    def get_log(self, n: int = 200) -> list[dict]:
        return list(reversed(self._log_entries[-n:]))

    def to_dict(self) -> dict:
        return {
            "bot_id":          self.bot_id,
            "running":         self.running,
            "finished":        self.finished,
            "started_at":      self.started_at or "–",
            "last_check":      self.last_check or "–",
            "next_check":      self.next_check_str,
            "tweets_posted":   self.tweets_posted,
            "errors":          self.errors,
            "pending_tasks":   self.pending_tasks,
            "pending_videos":  len(self._pending_videos),
            "instant_pages":   [p.get("name", p["page_id"]) for p in self.instant_pages],
            "queued_pages":    [p.get("name", p["page_id"]) for p in self.queued_pages],
            "check_interval":  self.check_interval,
        }

    # ── Persistence ─────────────────────────────────────────────────────────

    def to_state_dict(self) -> dict:
        return {
            "bot_id":           self.bot_id,
            "instant_page_ids": [p["page_id"] for p in self.instant_pages],
            "queued_page_ids":  [p["page_id"] for p in self.queued_pages],
            "check_interval":   self.check_interval,
            "running":          self.running,
            "finished":         self.finished,
            "started_at":       self.started_at,
            "last_check":       self.last_check,
            "tweets_posted":    self.tweets_posted,
            "errors":           self.errors,
            "seen_ids":         list(self._seen_ids),
            "initialized":      self._initialized,
            "start_ts":         self._start_ts,
            "next_slot_ts":     self._next_slot_ts,
            "tw_user_id":       self._tw_user_id,
            "log_entries":      self._log_entries[-500:],
            "queue": [
                {"run_at": t.run_at, "tweet_url": t.tweet_url,
                 "text": t.text, "photos": t.photos,
                 "page_id": t.page.get("page_id", "")}
                for t in self._queue
            ],
        }

    @classmethod
    def from_state_dict(cls, data: dict, all_pages: list[dict]):
        instant_pages = [p for p in all_pages
                         if p["page_id"] in data.get("instant_page_ids", [])]
        queued_pages  = [p for p in all_pages
                         if p["page_id"] in data.get("queued_page_ids", [])]
        if not instant_pages and not queued_pages:
            return None

        bot = cls(instant_pages, queued_pages,
                  check_interval=data.get("check_interval", CHECK_INTERVAL))
        bot.bot_id        = data.get("bot_id", bot.bot_id)
        bot.running       = False
        bot.finished      = data.get("finished", False)
        bot.started_at    = data.get("started_at")
        bot.last_check    = data.get("last_check")
        bot.tweets_posted = data.get("tweets_posted", 0)
        bot.errors        = data.get("errors", 0)
        bot._seen_ids     = set(data.get("seen_ids", []))
        bot._initialized  = data.get("initialized", False)
        bot._start_ts     = data.get("start_ts", 0)
        bot._next_slot_ts = data.get("next_slot_ts", 0)
        bot._tw_user_id   = data.get("tw_user_id")
        bot._log_entries  = list(data.get("log_entries", []))

        for t in data.get("queue", []):
            page = next((p for p in all_pages if p["page_id"] == t.get("page_id")), None)
            if not page:
                continue
            heapq.heappush(bot._queue, _PostTask(
                run_at=t["run_at"], tweet_url=t["tweet_url"],
                text=t.get("text", ""), photos=t.get("photos", []), page=page,
            ))
        return bot

    # ── İç mantık ────────────────────────────────────────────────────────────

    def _log(self, level: str, text: str):
        self._log_entries.append({
            "time":  datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "text":  text,
        })
        logger.info(f"[TW:{self.bot_id}] {text}")

    def _get_client(self):
        try:
            import tweepy
        except ImportError:
            raise RuntimeError("tweepy kurulu değil — pip install tweepy")
        import config as _cfg
        missing = [k for k, v in {
            "TW_API_KEY":             _cfg.TW_API_KEY,
            "TW_API_SECRET":          _cfg.TW_API_SECRET,
            "TW_ACCESS_TOKEN":        _cfg.TW_ACCESS_TOKEN,
            "TW_ACCESS_TOKEN_SECRET": _cfg.TW_ACCESS_TOKEN_SECRET,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Eksik Twitter credentials: {', '.join(missing)}")
        return tweepy.Client(
            consumer_key=_cfg.TW_API_KEY,
            consumer_secret=_cfg.TW_API_SECRET,
            access_token=_cfg.TW_ACCESS_TOKEN,
            access_token_secret=_cfg.TW_ACCESS_TOKEN_SECRET,
            wait_on_rate_limit=False,
        )

    def _run(self):
        from config import is_quiet_hours
        self._check()
        self._next_check_ts = int(datetime.now().timestamp()) + self.check_interval
        _was_quiet = False

        while self.running:
            if is_quiet_hours():
                if not _was_quiet:
                    self._log("warn", "Gece modu aktif — duraklatıldı")
                    _was_quiet = True
                time.sleep(10)
                continue
            if _was_quiet:
                self._log("info", "Gece modu bitti — devam ediyor")
                _was_quiet = False

            now = int(datetime.now().timestamp())

            while self._queue and self._queue[0].run_at <= now:
                task = heapq.heappop(self._queue)
                threading.Thread(target=self._execute_task, args=(task,), daemon=True).start()

            if now >= self._next_check_ts:
                self._check()
                self._next_check_ts = now + self.check_interval

            time.sleep(5)

    def _check(self):
        self.last_check = datetime.now().strftime("%H:%M:%S")
        self._log("info", "Liked tweets kontrol ediliyor...")

        try:
            client = self._get_client()
        except RuntimeError as e:
            self._log("error", str(e))
            return

        if not self._tw_user_id:
            try:
                me = client.get_me(user_auth=True)
                if me.data:
                    self._tw_user_id = str(me.data.id)
                    self._log("info", f"Twitter kullanıcı: @{me.data.username} (id={self._tw_user_id})")
                else:
                    self._log("error", "Twitter user ID alınamadı")
                    return
            except Exception as e:
                self._log("error", f"User ID hatası: {e}")
                return

        try:
            import tweepy
            liked = client.get_liked_tweets(
                self._tw_user_id,
                tweet_fields=["text", "note_tweet", "attachments", "author_id", "created_at"],
                expansions=["attachments.media_keys", "author_id"],
                media_fields=["url", "preview_image_url", "type"],
                user_fields=["username"],
                max_results=10,
                user_auth=True,
            )
        except tweepy.TooManyRequests:
            self._log("warn", "Rate limit — bir sonraki kontrolde tekrar denenir")
            return
        except Exception as e:
            self._log("error", f"API hatası: {e}")
            return

        if not liked.data:
            self._log("info", "Liked list boş veya erişilemiyor")
            return

        media_map = {}
        if liked.includes and "media" in liked.includes:
            for m in liked.includes["media"]:
                media_map[m.media_key] = m

        author_map = {}
        if liked.includes and "users" in liked.includes:
            for u in liked.includes["users"]:
                author_map[str(u.id)] = u.username

        if not self._initialized:
            for tweet in liked.data:
                self._seen_ids.add(str(tweet.id))
            self._initialized = True
            self._log("info", f"İlk snapshot: {len(liked.data)} mevcut like kaydedildi, izleme başladı")
            return

        new_likes = [t for t in liked.data if str(t.id) not in self._seen_ids]

        if not new_likes:
            self._log("info", f"Yeni like yok ({len(liked.data)} kontrol edildi)")
            return

        self._log("info", f"{len(new_likes)} yeni like tespit edildi")

        for tweet in new_likes:
            self._seen_ids.add(str(tweet.id))
            tweet_id   = str(tweet.id)
            author_usr = author_map.get(str(tweet.author_id), "")
            tweet_url  = (
                f"https://x.com/{author_usr}/status/{tweet_id}"
                if author_usr else
                f"https://x.com/i/web/status/{tweet_id}"
            )

            # Tam metin: note_tweet varsa onu, yoksa normal text
            raw_text = ""
            if hasattr(tweet, "note_tweet") and tweet.note_tweet:
                raw_text = tweet.note_tweet.get("text", "") if isinstance(tweet.note_tweet, dict) else str(tweet.note_tweet)
            if not raw_text:
                raw_text = tweet.text or ""
            text = re.sub(r"https://t\.co/\S+", "", raw_text).strip()

            # Medya analizi
            photos   = []
            has_video = False
            if tweet.attachments and tweet.attachments.get("media_keys"):
                for mk in tweet.attachments["media_keys"]:
                    m = media_map.get(mk)
                    if not m:
                        continue
                    if m.type == "photo" and m.url:
                        photos.append(m.url)
                    elif m.type in ("video", "animated_gif"):
                        has_video = True

            # Video → frame kuyruğuna al
            if has_video:
                self._log("info", f"🎥 Videolu tweet frame kuyruğuna alındı: {tweet_url}")
                threading.Thread(
                    target=self._queue_video,
                    args=(tweet_url, text),
                    daemon=True,
                ).start()
                continue

            # Fotoğraflı veya metin → normal akış

            # Anında sayfalar — aralarında 30sn bekleme
            for i, page in enumerate(self.instant_pages):
                delay      = i * INSTANT_SPACING
                page_name  = page.get("name", page["page_id"])
                media_info = f"{len(photos)} foto" if photos else "medyasız"
                run_time   = (datetime.now().timestamp() + delay)
                run_str    = datetime.fromtimestamp(run_time).strftime("%H:%M:%S")
                self._log("info", f"⚡ [{page_name}] {run_str}'de → {tweet_url} ({media_info})")
                task = _PostTask(run_at=0, tweet_url=tweet_url,
                                 text=text, photos=photos, page=page)
                t = threading.Timer(delay, self._execute_task, args=(task,))
                t.daemon = True
                t.start()

            # Kuyruklu sayfalar — +5dk aralıklı
            for page in self.queued_pages:
                self._next_slot_ts = max(
                    int(datetime.now().timestamp()) + POST_SPACING,
                    self._next_slot_ts + POST_SPACING,
                )
                slot_ts    = self._next_slot_ts
                page_name  = page.get("name", page["page_id"])
                run_time   = datetime.fromtimestamp(slot_ts).strftime("%H:%M")
                media_info = f"{len(photos)} foto" if photos else "medyasız"
                self._log("info", f"⏰ [{page_name}] {run_time}'de → {tweet_url} ({media_info})")
                heapq.heappush(self._queue, _PostTask(
                    run_at=slot_ts, tweet_url=tweet_url,
                    text=text, photos=photos, page=page,
                ))

    def _queue_video(self, tweet_url: str, text: str):
        """Tweet'i fetch edip frame'leri çıkarır, pending_videos'a ekler."""
        try:
            from twitter_fetcher import fetch_tweet
            import video_frames

            result = fetch_tweet(tweet_url)
            if not result.get("ok"):
                self._log("warn", f"Video fetch başarısız: {result.get('error')} — {tweet_url}")
                return
            if not result.get("video_url"):
                self._log("warn", f"Video URL bulunamadı: {tweet_url}")
                return

            self._log("info", f"🎬 Frame çıkarılıyor: {tweet_url}")
            frames_result = video_frames.extract_frames(result["video_url"], count=8)
            if not frames_result.get("ok"):
                self._log("warn", f"Frame çıkarma başarısız: {frames_result.get('error')}")
                return

            sid    = frames_result["session_id"]
            frames = [f"/frames/{sid}/{name}" for name in frames_result["frames"]]
            entry  = {
                "id":           uuid.uuid4().hex[:8],
                "tweet_url":    tweet_url,
                "text":         text,
                "author":       result.get("author", ""),
                "session_id":   sid,
                "frames":       frames,
                "duration":     frames_result.get("duration", 0),
                "detected_at":  datetime.now().strftime("%H:%M:%S"),
            }
            self._pending_videos.append(entry)
            self._log("success", f"✓ {len(frames)} frame hazır, TW Video sayfasında onay bekliyor")
        except Exception as e:
            self._log("error", f"Video kuyruk hatası: {e}")

    def _execute_task(self, task: _PostTask):
        from facebook_poster import upload_photo_to_page
        page_name = task.page.get("name", task.page["page_id"])
        caption   = task.text if task.text else ""

        try:
            if task.photos:
                result = upload_photo_to_page(task.page, task.photos, caption=caption)
                if result is True:
                    self.tweets_posted += 1
                    self._log("success", f"✓ [{page_name}] {len(task.photos)} foto paylaşıldı")
                else:
                    self.errors += 1
                    detail = f": {result}" if isinstance(result, str) else ""
                    self._log("error", f"✗ [{page_name}] foto yüklenemedi{detail}")
            else:
                ok = _post_text_to_fb(task.page, caption)
                if ok:
                    self.tweets_posted += 1
                    self._log("success", f"✓ [{page_name}] text post paylaşıldı")
                else:
                    self.errors += 1
                    self._log("error", f"✗ [{page_name}] text post başarısız")
        except Exception as e:
            self.errors += 1
            self._log("error", f"✗ [{page_name}] hata: {e}")
