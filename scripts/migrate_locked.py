"""Pre-start database gate: `check --database default`, then `migrate --noinput` under a PostgreSQL advisory lock.

Exits non-zero on any failure so the entrypoint never starts the web server against an unmigrated/unreachable database.
The lock serializes replicas that boot together; whoever comes second finds nothing left to apply. Never runs
makemigrations, flush or anything destructive. Uses whatever DATABASE_URL the process environment provides.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

LOCK_KEY = 726_001_001  # arbitrary app-wide constant for pg_advisory_lock


def main():
    import django
    from django.core.management import call_command
    from django.db import connection

    django.setup()
    db = connection.settings_dict
    print(f"[migrate] database: {db['ENGINE'].rsplit('.', 1)[-1]} host={db.get('HOST') or '-'} name={db.get('NAME')}", flush=True)

    print("[migrate] running database check...", flush=True)
    call_command("check", database=["default"], fail_level="ERROR")

    is_pg = connection.vendor == "postgresql"
    if is_pg:
        print("[migrate] acquiring migration lock (waits if another replica is migrating)...", flush=True)
        with connection.cursor() as c:
            c.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
    try:
        print("[migrate] applying migrations...", flush=True)
        call_command("migrate", interactive=False, verbosity=1)
    finally:
        if is_pg:
            with connection.cursor() as c:
                c.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
    print("[migrate] done.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - any failure must block startup
        print(f"[migrate] FAILED: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
