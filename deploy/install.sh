#!/bin/bash
# Install / update the job-search dashboard (Debian/Ubuntu). Run as root from the
# unpacked bundle directory. Safe to re-run: keeps .env and the database.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/jobs-app
TS=$(date +%Y%m%d-%H%M%S)

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv sqlite3 nginx >/dev/null

id jobs >/dev/null 2>&1 || useradd --system --home-dir $APP --shell /usr/sbin/nologin jobs
mkdir -p $APP/instance $APP/backups $APP/templates $APP/static $APP/bin
for f in app.py db.py search.py worker.py i18n.py presets.py mailalerts.py; do install -m 644 "$SRC/$f" $APP/$f; done
rm -rf $APP/templates/* $APP/static/*
cp -r "$SRC/templates/." $APP/templates/
cp -r "$SRC/static/." $APP/static/
install -m 755 "$SRC/deploy/backup-db.sh" $APP/bin/backup-db.sh

[ -x $APP/venv/bin/gunicorn ] || python3 -m venv $APP/venv
$APP/venv/bin/pip install -q --upgrade pip flask gunicorn beautifulsoup4 cryptography

if [ ! -f $APP/.env ]; then
    PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(14))')
    HASH=$($APP/venv/bin/python -c 'import sys; from werkzeug.security import generate_password_hash as g; print(g(sys.argv[1]))' "$PW")
    umask 077
    cat > $APP/.env <<ENV
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
JOBS_USER=tuxvador
JOBS_PASSWORD_HASH='$HASH'
JOBS_API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
ENV
    printf 'user: tuxvador\npassword: %s\n' "$PW" > /root/jobs-app-login.txt
    chmod 600 /root/jobs-app-login.txt
    echo "NEW login written to /root/jobs-app-login.txt"
fi
chown root:jobs $APP/.env; chmod 640 $APP/.env
chown -R jobs:jobs $APP/instance $APP/backups
[ -f $APP/instance/jobs.db ] && $APP/bin/backup-db.sh "pre-deploy-$TS" || true

install -m 644 "$SRC/deploy/jobs-app.service" /etc/systemd/system/jobs-app.service
install -m 644 "$SRC/deploy/jobs-worker.service" /etc/systemd/system/jobs-worker.service
systemctl daemon-reload
systemctl enable -q jobs-app jobs-worker
systemctl restart jobs-app
sleep 2   # let the app run the schema migration before the worker starts
systemctl restart jobs-worker

# nginx: our site becomes the default; older enabled sites are kept in a backup dir
mkdir -p /root/nginx-sites-backup-$TS
for f in /etc/nginx/sites-enabled/*; do
    [ -e "$f" ] || continue
    [ "$(basename "$f")" = jobs-app ] && continue
    cp -a "$f" /root/nginx-sites-backup-$TS/ && rm -f "$f"
done
install -m 644 "$SRC/deploy/nginx-jobs-app.conf" /etc/nginx/sites-available/jobs-app
ln -sf ../sites-available/jobs-app /etc/nginx/sites-enabled/jobs-app
nginx -t
systemctl reload nginx

for i in 1 2 3 4 5 6 7 8 9 10; do
    curl -fs http://127.0.0.1:5000/api/health && break; sleep 1
done
echo; echo "via nginx: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/login)"
