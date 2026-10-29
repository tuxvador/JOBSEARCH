"""Background worker (jobs-worker.service).

- Runs queued searches one at a time.
- Every 30 minutes (or on demand), reads each enabled mailbox for job-alert
  e-mails and scores the jobs found.

Selected jobs go to the user's "Nouvelles" tab (status 'proposed'); every
candidate is kept in search_results so the run page shows why each one was or
wasn't picked.
"""
import json
import logging
import signal
import sys
import time

import db
import mailalerts
import search
from i18n import tr

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("jobs-worker")
KEEP_RUNS_PER_USER = 30
MAIL_INTERVAL_MIN = 30
stopping = False


def _stop(*_):
    global stopping
    stopping = True


def settings_dict(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def existing_for(conn, uid):
    rows = conn.execute("SELECT url, title, company FROM jobs WHERE user_id=?", (uid,)).fetchall()
    return ({r["url"] for r in rows if r["url"]},
            {(search.normalize(r["title"]), search.normalize(r["company"])) for r in rows})


def store_results(conn, run_id, uid, results, lang):
    selected = 0
    for j in results[:1500]:
        job_id = None
        if j["selected"]:
            notes = " · ".join(x for x in (j.get("contract"), j.get("salary"),
                                           tr(lang, "publiée {date}", date=j["published"]) if j.get("published") else "",
                                           j.get("reasons")) if x)
            cur = conn.execute(
                "INSERT INTO jobs (user_id, title, company, location, url, source, notes, status, score) "
                "VALUES (?,?,?,?,?,?,?, 'proposed', ?)",
                (uid, j["title"], j["company"], j["location"], j["url"], j["source"], notes[:1000], j["score"]))
            job_id = cur.lastrowid
            selected += 1
        conn.execute(
            "INSERT INTO search_results (search_id, title, company, location, url, source, salary, "
            "contract, published, score, reasons, verdict, selected, job_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, j["title"], j["company"], j["location"], j["url"], j["source"],
             j.get("salary") or "", (j.get("contract") or "")[:200], j.get("published") or "",
             j["score"], j["reasons"], j["verdict"], j["selected"], job_id))
    conn.execute("UPDATE searches SET status='done', found=?, selected=?, finished_at=datetime('now') "
                 "WHERE id=?", (len(results), selected, run_id))
    conn.execute("DELETE FROM searches WHERE user_id=? AND id NOT IN "
                 "(SELECT id FROM searches WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                 (uid, uid, KEEP_RUNS_PER_USER))
    conn.commit()
    return selected


def note_fn(conn, run_id):
    lines = []

    def note(msg):
        lines.append(time.strftime("%H:%M:%S ") + msg)
        conn.execute("UPDATE searches SET log=? WHERE id=?", ("\n".join(lines), run_id))
        conn.commit()
    return note


def process_search(conn, run):
    lang = run["lang"] or "fr"
    crit = search.load_criteria(run["criteria"])
    urls, keys = existing_for(conn, run["user_id"])
    results = search.run(crit, settings_dict(conn), urls, keys, note_fn(conn, run["id"]), lang,
                         {"ip": run["requester_ip"], "ua": run["requester_ua"]})
    n = store_results(conn, run["id"], run["user_id"], results, lang)
    log.info("search %s user %s: %s candidates, %s selected", run["id"], run["user_id"], len(results), n)


def process_mailbox(conn, mb):
    uid = mb["user_id"]
    user = conn.execute("SELECT lang, disabled FROM users WHERE id=?", (uid,)).fetchone()
    if not user or user["disabled"]:
        return
    lang = user["lang"] or "fr"
    crit_row = conn.execute("SELECT data FROM criteria WHERE user_id=?", (uid,)).fetchone()
    crit = search.load_criteria(crit_row["data"] if crit_row else None)
    conn.execute("UPDATE mailboxes SET force=0, last_check=datetime('now') WHERE user_id=?", (uid,))
    conn.commit()
    try:
        jobs, n_msg, last_uid, uv = mailalerts.fetch(mb["host"], mb["port"], mb["username"],
                                                     mailalerts.decrypt(mb["password"]), mb["folder"],
                                                     mb["last_uid"], mb["uidvalidity"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        conn.execute("UPDATE mailboxes SET last_error=? WHERE user_id=?", (err, uid))
        conn.commit()
        log.warning("mailbox user %s: %s", uid, err)
        return
    conn.execute("UPDATE mailboxes SET last_uid=?, uidvalidity=?, last_error='' WHERE user_id=?",
                 (last_uid, uv, uid))
    conn.commit()
    if not n_msg:
        return  # nothing new: no run record, to keep the history readable
    cur = conn.execute("INSERT INTO searches (user_id, status, criteria, kind, lang, started_at) "
                       "VALUES (?, 'running', ?, 'email', ?, datetime('now'))",
                       (uid, json.dumps(crit, ensure_ascii=False), lang))
    run_id = cur.lastrowid
    conn.commit()
    note = note_fn(conn, run_id)
    note(tr(lang, "E-mail : {n} message(s) lu(s), {j} offre(s) trouvée(s)", n=n_msg, j=len(jobs)))
    urls, keys = existing_for(conn, uid)
    results = search.evaluate(jobs, crit, urls, keys, lang, top_n=False)
    n = store_results(conn, run_id, uid, results, lang)
    log.info("mailbox user %s: %s messages, %s jobs, %s selected", uid, n_msg, len(results), n)


def due_mailboxes(conn):
    return conn.execute(
        "SELECT * FROM mailboxes WHERE enabled=1 AND (force=1 OR last_check IS NULL OR "
        "last_check <= datetime('now', ?))", (f"-{MAIL_INTERVAL_MIN} minutes",)).fetchall()


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.init_db()
    conn = db.connect()
    conn.execute("UPDATE searches SET status='error', log=log || ?, finished_at=datetime('now') "
                 "WHERE status='running'", ("\n" + tr("fr", "interrompue (redémarrage du worker)"),))
    conn.commit()
    log.info("worker started")
    while not stopping:
        run = conn.execute("SELECT * FROM searches WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if run:
            claimed = conn.execute("UPDATE searches SET status='running', started_at=datetime('now') "
                                   "WHERE id=? AND status='queued'", (run["id"],)).rowcount
            conn.commit()
            if claimed:
                try:
                    process_search(conn, run)
                except Exception as e:
                    conn.rollback()
                    log.exception("search %s failed", run["id"])
                    conn.execute("UPDATE searches SET status='error', log=log || ?, finished_at=datetime('now') "
                                 "WHERE id=?", (f"\nerreur : {type(e).__name__}: {e}", run["id"]))
                    conn.commit()
            continue
        for mb in due_mailboxes(conn):
            if stopping:
                break
            try:
                process_mailbox(conn, mb)
            except Exception:
                conn.rollback()
                log.exception("mailbox user %s failed", mb["user_id"])
        time.sleep(2)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
