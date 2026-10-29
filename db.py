"""SQLite access shared by the web app and the search worker."""
import os
import secrets
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JOBS_DB", os.path.join(BASE, "instance", "jobs.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0,
    api_key       TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    company    TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed','accepted','rejected')),
    score      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS criteria (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','error')),
    criteria    TEXT NOT NULL,
    log         TEXT NOT NULL DEFAULT '',
    found       INTEGER NOT NULL DEFAULT 0,
    selected    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS search_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    title     TEXT NOT NULL,
    company   TEXT NOT NULL DEFAULT '',
    location  TEXT NOT NULL DEFAULT '',
    url       TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT '',
    salary    TEXT NOT NULL DEFAULT '',
    contract  TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    score     INTEGER NOT NULL DEFAULT 0,
    reasons   TEXT NOT NULL DEFAULT '',
    verdict   TEXT NOT NULL DEFAULT '',
    selected  INTEGER NOT NULL DEFAULT 0,
    job_id    INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL DEFAULT 993,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,          -- Fernet-encrypted with a key derived from SECRET_KEY
    folder      TEXT NOT NULL DEFAULT 'INBOX',
    enabled     INTEGER NOT NULL DEFAULT 1,
    force       INTEGER NOT NULL DEFAULT 0,
    last_uid    INTEGER NOT NULL DEFAULT 0,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_check  TEXT,
    last_error  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_results_search ON search_results(search_id, selected DESC, score DESC);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(admin_user=None, admin_hash=None):
    """Create tables, migrate the single-user schema, seed the first admin."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if "user_id" not in _columns(conn, "jobs"):
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER REFERENCES users(id)")
            except sqlite3.OperationalError as e:  # another process got there first
                if "duplicate column" not in str(e):
                    raise
        for table, col, ddl in (("users", "lang", "TEXT NOT NULL DEFAULT 'fr'"),
                                ("searches", "kind", "TEXT NOT NULL DEFAULT 'search'"),
                                ("searches", "requester_ip", "TEXT NOT NULL DEFAULT ''"),
                                ("searches", "requester_ua", "TEXT NOT NULL DEFAULT ''"),
                                ("searches", "lang", "TEXT NOT NULL DEFAULT 'fr'")):
            if col not in _columns(conn, table):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, status, score DESC, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(user_id, url)")
        if admin_user and admin_hash and not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin, api_key) VALUES (?,?,1,?)",
                    (admin_user, admin_hash, new_api_key()))
            except sqlite3.IntegrityError:
                pass
        first_admin = conn.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        if first_admin:  # jobs from the single-user version belong to the first admin
            conn.execute("UPDATE jobs SET user_id=? WHERE user_id IS NULL", (first_admin["id"],))
        conn.commit()
    finally:
        conn.close()


def new_api_key():
    return secrets.token_hex(32)


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
