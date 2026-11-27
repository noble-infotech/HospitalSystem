#!/usr/bin/env bash
# Nightly PostgreSQL backup. Keeps 30 days locally; copy the folder off-server too
# (e.g. to an S3 bucket in the Mumbai region) so a dead server doesn't take the backups with it.
#
# Add to crontab (runs at 11:30 PM every night):
#   30 23 * * * /srv/clinicms/scripts/backup.sh >> /var/log/clinic-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

DEST="backups"
mkdir -p "$DEST"
FILE="$DEST/clinic-$(date +%Y%m%d-%H%M).sql.gz"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$FILE"
chmod 600 "$FILE"
find "$DEST" -name 'clinic-*.sql.gz' -mtime +30 -delete
echo "$(date) backup written: $FILE"
