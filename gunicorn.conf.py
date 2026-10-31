"""Gunicorn config, auto-loaded from the working directory by any `gunicorn` invocation.

Binds to 0.0.0.0 (not gunicorn's default 127.0.0.1) so the reverse proxy can reach it.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
accesslog = "-"
errorlog = "-"
