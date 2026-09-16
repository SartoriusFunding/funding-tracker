#!/usr/bin/env python3
"""
Biotech Department Funding Tracker
==================================
Runs once a day (GitHub Actions) and maintains a ledger of NEW research funding
awarded to biotech-adjacent university departments.

  Column A = University        Column B = Department
  Every run date = one new column. An award appears ONLY on the day it is
  first seen (deduplicated forever via data/seen_awards.json). Cells with no
  new money show 0. Old columns are never rewritten.

Sources
  Tier 1 (structured APIs) : NIH RePORTER, NSF Awards API, USAspending
                             (ARPA-H / ASPR-BARDA / USDA NIFA / DOE Office of
                             Science / Army medical research)
  Tier 2 (best effort)     : CPRIT grants table, EurekAlert RSS,
                             Google News RSS  -> shown with a ~ mark

Outputs
  data/tracker_data.json   the ledger (source of truth, committed to repo)
  data/seen_awards.json    every award key ever emitted (dedup memory)
  docs/index.html          sortable/filterable page served by GitHub Pages

Usage
  python funding_tracker.py              normal daily run
  python funding_tracker.py --selftest   offline test with mock data (no network)
  python funding_tracker.py --tier1-only skip the scrapey Tier-2 sources
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG - edit these lists to tune what counts as "biotech adjacent"
# ----------------------------------------------------------------------------

# NIH RePORTER standardized dept_type values to INCLUDE (verbatim strings).
NIH_INCLUDE_DEPTS = [
    "BIOCHEMISTRY",
    "BIOMEDICAL ENGINEERING",
    "BIOPHYSICS",
    "BIOLOGY",
    "MICROBIOLOGY/IMMUN/VIROLOGY",
    "GENETICS",
    "ENGINEERING (ALL TYPES)",      # chemical/bioprocess eng lives here; extra keyword filter applied below
    "CHEMISTRY",
    "PHARMACOLOGY",
    "PHYSIOLOGY",
    "ANATOMY/CELL BIOLOGY",
    "NUTRITION",
    "VETERINARY SCIENCES",
]
# Clinical departments are excluded simply by not being in the include list.

# Awards from "ENGINEERING (ALL TYPES)" must match one of these keywords in the
# project title/terms, so civil/mechanical noise is dropped.
BIO_KEYWORDS = [
    "bio", "cell", "tissue", "protein", "gene", "genom", "rna", "dna",
    "microb", "ferment", "enzym", "vaccin", "antibod", "therapeut",
    "pharma", "drug", "organoid", "biomanufactur", "bioprocess", "biosens",
    "stem", "immun", "virus", "viral", "crispr", "molecul",
]

# NSF: include whole BIO directorate, these divisions, or CBET/ENG when a bio
# keyword is present in the program name or title.
NSF_INCLUDE_DIVISIONS = {"MCB", "DBI", "IOS", "EF"}
NSF_KEYWORD_DIVISIONS = {"CBET", "ENG", "EEC", "CMMI"}

# USAspending awarding sub-agencies to poll (each queried independently, so a
# bad name only disables itself). Tune names here if a query logs 0 forever.
USASPENDING_AGENCIES = [
    "Advanced Research Projects Agency for Health",
    "Administration for Strategic Preparedness and Response",
    "National Institute of Food and Agriculture",
    "Office of Science",
    "U.S. Army Medical Research Acquisition Activity",
]

# Recipient must look like a university/college for NSF & USAspending rows.
UNIVERSITY_MARKERS = ("UNIVERSITY", "COLLEGE", "INSTITUTE", "POLYTECH",
                      "SCHOOL OF", "ACADEMY OF")

# Lookback windows (days). Overlap is harmless - dedup catches repeats.
NIH_LOOKBACK = 10          # RePORTER refreshes weekly
NSF_LOOKBACK = 14
USASPENDING_LOOKBACK = 45  # federal reporting lag is 2 weeks - 90 days

GOOGLE_NEWS_QUERIES = [
    'university (grant OR award) (biotechnology OR bioengineering OR "biomedical engineering" OR biomanufacturing)',
    'university "million" grant (bioprocessing OR "cell therapy" OR "gene therapy" OR "synthetic biology")',
    '(ARPA-H OR "V Foundation" OR HHMI OR CPRIT) university award',
]
EUREKALERT_FEEDS = [
    "https://www.eurekalert.org/rss/grants.xml",
    "https://www.eurekalert.org/rss.xml",
]
CPRIT_URL = "https://www.cprit.texas.gov/grants-funded"

MIN_AWARD_AMOUNT = 1  # ignore $0 records

# ----------------------------------------------------------------------------
# Paths & state
# ----------------------------------------------------------------------------

HOME = Path(os.environ.get("TRACKER_HOME", Path(__file__).resolve().parent))
DATA_DIR = HOME / "data"
DOCS_DIR = HOME / "docs"
DATA_FILE = DATA_DIR / "tracker_data.json"
SEEN_FILE = DATA_DIR / "seen_awards.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "biotech-funding-tracker/1.0 (personal research tool)"})
TIMEOUT = 40


def log(msg: str) -> None:
    print(f"[tracker] {msg}", flush=True)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:  # corrupted file should not kill the run
            log(f"WARNING could not read {path.name}: {e}")
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True))


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def nice_name(s: str) -> str:
    """UNIVERSITY OF X -> University of X (keeps small words lower)."""
    small = {"of", "at", "and", "the", "for", "in", "on"}
    out = []
    for i, w in enumerate(re.split(r"\s+", s.strip())):
        lw = w.lower()
        out.append(lw if (lw in small and i > 0) else lw.capitalize())
    return " ".join(out)


def fmt_money(n: int) -> str:
    return f"${n:,.0f}"


MONEY_RE = re.compile(r"\$\s?([0-9][0-9,\.]*)\s*(billion|million|bn|mn|[MmBb])?\b")


def parse_money(text: str):
    """Return (amount_int, label) for the first dollar figure in text, else None."""
    m = MONEY_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "bn", "b"):
        val *= 1e9
    elif unit in ("million", "mn", "m"):
        val *= 1e6
    if val < 1000:  # "$5" style noise
        return None
    prefix = "up to " if re.search(r"up to\s*$", text[: m.start()], re.I) else ""
    return int(val), prefix + fmt_money(int(val))


UNI_PATTERNS = [
    re.compile(r"University of [A-Z][A-Za-z&.'\-]+(?:[ ,] ?[A-Z][A-Za-z&.'\-]+)*"),
    re.compile(r"[A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+)* University"),
    re.compile(r"[A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+)* Institute of Technology"),
    re.compile(r"(?:[A-Z][A-Za-z&.'\-]+ )+College\b"),
]


def find_university(text: str):
    for pat in UNI_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip(" ,.")
    return None


def looks_like_university(name: str) -> bool:
    up = name.upper()
    return any(marker in up for marker in UNIVERSITY_MARKERS)


def has_bio_keyword(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in BIO_KEYWORDS)


def content_key(prefix: str, text: str) -> str:
    return f"{prefix}:{hashlib.sha1(text.lower().encode()).hexdigest()[:16]}"


def make_award(key, university, department, amount, url, source, approx=False, label=None):
    return {
        "key": key,
        "university": university,
        "department": department,
        "amount": int(amount),
        "label": label or fmt_money(int(amount)),
        "url": url,
        "source": source,
        "approx": bool(approx),
    }


# ----------------------------------------------------------------------------
# TIER 1 FETCHERS
# ----------------------------------------------------------------------------

def fetch_nih(today: date):
    """NIH RePORTER v2 - the only source with true department names."""
    awards, offset = [], 0
    frm = (today - timedelta(days=NIH_LOOKBACK)).isoformat()
    while offset <= 9500:
        payload = {
            "criteria": {
                "date_added": {"from_date": frm, "to_date": today.isoformat()},
                "dept_types": NIH_INCLUDE_DEPTS,
                "org_countries": ["UNITED STATES"],
                "exclude_subprojects": True,
            },
            "include_fields": [
                "ApplId", "ProjectNum", "ProjectTitle", "AwardAmount",
                "Organization", "AwardNoticeDate", "DateAdded",
                "ProjectDetailUrl", "PrefTerms",
            ],
            "limit": 500,
            "offset": offset,
        }
        r = SESSION.post("https://api.reporter.nih.gov/v2/projects/search",
                         json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results", [])
        for p in results:
            amt = p.get("award_amount") or 0
            org = p.get("organization") or {}
            dept = (org.get("dept_type") or "").strip()
            name = (org.get("org_name") or "").strip()
            if amt < MIN_AWARD_AMOUNT or not name or dept not in NIH_INCLUDE_DEPTS:
                continue
            if dept == "ENGINEERING (ALL TYPES)" and not has_bio_keyword(
                    (p.get("project_title") or "") + " " + (p.get("pref_terms") or "")):
                continue
            appl = p.get("appl_id")
            url = p.get("project_detail_url") or f"https://reporter.nih.gov/project-details/{appl}"
            awards.append(make_award(
                key=f"NIH:{appl}",
                university=nice_name(name),
                department=nice_name(dept),
                amount=amt, url=url, source="NIH",
            ))
        if len(results) < 500:
            break
        offset += 500
        time.sleep(1)  # NIH asks <= 1 request/second
    return awards


def _classify_nsf(rec) -> str | None:
    """Return a department-proxy label if the NSF award is biotech-adjacent."""
    d, div = rec.get("dirAbbr", ""), rec.get("divAbbr", "")
    prog = rec.get("fundProgramName") or ""
    text = prog + " " + (rec.get("title") or "")
    if d == "BIO" or div in NSF_INCLUDE_DIVISIONS:
        pass
    elif div in NSF_KEYWORD_DIVISIONS and has_bio_keyword(text):
        pass
    else:
        return None
    label = nice_name(prog) if prog else (div or d or "NSF")
    return f"NSF program: {label}"


def fetch_nsf(today: date):
    """NSF Awards API (research.gov-backed). No dept field -> program proxy."""
    awards = []
    params_base = {
        "dateStart": (today - timedelta(days=NSF_LOOKBACK)).strftime("%m/%d/%Y"),
        "dateEnd": today.strftime("%m/%d/%Y"),
        "printFields": ",".join([
            "id", "title", "awardeeName", "fundsObligatedAmt",
            "estimatedTotalAmt", "date", "dirAbbr", "divAbbr",
            "fundProgramName", "cfdaNumber",
        ]),
    }
    offset = 1
    for _ in range(60):  # up to 1500 records
        params = dict(params_base, offset=offset)
        r = SESSION.get("https://api.nsf.gov/services/v1/awards.json",
                        params=params, timeout=TIMEOUT)
        r.raise_for_status()
        recs = (r.json().get("response") or {}).get("award", []) or []
        for rec in recs:
            dept_label = _classify_nsf(rec)
            name = rec.get("awardeeName") or ""
            if not dept_label or not looks_like_university(name):
                continue
            try:
                amt = int(float(rec.get("fundsObligatedAmt")
                                or rec.get("estimatedTotalAmt") or 0))
            except (TypeError, ValueError):
                amt = 0
            if amt < MIN_AWARD_AMOUNT:
                continue
            aid = rec.get("id")
            awards.append(make_award(
                key=f"NSF:{aid}",
                university=nice_name(name),
                department=dept_label,
                amount=amt,
                url=f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={aid}",
                source="NSF",
            ))
        if len(recs) < 25:
            break
        offset += 25
        time.sleep(1)
    return awards


def fetch_usaspending(today: date):
    """USAspending v2 - ARPA-H, BARDA/ASPR, NIFA, DOE-SC, Army med. No dept."""
    awards = []
    start = (today - timedelta(days=USASPENDING_LOOKBACK)).isoformat()
    for agency in USASPENDING_AGENCIES:
        # grants for everyone; ARPA-H mostly issues Other Transactions
        type_codes = ["02", "03", "04", "05"]
        if "Advanced Research Projects" in agency:
            type_codes += ["09", "11", "-1"]
        page, got = 1, 0
        try:
            while page <= 10:
                payload = {
                    "filters": {
                        "time_period": [{"start_date": start,
                                         "end_date": today.isoformat()}],
                        "agencies": [{"type": "awarding", "tier": "subtier",
                                      "name": agency}],
                        "award_type_codes": type_codes,
                    },
                    "fields": ["Award ID", "Recipient Name", "Award Amount",
                               "Description", "Start Date",
                               "Awarding Sub Agency", "generated_internal_id"],
                    "limit": 100, "page": page,
                }
                r = SESSION.post(
                    "https://api.usaspending.gov/api/v2/search/spending_by_award/",
                    json=payload, timeout=TIMEOUT)
                r.raise_for_status()
                rows = r.json().get("results", [])
                for row in rows:
                    name = row.get("Recipient Name") or ""
                    desc = row.get("Description") or ""
                    if not looks_like_university(name):
                        continue
                    if not has_bio_keyword(desc) and "Advanced Research Projects" not in agency:
                        continue  # ARPA-H is biotech by definition; others need keywords
                    try:
                        amt = int(float(row.get("Award Amount") or 0))
                    except (TypeError, ValueError):
                        amt = 0
                    if amt < MIN_AWARD_AMOUNT:
                        continue
                    gii = row.get("generated_internal_id")
                    key_id = gii or row.get("Award ID") or desc[:40]
                    url = (f"https://www.usaspending.gov/award/{gii}" if gii
                           else "https://www.usaspending.gov/search")
                    sub = row.get("Awarding Sub Agency") or agency
                    awards.append(make_award(
                        key=f"USA:{key_id}",
                        university=nice_name(name),
                        department=f"{sub} award (dept n/a)",
                        amount=amt, url=url, source="USAspending",
                    ))
                    got += 1
                if len(rows) < 100:
                    break
                page += 1
                time.sleep(1)
            log(f"  USAspending [{agency}]: {got} candidate rows")
        except Exception as e:
            log(f"  USAspending [{agency}] failed, skipping: {e}")
    return awards


# ----------------------------------------------------------------------------
# TIER 2 FETCHERS (best effort - every failure is non-fatal)
# ----------------------------------------------------------------------------

def _news_items_to_awards(items, prefix, source_name):
    awards = []
    for title, link in items:
        money = parse_money(title)
        uni = find_university(title)
        if not money or not uni:
            continue
        amt, label = money
        awards.append(make_award(
            key=content_key(prefix, title),
            university=uni,
            department="Press release (dept n/a)",
            amount=amt, url=link, source=source_name,
            approx=True, label="~" + label,
        ))
    return awards


def fetch_google_news(_today: date):
    import feedparser
    items = []
    for q in GOOGLE_NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en")
        feed = feedparser.parse(url)
        for e in feed.entries[:40]:
            items.append((e.get("title", ""), e.get("link", "")))
        time.sleep(1)
    return _news_items_to_awards(items, "NEWS", "Google News")


def fetch_eurekalert(_today: date):
    import feedparser
    items = []
    for feed_url in EUREKALERT_FEEDS:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            continue
        for e in feed.entries[:60]:
            text = e.get("title", "")
            if re.search(r"grant|award|\$", text, re.I):
                items.append((text, e.get("link", "")))
        break  # first feed that works is enough
    return _news_items_to_awards(items, "EA", "EurekAlert")


def fetch_cprit(_today: date):
    from bs4 import BeautifulSoup
    r = SESSION.get(CPRIT_URL, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    awards = []
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        gid = re.search(r"\b(?:RP|RR|DP|PP|PR)\d{6}\b", text)
        money = parse_money(text)
        if not gid or not money:
            continue
        uni = find_university(text)
        if not uni:
            continue
        amt, label = money
        a = tr.find("a", href=True)
        url = urllib.parse.urljoin(CPRIT_URL, a["href"]) if a else CPRIT_URL
        awards.append(make_award(
            key=f"CPRIT:{gid.group(0)}",
            university=uni,
            department="CPRIT award (dept n/a)",
            amount=amt, url=url, source="CPRIT", approx=False, label=label,
        ))
    return awards


TIER1 = [("NIH RePORTER", fetch_nih), ("NSF", fetch_nsf),
         ("USAspending", fetch_usaspending)]
TIER2 = [("CPRIT", fetch_cprit), ("EurekAlert", fetch_eurekalert),
         ("Google News", fetch_google_news)]


def collect_awards(today: date, tier1_only=False):
    all_awards = []
    for name, fn in TIER1 + ([] if tier1_only else TIER2):
        try:
            got = fn(today)
            log(f"{name}: {len(got)} award(s) matched filters")
            all_awards.extend(got)
        except Exception as e:
            log(f"{name} FAILED (continuing without it): {e}")
    return all_awards


# ----------------------------------------------------------------------------
# LEDGER: merge today's new awards, never touch history
# ----------------------------------------------------------------------------

def row_key(a) -> str:
    return f"{a['university'].lower()}||{a['department'].lower()}"


def ingest(awards, run_date: str):
    """Dedup against seen_awards, write new ones into today's column."""
    data = load_json(DATA_FILE, {"dates": [], "rows": {}})
    seen = load_json(SEEN_FILE, {})

    if run_date not in data["dates"]:
        data["dates"].append(run_date)
        data["dates"].sort()

    new_count = 0
    for a in awards:
        if a["key"] in seen:
            continue
        seen[a["key"]] = {"first_seen": run_date, "amount": a["amount"],
                          "university": a["university"]}
        rk = row_key(a)
        row = data["rows"].setdefault(rk, {
            "university": a["university"],
            "department": a["department"],
            "cells": {},
        })
        row["cells"].setdefault(run_date, []).append({
            "amount": a["amount"], "label": a["label"], "url": a["url"],
            "source": a["source"], "approx": a["approx"],
        })
        new_count += 1

    save_json(DATA_FILE, data)
    save_json(SEEN_FILE, seen)
    log(f"ingested {new_count} new award(s); ledger now has "
        f"{len(data['rows'])} rows x {len(data['dates'])} dates")
    return data, new_count


# ----------------------------------------------------------------------------
# HTML RENDER
# ----------------------------------------------------------------------------

def _cell_html(entries):
    if not entries:
        return '<td class="zero" data-v="0">0</td>'
    total = sum(e["amount"] for e in entries)
    links = " + ".join(
        f'<a href="{html_lib.escape(e["url"], quote=True)}" target="_blank" rel="noopener" '
        f'class="{"approx" if e["approx"] else "amt"}" '
        f'title="{html_lib.escape(e["source"])}">{html_lib.escape(e["label"])}</a>'
        for e in entries
    )
    return f'<td data-v="{total}">{links}</td>'


def render_html(data) -> str:
    dates = data["dates"]
    latest = dates[-1] if dates else None
    rows = sorted(data["rows"].values(), key=lambda r: r["university"].lower())

    total_today = sum(e["amount"]
                      for r in rows for e in r["cells"].get(latest, []))
    n_awards = sum(len(v) for r in rows for v in r["cells"].values())

    def dlabel(iso):
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d:%b} {d.day}"

    head_cells = "".join(
        f'<th class="num{" today" if d == latest else ""}" data-t="num" '
        f'title="{d}">{dlabel(d)}</th>' for d in dates)

    body = []
    for r in rows:
        cells = "".join(_cell_html(r["cells"].get(d, [])) for d in dates)
        body.append(
            "<tr>"
            f'<td class="uni">{html_lib.escape(r["university"])}</td>'
            f'<td class="dept">{html_lib.escape(r["department"])}</td>'
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
  --grow:#1B7A43; --broth:#EAF6EF; --caution:#A5670A;
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
  white-space:nowrap; text-align:right; font-size:14px; }}
th {{ position:sticky; top:0; background:var(--bg); z-index:3; cursor:pointer;
  font-weight:500; user-select:none; }}
th:hover {{ color:var(--grow); }}
th.sorted {{ box-shadow:inset 0 -2px 0 var(--grow); color:var(--grow); }}
th.today {{ background:var(--broth); }}
th:nth-child(1), td.uni {{ position:sticky; left:0; background:var(--bg);
  text-align:left; min-width:230px; max-width:300px; white-space:normal;
  z-index:2; font-weight:500; }}
th:nth-child(2), td.dept {{ position:sticky; left:230px; background:var(--bg);
  text-align:left; min-width:220px; max-width:280px; white-space:normal;
  z-index:2; color:#3C444C; }}
th:nth-child(1), th:nth-child(2) {{ z-index:4; }}
td[data-v]:not(.zero) {{ background:#fff; }}
tr:hover td {{ background:#F3F7F4; }}
.zero {{ color:var(--mut); }}
a.approx {{ color:var(--caution); border-bottom-color:#E4CDA5; }}
.legend {{ padding:12px 32px 40px; color:var(--mut); font-size:12.5px; }}
.legend .approx {{ color:var(--caution); }}
</style></head><body>

<div class="mast">
  <div>
    <h1>Biotech department funding ledger</h1>
    <div class="sub">New research awards to biotech-adjacent university
      departments &middot; updated {generated} &middot; {len(rows)} rows &middot;
      {n_awards} awards tracked</div>
  </div>
  <div class="todaybox">
    <div class="fig">{fmt_money(total_today)}</div>
    <div class="cap">new funding first seen {dlabel(latest) if latest else "-"}</div>
  </div>
</div>

<div class="controls">
  <input id="q" type="search" placeholder="Filter by university or department"
    aria-label="Filter rows">
  <span>Click any column header to sort &middot; click again to reverse</span>
</div>

<div class="wrap"><table id="t">
<thead><tr>
  <th data-t="text">University</th>
  <th data-t="text">Department</th>
  {head_cells}
</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div>

<div class="legend">Every dated column is one run of the tracker; an award
appears only on the day it was first detected, so amounts are never
double-counted. 0 = no new funding detected for that row that day.
<span class="approx">~ amber figures</span> are estimates parsed from press
coverage (source unverified) &mdash; click through before quoting them.
Sources: NIH RePORTER, NSF, USAspending (ARPA-H, ASPR/BARDA, NIFA, DOE-SC,
Army MRAA), CPRIT, EurekAlert, Google News.</div>

<script>
const table = document.getElementById('t');
const tbody = table.tBodies[0];
const ths = [...table.tHead.rows[0].cells];
let cur = {{ i:-1, dir:1 }};
ths.forEach((th, i) => th.addEventListener('click', () => {{
  const num = th.dataset.t === 'num';
  cur.dir = (cur.i === i) ? -cur.dir : (num ? -1 : 1); // amounts: desc first
  cur.i = i;
  ths.forEach(h => h.classList.remove('sorted'));
  th.classList.add('sorted');
  const rows = [...tbody.rows];
  rows.sort((a, b) => {{
    if (num) {{
      return ((+a.cells[i].dataset.v || 0) - (+b.cells[i].dataset.v || 0)) * cur.dir;
    }}
    return a.cells[i].innerText.localeCompare(b.cells[i].innerText) * cur.dir;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}));
document.getElementById('q').addEventListener('input', e => {{
  const v = e.target.value.toLowerCase();
  [...tbody.rows].forEach(r => {{
    const hay = (r.cells[0].innerText + ' ' + r.cells[1].innerText).toLowerCase();
    r.style.display = hay.includes(v) ? '' : 'none';
  }});
}});
</script>
</body></html>"""


def write_site(data) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(render_html(data))
    (DOCS_DIR / ".nojekyll").write_text("")
    log(f"wrote {DOCS_DIR/'index.html'}")


# ----------------------------------------------------------------------------
# Self-test: two simulated days, no network
# ----------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    global HOME, DATA_DIR, DOCS_DIR, DATA_FILE, SEEN_FILE
    tmp = Path(tempfile.mkdtemp(prefix="tracker_test_"))
    HOME, DATA_DIR, DOCS_DIR = tmp, tmp / "data", tmp / "docs"
    DATA_FILE, SEEN_FILE = DATA_DIR / "tracker_data.json", DATA_DIR / "seen_awards.json"

    day1 = [
        make_award("NIH:111", "Brown University", "Biomedical Engineering",
                   4_600_000, "https://reporter.nih.gov/project-details/111", "NIH"),
        make_award("NSF:222", "Tufts University", "NSF program: Cellular Biosciences",
                   750_000, "https://www.nsf.gov/awardsearch/showAward?AWD_ID=222", "NSF"),
    ]
    data, n1 = ingest(day1, "2026-09-15")
    assert n1 == 2 and len(data["rows"]) == 2, "day 1 ingest failed"

    day2 = [
        day1[0],  # duplicate: must NOT appear again
        make_award("USA:333", "Brown University",
                   "Advanced Research Projects Agency for Health award (dept n/a)",
                   1_000_000, "https://www.usaspending.gov/award/x", "USAspending"),
        make_award("NEWS:444", "University of Vermont", "Press release (dept n/a)",
                   2_000_000, "https://example.com/story", "Google News",
                   approx=True, label="~$2,000,000"),
    ]
    data, n2 = ingest(day2, "2026-09-16")
    assert n2 == 2, f"dedup failed, ingested {n2}"
    assert data["dates"] == ["2026-09-15", "2026-09-16"], "date columns wrong"
    brown_bme = data["rows"]["brown university||biomedical engineering"]
    assert "2026-09-16" not in brown_bme["cells"], "duplicate re-emitted!"
    assert brown_bme["cells"]["2026-09-15"][0]["amount"] == 4_600_000, "history changed!"

    write_site(data)
    page = (DOCS_DIR / "index.html").read_text()
    assert 'data-v="0">0<' in page, "zero cells missing"
    assert "reporter.nih.gov/project-details/111" in page, "hyperlink missing"
    assert "~$2,000,000" in page and "approx" in page, "tier-2 flag missing"
    assert page.count("<th ") == 2 + 2, "expected 2 date columns + 2 label columns"

    # rerun same day: nothing new, no duplicate column
    data, n3 = ingest(day2, "2026-09-16")
    assert n3 == 0 and data["dates"].count("2026-09-16") == 1, "same-day rerun broke"

    m = parse_money("awarded up to $39.2 million for RNA work")
    assert m == (39_200_000, "up to $39,200,000"), f"money parse: {m}"
    assert find_university("MIT teams with Boston University on grant") == "Boston University"

    print(f"SELFTEST PASSED  (artifacts in {tmp})")
    return 0


# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tier1-only", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    today = date.today().isoformat()
    log(f"run date {today}")
    awards = collect_awards(date.today(), tier1_only=args.tier1_only)
    data, _ = ingest(awards, today)
    write_site(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
