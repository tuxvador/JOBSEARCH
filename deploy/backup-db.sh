#!/bin/bash
# Snapshot the jobs DB: backup-db.sh [label]  -> /opt/jobs-app/backups/jobs-YYYYmmdd-HHMMSS-label.db
set -e
DB=/opt/jobs-app/instance/jobs.db
DIR=/opt/jobs-app/backups
TS=$(date '+%Y%m%d-%H%M%S')
OUT="$DIR/jobs-${TS}${1:+-$1}.db"
sqlite3 "$DB" ".backup '$OUT'"
echo "Backup saved: $OUT ($(du -h "$OUT" | cut -f1))"
