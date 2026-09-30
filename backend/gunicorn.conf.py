# Gunicorn + Uvicorn production configuration
#
# Usage:
#   gunicorn -c gunicorn.conf.py app.main:app
#
# Gunicorn manages worker PROCESSES.
# Each worker runs a Uvicorn ASGI server (async event loop).
# This gives us:
#   - Multi-core utilisation (Gunicorn processes)
#   - Async I/O within each worker (Uvicorn)
#   - Production-grade process management, graceful reloads, health checks

import multiprocessing
import os

# ── Server socket ────────────────────────────────────────────────────────────
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 2048

# ── Worker processes ─────────────────────────────────────────────────────────
# Formula: (2 × CPU cores) + 1  — standard recommendation for I/O-bound apps
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 120         # request timeout in seconds
keepalive = 5         # keep-alive connections
graceful_timeout = 30 # seconds to finish in-flight requests on SIGTERM

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog = "-"   # stdout
errorlog = "-"    # stdout
loglevel = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ── Process naming ────────────────────────────────────────────────────────────
proc_name = "allinhelp-api"
