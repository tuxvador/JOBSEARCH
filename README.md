# JOBSEARCH

Self-hosted, multi-user job-search dashboard (French / English). Each user sets
their own search criteria, runs searches against official job APIs, and reviews
scored offers before applying. Nothing is ever sent to recruiters automatically.

Runs at [job.tuxvador.fr](https://job.tuxvador.fr).

## Features

- **Users**: admins create, disable, reset and delete accounts. Each user has their
  own offers, criteria, password, API key and interface language (FR/EN).
- **Criteria** per user, started from a template (pentest / cybersecurity, supply
  chain, supply-chain work-study (*alternance*), blank):
  - keywords, sources, départements / city, contract types, maximum age, salary floor,
    countries accepted for remote jobs;
  - score categories (weight, terms, scope: title / location / everything) and
    *required* groups (signal, location, contract);
  - exclusions (score −999), one penalty, minimum score, top N per source.
- **Sources** (official APIs only):
  La bonne alternance (work-study jobs incl. France Travail's, read per
  département and filtered by the scoring), Jooble, Careerjet (free keys, entered under
  Admin → Settings), RemoteOK and Himalayas (no key).
- **Email alert import**: LinkedIn, Indeed, APEC, Welcome to the Jungle, HelloWork,
  Cadremploi… have no search API. Point their job alerts at a mailbox; the worker
  reads it over IMAP (read-only) every 30 minutes and scores each offer.
- **“+ My jobs” bookmarklet**: add the job page you're looking at, pre-filled and
  scored, in one click.
- **Run page**: every candidate with its score breakdown and the reason it was or
  wasn't selected; dropped offers can still be added by hand.
- **API** (`X-API-Key` header, per user): `POST /api/propose`, `GET /api/jobs`,
  `POST /api/job/<id>/status`, `POST /api/job/<id>/score`, `GET /api/criteria`,
  `GET /api/health`.

## Term syntax

Plain words match whole words with accents removed (`securite` finds “Sécurité”),
`*` at the end matches any ending (`logisti*` → logistique, logisticien), and
`re:<regex>` is a raw regular expression.

## Layout

| File | Role |
|------|------|
| `app.py` | Flask web app (UI, admin, API) |
| `worker.py` | background worker: queued searches + mailbox polling |
| `search.py` | scoring, criteria form parsing, job-API sources |
| `presets.py` | starting criteria templates |
| `mailalerts.py` | IMAP reader and job-alert email parser |
| `i18n.py` | French / English strings |
| `db.py` | SQLite schema and migrations |
| `deploy/` | installer, systemd units, nginx site, DB backup script |

## Install (Debian / Ubuntu, as root)

```bash
git clone https://github.com/tuxvador/JOBSEARCH.git
bash JOBSEARCH/deploy/install.sh
```

The installer puts the app in `/opt/jobs-app`, creates a `jobs` system user, a
Python venv, `/opt/jobs-app/.env` (random `SECRET_KEY` and API key) and an
admin login written to `/root/jobs-app-login.txt`. It installs two systemd
services (`jobs-app` — gunicorn on 127.0.0.1:5000, `jobs-worker`) and an nginx
site on port 80. Put a TLS reverse proxy in front of it. Re-running the installer
updates the code and keeps `.env` and the database (backed up first to
`/opt/jobs-app/backups/`).

The mailbox passwords are encrypted with a key derived from `SECRET_KEY`;
changing `SECRET_KEY` means re-entering them.

## Local development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
export SECRET_KEY=dev COOKIE_SECURE=0 JOBS_DB=./instance/jobs.db
export JOBS_PASSWORD_HASH=$(venv/bin/python -c "from werkzeug.security import generate_password_hash as g; print(g('changeme'))")
venv/bin/python app.py      # http://127.0.0.1:5000, user "tuxvador" / "changeme"
venv/bin/python worker.py   # in another terminal
```
