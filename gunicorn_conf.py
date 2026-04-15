"""Gunicorn config — gevent worker, 1 worker (in-memory state).
SSL terminasyonu nginx'te yapılıyor, gunicorn sadece localhost'ta plain HTTP."""
import os

bind = "127.0.0.1:5000"

# In-memory state (_repost_bots dict, _progress queue, vs.) tek worker'da yaşıyor.
# SSE concurrency için gevent + yüksek connection sayısı yeterli.
workers = 1
worker_class = "gevent"
worker_connections = 1000

# SSE ve uzun yüklemeler için
timeout = 300        # request başına max 5 dk
keepalive = 75
graceful_timeout = 30

# Logging — stdout/stderr → systemd journal
accesslog = "-"
errorlog  = "-"
loglevel  = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss'

# SSL artık nginx'te — gunicorn plain HTTP localhost'ta

# Önemli: preload yapmıyoruz — wsgi.py'deki _init_once() worker import'unda çalışsın
preload_app = False
