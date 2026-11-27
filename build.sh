#!/usr/bin/env bash
# Build step for Render (or any PaaS): install, collect static files, migrate.
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
# Create the owner's admin login on first deploy if the env vars are set.
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  python manage.py createsuperuser --noinput --email "${DJANGO_SUPERUSER_EMAIL:-admin@example.com}" 2>/dev/null \
    || echo "Superuser ${DJANGO_SUPERUSER_USERNAME} already exists, skipping."
fi
