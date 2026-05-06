"""
Gunicorn entry point.
Önce gevent monkey-patch, sonra app import + init (token refresh, state restore).
Çalıştırma:
  gunicorn -c gunicorn_conf.py wsgi:app
"""
# 1) Monkey patch ÖNCE — başka import'tan önce yapılmalı
from gevent import monkey
monkey.patch_all()

# 2) Şimdi app + init
import os
import threading

import config
from onapp import (
    app,
    refresh_all_page_tokens,
    update_env,
    _load_repost_bots_state,
    _load_tw_bot_state,
    _periodic_save_loop,
)


def _init_once():
    """Sadece master process'te (veya tek worker'da) bir kez çalışır."""
    if config.FB_USER_ACCESS_TOKEN and config.FB_PAGES:
        print("Sayfa token'ları yenileniyor...", flush=True)
        config.FB_PAGES = refresh_all_page_tokens(config.FB_PAGES, config.FB_USER_ACCESS_TOKEN)
        pages_str = ",".join(
            f"{p['page_id']}:{p['access_token']}" for p in config.FB_PAGES
        )
        update_env("FB_PAGES", pages_str)
        ok  = [p for p in config.FB_PAGES if not p.get("static_token")]
        bad = [p for p in config.FB_PAGES if p.get("static_token")]
        if ok:
            print(f"✓ {len(ok)} sayfa token'ı yenilendi.", flush=True)
        if bad:
            print(f"⚠ {len(bad)} sayfa yenilenemedi: "
                  f"{', '.join(p.get('name', p['page_id']) for p in bad)}", flush=True)

    _load_repost_bots_state()
    _load_tw_bot_state()
    threading.Thread(target=_periodic_save_loop, daemon=True).start()


_init_once()
