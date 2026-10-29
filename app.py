"""job.tuxvador.fr — multi-user job-search dashboard (Flask + SQLite), FR / EN.

Each user has their own offers, search criteria, password, API key, language
and (optionally) a mailbox for job-alert e-mails. Admins create and manage
users and enter the job-API credentials.

API (header X-API-Key: the user's key from "Compte"; JOBS_API_KEY in .env
still works and acts as the first admin) — same shape as the old pipeline:
  POST /api/propose              add an offer (status 'proposed'); scored with
                                 the user's criteria when no score is given
  GET  /api/jobs?status=...      list the user's offers
  POST /api/job/<id>/status      proposed | accepted | rejected
  POST /api/job/<id>/score
  GET  /api/criteria             the user's search criteria (for external scrapers)
  GET  /api/health               no auth
"""
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db
import mailalerts
import search
from i18n import LANGS, tr, tr_long

STATUSES = ("proposed", "accepted", "rejected")
TABS = [("accepted", "À postuler"), ("proposed", "Nouvelles"),
        ("rejected", "Archivées"), ("all", "Toutes")]
EXCLUDED_SCORE = -999
FIELDS = ("title", "company", "location", "url", "source", "notes")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")
MIN_PASSWORD = 10
PARIS = ZoneInfo("Europe/Paris")
SETTING_KEYS = ("ft_client_id", "ft_client_secret", "adzuna_app_id", "adzuna_app_key",
                "jooble_key", "careerjet_key", "careerjet_locale", "lba_key")
SECRET_SETTINGS = ("ft_client_secret", "adzuna_app_key", "jooble_key", "careerjet_key", "lba_key")

app = Flask(__name__)
# Chain: client -> front reverse proxy (TLS) -> local nginx -> gunicorn. The local
# nginx appends to X-Forwarded-For and passes the front proxy's Proto/Host through.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

db.init_db(os.environ.get("JOBS_USER", "tuxvador"), os.environ.get("JOBS_PASSWORD_HASH"))


# ---------------------------------------------------------------- helpers
def conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def _(s, /, **kw):
    return tr(getattr(g, "lang", "fr"), s, **kw)


def long_text(key):
    return Markup(tr_long(getattr(g, "lang", "fr"), key))


@app.template_filter("localtime")
def localtime(value, fmt="%d/%m/%Y %H:%M"):
    """UTC 'YYYY-mm-dd HH:MM:SS' from SQLite -> Paris time."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(PARIS).strftime(fmt)
    except ValueError:
        return value


def gen_password():
    return secrets.token_urlsafe(12)


def counts(uid):
    rows = conn().execute("SELECT status, COUNT(*) n FROM jobs WHERE user_id=? GROUP BY status",
                          (uid,)).fetchall()
    c = {s: 0 for s in STATUSES}
    c.update({r["status"]: r["n"] for r in rows})
    c["all"] = sum(c[s] for s in STATUSES)
    return c


def query_jobs(uid, status, q=""):
    sql, args = "SELECT * FROM jobs WHERE user_id = ?", [uid]
    if status in STATUSES:
        sql += " AND status = ?"
        args.append(status)
    if q:
        like = f"%{q}%"
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR notes LIKE ?)"
        args += [like] * 4
    sql += " ORDER BY score DESC, created_at DESC, id DESC"
    return conn().execute(sql, args).fetchall()


def set_status(uid, job_id, status):
    cur = conn().execute("UPDATE jobs SET status=?, updated_at=datetime('now') WHERE id=? AND user_id=?",
                         (status, job_id, uid))
    conn().commit()
    return cur.rowcount


def user_criteria(uid):
    row = conn().execute("SELECT data FROM criteria WHERE user_id=?", (uid,)).fetchone()
    return search.load_criteria(row["data"] if row else None)


def save_criteria(uid, crit):
    conn().execute("INSERT INTO criteria (user_id, data, updated_at) VALUES (?,?,datetime('now')) "
                   "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                   (uid, json.dumps(crit, ensure_ascii=False)))
    conn().commit()


def insert_job(uid, job, status="proposed", score=0):
    cur = conn().execute("INSERT INTO jobs (user_id, title, company, location, url, source, notes, status, score) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         [uid] + [job.get(f, "") for f in FIELDS] + [status, score])
    conn().commit()
    return cur.lastrowid


def find_duplicate(uid, url):
    if not url:
        return None
    return conn().execute("SELECT id, status FROM jobs WHERE user_id=? AND url=?", (uid, url)).fetchone()


# ------------------------------------------------------------------- auth
@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        u = conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u and not u["disabled"] and session.get("pwv") == u["password_hash"][-12:]:
            g.user = u
        else:  # deleted, disabled or password changed elsewhere
            session.pop("uid", None)
            session.pop("pwv", None)
    if g.user:
        g.lang = g.user["lang"] if g.user["lang"] in LANGS else "fr"
    else:
        g.lang = session.get("lang") or request.accept_languages.best_match(list(LANGS)) or "fr"


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.full_path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if not g.user["is_admin"]:
            abort(403)
        return view(*a, **kw)
    return wrapped


def api_user():
    given = request.headers.get("X-API-Key", "")
    if not given:
        return None
    env_key = os.environ.get("JOBS_API_KEY", "")
    if env_key and hmac.compare_digest(given, env_key):
        return conn().execute("SELECT * FROM users WHERE is_admin=1 AND disabled=0 ORDER BY id LIMIT 1").fetchone()
    u = conn().execute("SELECT * FROM users WHERE api_key=?", (given,)).fetchone()
    return u if u and not u["disabled"] else None


def api_key_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        g.api_user = api_user()
        if not g.api_user:
            return jsonify({"error": "Unauthorized"}), 401
        return view(*a, **kw)
    return wrapped


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    token = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(token, expected):
        abort(400, _("CSRF token invalide — rechargez la page."))


app.jinja_env.globals.update(csrf_token=csrf_token, _=_, long_text=long_text, LANGS=LANGS,
                             SOURCES=search.SOURCES, CONTRACTS=search.CONTRACTS,
                             SCOPES=search.SCOPES, GATES=search.GATES, PRESETS=search.PRESETS)


def start_session(u):
    lang = session.get("lang")
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["pwv"] = u["password_hash"][-12:]  # changing the password logs out other sessions
    if lang:
        session["lang"] = lang


@app.route("/favicon.ico")
def favicon():
    # browsers that ignore <link rel="icon"> still ask for /favicon.ico
    return redirect(url_for("static", filename="favicon.svg"), 301)


@app.route("/lang/<code>")
def set_lang(code):
    if code in LANGS:
        session["lang"] = code
        if g.user:
            conn().execute("UPDATE users SET lang=? WHERE id=?", (code, g.user["id"]))
            conn().commit()
    nxt = request.args.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        u = conn().execute("SELECT * FROM users WHERE username=?",
                           (request.form.get("username", "").strip(),)).fetchone()
        if u and not u["disabled"] and check_password_hash(u["password_hash"], request.form.get("password", "")):
            start_session(u)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        app.logger.warning("login failed for %r from %s", request.form.get("username", "")[:40], request.remote_addr)
        time.sleep(1.5)
        flash(_("Identifiants incorrects"), "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    lang = g.lang
    session.clear()
    session["lang"] = lang
    return redirect(url_for("login"))


# ----------------------------------------------------------------- offers
@app.route("/")
@login_required
def dashboard():
    status = request.args.get("status", "accepted")
    if status not in STATUSES and status != "all":
        status = "accepted"
    q = request.args.get("q", "").strip()[:100]
    jobs = query_jobs(g.user["id"], status, q)
    partial = request.args.get("partial") == "1" or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template("_joblist.html" if partial else "dashboard.html", jobs=jobs,
                           counts=counts(g.user["id"]), tabs=TABS, current_status=status, q=q, nav="jobs")


@app.route("/job/<int:job_id>/<action>", methods=["POST"])
@login_required
def job_action(job_id, action):
    check_csrf()
    mapping = {"accept": "accepted", "reject": "rejected", "pending": "proposed"}
    if action not in mapping:
        abort(404)
    if not set_status(g.user["id"], job_id, mapping[action]):
        abort(404)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"id": job_id, "status": mapping[action], "counts": counts(g.user["id"])})
    return redirect(url_for("dashboard", status=request.args.get("status", "accepted")))


@app.route("/job/new", methods=["POST"])
@login_required
def job_new():
    check_csrf()
    data = {f: request.form.get(f, "").strip() for f in FIELDS}
    if not data["title"]:
        flash(_("Le titre est obligatoire"), "err")
    else:
        insert_job(g.user["id"], data, "accepted")
        flash(_("Offre ajoutée"))
    return redirect(url_for("dashboard", status="accepted"))


# ------------------------------------------------- "add to my jobs" button
_SITE_SUFFIX = re.compile(r"\s*[|\-–—]\s*(LinkedIn|Indeed(\.com)?|Welcome to the Jungle|HelloWork|APEC|Apec\.fr|"
                          r"Cadremploi|France Travail|Jobteaser|Glassdoor|Monster)\s*$", re.I)


def guess_from_page(title, h1, url):
    """Best guess of (title, company) from the page the bookmarklet was used on."""
    raw = re.sub(r"^\(\d+\)\s*", "", (title or "").strip())  # LinkedIn "(3) …"
    raw = _SITE_SUFFIX.sub("", raw)
    parts = [p.strip() for p in re.split(r"\s+[|–—]\s+|\s+-\s+", raw) if p.strip()]
    job_title = (h1 or "").strip() or (parts[0] if parts else "")
    company = ""
    for p in parts:
        if p != job_title and not re.search(r"emploi|job|offre|recrute|hiring", p, re.I):
            company = p
            break
    host = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", url or "") or [None, ""])[1])
    source = next((name for key, name in (("linkedin", "LinkedIn"), ("indeed", "Indeed"),
                                          ("welcometothejungle", "Welcome to the Jungle"),
                                          ("hellowork", "HelloWork"), ("apec", "APEC"),
                                          ("cadremploi", "Cadremploi"), ("francetravail", "France Travail"),
                                          ("jobteaser", "Jobteaser"), ("glassdoor", "Glassdoor"))
                   if key in host), host)
    return job_title[:200], company[:120], source


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_page():
    uid = g.user["id"]
    if request.method == "POST":
        check_csrf()
        data = {f: request.form.get(f, "").strip() for f in FIELDS}
        if not data["title"]:
            flash(_("Le titre est obligatoire"), "err")
            return render_template("add.html", job=data, score=None, reasons="", saved=False, dup=False, popup=True)
        dup = find_duplicate(uid, data["url"])
        if not dup:
            score, reasons, _gates, _ex = search.Scorer(user_criteria(uid), g.lang).score(
                data["title"], data["company"], data["location"], "", data["notes"])
            if reasons:
                data["notes"] = " · ".join(x for x in (data["notes"], " · ".join(reasons)) if x)[:1000]
            insert_job(uid, data, "accepted" if request.form.get("to") == "accepted" else "proposed", score)
        return render_template("add.html", job=data, score=None, reasons="", saved=True, dup=bool(dup), popup=True)
    url = request.args.get("url", "")[:1000]
    title, company, source = guess_from_page(request.args.get("title", ""), request.args.get("h1", ""), url)
    job = {"title": title, "company": company, "location": "", "url": url, "source": source,
           "notes": request.args.get("sel", "")[:1000]}
    score, reasons, _gates, _ex = search.Scorer(user_criteria(uid), g.lang).score(
        title, company, "", "", job["notes"])
    return render_template("add.html", job=job, score=score, reasons=" · ".join(reasons), saved=False,
                           dup=bool(find_duplicate(uid, url)), popup=True)


# ----------------------------------------------------------------- search
def active_run(uid):
    return conn().execute("SELECT id FROM searches WHERE user_id=? AND kind='search' "
                          "AND status IN ('queued','running')", (uid,)).fetchone()


def recent_runs(uid):
    return conn().execute("SELECT id, status, kind, found, selected, created_at FROM searches WHERE user_id=? "
                          "ORDER BY id DESC LIMIT 12", (uid,)).fetchall()


def api_settings_state():
    c = conn()
    has = lambda *keys: all(db.get_setting(c, k) for k in keys)
    return {"francetravail": has("ft_client_id", "ft_client_secret"),
            "labonnealternance": has("lba_key"),
            "adzuna": has("adzuna_app_id", "adzuna_app_key"),
            "jooble": has("jooble_key"), "careerjet": has("careerjet_key"),
            "remoteok": True, "himalayas": True}


def render_search(crit, status=200):
    return render_template("search.html", crit=crit, runs=recent_runs(g.user["id"]), nav="search",
                           settings=api_settings_state()), status


@app.route("/search", methods=["GET", "POST"])
@login_required
def search_page():
    uid = g.user["id"]
    if request.method == "POST":
        check_csrf()
        if request.form.get("action") == "preset":
            preset = request.form.get("preset", "")
            if preset not in search.PRESETS:
                abort(400)
            save_criteria(uid, search.default_criteria(preset))
            flash(_("Modèle « {name} » chargé — ajustez-le puis enregistrez", name=_(search.PRESETS[preset][0])))
            return redirect(url_for("search_page"))
        crit, errors = search.criteria_from_form(request.form, g.lang)
        if errors:
            for e in errors:
                flash(e, "err")
            return render_search(crit, 400)
        save_criteria(uid, crit)
        if request.form.get("action") == "run":
            if active_run(uid):
                flash(_("Une recherche est déjà en cours."), "err")
                return redirect(url_for("search_page"))
            cur = conn().execute(
                "INSERT INTO searches (user_id, criteria, kind, lang, requester_ip, requester_ua) "
                "VALUES (?,?,'search',?,?,?)",
                (uid, json.dumps(crit, ensure_ascii=False), g.lang, request.remote_addr or "",
                 request.headers.get("User-Agent", "")[:300]))
            conn().commit()
            return redirect(url_for("search_run", run_id=cur.lastrowid))
        flash(_("Critères enregistrés"))
        return redirect(url_for("search_page"))
    return render_search(user_criteria(uid))


def get_run(run_id):
    run = conn().execute("SELECT * FROM searches WHERE id=? AND user_id=?", (run_id, g.user["id"])).fetchone()
    if not run:
        abort(404)
    return run


@app.route("/search/run/<int:run_id>")
@login_required
def search_run(run_id):
    run = get_run(run_id)
    results = conn().execute("SELECT * FROM search_results WHERE search_id=? ORDER BY selected DESC, score DESC",
                             (run_id,)).fetchall()
    return render_template("run.html", run=run, results=results, nav="search")


@app.route("/search/run/<int:run_id>/status")
@login_required
def search_run_status(run_id):
    run = get_run(run_id)
    return jsonify({"status": run["status"], "log": run["log"], "found": run["found"],
                    "selected": run["selected"]})


@app.route("/search/result/<int:res_id>/add", methods=["POST"])
@login_required
def search_result_add(res_id):
    check_csrf()
    r = conn().execute("SELECT r.*, s.user_id FROM search_results r JOIN searches s ON s.id=r.search_id "
                       "WHERE r.id=? AND s.user_id=?", (res_id, g.user["id"])).fetchone()
    if not r:
        abort(404)
    if not r["job_id"]:
        notes = " · ".join(x for x in (r["contract"], r["salary"], r["reasons"]) if x)
        job_id = insert_job(g.user["id"], {"title": r["title"], "company": r["company"], "location": r["location"],
                                           "url": r["url"], "source": r["source"], "notes": notes},
                            "proposed", r["score"])
        conn().execute("UPDATE search_results SET job_id=? WHERE id=?", (job_id, res_id))
        conn().commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("search_run", run_id=r["search_id"]))


# ---------------------------------------------------------------- account
@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    uid = g.user["id"]
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "password":
            new = request.form.get("new", "")
            if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
                flash(_("Mot de passe actuel incorrect"), "err")
            elif len(new) < MIN_PASSWORD:
                flash(_("Le nouveau mot de passe doit faire au moins {n} caractères", n=MIN_PASSWORD), "err")
            elif new != request.form.get("confirm", ""):
                flash(_("Les deux mots de passe ne correspondent pas"), "err")
            else:
                conn().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), uid))
                conn().commit()
                start_session(conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
                flash(_("Mot de passe modifié"))
        elif action == "apikey":
            conn().execute("UPDATE users SET api_key=? WHERE id=?", (db.new_api_key(), uid))
            conn().commit()
            flash(_("Nouvelle clé API générée — l'ancienne ne fonctionne plus"))
        elif action in ("mailbox", "mailbox_test"):
            old = conn().execute("SELECT * FROM mailboxes WHERE user_id=?", (uid,)).fetchone()
            host = request.form.get("host", "").strip()[:120]
            username = request.form.get("username", "").strip()[:200]
            pw = request.form.get("password", "")
            folder = request.form.get("folder", "").strip()[:100] or "INBOX"
            try:
                port = int(request.form.get("port") or 993)
            except ValueError:
                port = 993
            enc = mailalerts.encrypt(pw) if pw else (old["password"] if old else "")
            if not (host and username and enc):
                flash(_("Serveur, identifiant et mot de passe sont obligatoires"), "err")
            else:
                enabled = 1 if request.form.get("enabled") else 0
                reset = (not old) or old["host"] != host or old["username"] != username or old["folder"] != folder
                conn().execute(
                    "INSERT INTO mailboxes (user_id, host, port, username, password, folder, enabled, force) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET host=excluded.host, "
                    "port=excluded.port, username=excluded.username, password=excluded.password, "
                    "folder=excluded.folder, enabled=excluded.enabled, force=excluded.force, last_error=''"
                    + (", last_uid=0, uidvalidity=0" if reset else ""),
                    (uid, host, port, username, enc, folder, enabled, 1 if action == "mailbox_test" else 0))
                conn().commit()
                flash(_("Import lancé — résultat dans « Recherche »") if action == "mailbox_test"
                      else _("Boîte mail enregistrée"))
        elif action == "mailbox_delete":
            conn().execute("DELETE FROM mailboxes WHERE user_id=?", (uid,))
            conn().commit()
            flash(_("Configuration e-mail supprimée"))
        return redirect(url_for("account"))
    mailbox = conn().execute("SELECT * FROM mailboxes WHERE user_id=?", (uid,)).fetchone()
    return render_template("account.html", nav="account", mailbox=mailbox,
                           base_url=request.host_url.rstrip("/"))


# ------------------------------------------------------------------ admin
def admin_count():
    return conn().execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND disabled=0").fetchone()[0]


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "create":
            username = request.form.get("username", "").strip()
            pw = request.form.get("password", "") or gen_password()
            lang = request.form.get("lang") if request.form.get("lang") in LANGS else "fr"
            if not USERNAME_RE.match(username):
                flash(_("Nom d'utilisateur : 2 à 32 caractères (lettres, chiffres, . _ -)"), "err")
            elif len(pw) < MIN_PASSWORD:
                flash(_("Mot de passe : {n} caractères minimum (ou laissez vide pour en générer un)",
                        n=MIN_PASSWORD), "err")
            else:
                try:
                    cur = conn().execute(
                        "INSERT INTO users (username, password_hash, is_admin, api_key, lang) VALUES (?,?,?,?,?)",
                        (username, generate_password_hash(pw), 1 if request.form.get("is_admin") else 0,
                         db.new_api_key(), lang))
                    conn().execute("INSERT INTO criteria (user_id, data) VALUES (?,?)",
                                   (cur.lastrowid, json.dumps(search.default_criteria(request.form.get("preset", "")),
                                                              ensure_ascii=False)))
                    conn().commit()
                    flash(_("Utilisateur « {name} » créé — mot de passe : {pw}", name=username, pw=pw), "secret")
                except sqlite3.IntegrityError:
                    flash(_("« {name} » existe déjà", name=username), "err")
            return redirect(url_for("admin_users"))

        uid = int(request.form.get("user_id", 0) or 0)
        u = conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            abort(404)
        is_self = uid == g.user["id"]
        last_admin = u["is_admin"] and not u["disabled"] and admin_count() <= 1
        if action == "reset":
            pw = gen_password()
            conn().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
            flash(_("Nouveau mot de passe pour « {name} » : {pw}", name=u["username"], pw=pw), "secret")
        elif action == "toggle_admin":
            if is_self or last_admin:
                flash(_("Impossible : il doit rester au moins un administrateur (et pas vous-même)"), "err")
            else:
                conn().execute("UPDATE users SET is_admin=1-is_admin WHERE id=?", (uid,))
        elif action == "toggle_disabled":
            if is_self or last_admin:
                flash(_("Impossible de désactiver ce compte"), "err")
            else:
                conn().execute("UPDATE users SET disabled=1-disabled WHERE id=?", (uid,))
        elif action == "delete":
            if is_self or last_admin:
                flash(_("Impossible de supprimer ce compte"), "err")
            else:
                conn().execute("DELETE FROM jobs WHERE user_id=?", (uid,))
                conn().execute("DELETE FROM users WHERE id=?", (uid,))
                flash(_("« {name} » et ses offres ont été supprimés", name=u["username"]))
        conn().commit()
        return redirect(url_for("admin_users"))
    users = conn().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id) AS n_jobs, "
        "(SELECT MAX(created_at) FROM searches s WHERE s.user_id=u.id) AS last_search "
        "FROM users u ORDER BY u.username").fetchall()
    return render_template("admin_users.html", users=users, nav="admin", min_password=MIN_PASSWORD)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        check_csrf()
        for k in SETTING_KEYS:
            v = request.form.get(k, "").strip()
            if k in SECRET_SETTINGS and not v and not request.form.get(f"clear_{k}"):
                continue  # blank secret field = keep the current value
            db.set_setting(conn(), k, v)
        conn().commit()
        flash(_("Paramètres enregistrés"))
        return redirect(url_for("admin_settings"))
    vals = {k: db.get_setting(conn(), k) for k in SETTING_KEYS}
    return render_template("admin_settings.html", vals=vals, nav="admin", secret_keys=SECRET_SETTINGS)


# -------------------------------------------------------------------- API
def row_to_dict(r):
    return {k: r[k] for k in r.keys() if k != "user_id"}


@app.route("/api/health")
def api_health():
    try:
        n = conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return jsonify({"status": "ok", "jobs": n})
    except sqlite3.Error as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/propose", methods=["POST"])
@api_key_required
def api_propose():
    uid = g.api_user["id"]
    data = request.get_json(silent=True) or {}
    job = {f: str(data.get(f) or "").strip() for f in FIELDS}
    if not job["title"]:
        return jsonify({"error": "title is required"}), 400
    old = find_duplicate(uid, job["url"])
    if old:
        return jsonify({"id": old["id"], "status": old["status"], "duplicate": True}), 200
    if "score" in data:
        try:
            score = int(data.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
    else:  # score it with the user's own criteria
        score, reasons, _gates, _ex = search.Scorer(user_criteria(uid), g.api_user["lang"] or "fr").score(
            job["title"], job["company"], job["location"], str(data.get("contract") or ""), job["notes"])
        if reasons and not job["notes"]:
            job["notes"] = " · ".join(reasons)
    status = "rejected" if score <= EXCLUDED_SCORE else "proposed"
    job_id = insert_job(uid, job, status, score)
    return jsonify({"id": job_id, "status": status, "score": score}), 201


@app.route("/api/jobs")
@api_key_required
def api_jobs():
    status = request.args.get("status", "all")
    if status not in STATUSES and status != "all":
        return jsonify({"error": f"status must be one of {STATUSES + ('all',)}"}), 400
    return jsonify([row_to_dict(r) for r in query_jobs(g.api_user["id"], status)])


@app.route("/api/job/<int:job_id>/status", methods=["POST"])
@api_key_required
def api_job_status(job_id):
    uid = g.api_user["id"]
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in STATUSES:
        return jsonify({"error": f"status must be one of {STATUSES}"}), 400
    if "score" in data:
        try:
            conn().execute("UPDATE jobs SET score=? WHERE id=? AND user_id=?", (int(data["score"]), job_id, uid))
        except (TypeError, ValueError):
            return jsonify({"error": "score must be an integer"}), 400
    if not set_status(uid, job_id, status):
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": job_id, "status": status})


@app.route("/api/job/<int:job_id>/score", methods=["POST"])
@api_key_required
def api_job_score(job_id):
    uid = g.api_user["id"]
    data = request.get_json(silent=True) or {}
    try:
        score = int(data["score"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "score (integer) is required"}), 400
    cur = conn().execute("UPDATE jobs SET score=?, updated_at=datetime('now') WHERE id=? AND user_id=?",
                         (score, job_id, uid))
    if score <= EXCLUDED_SCORE:
        conn().execute("UPDATE jobs SET status='rejected' WHERE id=? AND user_id=?", (job_id, uid))
    conn().commit()
    if not cur.rowcount:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": job_id, "score": score})


@app.route("/api/criteria")
@api_key_required
def api_criteria():
    return jsonify(user_criteria(g.api_user["id"]))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    if request.endpoint not in ("static",):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
