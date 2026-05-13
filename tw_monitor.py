"""
Twitter Like Bot
- instant_pages: yeni like bulununca anında paylaşır (5dk ara ile)
- queued_pages:  her gönderi +5dk arayla planlanır (quiet hours'ta durur)
- prime_pages:   saate göre dinamik aralık (30/çarpan dk), 7/24 çalışır, quiet hours YOK
- Videolu tweet'ler:
    auto_post_video=True  → yt-dlp ile indir, FB Reels olarak yükle
    auto_post_video=False → _pending_videos kuyruğuna alınır, kullanıcı frame seçip paylaşır
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
INSTANT_SPACING  =  5 * 60   # instant sayfalardaki paylaşımlar arası bekleme (sn)

# Prime saatler — saat başına etkileşim çarpanı (Türkiye, spor kitlesi)
PRIME_SCHEDULE: dict[int, float] = {
    0: 0.3, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.3, 5: 0.3,  # gece
    6: 0.6,                                              # uyanış
    7: 1.4, 8: 1.4,                                     # sabah commute
    9: 1.1, 10: 1.1,                                    # iş başı
    11: 1.2, 12: 1.2,                                   # öğle öncesi
    13: 1.5,                                             # öğle molası ★
    14: 0.9, 15: 0.9,                                   # öğleden sonra
    16: 1.1, 17: 1.1,                                   # iş sonu
    18: 1.6, 19: 1.6,                                   # eve dönüş ★★
    20: 1.8, 21: 1.8,                                   # prime time ★★★
    22: 1.4,                                             # gece geç
    23: 0.8,                                             # düşüş
}
PRIME_BASE_MINUTES = 90  # baz aralık (dk) — çarpana bölünür (~16 post/gün kapasitesi)
PRIME_DEAD_START   = 0   # ölü saat başlangıcı (dahil)
PRIME_DEAD_END     = 7   # ölü saat bitişi (hariç) — 00:00-06:59 arası post yok


def _prime_interval_at(ts: int) -> int:
    """Verilen timestamp'teki saate göre prime aralığını saniye olarak döndürür."""
    hour = datetime.fromtimestamp(ts).hour
    multiplier = PRIME_SCHEDULE.get(hour, 1.0)
    return max(60, int((PRIME_BASE_MINUTES / multiplier) * 60))


def _prime_skip_dead(ts: int) -> int:
    """Slot ölü saate (00-07) düştüyse aynı günün 07:00'ına atar."""
    dt = datetime.fromtimestamp(ts)
    if PRIME_DEAD_START <= dt.hour < PRIME_DEAD_END:
        return int(dt.replace(hour=PRIME_DEAD_END, minute=0,
                               second=0, microsecond=0).timestamp())
    return ts


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
    run_at:         int
    tweet_url:      str       = field(compare=False)
    text:           str       = field(compare=False)
    photos:         list[str] = field(compare=False)
    page:           dict      = field(compare=False)
    is_video_post:  bool      = field(compare=False, default=False)
    task_id:        str       = field(compare=False,
                                      default_factory=lambda: uuid.uuid4().hex[:8])


class TWLikeBot:
    def __init__(self,
                 instant_pages:  list[dict],
                 queued_pages:   list[dict],
                 prime_pages:    list[dict] = None,
                 check_interval: int = CHECK_INTERVAL,
                 bot_name:        str = "",
                 tw_account:      int = 1,
                 watermark_icon:  str = "",
                 auto_post_video: bool = False):
        self.bot_id           = uuid.uuid4().hex[:8]
        self.bot_name         = bot_name
        self.tw_account       = tw_account      # 1 veya 2
        self.watermark_icon   = watermark_icon  # icons/ klasöründeki dosya adı, "" = yok
        self.auto_post_video  = auto_post_video # True = Reels olarak yükle, False = TW Video sayfasına
        self.instant_pages  = instant_pages
        self.queued_pages   = queued_pages
        self.prime_pages    = prime_pages or []
        self.check_interval = check_interval

        self.running           = False
        self.finished          = False
        self.started_at        = None
        self.last_check        = None
        self._next_check_ts    = 0
        self._next_slot_ts     = 0
        self._next_prime_slot_ts = 0
        self._sleep_until      = 0   # unix ts; 0 = uyutma yok

        self.tweets_posted  = 0
        self.errors         = 0

        self._log_entries    : list[dict]      = []
        self._thread         = None
        self._seen_ids       : set[str]        = set()
        self._initialized    = False
        self._start_ts       = 0
        self._tw_user_id     : str | None      = None
        self._queue          : list[_PostTask]        = []
        self._pending_videos : list[dict]             = []  # frame hazır, kullanıcı onayı bekliyor
        self._posted_history : list[dict]             = []  # son 10 başarılı paylaşım
        self._timers         : list[threading.Timer]  = []  # iptal edilebilir instant timer'lar
        self._last_post_ts   : int                    = 0   # son başarılı paylaşımın unix ts
        self._queue_lock     : threading.Lock         = threading.Lock()

    # ── Arayüz ──────────────────────────────────────────────────────────────

    def start(self):
        self.running       = True
        self._start_ts     = int(datetime.now().timestamp())
        self.started_at    = datetime.now().strftime("%H:%M:%S")
        self._next_slot_ts = int(datetime.now().timestamp())
        instant_names = [p.get("name", p["page_id"]) for p in self.instant_pages]
        queued_names  = [p.get("name", p["page_id"]) for p in self.queued_pages]
        prime_names   = [p.get("name", p["page_id"]) for p in self.prime_pages]
        self._log("info",
                  f"TW Like Bot başlatıldı — "
                  f"anında: {instant_names} | kuyruk: {queued_names} | "
                  f"prime: {prime_names} | kontrol: {self.check_interval // 60}dk")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        for t in self._timers:
            t.cancel()
        self._timers.clear()
        self._log("warn", f"Bot durduruldu. ({len(self._queue)} görev kuyrukta, "
                          f"{len(self._pending_videos)} video bekliyor)")

    def restart(self):
        """Durdurulmuş botu aynı ayar + kuyrukla yeniden başlatır."""
        if self.running:
            return
        self.running    = True
        self.finished   = False
        self._sleep_until = 0
        self.started_at = datetime.now().strftime("%H:%M:%S")
        self._log("info", f"Bot yeniden başlatıldı — "
                          f"{len(self._queue)} görev kuyrukta korundu")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def sleep_until(self, until_ts: int):
        """Botu belirtilen unix ts'e kadar uyut."""
        self._sleep_until = until_ts
        until_str = datetime.fromtimestamp(until_ts).strftime("%d.%m %H:%M")
        self._log("warn", f"Bot uyutuldu — {until_str}'e kadar kontrol yapılmayacak")

    def set_account(self, account: int):
        """Twitter hesabını değiştir (1/2/3)."""
        if account not in (1, 2, 3):
            return
        old = self.tw_account
        self.tw_account = account
        self._log("info", f"Twitter hesabı değiştirildi: {old} → {account}")

    def check_now(self):
        if not self.running:
            return
        self._next_check_ts = int(datetime.now().timestamp()) + self.check_interval
        threading.Thread(target=self._check, daemon=True).start()

    def dismiss_video(self, video_id: str):
        self._pending_videos = [v for v in self._pending_videos if v["id"] != video_id]

    def run_queue_item(self, task_id: str) -> bool:
        """Kuyruktaki görevi hemen çalıştırır ve kuyruktan kaldırır."""
        with self._queue_lock:
            task = next((t for t in self._queue if t.task_id == task_id), None)
            if task is None:
                return False
            self._queue = [t for t in self._queue if t.task_id != task_id]
            heapq.heapify(self._queue)
            now_ts = int(datetime.now().timestamp())
            if task.run_at > now_ts:
                self._next_slot_ts       = min(self._next_slot_ts, now_ts)
                self._next_prime_slot_ts = min(self._next_prime_slot_ts, now_ts)
        threading.Thread(target=self._execute_task, args=(task,), daemon=True).start()
        return True

    def remove_queue_item(self, task_id: str) -> bool:
        """Kuyruktaki belirli bir görevi iptal eder. True döner iptal edildiyse."""
        with self._queue_lock:
            before = len(self._queue)
            self._queue = [t for t in self._queue if t.task_id != task_id]
            heapq.heapify(self._queue)
            return len(self._queue) < before

    def reschedule_queue_item(self, task_id: str) -> dict | None:
        """
        Zaman çizelgesi: (son post) → q1 → q2 → ...
        Mevcut prime interval kadar boşluk olan ilk gap'e sıkıştır.
        Uygun gap yoksa → olduğu yerde bırak.
        """
        with self._queue_lock:
            task = next((t for t in self._queue if t.task_id == task_id), None)
            if task is None:
                return None

            original_run_at = task.run_at
            remaining = sorted(
                [t for t in self._queue if t.task_id != task_id],
                key=lambda t: t.run_at,
            )

            now_ts         = int(datetime.now().timestamp())
            prime_interval = _prime_interval_at(now_ts)
            anchor         = self._last_post_ts if self._last_post_ts > 0 else now_ts
            timeline       = [anchor] + [t.run_at for t in remaining]

            new_slot = None
            for i in range(len(timeline)):
                gap_start = timeline[i]
                gap_end   = timeline[i + 1] if i + 1 < len(timeline) else float('inf')
                candidate = gap_start + prime_interval
                if (gap_end - gap_start >= prime_interval + POST_SPACING
                        and candidate > now_ts
                        and gap_end - candidate >= POST_SPACING):
                    adjusted = _prime_skip_dead(int(candidate))
                    # _prime_skip_dead sonrası mevcut slotlarla çakışıyor olabilir
                    for _ in range(20):
                        if any(abs(t.run_at - adjusted) < POST_SPACING for t in remaining):
                            adjusted = _prime_skip_dead(adjusted + _prime_interval_at(adjusted))
                        else:
                            break
                    new_slot = adjusted
                    break

            if new_slot is None:
                return {
                    "run_at":    original_run_at,
                    "run_time":  datetime.fromtimestamp(original_run_at).strftime("%d.%m %H:%M"),
                    "unchanged": True,
                }

            self._queue = [t for t in self._queue if t.task_id != task_id]
            heapq.heapify(self._queue)
            task.run_at = new_slot
            heapq.heappush(self._queue, task)
            return {"run_at": new_slot, "run_time": datetime.fromtimestamp(new_slot).strftime("%d.%m %H:%M")}

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
            "bot_name":        self.bot_name,
            "tw_account":      self.tw_account,
            "watermark_icon":  self.watermark_icon,
            "auto_post_video": self.auto_post_video,
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
            "prime_pages":     [p.get("name", p["page_id"]) for p in self.prime_pages],
            "check_interval":  self.check_interval,
            "sleep_until":     self._sleep_until,
            "sleep_until_str": datetime.fromtimestamp(self._sleep_until).strftime("%d.%m %H:%M") if self._sleep_until > int(datetime.now().timestamp()) else "",
        }

    # ── Persistence ─────────────────────────────────────────────────────────

    def to_state_dict(self) -> dict:
        return {
            "bot_id":            self.bot_id,
            "bot_name":          self.bot_name,
            "tw_account":        self.tw_account,
            "watermark_icon":    self.watermark_icon,
            "auto_post_video":   self.auto_post_video,
            "instant_page_ids":  [p["page_id"] for p in self.instant_pages],
            "queued_page_ids":   [p["page_id"] for p in self.queued_pages],
            "prime_page_ids":    [p["page_id"] for p in self.prime_pages],
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
            "next_slot_ts":       self._next_slot_ts,
            "next_prime_slot_ts": self._next_prime_slot_ts,
            "tw_user_id":         self._tw_user_id,
            "log_entries":      self._log_entries[-500:],
            "posted_history":   self._posted_history,
            "last_post_ts":     self._last_post_ts,
            "sleep_until":      self._sleep_until,
            "queue": [
                {"run_at": t.run_at, "tweet_url": t.tweet_url,
                 "text": t.text, "photos": t.photos,
                 "is_video_post": t.is_video_post,
                 "page_id": t.page.get("page_id", ""),
                 "task_id": t.task_id}
                for t in self._queue
            ],
        }

    @classmethod
    def from_state_dict(cls, data: dict, all_pages: list[dict]):
        instant_pages = [p for p in all_pages
                         if p["page_id"] in data.get("instant_page_ids", [])]
        queued_pages  = [p for p in all_pages
                         if p["page_id"] in data.get("queued_page_ids", [])]
        prime_pages   = [p for p in all_pages
                         if p["page_id"] in data.get("prime_page_ids", [])]
        if not instant_pages and not queued_pages and not prime_pages:
            return None

        bot = cls(instant_pages, queued_pages, prime_pages,
                  check_interval=data.get("check_interval", CHECK_INTERVAL),
                  bot_name=data.get("bot_name", ""),
                  tw_account=data.get("tw_account", 1),
                  watermark_icon=data.get("watermark_icon", ""),
                  auto_post_video=data.get("auto_post_video", False))
        bot.bot_id               = data.get("bot_id", bot.bot_id)
        bot.running              = False
        bot.finished             = data.get("finished", False)
        bot.started_at           = data.get("started_at")
        bot.last_check           = data.get("last_check")
        bot.tweets_posted        = data.get("tweets_posted", 0)
        bot.errors               = data.get("errors", 0)
        bot._seen_ids            = set(data.get("seen_ids", []))
        bot._initialized         = data.get("initialized", False)
        bot._start_ts            = data.get("start_ts", 0)
        bot._next_slot_ts        = data.get("next_slot_ts", 0)
        bot._next_prime_slot_ts  = data.get("next_prime_slot_ts", 0)
        bot._tw_user_id          = data.get("tw_user_id")
        bot._log_entries         = list(data.get("log_entries", []))
        bot._posted_history      = list(data.get("posted_history", []))
        bot._last_post_ts        = data.get("last_post_ts", 0)
        bot._sleep_until         = data.get("sleep_until", 0)

        for t in data.get("queue", []):
            page = next((p for p in all_pages if p["page_id"] == t.get("page_id")), None)
            if not page:
                continue
            heapq.heappush(bot._queue, _PostTask(
                run_at=t["run_at"], tweet_url=t["tweet_url"],
                text=t.get("text", ""), photos=t.get("photos", []), page=page,
                is_video_post=t.get("is_video_post", False),
                task_id=t.get("task_id", uuid.uuid4().hex[:8]),
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
        if self.tw_account == 2:
            at  = _cfg.TW2_ACCESS_TOKEN
            ats = _cfg.TW2_ACCESS_TOKEN_SECRET
            if not at or not ats:
                raise RuntimeError("Hesap 2 access token'ları .env'de tanımlı değil "
                                   "(TW2_ACCESS_TOKEN / TW2_ACCESS_TOKEN_SECRET)")
        elif self.tw_account == 3:
            at  = _cfg.TW3_ACCESS_TOKEN
            ats = _cfg.TW3_ACCESS_TOKEN_SECRET
            if not at or not ats:
                raise RuntimeError("Hesap 3 access token'ları .env'de tanımlı değil "
                                   "(TW3_ACCESS_TOKEN / TW3_ACCESS_TOKEN_SECRET)")
        else:
            at  = _cfg.TW_ACCESS_TOKEN
            ats = _cfg.TW_ACCESS_TOKEN_SECRET

        pfx = "TW3" if self.tw_account == 3 else ("TW2" if self.tw_account == 2 else "TW")
        missing = [k for k, v in {
            "TW_API_KEY":    _cfg.TW_API_KEY,
            "TW_API_SECRET": _cfg.TW_API_SECRET,
            f"{pfx}_ACCESS_TOKEN":        at,
            f"{pfx}_ACCESS_TOKEN_SECRET":  ats,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Eksik Twitter credentials: {', '.join(missing)}")
        return tweepy.Client(
            consumer_key=_cfg.TW_API_KEY,
            consumer_secret=_cfg.TW_API_SECRET,
            access_token=at,
            access_token_secret=ats,
            wait_on_rate_limit=False,
        )

    def _run(self):
        from config import is_quiet_hours
        self._check()
        self._next_check_ts = int(datetime.now().timestamp()) + self.check_interval
        _was_quiet = False

        _was_sleeping = False
        while self.running:
            now   = int(datetime.now().timestamp())
            quiet = is_quiet_hours()

            # Uyutma kontrolü
            if self._sleep_until > now:
                if not _was_sleeping:
                    _was_sleeping = True
                time.sleep(5)
                continue
            elif _was_sleeping:
                _was_sleeping = False
                self._sleep_until = 0
                self._log("info", "Uyku süresi bitti — devam ediyor")

            if quiet and not _was_quiet:
                self._log("warn", "Gece modu aktif — instant/kuyruk duraklatıldı"
                          + (" | prime devam ediyor" if self.prime_pages else ""))
                _was_quiet = True
            elif not quiet and _was_quiet:
                self._log("info", "Gece modu bitti — devam ediyor")
                _was_quiet = False

            # Kuyruk: prime sayfalar quiet hours'tan bağımsız çalışır
            with self._queue_lock:
                due = []
                while self._queue and self._queue[0].run_at <= now:
                    due.append(heapq.heappop(self._queue))
            for task in due:
                threading.Thread(target=self._execute_task, args=(task,), daemon=True).start()

            # Check: prime varsa quiet hours'ta da çalışır (yeni tweet bulmak için),
            # yoksa sadece quiet olmayan saatlerde.
            if now >= self._next_check_ts:
                if not quiet or self.prime_pages:
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
                max_results=25,
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

        # Liked listesinden çıkmış ID'leri seen_ids'ten sil (unfav → refav algısı için)
        current_ids = {str(t.id) for t in liked.data}
        self._seen_ids -= (self._seen_ids - current_ids)

        new_likes = [t for t in liked.data if str(t.id) not in self._seen_ids]

        if not new_likes:
            self._log("info", f"Yeni like yok ({len(liked.data)} kontrol edildi)")
            return

        self._log("info", f"{len(new_likes)} yeni like tespit edildi")

        # Instant sayfalardaki paylaşımları aynı kontrol içinde teker teker sıraya
        # diziyoruz; aynı anda 2 like geldiyse, ikisi de aynı anda paylaşılmasın.
        # Her (tweet, page) kombinasyonu için INSTANT_SPACING kadar offset eklenir.
        instant_offset = 0

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

            # Video → auto_post_video açıksa Reels olarak planla, kapalıysa eski akış
            if has_video:
                if self.auto_post_video:
                    self._log("info", f"🎥 Videolu tweet Reels kuyruğuna alındı: {tweet_url}")
                    self._schedule_video_tasks(tweet_url, text, instant_offset)
                    # instant_offset'i video için de ilerlet
                    instant_offset += INSTANT_SPACING * len(self.instant_pages)
                else:
                    self._log("info", f"🎥 Videolu tweet frame kuyruğuna alındı: {tweet_url}")
                    threading.Thread(
                        target=self._queue_video,
                        args=(tweet_url, text),
                        daemon=True,
                    ).start()
                continue

            # Fotoğraflı veya metin → normal akış
            from config import is_quiet_hours
            quiet = is_quiet_hours()

            # ⚡ Anında sayfalar — quiet hours'ta planlanmaz
            if not quiet:
                for page in self.instant_pages:
                    delay      = instant_offset
                    instant_offset += INSTANT_SPACING
                    page_name  = page.get("name", page["page_id"])
                    media_info = f"{len(photos)} foto" if photos else "medyasız"
                    run_time   = datetime.now().timestamp() + delay
                    run_str    = datetime.fromtimestamp(run_time).strftime("%H:%M:%S")
                    tag        = "⚡" if delay == 0 else "⏱"
                    self._log("info", f"{tag} [{page_name}] {run_str}'de → {tweet_url} ({media_info})")
                    task = _PostTask(run_at=0, tweet_url=tweet_url,
                                     text=text, photos=photos, page=page)
                    t = threading.Timer(delay, self._execute_task, args=(task,))
                    t.daemon = True
                    t.start()
                    self._timers.append(t)

            # ⏰ Kuyruklu sayfalar — +5dk aralıklı, quiet hours'ta planlanmaz
            if not quiet:
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
                    with self._queue_lock:
                        heapq.heappush(self._queue, _PostTask(
                            run_at=slot_ts, tweet_url=tweet_url,
                            text=text, photos=photos, page=page,
                        ))

            # 📈 Prime sayfalar — 07-24, quiet hours YOK, saate göre dinamik aralık
            for page in self.prime_pages:
                now_ts = int(datetime.now().timestamp())
                self._next_prime_slot_ts = max(now_ts, self._next_prime_slot_ts)
                slot_ts = _prime_skip_dead(self._next_prime_slot_ts)
                # Mevcut queue ile çakışma varsa ilerle (manuel reschedule'a saygı)
                with self._queue_lock:
                    existing = list(self._queue)
                for _ in range(20):
                    if any(abs(t.run_at - slot_ts) < POST_SPACING for t in existing):
                        interval = _prime_interval_at(slot_ts)
                        slot_ts  = _prime_skip_dead(slot_ts + interval)
                    else:
                        break
                interval = _prime_interval_at(slot_ts)
                self._next_prime_slot_ts = slot_ts + interval
                page_name  = page.get("name", page["page_id"])
                run_time   = datetime.fromtimestamp(slot_ts).strftime("%H:%M")
                media_info = f"{len(photos)} foto" if photos else "medyasız"
                interval_m = round(interval / 60, 1)
                self._log("info", f"📈 [{page_name}] {run_time}'de → {tweet_url} ({media_info}, aralık: {interval_m}dk)")
                with self._queue_lock:
                    heapq.heappush(self._queue, _PostTask(
                        run_at=slot_ts, tweet_url=tweet_url,
                        text=text, photos=photos, page=page,
                    ))

    def _schedule_video_tasks(self, tweet_url: str, text: str, instant_offset: int = 0):
        """auto_post_video modunda video görevlerini instant/queued/prime kuyruklarına ekler."""
        from config import is_quiet_hours
        quiet = is_quiet_hours()

        # ⚡ Anında sayfalar
        if not quiet:
            for page in self.instant_pages:
                delay      = instant_offset
                instant_offset += INSTANT_SPACING
                page_name  = page.get("name", page["page_id"])
                run_time   = datetime.now().timestamp() + delay
                run_str    = datetime.fromtimestamp(run_time).strftime("%H:%M:%S")
                tag        = "⚡" if delay == 0 else "⏱"
                self._log("info", f"{tag} [{page_name}] {run_str}'de video → {tweet_url}")
                task = _PostTask(run_at=0, tweet_url=tweet_url,
                                 text=text, photos=[], page=page, is_video_post=True)
                t = threading.Timer(delay, self._execute_task, args=(task,))
                t.daemon = True
                t.start()
                self._timers.append(t)

        # ⏰ Kuyruklu sayfalar
        if not quiet:
            for page in self.queued_pages:
                self._next_slot_ts = max(
                    int(datetime.now().timestamp()) + POST_SPACING,
                    self._next_slot_ts + POST_SPACING,
                )
                slot_ts   = self._next_slot_ts
                page_name = page.get("name", page["page_id"])
                run_time  = datetime.fromtimestamp(slot_ts).strftime("%H:%M")
                self._log("info", f"⏰ [{page_name}] {run_time}'de video → {tweet_url}")
                with self._queue_lock:
                    heapq.heappush(self._queue, _PostTask(
                        run_at=slot_ts, tweet_url=tweet_url,
                        text=text, photos=[], page=page, is_video_post=True,
                    ))

        # 📈 Prime sayfalar
        for page in self.prime_pages:
            now_ts = int(datetime.now().timestamp())
            self._next_prime_slot_ts = max(now_ts, self._next_prime_slot_ts)
            slot_ts  = _prime_skip_dead(self._next_prime_slot_ts)
            interval = _prime_interval_at(slot_ts)
            self._next_prime_slot_ts = slot_ts + interval
            page_name  = page.get("name", page["page_id"])
            run_time   = datetime.fromtimestamp(slot_ts).strftime("%H:%M")
            interval_m = round(interval / 60, 1)
            self._log("info", f"📈 [{page_name}] {run_time}'de video → {tweet_url} (aralık: {interval_m}dk)")
            with self._queue_lock:
                heapq.heappush(self._queue, _PostTask(
                    run_at=slot_ts, tweet_url=tweet_url,
                    text=text, photos=[], page=page, is_video_post=True,
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

    def _record_posted(self, task: _PostTask, page_name: str, post_url: str = ""):
        self._last_post_ts = int(datetime.now().timestamp())
        self._posted_history.append({
            "posted_at":  datetime.now().strftime("%d.%m %H:%M"),
            "tweet_url":  task.tweet_url,
            "text":       task.text[:120] if task.text else "",
            "photos":     len(task.photos),
            "page":       page_name,
            "page_id":    task.page.get("page_id", ""),
            "page_token": task.page.get("access_token", ""),
            "post_url":   post_url,
        })
        if len(self._posted_history) > 10:
            self._posted_history = self._posted_history[-10:]

    def _execute_task(self, task: _PostTask):
        from facebook_poster import upload_photo_to_page
        page_name = task.page.get("name", task.page["page_id"])
        caption   = task.text if task.text else ""

        # Video Reels yükleme
        if task.is_video_post:
            try:
                from video_downloader import download_video, delete_video
                from facebook_poster import upload_video_instant
                import re as _re
                # Tweet URL'den ID çıkar
                m = _re.search(r"/status/(\d+)", task.tweet_url)
                vid_id = m.group(1) if m else uuid.uuid4().hex[:8]
                self._log("info", f"⬇ [{page_name}] Video indiriliyor: {task.tweet_url}")
                video_path = download_video(task.tweet_url, f"tw_{vid_id}")
                if not video_path:
                    self.errors += 1
                    self._log("error", f"✗ [{page_name}] Video indirilemedi: {task.tweet_url}")
                    return
                wm_video_path = video_path
                if self.watermark_icon:
                    from watermark import apply_video_watermark
                    wm_video_path = apply_video_watermark(video_path, self.watermark_icon)
                    if wm_video_path != video_path:
                        self._log("info", f"🖼 [{page_name}] Video watermark uygulandı")
                self._log("info", f"⬆ [{page_name}] Reels yükleniyor...")
                result = upload_video_instant(task.page, wm_video_path, caption or "Video", task.tweet_url)
                # Watermark uygulandıysa geçici dosyayı sil, sonra orijinali de sil
                if wm_video_path != video_path:
                    delete_video(wm_video_path)
                delete_video(video_path)
                if result:
                    self.tweets_posted += 1
                    post_url = f"https://www.facebook.com/{task.page['page_id']}/videos/{result}"
                    self._log("success", f"✓ [{page_name}] Video Reels yüklendi (id={result})")
                    self._record_posted(task, page_name, post_url)
                else:
                    self.errors += 1
                    self._log("error", f"✗ [{page_name}] Reels yükleme başarısız")
            except Exception as e:
                self.errors += 1
                self._log("error", f"✗ [{page_name}] video hata: {e}")
            return

        try:
            if task.photos:
                # Watermark varsa fotoları işle
                photos     = task.photos
                tmp_files  = []
                if self.watermark_icon:
                    from watermark import apply_watermarks
                    photos, tmp_files = apply_watermarks(task.photos, self.watermark_icon)
                    if tmp_files:
                        self._log("info", f"🖼 [{page_name}] {len(tmp_files)}/{len(task.photos)} fotoya watermark uygulandı")

                result, post_url = upload_photo_to_page(
                    task.page, photos, caption=caption, _return_post_url=True)

                # Geçici dosyaları temizle
                if tmp_files:
                    from watermark import cleanup_temps
                    cleanup_temps(tmp_files)

                if result is True:
                    self.tweets_posted += 1
                    self._log("success", f"✓ [{page_name}] {len(task.photos)} foto paylaşıldı")
                    self._record_posted(task, page_name, post_url)
                else:
                    self.errors += 1
                    detail = f": {result}" if isinstance(result, str) else ""
                    self._log("error", f"✗ [{page_name}] foto yüklenemedi{detail}")
            else:
                ok = _post_text_to_fb(task.page, caption)
                if ok:
                    self.tweets_posted += 1
                    self._log("success", f"✓ [{page_name}] text post paylaşıldı")
                    self._record_posted(task, page_name, "")
                else:
                    self.errors += 1
                    self._log("error", f"✗ [{page_name}] text post başarısız")
        except Exception as e:
            self.errors += 1
            self._log("error", f"✗ [{page_name}] hata: {e}")
