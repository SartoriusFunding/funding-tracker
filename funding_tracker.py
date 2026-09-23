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
import time
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
# RePORTER's internal project-info service (feeds the "View Email" button on
# project pages). Undocumented: if it errors repeatedly we stop calling it
# for the run and rely on PubMed.
REPORTER_INFO_URL = ("https://reporter.nih.gov/services/Projects/ProjectInfo"
                     "?projectId={appl}")
REPORTER_SLEEP = 0.6
REPORTER_MAX_CONSECUTIVE_FAILS = 3
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()  # optional, free
EMAIL_SLEEP = 0.12 if NCBI_API_KEY else 0.35
EMAIL_RETRY_DAYS = 45
MAX_EMAIL_LOOKUPS_PER_RUN = 600

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


def log(msg: str) -> None:
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
# PI email lookup: RePORTER project-info first, PubMed fallback
# ----------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_EDU_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.edu\b", re.I)
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


class ReporterEmailSource:
    """Wraps the undocumented project-info endpoint with a circuit breaker."""

    def __init__(self):
        self.consecutive_fails = 0
        self.no_email_streak = 0
        self.disabled = False
        self.logged_shape = False

    def get(self, appl: str, stats: dict) -> str:
        if self.disabled or not appl:
            return ""
        try:
            stats["net"] = stats.get("net", 0) + 1
            r = SESSION.get(REPORTER_INFO_URL.format(appl=appl), timeout=TIMEOUT)
            time.sleep(REPORTER_SLEEP)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            payload = r.json()
            if not self.logged_shape:
                self.logged_shape = True
                keys = (sorted(payload.keys())[:15]
                        if isinstance(payload, dict) else type(payload).__name__)
                log(f"  reporter payload shape: {keys}")
            email = _walk_for_email(payload)
            self.consecutive_fails = 0
            if email:
                self.no_email_streak = 0
            else:
                self.no_email_streak += 1
                if self.no_email_streak >= 8:
                    self.disabled = True
                    log("RePORTER endpoint responds but carries no email field "
                        "- skipping it for the rest of this run (paste the "
                        "'reporter payload shape' log line to Claude)")
            return email
        except Exception as exc:
            self.consecutive_fails += 1
            if self.consecutive_fails >= REPORTER_MAX_CONSECUTIVE_FAILS:
                self.disabled = True
                log(f"RePORTER email endpoint unavailable ({exc}); "
                    f"falling back to PubMed for the rest of this run - "
                    f"report this line to Claude if it persists")
            return ""


def _uni_affil_terms(university: str) -> str:
    """One distinctive token, e.g. 'Vanderbilt University Medical Center' ->
    Vanderbilt. Stitching two non-adjacent words into a quoted phrase (the
    old behaviour) never matched real affiliation strings."""
    toks = [w for w in re.split(r"[^A-Za-z]+", university)
            if w and w.lower() not in AFFIL_STOP and len(w) > 2]
    if not toks:
        toks = [w for w in re.split(r"[^A-Za-z]+", university)
                if w and w.lower() not in {"of", "the", "at", "and"}]
    return toks[0] if toks else ""


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


def _extract_edu_email(xml_text: str, first: str, last: str) -> str:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    last_l = last.lower()
    for au in root.iter("Author"):
        if (au.findtext("LastName") or "").lower() != last_l:
            continue
        for aff in au.iter("Affiliation"):
            for e in EMAIL_EDU_RE.findall(aff.text or ""):
                if _plausible_own_email(e, first, last):
                    return e
    for aff in root.iter("Affiliation"):
        for e in EMAIL_EDU_RE.findall(aff.text or ""):
            if _plausible_own_email(e, first, last):
                return e
    return ""


def _ncbi_get(url: str, params: dict):
    """GET with one polite retry on throttling (HTTP 429)."""
    r = SESSION.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 429:
        time.sleep(2.5)
        r = SESSION.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _pubmed_email(first: str, last: str, university: str, stats: dict,
                  verbose: bool = False):
    """Return an email str, '' for a clean no-hit, or None on transient error
    (None is never cached, so the PI is retried next run)."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    common = {"db": "pubmed", "tool": "biotech-funding-tracker"}
    if NCBI_API_KEY:
        common["api_key"] = NCBI_API_KEY
    author = f"{last} {first[:1]}[Author]"
    term = _uni_affil_terms(university)
    queries = ([f"{author} AND {term}[Affiliation]"] if term else []) + [author]
    try:
        for q in queries:
            stats["net"] = stats.get("net", 0) + 1
            r = _ncbi_get(base + "esearch.fcgi",
                          dict(common, term=q, retmax="10", retmode="json",
                               reldate="4000", datetype="pdat"))
            time.sleep(EMAIL_SLEEP)
            ids = ((r.json().get("esearchresult") or {}).get("idlist")) or []
            if verbose:
                log(f"    pubmed q=[{q}] -> {len(ids)} pmid(s)")
            if not ids:
                continue
            stats["net"] += 1
            r2 = _ncbi_get(base + "efetch.fcgi",
                           dict(common, id=",".join(ids), retmode="xml"))
            time.sleep(EMAIL_SLEEP)
            email = _extract_edu_email(r2.text, first, last)
            if verbose:
                log(f"    -> {email or 'no matching .edu email in affiliations'}")
            if email:
                return email
        return ""
    except Exception as exc:
        log(f"  PubMed lookup errored for {first} {last}: {exc}")
        return None


def lookup_pi_email(pi: str, university: str, appl: str, cache: dict,
                    stats: dict, reporter: "ReporterEmailSource",
                    verbose: bool = False):
    """Return (email, source). Cached forever; misses retried after a while."""
    key = f"{pi}|{university}".lower()
    ent = cache.get(key)
    if ent is not None:
        if ent.get("email"):
            return ent["email"], ent.get("src", "")
        try:
            checked = date.fromisoformat(ent.get("checked", "1970-01-01"))
        except ValueError:
            checked = date(1970, 1, 1)
        if (date.today() - checked).days < EMAIL_RETRY_DAYS:
            return "", ""
    email, src = reporter.get(appl, stats), "RePORTER"
    if not email:
        parts = pi.split()
        if len(parts) >= 2:
            pm = _pubmed_email(parts[0], parts[-1], university, stats, verbose)
            if pm is None:  # transient error: don't cache, retry next run
                return "", ""
            email, src = pm, "PubMed"
        else:
            email = ""
    if not email:
        src = ""
    cache[key] = {"email": email, "src": src,
                  "checked": date.today().isoformat()}
    return email, src


def sweep_pi_emails(data):
    """Fill missing PI emails, newest weeks first. Progress-logged and
    checkpointed so an interrupted run keeps its work."""
    if not ENABLE_EMAIL_LOOKUP:
        return data
    cache = load_json(email_cache_path(), {})
    # One-time flush (v2): earlier versions cached transient errors as
    # 45-day misses. Drop all cached misses once so those PIs get a clean
    # retry; confirmed hits are kept.
    if (cache.get("_meta") or {}).get("v") != 3:
        dropped = sum(1 for k, v in cache.items()
                      if isinstance(v, dict) and not v.get("email"))
        cache = {k: v for k, v in cache.items()
                 if isinstance(v, dict) and v.get("email")}
        cache["_meta"] = {"v": 3}
        if dropped:
            log(f"cleared {dropped} cached miss(es) so they retry now")
    stats = {"net": 0, "pis": 0}
    reporter = ReporterEmailSource()
    filled = {"RePORTER": 0, "PubMed": 0}
    changed = False
    pending = sum(
        1 for row in data["rows"].values() for entries in row["cells"].values()
        for e in entries if e.get("pi") and not e.get("pi_email"))
    if pending:
        log(f"PI email sweep: {pending} entries need emails "
            f"(up to {MAX_EMAIL_LOOKUPS_PER_RUN} PIs attempted this run)")

    def finish():
        save_json(email_cache_path(), cache)
        if changed:
            save_json(DATA_FILE, data)
        none_n = max(pending - filled["RePORTER"] - filled["PubMed"], 0)
        log(f"emails: reporter={filled['RePORTER']}, pubmed={filled['PubMed']}, "
            f"none={none_n} (PIs attempted: {stats['pis']}, "
            f"API calls: {stats['net']}, cache: {len(cache) - 1})")

    for wk in reversed(data["dates"]):
        for row in data["rows"].values():
            for e in row["cells"].get(wk, []):
                if stats["pis"] >= MAX_EMAIL_LOOKUPS_PER_RUN:
                    log("PI lookup cap reached; the rest continue next run")
                    finish()
                    return data
                if not e.get("pi") or e.get("pi_email"):
                    continue
                m = re.search(r"project-details/(\d+)", e.get("url", ""))
                appl = m.group(1) if m else ""
                ck = f"{e['pi']}|{row['university']}".lower()
                fresh = ck not in cache
                verbose = fresh and stats["pis"] < 3  # trace the first few
                if verbose:
                    log(f"  lookup: {e['pi']} @ {row['university']} "
                        f"(appl {appl or 'n/a'})")
                em, src = lookup_pi_email(e["pi"], row["university"], appl,
                                          cache, stats, reporter, verbose)
                if fresh:
                    stats["pis"] += 1
                if em:
                    e["pi_email"] = em
                    e["pi_email_via"] = src
                    filled[src] = filled.get(src, 0) + 1
                    changed = True
                if fresh:
                    if stats["pis"] % 25 == 0:
                        log(f"  progress: {stats['pis']} PIs attempted, "
                            f"{sum(filled.values())} emails found")
                    if stats["pis"] % 50 == 0:
                        save_json(email_cache_path(), cache)
                        if changed:
                            save_json(DATA_FILE, data)
    finish()
    return data


# ----------------------------------------------------------------------------
# NIH fetch: awards NOTICED during the target week
# ----------------------------------------------------------------------------

def make_award(key, university, department, amount, url, funder, pi,
               pi_email=""):
    return {
        "key": key, "university": university, "department": department,
        "funder": funder, "amount": int(amount), "url": url, "pi": pi,
        "pi_email": pi_email,
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
                "PrincipalInvestigators",
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
                pi_email=api_email,
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

def _cell_html(entries):
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
    lines += f'<div class="pib">PI Name: {pi_name}<br>PI Email: {pi_mail}</div>'
    return f'<td data-v="{total}">{lines}</td>'


def render_html(data) -> str:
    weeks = data["dates"]
    latest = weeks[-1] if weeks else None
    rows = sorted(
        data["rows"].values(),
        key=lambda r: (r["university"].lower(), r.get("funder", "").lower(),
                       r["department"].lower()),
    )

    total_latest = sum(e["amount"]
                       for r in rows for e in r["cells"].get(latest, []))
    n_awards = sum(len(v) for r in rows for v in r["cells"].values())

    head_cells = "".join(
        f'<th class="num{" today" if w == latest else ""}" data-t="num" '
        f'title="{w}">{wlabel(w)}</th>' for w in weeks)

    body = []
    for i, r in enumerate(rows, start=1):
        has_email = any(e.get("pi_email")
                        for v in r["cells"].values() for e in v)
        cells = "".join(_cell_html(r["cells"].get(w, [])) for w in weeks)
        body.append(
            f'<tr data-he="{1 if has_email else 0}">'
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
  margin-top:9px; font-size:13px; color:var(--ink); cursor:pointer;
  user-select:none; }}
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
th.today {{ background:var(--broth); }}
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
    <div class="fig">{fmt_money(total_latest)}</div>
    <div class="cap">granted {wlabel(latest) if latest else "-"}</div>
    <label class="emailchk"><input type="checkbox" id="onlyemail">
      Only show rows with a PI email</label>
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
    const ok = hay.includes(v) && (!only.checked || r.dataset.he === '1');
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
    assert _extract_edu_email(xml, "Jane", "Big") == "jane_big@brown.edu"
    assert _extract_edu_email(xml, "Zed", "Nowhere") == ""

    # email sweep with stubbed lookup (no network) + cache-flush check
    save_json(email_cache_path(), {
        "old hit|somewhere": {"email": "keep@x.edu", "src": "PubMed",
                              "checked": "2026-09-01"},
        "poisoned miss|somewhere": {"email": "", "src": "",
                                    "checked": "2026-09-01"},
    })
    orig = globals()["lookup_pi_email"]
    globals()["lookup_pi_email"] = (
        lambda pi, uni, appl, cache, stats, rep, verbose=False:
        (("jbig@brown.edu", "RePORTER") if pi == "Jane Big" else ("", "")))
    data = sweep_pi_emails(data)
    globals()["lookup_pi_email"] = orig
    assert data["rows"]["brown university||biomedical engineering||nih (nigms)"][
        "cells"]["2026-09-06"][0]["pi_email"] == "jbig@brown.edu"
    flushed = load_json(email_cache_path(), {})
    assert "old hit|somewhere" in flushed, "flush must keep confirmed hits"
    assert "poisoned miss|somewhere" not in flushed, "flush must drop misses"
    assert flushed.get("_meta", {}).get("v") == 3

    write_site(data)
    page = (DOCS_DIR / "index.html").read_text()
    assert page.count("<th ") == 5, "expected 3 label ths + 2 week ths (# th has no attrs)"
    assert "Sep 6 \u2013 Sep 12" in page and "Sep 13 \u2013 Sep 19" in page
    assert '<td class="rownum">1</td>' in page and '<td class="rownum">3</td>' in page
    assert 'id="onlyemail"' in page and 'data-he="1"' in page and 'data-he="0"' in page
    assert '<div class="tot">= $5,100,000</div>' in page
    assert "PI Name: Jane Big" in page and 'href="mailto:jbig@brown.edu"' in page
    assert 'title="source: RePORTER"' in page
    assert "PI Email: --" in page
    assert 'data-v="0">0<' in page
    assert "renumber" in page and "r.cells[0].textContent" in page

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
                    help="skip the PI-email sweep this run")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.skip_emails:
        ENABLE_EMAIL_LOOKUP = False

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
