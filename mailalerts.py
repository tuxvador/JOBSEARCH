"""Read job-alert e-mails (LinkedIn, APEC, Indeed, WTTJ, HelloWork, Cadremploi,
France Travail…) from a user's mailbox over IMAP, read-only.

Each alert lists jobs as links. A link is recognised by its URL (unwrapping
tracking redirects); the title is the link text and company / location come
from the few lines around it. Alert layouts change, so this is best effort:
unrecognised links are ignored, never guessed.
"""
import base64
import email
import hashlib
import imaplib
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken

MAX_MESSAGES = 200
FIRST_RUN_DAYS = 14

# (source, regex on the unwrapped URL, canonical URL template)
PATTERNS = [
    ("LinkedIn", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})"),
     "https://www.linkedin.com/jobs/view/{0}/"),
    ("APEC", re.compile(r"apec\.fr/.*?detail-offre/([0-9A-Za-z]+)"),
     "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{0}"),
    ("Indeed", re.compile(r"indeed\.[a-z.]+/.*?[?&](?:jk|vjk)=([0-9a-f]{8,})"),
     "https://fr.indeed.com/viewjob?jk={0}"),
    ("Welcome to the Jungle", re.compile(r"welcometothejungle\.com/(?:fr|en)/companies/([^/?#]+)/jobs/([^/?#]+)"),
     "https://www.welcometothejungle.com/fr/companies/{0}/jobs/{1}"),
    ("HelloWork", re.compile(r"hellowork\.com/fr-fr/emplois/(\d+)\.html"),
     "https://www.hellowork.com/fr-fr/emplois/{0}.html"),
    ("Cadremploi", re.compile(r"cadremploi\.fr/emploi/detail_offre\?(?:.*&)?offreId=(\d+)"),
     "https://www.cadremploi.fr/emploi/detail_offre?offreId={0}"),
    ("France Travail", re.compile(r"francetravail\.fr/offres/recherche/detail/([0-9A-Za-z]+)"),
     "https://candidat.francetravail.fr/offres/recherche/detail/{0}"),
    ("Jobteaser", re.compile(r"jobteaser\.com/[a-z]{2}/job-offers/([0-9a-f-]{8,})"),
     "https://www.jobteaser.com/fr/job-offers/{0}"),
]
GENERIC_LINK_TEXT = re.compile(
    r"^(voir|view|see|postuler|apply|candidater|d[ée]couvrir|en savoir|plus|more|afficher|show|"
    r"ouvrir|open|cliquez|click|ici|here)\b", re.I)


# ----------------------------------------------------------- credentials
def _fernet():
    key = hashlib.sha256(("mailbox:" + os.environ["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        raise RuntimeError("mot de passe illisible (SECRET_KEY changée ?) — ressaisissez-le")


# ---------------------------------------------------------------- parsing
def unwrap(url, depth=0):
    """Follow URL-encoded targets inside tracking redirects (no network)."""
    url = (url or "").strip()
    if depth > 3:
        return url
    for pat, _src, _tpl in ((p, s, t) for s, p, t in PATTERNS):
        if pat.search(url):
            return url
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:
        return url
    for values in q.values():
        for v in values:
            if v.startswith("http"):
                return unwrap(v, depth + 1)
    return url


def match_job_url(href):
    url = unwrap(href)
    for source, pat, tpl in PATTERNS:
        m = pat.search(url)
        if m:
            return source, (source,) + m.groups(), tpl.format(*m.groups())
    return None


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _lines(node):
    return [_clean(x) for x in node.get_text("\n").splitlines() if _clean(x)]


def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")
    groups = {}
    for a in soup.find_all("a", href=True):
        m = match_job_url(a["href"])
        if not m:
            continue
        source, key, url = m
        groups.setdefault(key, {"source": source, "url": url, "anchors": []})["anchors"].append(a)
    jobs = []
    for g in groups.values():
        texts = [(_clean(a.get_text(" ")), a) for a in g["anchors"]]
        texts = [(t, a) for t, a in texts if 4 <= len(t) <= 200 and not GENERIC_LINK_TEXT.match(t)]
        if texts:
            title, anchor = max(texts, key=lambda x: len(x[0]))
        else:
            anchor, title = g["anchors"][0], ""
        # smallest surrounding block that holds a few lines of context
        block, lines = anchor, []
        for parent in anchor.parents:
            if parent.name in ("td", "tr", "table", "div", "li", "p", "section"):
                lines = _lines(parent)
                if len(lines) >= 2:
                    block = parent
                    break
            if parent.name in ("body", "html"):
                break
        if not title:
            title = lines[0] if lines else ""
        if not title:
            continue
        rest = [l for l in lines if l != title and not GENERIC_LINK_TEXT.match(l)][:6]
        company = location = ""
        if rest:
            first = rest[0]
            if " · " in first:  # LinkedIn: "Company · Location"
                company, _, location = first.partition(" · ")
            else:
                company = first
                location = rest[1] if len(rest) > 1 else ""
        jobs.append({"title": title[:200], "company": company[:120], "location": location[:120],
                     "url": g["url"], "source": g["source"], "snippet": " ".join(rest)[:300]})
    return jobs


def parse_text(text):
    """Plain-text alerts: each job link is preceded by 'title / company / location' lines."""
    jobs, seen, since_last = [], set(), []
    for line in (l.strip() for l in (text or "").splitlines()):
        urls = re.findall(r"https?://\S+", line)
        if not urls:
            if line:
                since_last.append(line)
            continue
        label = re.sub(r"https?://\S+", "", line).strip(" :-<>")
        for href in urls:
            m = match_job_url(href.rstrip(">)]."))
            if not m or m[1] in seen:
                continue
            seen.add(m[1])
            ctx = ([label] if label else []) or since_last[-3:]
            if label:
                ctx += since_last[-2:]
            title, company, location = (ctx + ["", "", ""])[:3]
            if title:
                jobs.append({"title": title[:200], "company": company[:120], "location": location[:120],
                             "url": m[2], "source": m[0], "snippet": " ".join(ctx)[:300]})
        since_last = []
    return jobs


def parse_message(msg):
    html = text = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True)
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else ""
        except (LookupError, AttributeError):
            continue
        if ctype == "text/html" and html is None:
            html = body
        elif ctype == "text/plain" and text is None:
            text = body
    jobs = parse_html(html) if html else []
    if not jobs and text:
        jobs = parse_text(text)
    try:
        published = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        published = ""
    for j in jobs:
        j.update(published=published, contract="", salary="", salary_eur=None)
    return jobs


def subject(msg):
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        return msg.get("Subject", "")


# ------------------------------------------------------------------- IMAP
def fetch(host, port, username, password, folder, last_uid, uidvalidity):
    """-> (jobs, n_messages, new_last_uid, uidvalidity). Read-only: nothing is
    marked as read, moved or deleted."""
    m = imaplib.IMAP4_SSL(host, int(port or 993), timeout=40)
    try:
        m.login(username, password)
        typ, _ = m.select(f'"{folder or "INBOX"}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"dossier introuvable : {folder}")
        _typ, uvdata = m.response("UIDVALIDITY")
        uv = (uvdata or [b"0"])[0] or b"0"
        uv = int(uv.decode() if isinstance(uv, bytes) else uv)
        if not last_uid or uv != uidvalidity:
            since = (datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)).strftime("%d-%b-%Y")
            typ, data = m.uid("search", None, "SINCE", since)
        else:
            typ, data = m.uid("search", None, f"UID {int(last_uid) + 1}:*")
        uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
        uids = [u for u in uids if u > (last_uid if uv == uidvalidity else 0)][-MAX_MESSAGES:]
        jobs, newest = [], (last_uid if uv == uidvalidity else 0)
        for uid in uids:
            typ, parts = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            newest = max(newest, uid)
            if not raw:
                continue
            jobs.extend(parse_message(email.message_from_bytes(raw)))
        return jobs, len(uids), newest, uv
    finally:
        try:
            m.logout()
        except Exception:
            pass
