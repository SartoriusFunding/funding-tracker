#!/usr/bin/env python3
"""
Biotech Department Funding Tracker  (weekly, NIH-only)
======================================================
Runs once a week (GitHub Actions, Monday morning, right after NIH RePORTER's
Sunday-night refresh) and maintains a ledger of NIH awards GRANTED during the
just-completed week to biotech-adjacent university departments.

  Column A = #   B = University   C = Department   D = Funder (NIH institute)
  Every completed Sunday-Saturday week = one column, labeled like
  "Sep 6 - Sep 12". An award appears only once, in the week of its NIH award
  notice date. Cells show each award hyperlinked to its RePORTER project
  page, a ruled "= total" when there are several, and the contact PI's name
  and email.

PI emails, in order of preference:
  1. RePORTER's own project-info service (what the "View Email" button on a
     project page reveals) - undocumented endpoint, so failures are logged
     and tolerated;
  2. the PI's own recent PubMed publications (.edu addresses that contain
     the PI's name);
  3. "--" if neither yields one. All lookups (hits and misses) are cached in
     data/pi_email_cache.json so each PI costs at most one lookup.

Outputs
  data/tracker_data.json   the ledger (source of truth, committed to repo)
  data/seen_awards.json    every award id ever emitted (dedup memory)
  data/pi_email_cache.json PI email lookup cache
  docs/index.html          sortable/filterable page served by GitHub Pages

Usage
  python funding_tracker.py              normal weekly run
  python funding_tracker.py --selftest   offline test with mock data
  python funding_tracker.py --skip-emails  skip email lookups this run
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# NIH standardized dept_type values to INCLUDE (verbatim strings). Awards
# whose org reports NO department (common outside medical schools) are also
# kept when the project title/terms match BIO_KEYWORDS, shown as "--".
NIH_INCLUDE_DEPTS = [
    "BIOCHEMISTRY",
    "BIOMEDICAL ENGINEERING",
    "BIOPHYSICS",
    "BIOLOGY",
    "MICROBIOLOGY/IMMUN/VIROLOGY",
    "GENETICS",
    "ENGINEERING (ALL TYPES)",   # bioprocess/chem-eng lives here; keyword-gated
    "CHEMISTRY",
    "PHARMACOLOGY",
    "PHYSIOLOGY",
    "ANATOMY/CELL BIOLOGY",
    "NUTRITION",
    "VETERINARY SCIENCES",
]

BIO_KEYWORDS = [
    "bio", "cell", "tissue", "protein", "gene", "genom", "rna", "dna",
    "microb", "ferment", "enzym", "vaccin", "antibod", "therapeut",
    "pharma", "drug", "organoid", "biomanufactur", "bioprocess", "biosens",
    "stem", "immun", "virus", "viral", "crispr", "molecul",
]

NON_NIH_AGENCIES = {"CDC", "FDA", "AHRQ", "ACF", "VA", "HRSA", "CMS", "SAMHSA"}

NO_DEPT_LABEL = "--"

UNIVERSITY_MARKERS = ("UNIVERSITY", "COLLEGE", "INSTITUTE", "POLYTECH",
                      "SCHOOL OF", "ACADEMY OF")

SCHEMA = "nih-weekly-1"  # ledger format tag; older data is reset automatically

# --- PI email lookup --------------------------------------------------------
ENABLE_EMAIL_LOOKUP = True
# NOTE: RePORTER's own "View Email" button is reCAPTCHA-gated and its
# project-info service carries no email field (verified), so PubMed /
# Europe PMC publications are the email source.
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()  # optional, free
EMAIL_WORKERS = 6                 # parallel PI lookups
NCBI_RATE = 8.0 if NCBI_API_KEY else 2.5   # requests/sec (limit: 10 / 3)
EPMC_RATE = 4.0                   # Europe PMC requests/sec (be polite)
MAX_PMC_FULLTEXT_PER_PI = 1       # open-access full texts to inspect per PI
EMAIL_RETRY_DAYS = 45
MAX_EMAIL_LOOKUPS_PER_RUN = 900
CACHE_VERSION = 7                 # bump ONLY to force a re-try of cached misses

# ----------------------------------------------------------------------------
# Paths & session
# ----------------------------------------------------------------------------

HOME = Path(os.environ.get("TRACKER_HOME", Path(__file__).resolve().parent))
DATA_DIR = HOME / "data"
DOCS_DIR = HOME / "docs"
DATA_FILE = DATA_DIR / "tracker_data.json"
SEEN_FILE = DATA_DIR / "seen_awards.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "biotech-funding-tracker/2.0 (personal research tool)"})
TIMEOUT = 40


class _RateLimiter:
    """Shared, thread-safe pacing so N workers never exceed a host's limit."""

    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


NCBI_LIMIT = _RateLimiter(NCBI_RATE)
EPMC_LIMIT = _RateLimiter(EPMC_RATE)
_STATS_LOCK = threading.Lock()
_TLS = threading.local()


def _session():
    """requests.Session per worker thread; the main thread uses SESSION."""
    if threading.current_thread() is threading.main_thread():
        return SESSION
    s = getattr(_TLS, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(SESSION.headers)
        _TLS.session = s
    return s


def _bump(stats: dict, key: str = "net", n: int = 1):
    with _STATS_LOCK:
        stats[key] = stats.get(key, 0) + n


_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    with _LOG_LOCK:
        print(f"[tracker] {msg}", flush=True)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log(f"WARNING could not read {path.name}: {e}")
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True))


def email_cache_path() -> Path:
    return DATA_DIR / "pi_email_cache.json"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def nice_name(s: str) -> str:
    small = {"of", "at", "and", "the", "for", "in", "on"}
    out = []
    for i, w in enumerate(re.split(r"\s+", s.strip())):
        lw = w.lower()
        out.append(lw if (lw in small and i > 0) else lw.capitalize())
    return " ".join(out)


def fmt_money(n: int) -> str:
    return f"${n:,.0f}"


def flip_name(name: str) -> str:
    """NIH gives 'DOE, JANE A' -> 'Jane A Doe'."""
    name = (name or "").strip()
    if "," in name:
        last, _, rest = name.partition(",")
        name = f"{rest.strip()} {last.strip()}"
    return nice_name(name) if name else ""


def looks_like_university(name: str) -> bool:
    up = name.upper()
    return any(marker in up for marker in UNIVERSITY_MARKERS)


def has_bio_keyword(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in BIO_KEYWORDS)


def last_completed_week(today: date):
    """The most recently completed Sunday-Saturday week."""
    days_since_sunday = (today.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun->0
    this_sunday = today - timedelta(days=days_since_sunday)
    start = this_sunday - timedelta(days=7)
    return start, start + timedelta(days=6)


def wlabel(week_start_iso: str) -> str:
    s = datetime.strptime(week_start_iso, "%Y-%m-%d").date()
    e = s + timedelta(days=6)
    return f"{s:%b} {s.day} \u2013 {e:%b} {e.day}"


# ----------------------------------------------------------------------------
# PI email lookup: grant-linked publications, then PubMed author search,
# then Europe PMC open-access full text
# ----------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Institutional addresses are not all .edu - research institutes (.org),
# hospitals, government labs (.gov) and foreign universities (.ac.uk, .ca)
# are all legitimate. Reject only free consumer providers.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "mail.com", "yandex.ru", "qq.com", "163.com", "126.com", "sina.com",
    "example.com",
}


def _institutional(email: str) -> bool:
    dom = email.split("@")[-1].lower().rstrip(".")
    return bool(dom) and dom not in FREE_EMAIL_DOMAINS
AFFIL_STOP = {"university", "of", "the", "at", "and", "a", "an", "in", "for",
              "system", "medical", "college", "school", "institute", "center",
              "centre", "hospital", "health", "sciences", "science",
              "research", "graduate", "state"}


def _walk_for_email(obj, require_pi=True):
    """Recursively scan a JSON payload for an email under an email-ish key.
    First pass prefers keys mentioning both 'pi' and 'email'; second pass
    accepts any 'email' key."""
    hits = []

    def walk(o, keypath=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{keypath}.{k}".lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, keypath)
        elif isinstance(o, str) and "email" in keypath:
            m = EMAIL_RE.search(o)
            if m:
                hits.append((keypath, m.group(0)))

    walk(obj)
    for kp, e in hits:
        if "pi" in kp or "contact" in kp:
            return e
    if not require_pi and hits:
        return hits[0][1]
    return hits[0][1] if hits else ""


def _uni_affil_terms(university: str) -> str:
    """One distinctive token, e.g. 'Vanderbilt University Medical Center' ->
    Vanderbilt. Stitching two non-adjacent words into a quoted phrase (the
    old behaviour) never matched real affiliation strings."""
    toks = [w for w in re.split(r"[^A-Za-z]+", university)
            if w and w.lower() not in AFFIL_STOP and len(w) > 2]
    if not toks:
        toks = [w for w in re.split(r"[^A-Za-z]+", university)
                if w and w.lower() not in {"of", "the", "at", "and"}]
    if not toks:
        return ""
    # longest wins (ties -> earliest): "Ada Forsyth" -> Forsyth, not Ada
    return max(toks, key=lambda w: (len(w), -toks.index(w)))


def _name_forms(pi: str):
    """('Jessica Leigh Mark Welch') -> [('Jessica','Welch'),('Jessica','Mark Welch')]
    so compound surnames are not silently truncated."""
    parts = [x for x in pi.split() if x]
    if len(parts) < 2:
        return []
    forms = [(parts[0], parts[-1])]
    if len(parts) >= 3:
        forms.append((parts[0], " ".join(parts[-2:])))
    return forms


def _plausible_own_email(email: str, first: str, last: str) -> bool:
    """Accept jane_big@, jbig@, big@, bigj@, j.big@ - reject co-authors."""
    lp = email.split("@")[0].lower()
    last_l = re.sub(r"[^a-z]", "", last.lower())
    fi = re.sub(r"[^a-z]", "", first.lower())[:1]
    if not last_l:
        return False
    if len(last_l) >= 4 and last_l[:4] in lp:
        return True
    if len(last_l) == 3 and (lp.startswith(last_l) or lp.endswith(last_l)):
        return True
    if fi and lp.startswith(fi) and len(last_l) >= 3 and last_l[:3] in lp:
        return True
    return False


def _extract_email(xml_text: str, forms) -> str:
    """Pull a PI's own institutional email out of PubMed efetch XML.
    `forms` is [(first, last), ...] - any form may match."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    lasts = {l.lower() for _, l in forms} | {l.split()[-1].lower() for _, l in forms}

    def ok(e):
        return _institutional(e) and any(
            _plausible_own_email(e, f, l) for f, l in forms)

    # pass 1: affiliations attached to an author whose surname matches
    for au in root.iter("Author"):
        if (au.findtext("LastName") or "").lower() not in lasts:
            continue
        for aff in au.iter("Affiliation"):
            for e in EMAIL_RE.findall(aff.text or ""):
                if ok(e):
                    return e
    # pass 2: any affiliation line, name match still required
    for aff in root.iter("Affiliation"):
        for e in EMAIL_RE.findall(aff.text or ""):
            if ok(e):
                return e
    return ""


def _ncbi_get(url: str, params: dict):
    """Rate-limited GET with polite retries: throttling (429), NCBI server
    hiccups (500/502/503/504) and dropped connections, growing pauses."""
    last_exc = None
    for attempt, pause in enumerate((0, 2.5, 6.0)):
        if pause:
            time.sleep(pause)
        NCBI_LIMIT.wait()
        try:
            r = _session().get(url, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            last_exc = requests.HTTPError(f"{r.status_code}")
            continue
        r.raise_for_status()
        return r
    raise last_exc or RuntimeError("NCBI request failed")


_REPORTER_FAIL_LOGS = [0]


def _reporter_post(url: str, payload: dict):
    """POST to RePORTER politely (~1 req/s, main thread only); log the first
    few failures so an HTTP error is never mistaken for 'no results'."""
    r = SESSION.post(url, json=payload, timeout=TIMEOUT)
    time.sleep(1.0)
    if r.status_code != 200:
        if _REPORTER_FAIL_LOGS[0] < 3:
            _REPORTER_FAIL_LOGS[0] += 1
            log(f"    RePORTER {url.rsplit('/', 2)[-2]} HTTP {r.status_code}: "
                f"{r.text[:160]!r}")
        return None
    return r.json()


def _batch_grant_pmids(cores) -> dict:
    """{core project number: [pmids, newest first]} for many grants in a few
    calls, instead of one RePORTER call per PI. Papers NIH links to the
    grant are the most precise identity match for common names."""
    cores = [c for c in dict.fromkeys(cores) if c]
    out = {}
    shape_logged = False
    for i in range(0, len(cores), 40):
        chunk = cores[i:i + 40]
        try:
            for offset in (0, 500, 1000):
                data = _reporter_post(
                    "https://api.reporter.nih.gov/v2/publications/search",
                    {"criteria": {"core_project_nums": chunk},
                     "limit": 500, "offset": offset})
                results = (data or {}).get("results") or []
                if results and not shape_logged:
                    shape_logged = True
                    log(f"  publications record keys: {sorted(results[0].keys())[:8]}")
                for x in results:
                    core = x.get("coreproject") or x.get("core_project_num") or ""
                    pm = x.get("pmid")
                    if core and pm:
                        out.setdefault(core, []).append(str(pm))
                if len(results) < 500:
                    break
        except Exception as exc:
            log(f"  grant publications batch failed: {exc}")
    for core, lst in out.items():
        lst = sorted(set(lst), key=int, reverse=True)  # PMIDs grow over time
        out[core] = lst[:20]
    return out


def _pmcids_in(xml_text: str):
    """PMC ids listed in PubMed efetch XML (open-access full text exists)."""
    return re.findall(r'IdType="pmc">\s*(PMC\d+)', xml_text)


def _europepmc_email(pmcid: str, forms, stats, verbose=False) -> str:
    """Open-access full text (JATS) carries explicit corresponding-author
    <email> tags that PubMed's affiliation field often omits."""
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    try:
        _bump(stats)
        EPMC_LIMIT.wait()
        r = _session().get(url, timeout=TIMEOUT)
        if r.status_code != 200 or not r.text:
            return ""
        for e in EMAIL_RE.findall(r.text):
            if _institutional(e) and any(
                    _plausible_own_email(e, f, l) for f, l in forms):
                if verbose:
                    log(f"    -> {e} (Europe PMC full text, {pmcid})")
                return e
        return ""
    except Exception:
        return ""


def _efetch_email(pmids, forms, stats, verbose=False):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    common = {"db": "pubmed", "tool": "biotech-funding-tracker"}
    if NCBI_API_KEY:
        common["api_key"] = NCBI_API_KEY
    _bump(stats)
    r = _ncbi_get(base + "efetch.fcgi",
                  dict(common, id=",".join(pmids), retmode="xml"))
    email = _extract_email(r.text, forms)
    if verbose:
        log(f"    -> {email or 'no matching institutional email in affiliations'}")
    if email:
        return email
    for pmcid in _pmcids_in(r.text)[:MAX_PMC_FULLTEXT_PER_PI]:
        email = _europepmc_email(pmcid, forms, stats, verbose)
        if email:
            return email
    return ""


def _pubmed_email(pi: str, university: str, linked, stats: dict,
                  verbose: bool = False):
    """Return an email str, '' for a clean no-hit, or None on transient error
    (None is never cached, so the PI is retried next run).

    Order: papers NIH links to this grant first, then author-name search."""
    forms = _name_forms(pi)
    if not forms:
        return ""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    common = {"db": "pubmed", "tool": "biotech-funding-tracker"}
    if NCBI_API_KEY:
        common["api_key"] = NCBI_API_KEY
    try:
        if verbose:
            log(f"    grant-linked publications: {len(linked or [])} pmid(s)")
        if linked:
            email = _efetch_email(linked, forms, stats, verbose)
            if email:
                return email
        term = _uni_affil_terms(university)
        queries = []
        for first, last in forms:
            surname = last.split()[-1]
            author = f"{surname} {first[:1]}[Author]"
            if term:
                queries.append(f"{author} AND {term}[Affiliation]")
            queries.append(author)
        seen_q = set()
        for q in queries:
            if q in seen_q:
                continue
            seen_q.add(q)
            _bump(stats)
            r = _ncbi_get(base + "esearch.fcgi",
                          dict(common, term=q, retmax="20", retmode="json",
                               reldate="4000", datetype="pdat"))
            ids = ((r.json().get("esearchresult") or {}).get("idlist")) or []
            if verbose:
                log(f"    pubmed q=[{q}] -> {len(ids)} pmid(s)")
            if not ids:
                continue
            email = _efetch_email(ids, forms, stats, verbose)
            if email:
                return email
        return ""
    except Exception as exc:
        log(f"  PubMed lookup errored for {pi}: {exc}")
        return None


def lookup_pi_email(pi: str, university: str, linked, stats: dict,
                    verbose: bool = False):
    """Return (email, source); ('', '') for a clean miss; (None, None) on a
    transient error (never cached). Runs inside a worker thread."""
    pm = _pubmed_email(pi, university, linked, stats, verbose)
    if pm is None:
        return None, None
    return pm, ("PubMed" if pm else "")


def _backfill_core_numbers(data) -> None:
    """One-time: entries recorded before core project numbers were stored
    get them via batched RePORTER lookups (500 appl ids per call)."""
    missing = {}
    for row in data["rows"].values():
        for entries in row["cells"].values():
            for e in entries:
                if "core" in e:
                    continue
                m = re.search(r"project-details/(\d+)", e.get("url", ""))
                if m:
                    missing.setdefault(m.group(1), []).append(e)
                else:
                    e["core"] = ""
    if not missing:
        return
    ids = list(missing)
    found = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            data_json = _reporter_post(
                "https://api.reporter.nih.gov/v2/projects/search",
                {"criteria": {"appl_ids": [int(x) for x in chunk]},
                 "include_fields": ["ApplId", "CoreProjectNum"],
                 "limit": 500, "offset": 0})
            for p in (data_json or {}).get("results") or []:
                core = p.get("core_project_num") or ""
                for e in missing.get(str(p.get("appl_id")), []):
                    e["core"] = core
                    found += 1
        except Exception as exc:
            log(f"  core-number backfill chunk failed: {exc}")
    for lst in missing.values():
        for e in lst:
            e.setdefault("core", "")
    save_json(DATA_FILE, data)
    log(f"backfilled core project numbers for {found} entries")


def sweep_pi_emails(data):
    """Fill missing PI emails. Cache-first (so re-runs cost nothing), then
    the remaining PIs are looked up in parallel, newest weeks first.
    Progress-logged and checkpointed so an interrupted run keeps its work."""
    if not ENABLE_EMAIL_LOOKUP:
        return data
    cache = load_json(email_cache_path(), {})
    meta = cache.get("_meta") or {}
    if meta.get("v", 0) < 5:
        # legacy caches (before v5) stored transient errors as misses
        cache = {k: v for k, v in cache.items()
                 if isinstance(v, dict) and v.get("email")}
    cache["_meta"] = {"v": CACHE_VERSION}
    _backfill_core_numbers(data)

    # group every entry that still lacks an email by PI + university
    groups, order = {}, []
    for wk in reversed(data["dates"]):
        for row in data["rows"].values():
            for e in row["cells"].get(wk, []):
                if not e.get("pi") or e.get("pi_email"):
                    continue
                key = f"{e['pi']}|{row['university']}".lower()
                if key not in groups:
                    groups[key] = {"pi": e["pi"], "university": row["university"],
                                   "core": e.get("core", ""), "entries": []}
                    order.append(key)
                groups[key]["entries"].append(e)
    pending = sum(len(g["entries"]) for g in groups.values())
    if not pending:
        log("PI email sweep: nothing to look up")
        save_json(email_cache_path(), cache)
        return data

    changed = False
    filled = 0
    todo = []
    today = date.today()
    for key in order:
        g = groups[key]
        ent = cache.get(key)
        if ent and ent.get("email"):
            for e in g["entries"]:
                e["pi_email"] = ent["email"]
                e["pi_email_via"] = ent.get("src", "PubMed")
            filled += len(g["entries"])
            changed = True
            continue
        if ent:
            try:
                checked = date.fromisoformat(ent.get("checked", "1970-01-01"))
            except ValueError:
                checked = date(1970, 1, 1)
            if (today - checked).days < EMAIL_RETRY_DAYS:
                continue  # recent miss: wait it out
        todo.append(g)
    skipped_cap = max(len(todo) - MAX_EMAIL_LOOKUPS_PER_RUN, 0)
    todo = todo[:MAX_EMAIL_LOOKUPS_PER_RUN]
    log(f"PI email sweep: {pending} entries need emails; {filled} filled from "
        f"cache, {len(todo)} PI(s) to look up"
        + (f", {skipped_cap} deferred to next run" if skipped_cap else ""))

    stats = {"net": 0, "pis": 0}
    if todo:
        core_map = _batch_grant_pmids(g["core"] for g in todo)
        log(f"  grant-linked papers found for {len(core_map)} of {len(todo)} grant(s)")

        def work(g, verbose):
            if verbose:
                log(f"  lookup: {g['pi']} @ {g['university']}")
            return lookup_pi_email(g["pi"], g["university"],
                                   core_map.get(g["core"], []), stats, verbose)

        found_new = 0
        with ThreadPoolExecutor(max_workers=EMAIL_WORKERS) as pool:
            futures = {pool.submit(work, g, i < 3): g for i, g in enumerate(todo)}
            for fut in as_completed(futures):
                g = futures[fut]
                key = f"{g['pi']}|{g['university']}".lower()
                try:
                    email, src = fut.result()
                except Exception as exc:
                    log(f"  lookup crashed for {g['pi']}: {exc}")
                    email, src = None, None
                stats["pis"] += 1
                if email is not None:  # None = transient error: not cached
                    cache[key] = {"email": email, "src": src,
                                  "checked": today.isoformat()}
                    if email:
                        for e in g["entries"]:
                            e["pi_email"] = email
                            e["pi_email_via"] = src
                        filled += len(g["entries"])
                        found_new += 1
                        changed = True
                if stats["pis"] % 25 == 0:
                    log(f"  progress: {stats['pis']}/{len(todo)} PIs, "
                        f"{found_new} new emails")
                if stats["pis"] % 50 == 0:
                    save_json(email_cache_path(), cache)
                    if changed:
                        save_json(DATA_FILE, data)

    save_json(email_cache_path(), cache)
    if changed:
        save_json(DATA_FILE, data)
    log(f"emails: found={filled}, none={max(pending - filled, 0)} "
        f"(PIs looked up: {stats['pis']}, API calls: {stats['net']}, "
        f"cache: {len(cache) - 1})")
    return data


# ----------------------------------------------------------------------------
# NIH fetch: awards NOTICED during the target week
# ----------------------------------------------------------------------------

def make_award(key, university, department, amount, url, funder, pi,
               pi_email="", core=""):
    return {
        "key": key, "university": university, "department": department,
        "funder": funder, "amount": int(amount), "url": url, "pi": pi,
        "pi_email": pi_email, "core": core,
    }


def fetch_nih_week(week_start: date, week_end: date):
    awards, offset = [], 0
    while offset <= 9500:
        payload = {
            "criteria": {
                "award_notice_date": {"from_date": week_start.isoformat(),
                                      "to_date": week_end.isoformat()},
                "org_countries": ["UNITED STATES"],
                "exclude_subprojects": True,
            },
            "include_fields": [
                "ApplId", "ProjectNum", "ProjectTitle", "AwardAmount",
                "Organization", "AwardNoticeDate", "ProjectDetailUrl",
                "PrefTerms", "AgencyIcAdmin", "ContactPiName",
                "PrincipalInvestigators", "CoreProjectNum",
            ],
            "limit": 500,
            "offset": offset,
        }
        r = SESSION.post("https://api.reporter.nih.gov/v2/projects/search",
                         json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results", [])
        if offset == 0 and results:
            # One-time diagnostic: does the public API carry PI emails at all?
            p0 = results[0]
            pis0 = p0.get("principal_investigators") or []
            log(f"  NIH PI object keys: "
                f"{sorted(pis0[0].keys()) if pis0 else 'no PI array returned'}")
            found = EMAIL_RE.findall(json.dumps(p0))
            log(f"  emails inside the API record: {found[:2] if found else 'NONE'}")
        for p in results:
            amt = p.get("award_amount") or 0
            org = p.get("organization") or {}
            dept = (org.get("dept_type") or "").strip().upper()
            name = (org.get("org_name") or "").strip()
            text = (p.get("project_title") or "") + " " + (p.get("pref_terms") or "")
            if amt < 1 or not name:
                continue
            if dept in NIH_INCLUDE_DEPTS:
                if dept == "ENGINEERING (ALL TYPES)" and not has_bio_keyword(text):
                    continue
                dept_label = nice_name(dept)
            elif (dept in ("", "NONE", "NO CODE ASSIGNED")
                  and has_bio_keyword(text)
                  and looks_like_university(name)):
                dept_label = NO_DEPT_LABEL  # NIH reported no department
            else:
                continue  # named clinical dept, company, or off-topic
            ic = (p.get("agency_ic_admin") or {}).get("abbreviation") or ""
            funder = (ic if ic in NON_NIH_AGENCIES
                      else f"NIH ({ic})" if ic else "NIH")
            appl = p.get("appl_id")
            url = p.get("project_detail_url") or \
                f"https://reporter.nih.gov/project-details/{appl}"
            # If the API itself carries an email, take it - free and exact.
            api_email = ""
            pis = p.get("principal_investigators") or []
            for pi_obj in pis:
                if not isinstance(pi_obj, dict):
                    continue
                cand = _walk_for_email(pi_obj)
                if cand and (pi_obj.get("is_contact_pi") or not api_email):
                    api_email = cand
                    if pi_obj.get("is_contact_pi"):
                        break
            awards.append(make_award(
                key=f"NIH:{appl}", university=nice_name(name),
                department=dept_label, amount=amt, url=url, funder=funder,
                pi=flip_name(p.get("contact_pi_name") or ""),
                pi_email=api_email, core=(p.get("core_project_num") or ""),
            ))
        if len(results) < 500:
            break
        offset += 500
        time.sleep(1)
    return awards


# ----------------------------------------------------------------------------
# LEDGER
# ----------------------------------------------------------------------------

def row_key(a) -> str:
    return f"{a['university'].lower()}||{a['department'].lower()}||{a['funder'].lower()}"


def ingest(awards, week_start_iso: str):
    data = load_json(DATA_FILE, {"dates": [], "rows": {}, "meta": {}})
    seen = load_json(SEEN_FILE, {})

    # Automatic fresh start when the ledger predates the weekly NIH-only
    # format (old daily columns can't be re-bucketed into award-date weeks).
    meta = data.setdefault("meta", {})
    if meta.get("schema") != SCHEMA:
        if data.get("rows"):
            log("ledger schema changed -> starting the weekly NIH-only "
                "ledger fresh (old daily data cleared automatically)")
        data = {"dates": [], "rows": {}, "meta": {"schema": SCHEMA}}
        seen = {}

    if week_start_iso not in data["dates"]:
        data["dates"].append(week_start_iso)
        data["dates"].sort()

    new_count = 0
    for a in awards:
        if a["key"] in seen:
            continue
        seen[a["key"]] = {"week": week_start_iso, "amount": a["amount"]}
        row = data["rows"].setdefault(row_key(a), {
            "university": a["university"],
            "department": a["department"],
            "funder": a["funder"],
            "cells": {},
        })
        row["cells"].setdefault(week_start_iso, []).append({
            "amount": a["amount"], "label": fmt_money(a["amount"]),
            "url": a["url"], "pi": a["pi"], "pi_email": a["pi_email"],
            "core": a.get("core", ""),
        })
        new_count += 1

    save_json(DATA_FILE, data)
    save_json(SEEN_FILE, seen)
    log(f"ingested {new_count} new award(s); ledger now has "
        f"{len(data['rows'])} rows x {len(data['dates'])} week(s)")
    return data, new_count


# ----------------------------------------------------------------------------
# HTML RENDER
# ----------------------------------------------------------------------------

def _cell_html(entries, institution=""):
    if not entries:
        return '<td class="zero" data-v="0">0</td>'
    total = sum(e["amount"] for e in entries)
    lines = "".join(
        f'<div class="aline"><a href="{html_lib.escape(e["url"], quote=True)}" '
        f'target="_blank" rel="noopener">{html_lib.escape(e["label"])}</a></div>'
        for e in entries
    )
    if len(entries) > 1:
        lines += f'<div class="tot">= {fmt_money(total)}</div>'
    top = max(entries, key=lambda e: e["amount"])
    pi_name = html_lib.escape(top.get("pi") or "--")
    email = (top.get("pi_email") or "").strip()
    if email:
        via = html_lib.escape(top.get("pi_email_via") or "")
        t = f' title="source: {via}"' if via else ""
        pi_mail = (f'<a href="mailto:{html_lib.escape(email, quote=True)}"{t}>'
                   f"{html_lib.escape(email)}</a>")
    else:
        pi_mail = "--"
    inst = html_lib.escape(institution.strip() or "--")
    lines += (f'<div class="pib">PI Name: {pi_name}<br>PI Email: {pi_mail}'
              f'<br>Institution: {inst}</div>')
    return f'<td data-v="{total}">{lines}</td>'


def render_html(data) -> str:
    weeks = data["dates"]
    latest = weeks[-1] if weeks else None
    rows = sorted(
        data["rows"].values(),
        key=lambda r: (r["university"].lower(), r.get("funder", "").lower(),
                       r["department"].lower()),
    )

    week_totals = {
        w: sum(e["amount"] for r in rows for e in r["cells"].get(w, []))
        for w in weeks}
    total_latest = week_totals.get(latest, 0)
    n_awards = sum(len(v) for r in rows for v in r["cells"].values())

    head_cells = "".join(
        f'<th class="num{" sel" if w == latest else ""}" data-t="num" '
        f'data-total="{week_totals[w]}" data-label="{wlabel(w)}" '
        f'title="{w}">{wlabel(w)}</th>' for w in weeks)

    body = []
    for i, r in enumerate(rows, start=1):
        all_entries = [e for v in r["cells"].values() for e in v]
        has_email = any(e.get("pi_email") for e in all_entries)
        has_name = any((e.get("pi") or "").strip() for e in all_entries)
        has_inst = bool((r.get("university") or "").strip())
        cells = "".join(_cell_html(r["cells"].get(w, []), r.get("university", ""))
                        for w in weeks)
        body.append(
            f'<tr data-he="{1 if has_email else 0}" data-hn="{1 if has_name else 0}" '
            f'data-hi="{1 if has_inst else 0}">'
            f'<td class="rownum">{i}</td>'
            f'<td class="uni">{html_lib.escape(r["university"])}</td>'
            f'<td class="dept">{html_lib.escape(r["department"])}</td>'
            f'<td class="funder">{html_lib.escape(r.get("funder", "\u2014"))}</td>'
            f"{cells}</tr>"
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biotech department funding ledger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#FBFBFD; --ink:#101418; --line:#E3E6EA; --mut:#8A939C;
  --grow:#1B7A43; --broth:#EAF6EF;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 "IBM Plex Sans", system-ui, sans-serif; }}
a {{ color:var(--grow); text-decoration:none; border-bottom:1px solid #BFE0CC; }}
a:hover {{ border-bottom-color:var(--grow); }}
.mast {{ display:flex; flex-wrap:wrap; gap:24px; align-items:flex-end;
  justify-content:space-between; padding:28px 32px 18px; }}
.mast h1 {{ margin:0; font-size:22px; font-weight:600; letter-spacing:-.01em; }}
.mast .sub {{ color:var(--mut); font-size:13px; margin-top:4px; }}
.todaybox {{ text-align:right; }}
.todaybox .fig {{ font-size:26px; font-weight:600; color:var(--grow);
  font-variant-numeric:tabular-nums; }}
.todaybox .cap {{ font-size:12px; color:var(--mut); }}
.emailchk {{ display:flex; gap:7px; align-items:center; justify-content:flex-end;
  margin-top:6px; font-size:13px; color:var(--ink); cursor:pointer;
  user-select:none; }}
.emailchk:first-of-type {{ margin-top:9px; }}
.emailchk input {{ width:15px; height:15px; accent-color:var(--grow);
  cursor:pointer; }}
.controls {{ display:flex; gap:16px; align-items:center; flex-wrap:wrap;
  padding:0 32px 14px; font-size:13px; color:var(--mut); }}
.controls input {{ font:inherit; color:var(--ink); padding:7px 10px;
  border:1px solid var(--line); border-radius:6px; background:#fff; width:260px; }}
.controls input:focus {{ outline:2px solid var(--broth); border-color:var(--grow); }}
.wrap {{ overflow:auto; max-height:calc(100vh - 150px);
  border-top:1px solid var(--line); }}
table {{ border-collapse:separate; border-spacing:0; min-width:100%;
  font-variant-numeric:tabular-nums; }}
th, td {{ padding:9px 14px; border-bottom:1px solid var(--line);
  white-space:nowrap; text-align:right; font-size:14px; vertical-align:top; }}
.aline {{ display:block; line-height:1.6; }}
.tot {{ display:inline-block; border-top:1px solid var(--ink); margin-top:3px;
  padding-top:3px; font-weight:600; }}
.pib {{ margin-top:7px; font-size:12px; color:var(--mut); font-weight:400;
  line-height:1.5; }}
.pib a {{ color:var(--grow); border-bottom-color:#BFE0CC; }}
th {{ position:sticky; top:0; background:var(--bg); z-index:3; cursor:pointer;
  font-weight:500; user-select:none; }}
th::after {{ content:"\u21C5"; margin-left:6px; font-size:11px; color:var(--mut); }}
th:first-child {{ cursor:default; }}
th:first-child::after {{ content:""; }}
th.sorted {{ box-shadow:inset 0 -2px 0 var(--grow); color:var(--grow); }}
th.sorted.asc::after {{ content:"\u25B2"; color:var(--grow); }}
th.sorted.desc::after {{ content:"\u25BC"; color:var(--grow); }}
th:hover {{ color:var(--grow); }}
th.sel {{ background:var(--broth); }}
th:nth-child(1), td.rownum {{ position:sticky; left:0; background:var(--bg);
  text-align:right; min-width:48px; max-width:48px; z-index:2;
  color:var(--mut); font-size:12.5px; }}
th:nth-child(2), td.uni {{ position:sticky; left:48px; background:var(--bg);
  text-align:left; min-width:220px; max-width:220px; white-space:normal;
  z-index:2; font-weight:500; }}
th:nth-child(3), td.dept {{ position:sticky; left:268px; background:var(--bg);
  text-align:left; min-width:200px; max-width:200px; white-space:normal;
  z-index:2; color:#3C444C; }}
th:nth-child(4), td.funder {{ position:sticky; left:468px; background:var(--bg);
  text-align:left; min-width:130px; max-width:170px; white-space:normal;
  z-index:2; box-shadow:2px 0 0 var(--line); }}
th:nth-child(-n+4) {{ z-index:4; }}
td[data-v]:not(.zero) {{ background:#fff; }}
tr:hover td {{ background:#F3F7F4; }}
.zero {{ color:var(--mut); }}
.legend {{ padding:12px 32px 40px; color:var(--mut); font-size:12.5px; }}
</style></head><body>

<div class="mast">
  <div>
    <h1>Biotech department funding ledger</h1>
    <div class="sub">NIH awards granted each week to biotech-adjacent
      university departments &middot; updated {generated} &middot;
      {len(rows)} rows &middot; {n_awards} awards tracked</div>
  </div>
  <div class="todaybox">
    <div class="fig" id="fig">{fmt_money(total_latest)}</div>
    <div class="cap">granted <span id="cap">{wlabel(latest) if latest else "-"}</span></div>
    <label class="emailchk"><input type="checkbox" id="onlyemail">
      Only show rows with a PI email</label>
    <label class="emailchk"><input type="checkbox" id="onlyname">
      Only show rows with a PI name</label>
    <label class="emailchk"><input type="checkbox" id="onlyinst">
      Only show rows with an institution</label>
  </div>
</div>

<div class="controls">
  <input id="q" type="search" placeholder="Filter by university, department or funder"
    aria-label="Filter rows">
  <span>The \u21C5 arrows mean a column is sortable &mdash; click to sort,
    click again to reverse</span>
</div>

<div class="wrap"><table id="t">
<thead><tr>
  <th>#</th>
  <th data-t="text">University</th>
  <th data-t="text">Department</th>
  <th data-t="text">Funder</th>
  {head_cells}
</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div>

<div class="legend">Each column is one completed Sunday&ndash;Saturday week;
an award appears in the week of its NIH award-notice date, once, so amounts
are never double-counted. 0 = no award granted to that row that week. Cells
with several awards list each one and end with a ruled, unlinked
<b>= total</b>. A <b>--</b> Department means NIH reported no department for
that keyword-matched award. PI lines show the contact PI of the cell's
largest award; emails come from NIH's own project record when available,
otherwise from the PI's recent PubMed publications (hover an email to see
which), and -- means neither source had one yet. Row numbers always follow
the current sort and filter. Source: NIH RePORTER (refreshes Sunday nights;
this page updates every Monday morning).</div>

<script>
const table = document.getElementById('t');
const tbody = table.tBodies[0];
const ths = [...table.tHead.rows[0].cells];
const q = document.getElementById('q');
const only = document.getElementById('onlyemail');
const onlyName = document.getElementById('onlyname');
const onlyInst = document.getElementById('onlyinst');
let cur = {{ i:-1, dir:1 }};

function renumber() {{
  let n = 0;
  [...tbody.rows].forEach(r => {{
    if (r.style.display !== 'none') r.cells[0].textContent = ++n;
  }});
}}
function applyFilter() {{
  const v = q.value.toLowerCase();
  [...tbody.rows].forEach(r => {{
    const hay = (r.cells[1].innerText + ' ' + r.cells[2].innerText + ' '
                 + r.cells[3].innerText).toLowerCase();
    const ok = hay.includes(v)
      && (!only.checked || r.dataset.he === '1')
      && (!onlyName.checked || r.dataset.hn === '1')
      && (!onlyInst.checked || r.dataset.hi === '1');
    r.style.display = ok ? '' : 'none';
  }});
  renumber();
}}
ths.forEach((th, i) => {{
  if (i === 0) return; // the # column reflects position, it never sorts
  th.addEventListener('click', () => {{
    const num = th.dataset.t === 'num';
    cur.dir = (cur.i === i) ? -cur.dir : (num ? -1 : 1);
    cur.i = i;
    ths.forEach(h => h.classList.remove('sorted', 'asc', 'desc'));
    th.classList.add('sorted', cur.dir === 1 ? 'asc' : 'desc');
    if (num && th.dataset.total !== undefined) {{
      ths.forEach(h => h.classList.remove('sel'));
      th.classList.add('sel');
      document.getElementById('fig').textContent =
        '$' + (+th.dataset.total).toLocaleString('en-US');
      document.getElementById('cap').textContent = th.dataset.label;
    }}
    const rows = [...tbody.rows];
    rows.sort((a, b) => {{
      if (num) {{
        return ((+a.cells[i].dataset.v || 0) - (+b.cells[i].dataset.v || 0)) * cur.dir;
      }}
      return a.cells[i].innerText.localeCompare(b.cells[i].innerText) * cur.dir;
    }});
    rows.forEach(r => tbody.appendChild(r));
    renumber();
  }});
}});
q.addEventListener('input', applyFilter);
only.addEventListener('change', applyFilter);
onlyName.addEventListener('change', applyFilter);
onlyInst.addEventListener('change', applyFilter);
</script>
</body></html>"""


def write_site(data) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(render_html(data))
    (DOCS_DIR / ".nojekyll").write_text("")
    log(f"wrote {DOCS_DIR/'index.html'}")


# ----------------------------------------------------------------------------
# Self-test (offline)
# ----------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    global HOME, DATA_DIR, DOCS_DIR, DATA_FILE, SEEN_FILE
    tmp = Path(tempfile.mkdtemp(prefix="tracker_test_"))
    HOME, DATA_DIR, DOCS_DIR = tmp, tmp / "data", tmp / "docs"
    DATA_FILE, SEEN_FILE = DATA_DIR / "tracker_data.json", DATA_DIR / "seen_awards.json"

    # week math + labels
    assert last_completed_week(date(2026, 9, 16)) == (date(2026, 9, 6), date(2026, 9, 12))
    assert last_completed_week(date(2026, 9, 21)) == (date(2026, 9, 13), date(2026, 9, 19))
    assert last_completed_week(date(2026, 9, 20)) == (date(2026, 9, 13), date(2026, 9, 19))
    assert wlabel("2026-09-06") == "Sep 6 \u2013 Sep 12"
    assert wlabel("2026-09-27") == "Sep 27 \u2013 Oct 3"

    # automatic fresh start from old daily-format data
    save_json(DATA_FILE, {"dates": ["2026-09-16"], "rows": {
        "x||y||z": {"university": "X", "department": "Y", "funder": "Z",
                    "cells": {"2026-09-16": [{"amount": 1, "label": "$1",
                                              "url": "u", "pi": "",
                                              "pi_email": ""}]}}}})
    save_json(SEEN_FILE, {"NIH:1": {}})

    wk1 = [
        make_award("NIH:111", "Brown University", "Biomedical Engineering",
                   4_600_000, "https://reporter.nih.gov/project-details/111",
                   "NIH (NIGMS)", "Jane Big"),
        make_award("NIH:112", "Brown University", "Biomedical Engineering",
                   500_000, "https://reporter.nih.gov/project-details/112",
                   "NIH (NIGMS)", "John Small"),
        make_award("NIH:222", "Tufts University", NO_DEPT_LABEL,
                   750_000, "https://reporter.nih.gov/project-details/222",
                   "NIH (NIAID)", "Ada Lovelace"),
    ]
    data, n1 = ingest(wk1, "2026-09-06")
    assert data["meta"]["schema"] == SCHEMA and n1 == 3, "fresh start failed"
    assert len(data["rows"]) == 2, "old-format rows were not cleared"
    assert "NIH:1" not in load_json(SEEN_FILE, {}), "seen not reset"

    data, n2 = ingest([wk1[0], make_award(
        "NIH:333", "Brown University", "Biomedical Engineering", 1_000_000,
        "https://reporter.nih.gov/project-details/333", "NIH (NCI)",
        "Amy New")], "2026-09-13")
    assert n2 == 1, "dedup failed"
    assert data["dates"] == ["2026-09-06", "2026-09-13"]
    bme = data["rows"]["brown university||biomedical engineering||nih (nigms)"]
    assert "2026-09-13" not in bme["cells"], "duplicate re-emitted"
    assert [e["amount"] for e in bme["cells"]["2026-09-06"]] == [4_600_000, 500_000]

    # reporter payload email extraction
    payload = {"contact_pi": {"full_name": "MILOSAVLJEVIC, ALEKSANDAR",
                              "contact_pi_email": "amilosav@bcm.edu"},
               "other": [{"email_notes": "none"}]}
    assert _walk_for_email(payload) == "amilosav@bcm.edu"
    assert _walk_for_email({"foo": "bar"}) == ""

    # pubmed extraction still guarded by name match
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>"
           "<AuthorList><Author><LastName>Big</LastName><ForeName>Jane</ForeName>"
           "<AffiliationInfo><Affiliation>Brown University. jane_big@brown.edu"
           "</Affiliation></AffiliationInfo></Author></AuthorList>"
           "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
    assert _extract_email(xml, [("Jane", "Big")]) == "jane_big@brown.edu"
    assert _extract_email(xml, [("Zed", "Nowhere")]) == ""

    # .org / .gov institutional addresses accepted, free-mail rejected
    assert _institutional("jmarkwelch@forsyth.org")
    assert _institutional("someone@nih.gov")
    assert not _institutional("someone@gmail.com")

    # compound surnames: both forms tried, PubMed's "Mark Welch" matches
    forms = _name_forms("Jessica Leigh Mark Welch")
    assert ("Jessica", "Welch") in forms and ("Jessica", "Mark Welch") in forms
    xml2 = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>"
            "<AuthorList><Author><LastName>Mark Welch</LastName>"
            "<ForeName>Jessica L</ForeName><AffiliationInfo><Affiliation>"
            "ADA Forsyth Institute, Somerville MA. jmarkwelch@forsyth.org"
            "</Affiliation></AffiliationInfo></Author></AuthorList>"
            "</Article></MedlineCitation><PubmedData><ArticleIdList>"
            "<ArticleId IdType=\"pmc\">PMC9999999</ArticleId>"
            "</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>")
    assert _extract_email(xml2, forms) == "jmarkwelch@forsyth.org", \
        "compound surname + .org email should match"
    assert _pmcids_in(xml2) == ["PMC9999999"], "PMC id extraction"

    # affiliation token picks the distinctive word
    assert _uni_affil_terms("Ada Forsyth Institute, Inc.") == "Forsyth"
    assert _uni_affil_terms("Johns Hopkins University") == "Hopkins"
    assert _uni_affil_terms("University of Minnesota") == "Minnesota"

    # email sweep with stubbed lookup (no network) + cache-flush check
    save_json(email_cache_path(), {
        "old hit|somewhere": {"email": "keep@x.edu", "src": "PubMed",
                              "checked": "2026-09-01"},
        "poisoned miss|somewhere": {"email": "", "src": "",
                                    "checked": "2026-09-01"},
    })
    orig_lookup = globals()["lookup_pi_email"]
    orig_batch = globals()["_batch_grant_pmids"]
    calls = []
    globals()["_batch_grant_pmids"] = lambda cores: {}
    globals()["lookup_pi_email"] = (
        lambda pi, uni, linked, stats, verbose=False:
        (calls.append(pi) or (("jbig@brown.edu", "PubMed") if pi == "Jane Big"
                              else ("", ""))))
    data = sweep_pi_emails(data)
    assert data["rows"]["brown university||biomedical engineering||nih (nigms)"][
        "cells"]["2026-09-06"][0]["pi_email"] == "jbig@brown.edu"
    flushed = load_json(email_cache_path(), {})
    assert "old hit|somewhere" in flushed, "legacy flush must keep confirmed hits"
    assert "poisoned miss|somewhere" not in flushed, "legacy flush must drop misses"
    assert flushed.get("_meta", {}).get("v") == CACHE_VERSION
    assert len(calls) == 4, f"expected 4 fresh lookups, got {len(calls)}"

    # re-run: every PI is now cached (hit or fresh miss) -> ZERO lookups
    calls.clear()
    data = sweep_pi_emails(data)
    assert calls == [], f"re-run must not query anything, but queried {calls}"
    globals()["lookup_pi_email"] = orig_lookup
    globals()["_batch_grant_pmids"] = orig_batch

    write_site(data)
    page = (DOCS_DIR / "index.html").read_text()
    assert page.count("<th ") == 5, "expected 3 label ths + 2 week ths (# th has no attrs)"
    assert "Sep 6 \u2013 Sep 12" in page and "Sep 13 \u2013 Sep 19" in page
    assert '<td class="rownum">1</td>' in page and '<td class="rownum">3</td>' in page
    assert 'id="onlyemail"' in page and 'data-he="1"' in page and 'data-he="0"' in page
    assert 'id="onlyname"' in page and 'id="onlyinst"' in page, "new checkboxes"
    assert 'data-hn="1"' in page and 'data-hi="1"' in page, "row flags"
    assert "Institution: Brown University" in page, "institution line"
    assert "onlyName.checked" in page and "onlyInst.checked" in page, "combined filter"
    assert '<div class="tot">= $5,100,000</div>' in page
    assert "PI Name: Jane Big" in page and 'href="mailto:jbig@brown.edu"' in page
    assert 'title="source: PubMed"' in page
    assert "PI Email: --" in page
    assert 'data-v="0">0<' in page
    assert "renumber" in page and "r.cells[0].textContent" in page
    assert 'data-total="5850000"' in page, \
        "week header must carry its own total (4.6M + 0.5M + 0.75M)"
    assert 'data-label="Sep 6 \u2013 Sep 12"' in page, "week header label missing"
    assert 'id="fig"' in page and 'id="cap"' in page, "summary ids missing"
    assert page.count('class="num sel"') == 1, "exactly one week highlighted"

    data, n3 = ingest([wk1[0]], "2026-09-13")
    assert n3 == 0 and data["dates"].count("2026-09-13") == 1, "rerun broke"

    print(f"SELFTEST PASSED  (artifacts in {tmp})")
    return 0


# ----------------------------------------------------------------------------

def main() -> int:
    global ENABLE_EMAIL_LOOKUP
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--skip-emails", action="store_true",
                    help="update the table only; skip the PI-email sweep")
    ap.add_argument("--emails-only", action="store_true",
                    help="skip the NIH fetch; only fill PI emails and re-render")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.skip_emails:
        ENABLE_EMAIL_LOOKUP = False
    if args.emails_only:
        data = load_json(DATA_FILE, {"dates": [], "rows": {}, "meta": {}})
        data = sweep_pi_emails(data)
        write_site(data)
        return 0

    start, end = last_completed_week(date.today())
    log(f"target week: {start} .. {end} (awards by NIH award-notice date)")
    awards = fetch_nih_week(start, end)
    log(f"NIH RePORTER: {len(awards)} award(s) matched filters")
    data, _ = ingest(awards, start.isoformat())
    data = sweep_pi_emails(data)
    write_site(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
