"""Gunicorn config, auto-loaded from the working directory by any `gunicorn` invocation.

Ensures the server is reachable from the reverse proxy (0.0.0.0, not 127.0.0.1)
even when the platform's default start command passes no --bind.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
accesslog = "-"
errorlog = "-"


def on_starting(server):
    # Collect static files (admin/DRF assets served by WhiteNoise) before workers boot.
    import django
    from django.core.management import call_command

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    call_command("collectstatic", interactive=False, verbosity=0)
