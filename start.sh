#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

# Create the admin user once (skipped if ADMIN_PASSWORD is unset or the user exists).
if [ -n "$ADMIN_PASSWORD" ]; then
  DJANGO_SUPERUSER_USERNAME="${ADMIN_USERNAME:-admin}" \
  DJANGO_SUPERUSER_EMAIL="${ADMIN_EMAIL:-admin@example.com}" \
  DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
  python manage.py createsuperuser --noinput || echo "Admin user already exists, skipping."
fi

echo "Starting gunicorn on port ${PORT:-8000}..."
exec python -m gunicorn config.wsgi:application --config gunicorn.conf.py
