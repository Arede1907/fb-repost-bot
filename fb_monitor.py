"""
Facebook Repost Bot
- Yalnızca yönetilen (token'ı olan) sayfaları izler
- Graph API /feed endpoint'i ile yeni postları çeker
- Yeni postları hedef sayfalara 3 dk arayla repost eder
"""
import heapq
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# ── Bot aktivite loglarını dosyaya yaz ──────────────────────────────────
_log_dir = os.path.dirname(os.path.abspath(__file__))
_bot_log_file = os.path.join(_log_dir, "repost_activity.log")
_file_handler = logging.FileHandler(_bot_log_file, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                              datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_file_handler)
logger.setLevel(logging.INFO)

CHECK_INTERVAL = 30       # saniye — kaynak sayfa kontrol sıklığı
REPOST_SPACING = 3 * 60  # saniye — repostlar arası minimum bekleme


def get_page_posts(page_id: str, access_token: str, since_ts: int = None,
                   log_fn=None) -> list[dict]:
    """
    Yönetilen bir FB sayfasının son gönderilerini çeker.
    User Access Token veya Page Token ile çalışır.
    """
    import config as _cfg

    def _log(level, text):
        logger.info(text)
        if log_fn:
            log_fn(level, text)

    # Feed okuma için: önce page token dene (kendi sayfasını okumak için daha güvenilir),
    # page token yoksa user token'a düş.
    token = access_token or _cfg.FB_USER_ACCESS_TOKEN
    if not token:
        _log("error", "Token bulunamadı.")
        return []

    params = {
        "access_token": token,
        "fields": "id,message,created_time,permalink_url",
        "limit": 20,
    }
    if since_ts:
        params["since"] = str(since_ts)

    url = f"https://graph.facebook.com/v19.0/{page_id}/feed"

    for attempt in range(2):          # ilk deneme + 1 retry
        try:
            resp = requests.get(url, params=params, timeout=20)
            data = resp.json()

            if "error" in data:
                err_obj  = data["error"]
                err_msg  = err_obj.get("message", str(err_obj))
                err_code = err_obj.get("code", "?")
                err_sub  = err_obj.get("error_subcode", "")
                detail   = f"kod={err_code}" + (f"/{err_sub}" if err_sub else "")

                # Auth hatası (kod 190) → bir kez retry, sonra token uyarısı
                if err_code == 190 and attempt == 0:
                    _log("warn", f"⚠ API auth hatası ({detail}), 5s sonra tekrar deneniyor...")
                    time.sleep(5)
                    continue

                if err_code == 190:
                    _log("error", f"Token geçersiz veya süresi dolmuş ({detail}) — "
                                  f"Ayarlar > User Token bölümünden güncelleyin")
                else:
                    _log("error", f"API hatası [{detail}]: {err_msg}")
                return []

            posts = data.get("data", [])
            _log("info", f"API → {len(posts)} gönderi")
            return posts

        except Exception as e:
            if attempt == 0:
                _log("warn", f"Bağlantı hatası, retry: {e}")
                time.sleep(5)
            else:
                _log("error", f"Bağlantı hatası: {e}")

    return []


# ── Görev ────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class _RepostTask:
    run_at:   int
    post_url: str  = field(compare=False)
    message:  str  = field(compare=False)
    page:     dict = field(compare=False)
    post_id:  str  = field(compare=False)


# ── Bot ───────────────────────────────────────────────────────────────────────

class FBRepostBot:
    """
    Yönetilen bir FB sayfasını izler, yeni postları hedef sayfalara repost eder.
    """

    def __init__(self, source_page: dict, target_pages: list[dict],
                 check_interval: int = CHECK_INTERVAL,
                 repost_spacing: int = REPOST_SPACING):
        self.bot_id         = uuid.uuid4().hex[:8]
        self.source_page    = source_page                        # {page_id, name, access_token}
        self.source_page_id = source_page["page_id"]
        self.source_name    = source_page.get("name", source_page["page_id"])
        self.source_token   = source_page["access_token"]
        self.target_pages   = target_pages
        self.check_interval = check_interval
        self.repost_spacing = repost_spacing

        self.running        = False
        self.finished       = False   # Restart sonrası durmuş bot'lar için
        self.started_at     = None
        self.last_check     = None
        self._next_check_ts = 0
        self._next_slot_ts  = 0

        self.posts_reposted = 0
        self.errors         = 0

        self._log_entries : list[dict] = []
        self._thread      = None
        self._seen_ids    : set[str] = set()
        self._initialized = False     # İlk check'te mevcut postları seed'ler
        self._start_ts    = 0         # Bot başlangıç zamanı (unix)
        self._repost_fn   = None
        self._queue       : list[_RepostTask] = []

    # ── Arayüz ───────────────────────────────────────────────────────────────

    def start(self):
        from facebook_poster import repost_to_page
        self._repost_fn    = repost_to_page
        self.running       = True
        self._start_ts     = int(datetime.now().timestamp())
        self.started_at    = datetime.now().strftime("%H:%M:%S")
        self._next_slot_ts = int(datetime.now().timestamp())
        self._thread       = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("info", f"Bot başlatıldı → {self.source_name} "
                          f"(kontrol: {self.check_interval}s, "
                          f"repost aralık: {self.repost_spacing//60}dk)")

    def stop(self):
        self.running = False
        self._log("warn", "Bot durduruldu.")

    @property
    def next_check_str(self) -> str:
        if not self._next_check_ts or not self.running:
            return "–"
        return datetime.fromtimestamp(self._next_check_ts).strftime("%H:%M:%S")

    @property
    def pending_tasks(self) -> int:
        return len(self._queue)

    def get_log(self, n: int = 200) -> list[dict]:
        return list(reversed(self._log_entries[-n:]))

    def to_dict(self) -> dict:
        return {
            "bot_id":         self.bot_id,
            "source_name":    self.source_name,
            "source_page_id": self.source_page_id,
            "targets":        [p.get("name", p["page_id"]) for p in self.target_pages],
            "running":        self.running,
            "finished":       self.finished,
            "started_at":     self.started_at,
            "last_check":     self.last_check or "–",
            "next_check":     self.next_check_str,
            "posts_reposted": self.posts_reposted,
            "pending_tasks":  self.pending_tasks,
            "errors":         self.errors,
        }

    # ── Persistence ──────────────────────────────────────────────────────────

    def to_state_dict(self) -> dict:
        """Disk'e yazılabilir tam durum — sadece config'den lookup edilemeyen
        runtime alanları + identifier'lar."""
        return {
            "bot_id":         self.bot_id,
            "source_page_id": self.source_page_id,
            "target_page_ids": [p["page_id"] for p in self.target_pages],
            "check_interval": self.check_interval,
            "repost_spacing": self.repost_spacing,
            "running":        self.running,
            "finished":       self.finished,
            "started_at":     self.started_at,
            "last_check":     self.last_check,
            "posts_reposted": self.posts_reposted,
            "errors":         self.errors,
            "seen_ids":       list(self._seen_ids),
            "initialized":    self._initialized,
            "start_ts":       self._start_ts,
            "next_slot_ts":   self._next_slot_ts,
            "queue": [
                {
                    "run_at":   t.run_at,
                    "post_url": t.post_url,
                    "message":  t.message,
                    "page_id":  t.page.get("page_id", ""),
                    "post_id":  t.post_id,
                }
                for t in self._queue
            ],
            "log_entries": self._log_entries[-500:],  # son 500 log
        }

    @classmethod
    def from_state_dict(cls, data: dict, all_pages: list[dict]):
        """State dict'inden restore et. all_pages = config.FB_PAGES (güncel token'lar).

        Source veya target sayfalarından en az biri config'de yoksa None döner.
        Restore edilen bot başlatılmaz; çağıran tarafın durumuna göre .start()
        çağırması veya finished=True bırakması gerekir.
        """
        source_page = next(
            (p for p in all_pages if p["page_id"] == data["source_page_id"]),
            None,
        )
        if not source_page:
            return None

        target_page_ids = data.get("target_page_ids", [])
        target_pages = [p for p in all_pages if p["page_id"] in target_page_ids]
        if not target_pages:
            return None

        bot = cls(
            source_page,
            target_pages,
            check_interval=data.get("check_interval", CHECK_INTERVAL),
            repost_spacing=data.get("repost_spacing", REPOST_SPACING),
        )
        # Yeni bot uuid üretti — saklanmış id'yi geri yükle
        bot.bot_id         = data.get("bot_id", bot.bot_id)
        bot.running        = False  # Henüz başlamadı; çağıran karar verecek
        bot.finished       = data.get("finished", False)
        bot.started_at     = data.get("started_at")
        bot.last_check     = data.get("last_check")
        bot.posts_reposted = data.get("posts_reposted", 0)
        bot.errors         = data.get("errors", 0)
        bot._seen_ids      = set(data.get("seen_ids", []))
        bot._initialized   = data.get("initialized", False)
        bot._start_ts      = data.get("start_ts", 0)
        bot._next_slot_ts  = data.get("next_slot_ts", 0)
        bot._log_entries   = list(data.get("log_entries", []))

        # Pending task queue'yi restore et — page lookup config'den
        for t in data.get("queue", []):
            page = next(
                (p for p in all_pages if p["page_id"] == t.get("page_id")),
                None,
            )
            if not page:
                continue
            heapq.heappush(bot._queue, _RepostTask(
                run_at   = t["run_at"],
                post_url = t["post_url"],
                message  = t.get("message", ""),
                page     = page,
                post_id  = t.get("post_id", ""),
            ))

        return bot

    # ── İç mantık ────────────────────────────────────────────────────────────

    def _log(self, level: str, text: str):
        self._log_entries.append({
            "time":  datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "text":  text,
        })
        logger.info(f"[Bot:{self.bot_id}] {text}")

    def _run(self):
        from config import is_quiet_hours

        self._check()
        self._next_check_ts = int(datetime.now().timestamp()) + self.check_interval
        _was_quiet = False

        while self.running:
            # Gece modu kontrolü
            if is_quiet_hours():
                if not _was_quiet:
                    self._log("warn", "Gece modu aktif — işlemler duraklatıldı")
                    _was_quiet = True
                time.sleep(10)
                continue
            if _was_quiet:
                self._log("info", "Gece modu bitti — işlemler devam ediyor")
                _was_quiet = False

            now = int(datetime.now().timestamp())

            # Zamanı gelen repost görevlerini yürüt
            while self._queue and self._queue[0].run_at <= now:
                task = heapq.heappop(self._queue)
                self._execute_task(task)

            # Kontrol zamanı geldiyse kontrol et
            if now >= self._next_check_ts:
                self._check()
                self._next_check_ts = now + self.check_interval

            time.sleep(1)

    def _check(self):
        self.last_check = datetime.now().strftime("%H:%M:%S")
        self._log("info", "Kontrol ediliyor...")

        # Config'den güncel page token'ı al (startup'ta yenilenmiş olabilir)
        import config as _cfg
        fresh_token = next(
            (p["access_token"] for p in _cfg.FB_PAGES if p["page_id"] == self.source_page_id),
            self.source_token,
        )
        # page token yoksa user token'a düş
        if not fresh_token:
            fresh_token = _cfg.FB_USER_ACCESS_TOKEN

        self._log("info", f"Feed token: ...{fresh_token[-10:] if fresh_token else 'YOK'}")

        # since_ts kullanmıyoruz — FB API'nin since filtresi güvenilmez.
        # Son 20 postu çek, _seen_ids ile yenileri biz takip edelim.
        posts = get_page_posts(self.source_page_id, fresh_token,
                               since_ts=None, log_fn=self._log)

        if not posts:
            self._log("info", f"Yeni gönderi yok (0 kontrol edildi)")
            return

        # ── İlk çalışma: mevcut postları "görüldü" olarak kaydet, repost etme ──
        if not self._initialized:
            for p in posts:
                self._seen_ids.add(p.get("id", ""))
            self._initialized = True
            self._log("info", f"Başlangıç snapshot: {len(posts)} mevcut gönderi kaydedildi, izlemeye alındı")
            return

        # ── Sonraki çalışmalar: yeni postları bul ────────────────────────────
        # Hem seen_ids'te olmamalı hem de bot başladıktan SONRA oluşturulmuş olmalı
        def _post_ts(p):
            ct = p.get("created_time", "")
            try:
                from datetime import timezone
                return int(datetime.fromisoformat(ct.replace("Z", "+00:00"))
                           .astimezone(timezone.utc).timestamp())
            except Exception:
                return 0

        new_posts = [
            p for p in posts
            if p.get("id")
            and p["id"] not in self._seen_ids
            and _post_ts(p) >= self._start_ts
        ]

        if not new_posts:
            self._log("info", f"Yeni gönderi yok ({len(posts)} kontrol edildi)")
            return

        self._log("info", f"{len(new_posts)} yeni gönderi bulundu")

        for post in new_posts:
            self._seen_ids.add(post["id"])
            post_url = post.get("permalink_url")
            if not post_url:
                continue

            for page in self.target_pages:
                slot_ts = max(int(datetime.now().timestamp()), self._next_slot_ts)
                self._next_slot_ts = slot_ts + self.repost_spacing
                heapq.heappush(self._queue, _RepostTask(
                    run_at   = slot_ts,
                    post_url = post_url,
                    message  = post.get("message", ""),
                    page     = page,
                    post_id  = post.get("id", ""),
                ))
                page_name = page.get("name", page["page_id"])
                run_time  = datetime.fromtimestamp(slot_ts).strftime("%H:%M:%S")
                self._log("info", f"⏰ [{page_name}] → {run_time}  {post_url}")

    def _execute_task(self, task: _RepostTask):
        import config as _cfg
        from facebook_poster import like_post_as_page
        page_name = task.page.get("name", task.page["page_id"])

        # 1) Önce kaynak postu beğen (target page'in token'ı ile)
        if task.post_id:
            try:
                like_ok = like_post_as_page(
                    task.page, task.post_id,
                    user_token=_cfg.FB_USER_ACCESS_TOKEN,
                )
                if like_ok is True:
                    self._log("success", f"♥ [{page_name}] kaynak post beğenildi")
                else:
                    detail = f": {like_ok}" if isinstance(like_ok, str) else ""
                    self._log("warn", f"⚠ [{page_name}] like başarısız{detail}")
            except Exception as e:
                self._log("warn", f"⚠ [{page_name}] like hatası: {e}")

        # 2) Sonra repost
        try:
            ok = self._repost_fn(task.page, task.post_url, task.message,
                                 user_token=_cfg.FB_USER_ACCESS_TOKEN)
            if ok is True:
                self.posts_reposted += 1
                self._log("success", f"✓ [{page_name}] {task.post_url}")
            else:
                self.errors += 1
                detail = f": {ok}" if isinstance(ok, str) else ""
                self._log("error", f"✗ [{page_name}] repost başarısız{detail}")
        except Exception as e:
            self.errors += 1
            self._log("error", f"✗ [{page_name}] hata: {e}")
