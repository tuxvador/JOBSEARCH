"""Search criteria, scoring and job sources.

Scoring follows the job-search skill (references/scoring-algorithm.md), made
configurable per user:
  - hard exclusions        -> score -999, never selected
  - one penalty            -> applied once (first matching term)
  - score categories       -> each adds its weight once if any term matches
                              in its scope (title / location / everything)
  - required groups        -> categories tagged "signal", "lieu" or "contrat":
                              a job must hit at least one category of each group
  - min score, top N per source, salary floor, max age

Term syntax (one per line or comma-separated): plain words match whole words
after accents are removed ("securite" matches "Sécurité"); a trailing * matches
any ending ("logisti*" -> logistique); "re:<regex>" is a raw regex.

Sources use official APIs only. LinkedIn, APEC, Indeed… have no search API;
their offers come in through e-mail alerts (mailalerts.py) or the bookmarklet.
"""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import presets
from i18n import tr

USER_AGENT = "job.tuxvador.fr/1.0 (personal job dashboard)"
USD_TO_EUR = 0.9  # rough, only used to compare foreign salaries with the floor

SOURCES = {
    "francetravail": "France Travail",
    "labonnealternance": "La bonne alternance",
    "adzuna": "Adzuna",
    "jooble": "Jooble",
    "careerjet": "Careerjet",
    "remoteok": "RemoteOK",
    "himalayas": "Himalayas",
}
CONTRACTS = ["CDI", "CDD", "Alternance", "Freelance", "Intérim"]
SCOPES = {"title": "Titre", "location": "Lieu", "all": "Titre + entreprise + lieu + contrat"}
GATES = {"": "—", "signal": "Signal obligatoire", "lieu": "Lieu obligatoire",
         "contrat": "Contrat obligatoire"}
GATE_VERDICT = {"signal": "pas de signal", "lieu": "lieu non souhaité", "contrat": "contrat non souhaité"}

PRESETS = presets.PRESETS
DEFAULT_CRITERIA = presets.BLANK


# ------------------------------------------------------------------ terms
def normalize(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip()


def compile_term(term):
    term = term.strip()
    if not term:
        return None
    if term.startswith("re:"):
        return re.compile(term[3:].strip())
    n = normalize(term)
    wild = n.endswith("*")
    n = n.rstrip("*").strip()
    if not n:
        return None
    pat = r"\b" + r"\s+".join(re.escape(w) for w in n.split(" "))
    return re.compile(pat if wild else pat + r"\b")


def split_terms(text):
    """Textarea -> list of terms (newlines; commas too, except inside re: lines)."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("re:"):
            out.append(line)
        else:
            out.extend(t.strip() for t in line.split(",") if t.strip())
    return out


class Scorer:
    def __init__(self, crit, lang="fr"):
        self.lang = lang
        self.exclude = [(t, compile_term(t)) for t in crit["exclude"]]
        self.penalty = [(t, compile_term(t)) for t in crit["penalty"]]
        self.penalty_weight = int(crit["penalty_weight"])
        self.cats = [dict(c, rx=[(t, compile_term(t)) for t in c["terms"]])
                     for c in crit["categories"] if c.get("terms")]
        self.gates = {c["gate"] for c in self.cats if c["gate"]}
        self.min_score = int(crit["min_score"])

    @staticmethod
    def _hit(pairs, text):
        for term, rx in pairs:
            if rx is not None and rx.search(text):
                return term
        return None

    def score(self, title, company, location, contract="", snippet=""):
        """-> (score, reasons[list], gates_hit[set], excluded_term or None)

        Exclusions and penalty look at title, company, location and contract;
        "everything" categories also look at the description excerpt."""
        t, c, l, k = normalize(title), normalize(company), normalize(location), normalize(contract)
        combined = f"{t} {c} {l} {k}"
        everything = f"{combined} {normalize(snippet)}"
        hit = self._hit(self.exclude, combined)
        if hit:
            return -999, [tr(self.lang, "exclu : {term}", term=hit)], set(), hit
        score, reasons, gates = 0, [], set()
        hit = self._hit(self.penalty, combined)
        if hit:
            score += self.penalty_weight
            reasons.append(tr(self.lang, "{w} pénalité ({term})", w=f"{self.penalty_weight:+d}", term=hit))
        for cat in self.cats:
            text = {"title": t, "location": l}.get(cat["scope"], everything)
            hit = self._hit(cat["rx"], text)
            if hit:
                score += int(cat["weight"])
                reasons.append(f"{int(cat['weight']):+d} {cat['name']} ({hit})")
                if cat["gate"]:
                    gates.add(cat["gate"])
        return score, reasons, gates, None

    def verdict(self, score, gates, excluded):
        """'' when selectable, otherwise the reason it isn't."""
        if excluded:
            return tr(self.lang, "exclu")
        for gate in ("signal", "contrat", "lieu"):
            if gate in self.gates and gate not in gates:
                return tr(self.lang, GATE_VERDICT[gate])
        if score < self.min_score:
            return tr(self.lang, "score < {n}", n=self.min_score)
        return ""


# --------------------------------------------------------------- criteria
def default_criteria(preset=presets.DEFAULT_PRESET):
    return presets.get(preset)


def load_criteria(raw):
    crit = default_criteria()
    if raw:
        try:
            crit.update(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return crit


def _int(v, default, lo=None, hi=None):
    try:
        v = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def criteria_from_form(form, lang="fr"):
    """Build criteria from the search form. Returns (criteria, errors)."""
    errors = []
    crit = {
        "keywords": [k for k in split_terms(form.get("keywords", "")) if len(k) >= 2][:15],
        "sources": [s for s in form.getlist("sources") if s in SOURCES],
        "ft_departements": ",".join(re.findall(r"\b(?:\d{2,3}|2[AB])\b", form.get("ft_departements", "")))[:60],
        "adzuna_where": form.get("adzuna_where", "").strip()[:60],
        "lba_romes": ",".join(dict.fromkeys(re.findall(r"\b[A-N]\d{4}\b", form.get("lba_romes", "").upper())))[:200],
        "contracts": [c for c in form.getlist("contracts") if c in CONTRACTS],
        "max_age_days": _int(form.get("max_age_days"), 31, 1, 90),
        "salary_floor": _int(form.get("salary_floor"), 0, 0, 1000000),
        "remote_countries": [normalize(x) for x in split_terms(form.get("remote_countries", ""))],
        "exclude": split_terms(form.get("exclude", "")),
        "penalty_weight": _int(form.get("penalty_weight"), -20, -500, 0),
        "penalty": split_terms(form.get("penalty", "")),
        "min_score": _int(form.get("min_score"), 20, -500, 500),
        "top_n": _int(form.get("top_n"), 10, 1, 100),
        "categories": [],
    }
    i = 0
    while f"cat-{i}-name" in form:
        name = form.get(f"cat-{i}-name", "").strip()[:60]
        terms = split_terms(form.get(f"cat-{i}-terms", ""))
        if name and terms and not form.get(f"cat-{i}-delete"):
            crit["categories"].append({
                "name": name,
                "weight": _int(form.get(f"cat-{i}-weight"), 0, -500, 500),
                "scope": form.get(f"cat-{i}-scope") if form.get(f"cat-{i}-scope") in SCOPES else "title",
                "gate": form.get(f"cat-{i}-gate") if form.get(f"cat-{i}-gate") in GATES else "",
                "terms": terms,
            })
        i += 1
    if not crit["keywords"]:
        errors.append(tr(lang, "Au moins un mot-clé de recherche (2 caractères minimum)."))
    if not crit["sources"]:
        errors.append(tr(lang, "Choisissez au moins une source."))
    for label, terms in [(tr(lang, "exclusions"), crit["exclude"]), (tr(lang, "pénalités"), crit["penalty"])] + \
            [(c["name"], c["terms"]) for c in crit["categories"]]:
        for t in terms:
            try:
                compile_term(t)
            except re.error as e:
                errors.append(tr(lang, "Expression invalide dans « {label} » : {term} ({err})",
                                 label=label, term=t, err=e))
    return crit, errors


def search_terms(crit):
    """Keywords as sent to sources with no contract filter: when work-study is
    the only contract wanted, 'alternance' is added to each keyword."""
    kws = crit["keywords"]
    if crit.get("contracts") == ["Alternance"]:
        return [k if re.search(r"alternan|apprenti", normalize(k)) else f"alternance {k}" for k in kws]
    return kws


# ---------------------------------------------------------------- sources
def http_json(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, (json.loads(body) if body.strip() else None)


def _ts(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_money(v):
    return f"{int(float(v)):,}".replace(",", " ")


def _salary_text(lo, hi, cur="EUR", period=""):
    try:
        if not lo and not hi:
            return ""
        if lo and hi and float(lo) != float(hi):
            s = f"{_fmt_money(lo)}–{_fmt_money(hi)}"
        else:
            s = _fmt_money(lo or hi)
    except (TypeError, ValueError):
        return ""
    return f"{s} {cur}{'/' + period if period else ''}"


def _salary_eur_year(lo, hi, cur="EUR", period="year"):
    v = hi or lo
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not v:
        return None
    period = (period or "year").lower()
    if period[:1] == "m":  # month / mensuel
        v *= 12
    elif period[:1] not in ("y", "a"):  # year / annuel / an
        return None
    if cur and cur.upper() == "USD":
        v *= USD_TO_EUR
    return v


def _too_old(dt, crit):
    return dt is not None and (datetime.now(timezone.utc) - dt).days > crit["max_age_days"]


_FT_ALTERNANCE_CACHE = {}


def _ft_alternance_codes(auth, log, lang):
    """natureContrat codes for apprenticeship / professionalisation, read from
    France Travail's own reference list (not hard-coded)."""
    if "codes" not in _FT_ALTERNANCE_CACHE:
        _, ref = http_json("https://api.francetravail.io/partenaire/offresdemploi/v2/referentiel/naturesContrats",
                           headers=auth)
        codes = [r["code"] for r in (ref or [])
                 if re.search(r"apprenti|professionnali", normalize(r.get("libelle", "")))]
        _FT_ALTERNANCE_CACHE["codes"] = codes
        log(tr(lang, "France Travail : codes alternance {codes}", codes=", ".join(codes) or "—"))
    return _FT_ALTERNANCE_CACHE["codes"]


def src_francetravail(crit, settings, log, lang, requester):
    cid, secret = settings.get("ft_client_id"), settings.get("ft_client_secret")
    if not (cid and secret):
        log(tr(lang, "{src} : identifiants API non configurés (Admin → Paramètres) — ignoré", src="France Travail"))
        return []
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                   "client_secret": secret, "scope": "api_offresdemploiv2 o2dsoffre"}).encode()
    _, tok = http_json("https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
                       data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    since = next((d for d in (1, 3, 7, 14, 31) if d >= crit["max_age_days"]), 31)
    codes = {"CDI": "CDI", "CDD": "CDD", "Freelance": "LIB", "Intérim": "MIS"}
    filters = []  # one query per filter set (contract type OR work-study)
    types = [codes[c] for c in crit["contracts"] if c in codes]
    if types:
        filters.append({"typeContrat": ",".join(types)})
    if "Alternance" in crit["contracts"]:
        alt = _ft_alternance_codes(auth, log, lang)
        if alt:
            filters.append({"natureContrat": ",".join(alt)})
    if not filters:
        filters.append({})
    out = []
    for kw in crit["keywords"]:
        n = 0
        for extra in filters:
            params = {"motsCles": kw, "range": "0-149", "publieeDepuis": since, **extra}
            if crit["ft_departements"]:
                params["departement"] = crit["ft_departements"]
            status, data = http_json("https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search?"
                                     + urllib.parse.urlencode(params), headers=auth)
            rows = (data or {}).get("resultats", []) if status in (200, 206) else []
            n += len(rows)
            for o in rows:
                sal = (o.get("salaire") or {}).get("libelle", "")
                nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", sal)]
                contract = " · ".join(x for x in (o.get("typeContratLibelle"), o.get("natureContrat"),
                                                  "alternance" if o.get("alternance") else "") if x)
                out.append({
                    "title": o.get("intitule", ""),
                    "company": (o.get("entreprise") or {}).get("nom", ""),
                    "location": (o.get("lieuTravail") or {}).get("libelle", ""),
                    "url": (o.get("origineOffre") or {}).get("urlOrigine")
                           or f"https://candidat.francetravail.fr/offres/recherche/detail/{o.get('id')}",
                    "salary": sal,
                    "salary_eur": _salary_eur_year(min(nums) if nums else None, max(nums) if nums else None,
                                                   "EUR", sal.split(" ")[0] if sal else ""),
                    "contract": contract,
                    "snippet": (o.get("description") or "")[:300],
                    "published": (o.get("dateCreation") or "")[:10],
                })
            time.sleep(0.4)
        log(tr(lang, "{src} « {kw} » : {n}", src="France Travail", kw=kw, n=n))
    return out


def src_labonnealternance(crit, settings, log, lang, requester):
    """La bonne alternance (api.apprentissage.beta.gouv.fr): apprenticeship
    offers only (LBA + France Travail + partners). No keyword search: the scope
    is ROME job-family codes and départements; scoring does the rest."""
    key = settings.get("lba_key")
    if not key:
        log(tr(lang, "{src} : identifiants API non configurés (Admin → Paramètres) — ignoré", src="La bonne alternance"))
        return []
    params = []
    if crit.get("lba_romes"):
        params.append(("romes", crit["lba_romes"]))
    for d in (crit.get("ft_departements") or "").split(","):
        if d.strip():
            params.append(("departements", d.strip()))
    _, data = http_json("https://api.apprentissage.beta.gouv.fr/api/job/v1/search?" + urllib.parse.urlencode(params),
                        headers={"Authorization": f"Bearer {key}"})
    rows = (data or {}).get("jobs") or []
    for w in (data or {}).get("warnings") or []:
        log(f"La bonne alternance : {w.get('code', '')} {w.get('message', '')}".strip())
    out = []
    for o in rows:
        off, wp, c = o.get("offer") or {}, o.get("workplace") or {}, o.get("contract") or {}
        created = (off.get("publication") or {}).get("creation") or ""
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if _too_old(dt, crit):
            continue
        diploma = (off.get("target_diploma") or {}).get("label") or ""
        contract = " · ".join(x for x in ["alternance", ", ".join(c.get("type") or []),
                                          tr(lang, "{n} mois", n=c["duration"]) if c.get("duration") else "",
                                          diploma] if x)
        out.append({
            "title": off.get("title", ""),
            "company": wp.get("brand") or wp.get("name") or wp.get("legal_name") or "",
            "location": (wp.get("location") or {}).get("address", ""),
            "url": (o.get("apply") or {}).get("url", ""),
            "salary": "",
            "salary_eur": None,
            "contract": contract,
            "snippet": re.sub(r"<[^>]+>", " ", off.get("description") or "")[:300],
            "published": dt.strftime("%Y-%m-%d") if dt else "",
        })
    log(tr(lang, "{src} : {n} offres, {m} assez récentes", src="La bonne alternance", n=len(rows), m=len(out)))
    return out


def src_adzuna(crit, settings, log, lang, requester):
    app_id, app_key = settings.get("adzuna_app_id"), settings.get("adzuna_app_key")
    if not (app_id and app_key):
        log(tr(lang, "{src} : identifiants API non configurés (Admin → Paramètres) — ignoré", src="Adzuna"))
        return []
    out = []
    for kw in search_terms(crit):
        params = {"app_id": app_id, "app_key": app_key, "what_phrase": kw,
                  "max_days_old": crit["max_age_days"], "results_per_page": 50,
                  "content-type": "application/json"}
        if crit["adzuna_where"]:
            params["where"] = crit["adzuna_where"]
            params["distance"] = 40
        if crit["contracts"] == ["CDI"]:
            params["permanent"] = 1
        _, data = http_json("https://api.adzuna.com/v1/api/jobs/fr/search/1?" + urllib.parse.urlencode(params))
        rows = (data or {}).get("results", [])
        log(tr(lang, "{src} « {kw} » : {n}", src="Adzuna", kw=kw, n=len(rows)))
        for o in rows:
            lo, hi = o.get("salary_min"), o.get("salary_max")
            out.append({
                "title": re.sub(r"<[^>]+>", "", o.get("title", "")),
                "company": (o.get("company") or {}).get("display_name", ""),
                "location": (o.get("location") or {}).get("display_name", ""),
                "url": o.get("redirect_url", ""),
                "salary": _salary_text(lo, hi, "EUR", "an"),
                "salary_eur": None if str(o.get("salary_is_predicted")) == "1" else _salary_eur_year(lo, hi),
                "contract": {"permanent": "CDI", "contract": "CDD"}.get(o.get("contract_type"), ""),
                "snippet": re.sub(r"<[^>]+>", "", o.get("description", ""))[:300],
                "published": (o.get("created") or "")[:10],
            })
        time.sleep(0.4)
    return out


def src_jooble(crit, settings, log, lang, requester):
    key = settings.get("jooble_key")
    if not key:
        log(tr(lang, "{src} : identifiants API non configurés (Admin → Paramètres) — ignoré", src="Jooble"))
        return []
    out = []
    for kw in search_terms(crit):
        body = json.dumps({"keywords": kw, "location": crit["adzuna_where"] or "France",
                           "page": "1", "ResultOnPage": "50"}).encode()
        _, data = http_json(f"https://jooble.org/api/{urllib.parse.quote(key)}", data=body,
                            headers={"Content-Type": "application/json"})
        rows = (data or {}).get("jobs", [])
        n = 0
        for o in rows:
            try:
                dt = datetime.fromisoformat(str(o.get("updated", ""))[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
            if _too_old(dt, crit):
                continue
            n += 1
            snippet = re.sub(r"<[^>]+>", "", o.get("snippet", "") or "")
            out.append({
                "title": re.sub(r"<[^>]+>", "", o.get("title", "") or ""),
                "company": o.get("company", "") or "",
                "location": o.get("location", "") or "",
                "url": o.get("link", "") or "",
                "salary": o.get("salary", "") or "",
                "salary_eur": None,
                "contract": o.get("type", "") or "",
                "snippet": snippet[:300],
                "published": dt.strftime("%Y-%m-%d") if dt else "",
            })
        log(tr(lang, "{src} « {kw} » : {n}", src="Jooble", kw=kw, n=n))
        time.sleep(0.4)
    return out


def src_careerjet(crit, settings, log, lang, requester):
    key = settings.get("careerjet_key")
    if not key:
        log(tr(lang, "{src} : identifiants API non configurés (Admin → Paramètres) — ignoré", src="Careerjet"))
        return []
    if not (requester or {}).get("ip"):
        log(tr(lang, "Careerjet : IP de l'utilisateur inconnue — ignoré"))
        return []
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode(),
            "Referer": "https://job.tuxvador.fr/search"}
    letters = {"CDI": "p", "CDD": "t", "Intérim": "t", "Freelance": "c"}
    wanted = {letters[c] for c in crit["contracts"] if c in letters}
    out = []
    for kw in search_terms(crit):
        params = {"locale_code": settings.get("careerjet_locale") or "fr_FR", "keywords": kw,
                  "page_size": 50, "sort": "date",
                  "user_ip": requester["ip"], "user_agent": requester.get("ua") or USER_AGENT}
        if crit["adzuna_where"]:
            params["location"] = crit["adzuna_where"]
        if len(wanted) == 1 and "Alternance" not in crit["contracts"]:
            params["contract_type"] = next(iter(wanted))
        _, data = http_json("https://search.api.careerjet.net/v4/query?" + urllib.parse.urlencode(params),
                            headers=auth)
        rows = (data or {}).get("jobs", []) if (data or {}).get("type") == "JOBS" else []
        n = 0
        for o in rows:
            try:
                dt = parsedate_to_datetime(o.get("date", ""))
            except (TypeError, ValueError):
                dt = None
            if _too_old(dt, crit):
                continue
            n += 1
            period = {"Y": "year", "M": "month"}.get(o.get("salary_type", ""), "")
            out.append({
                "title": o.get("title", ""),
                "company": o.get("company", ""),
                "location": o.get("locations", "") if isinstance(o.get("locations"), str)
                            else ", ".join(o.get("locations") or []),
                "url": o.get("url", ""),
                "salary": o.get("salary", "") or "",
                "salary_eur": _salary_eur_year(o.get("salary_min"), o.get("salary_max"),
                                               o.get("salary_currency_code") or "EUR", period) if period else None,
                "contract": "",
                "snippet": re.sub(r"<[^>]+>", "", o.get("description", "") or "")[:300],
                "published": dt.strftime("%Y-%m-%d") if dt else "",
            })
        log(tr(lang, "{src} « {kw} » : {n}", src="Careerjet", kw=kw, n=n))
        time.sleep(0.4)
    return out


def _keyword_match(crit, *texts):
    blob = normalize(" ".join(texts))
    return any(normalize(k) in blob for k in crit["keywords"])


def src_remoteok(crit, settings, log, lang, requester):
    _, data = http_json("https://remoteok.com/api")
    rows = [r for r in (data or []) if isinstance(r, dict) and r.get("id")]
    cutoff = time.time() - crit["max_age_days"] * 86400
    out = []
    for o in rows:
        if int(o.get("epoch") or 0) < cutoff:
            continue
        if not _keyword_match(crit, o.get("position", ""), " ".join(o.get("tags") or [])):
            continue
        lo, hi = o.get("salary_min"), o.get("salary_max")
        out.append({
            "title": o.get("position", ""),
            "company": o.get("company", ""),
            "location": f"Remote — {o.get('location') or 'Worldwide'}",
            "url": o.get("url", ""),
            "salary": _salary_text(lo, hi, "USD", "an"),
            "salary_eur": _salary_eur_year(lo, hi, "USD"),
            "contract": "",
            "published": _ts(o.get("epoch")),
        })
    log(tr(lang, "RemoteOK : {n} offres récentes, {m} correspondent aux mots-clés", n=len(rows), m=len(out)))
    return out


def src_himalayas(crit, settings, log, lang, requester):
    out = []
    cutoff = time.time() - crit["max_age_days"] * 86400
    for kw in search_terms(crit):
        n = 0
        for page in (1, 2, 3):
            _, data = http_json("https://himalayas.app/jobs/api/search?"
                                + urllib.parse.urlencode({"q": kw, "limit": 20, "page": page}))
            jobs = (data or {}).get("jobs", [])
            for o in jobs:
                if int(o.get("pubDate") or 0) < cutoff:
                    continue
                lo, hi, cur = o.get("minSalary"), o.get("maxSalary"), o.get("currency") or "USD"
                n += 1
                out.append({
                    "title": o.get("title", ""),
                    "company": o.get("companyName", ""),
                    "location": "Remote — " + (", ".join(o.get("locationRestrictions") or []) or "Worldwide"),
                    "url": o.get("applicationLink", ""),
                    "salary": _salary_text(lo, hi, cur, o.get("salaryPeriod") or ""),
                    "salary_eur": _salary_eur_year(lo, hi, cur, o.get("salaryPeriod") or "year"),
                    "contract": o.get("employmentType") or "",
                    "published": _ts(o.get("pubDate")),
                })
            if len(jobs) < 20:
                break
            time.sleep(0.4)
        log(tr(lang, "{src} « {kw} » : {n}", src="Himalayas", kw=kw, n=n))
    return out


SOURCE_FUNCS = {"francetravail": src_francetravail, "labonnealternance": src_labonnealternance, "adzuna": src_adzuna, "jooble": src_jooble,
                "careerjet": src_careerjet, "remoteok": src_remoteok, "himalayas": src_himalayas}


# -------------------------------------------------------------------- run
def _remote_country_ok(crit, job):
    """Remote boards: keep jobs open to one of the accepted countries/regions."""
    loc = normalize(job["location"])
    if not loc.startswith("remote"):
        return True
    where = loc.split("—", 1)[-1] if "—" in loc else loc.split("-", 1)[-1]
    allowed = crit.get("remote_countries") or []
    return not allowed or any(re.search(r"\b" + re.escape(a), where) for a in allowed)


def evaluate(jobs, crit, existing_urls, existing_keys, lang="fr", top_n=True):
    """Dedup, score and select a list of candidate dicts (in place). Returns the unique list."""
    scorer = Scorer(crit, lang)
    seen_urls, seen_keys, unique = set(), set(), []
    for j in jobs:
        key = (normalize(j["title"]), normalize(j["company"]))
        if (j["url"] and j["url"] in seen_urls) or key in seen_keys:
            continue
        seen_urls.add(j["url"])
        seen_keys.add(key)
        score, reasons, gates, excluded = scorer.score(j["title"], j["company"], j["location"],
                                                      j.get("contract", ""), j.get("snippet", ""))
        verdict = scorer.verdict(score, gates, excluded)
        if not verdict and not _remote_country_ok(crit, j):
            verdict = tr(lang, "pays non accepté")
        floor = crit.get("salary_floor") or 0
        if not verdict and floor and j.get("salary_eur") and j["salary_eur"] < floor:
            verdict = tr(lang, "salaire < {n}", n=floor)
        if not verdict and ((j["url"] and j["url"] in existing_urls) or key in existing_keys):
            verdict = tr(lang, "déjà dans vos offres")
        j.update(score=score, reasons=" · ".join(reasons), verdict=verdict, selected=0)
        unique.append(j)

    by_src = {}
    for j in unique:
        if not j["verdict"]:
            by_src.setdefault(j["source"], []).append(j)
    for lst in by_src.values():
        lst.sort(key=lambda x: x.get("published") or "", reverse=True)  # newest first on ties
        lst.sort(key=lambda x: -x["score"])
        for i, j in enumerate(lst):
            if not top_n or i < crit["top_n"]:
                j["selected"] = 1
            else:
                j["verdict"] = tr(lang, "hors top {n}", n=crit["top_n"])
    unique.sort(key=lambda x: (-x["selected"], -x["score"]))
    return unique


def run(crit, settings, existing_urls, existing_keys, log, lang="fr", requester=None):
    """Fetch from the chosen sources, score and select. Returns candidate dicts."""
    candidates = []
    for src in crit["sources"]:
        try:
            for job in SOURCE_FUNCS[src](crit, settings, log, lang, requester or {}):
                job["source"] = SOURCES[src]
                candidates.append(job)
        except urllib.error.HTTPError as e:
            log(tr(lang, "{src} : erreur HTTP {code} — source ignorée", src=SOURCES[src], code=e.code))
        except Exception as e:  # one broken source must not kill the run
            log(tr(lang, "{src} : échec ({err}) — source ignorée", src=SOURCES[src], err=f"{type(e).__name__}: {e}"))
    unique = evaluate(candidates, crit, existing_urls, existing_keys, lang)
    log(tr(lang, "Total : {n} résultats, {u} uniques, {s} sélectionnés",
           n=len(candidates), u=len(unique), s=sum(j["selected"] for j in unique)))
    return unique
