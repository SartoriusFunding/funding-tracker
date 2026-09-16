# Biotech Department Funding Tracker

A self-updating ledger of new research funding awarded to biotech-adjacent
university departments. Runs automatically every day **on GitHub's own
servers** (GitHub Actions) and publishes a sortable, shareable table to
GitHub Pages — no Python or software needed on your computer, ever.

**Live page (after setup):** `https://sartoriusfunding.github.io/funding-tracker/`

## How it works

- Column A = University, Column B = Department, and every run date becomes a
  new column. Old columns are never touched.
- An award appears **only on the day it is first detected** (tracked forever in
  `data/seen_awards.json`), so nothing is ever double-counted. Days with no new
  money show **0** — normal, since NIH RePORTER refreshes weekly.
- Amounts are hyperlinked to their source (NIH project page, NSF award page,
  USAspending record, or press release).
- Amber `~` amounts are best-effort parses from press coverage (Tier 2) —
  click through before quoting them.
- Click any header to sort: University/Department alphabetically, date columns
  by amount (descending first, click again for ascending). The search box
  filters rows.

**Sources.** Tier 1 (APIs): NIH RePORTER (the only source with real department
names), NSF, USAspending (ARPA-H, ASPR/BARDA, USDA NIFA, DOE Office of
Science, Army MRAA). Tier 2 (best effort): CPRIT, EurekAlert RSS, Google News
RSS. Clinical/medical departments are excluded by the include-list at the top
of `funding_tracker.py` — edit that config block to tune scope.

## Setup (one time, ~10 minutes, all in the browser)

Do this logged in as **SartoriusFunding** on github.com. Make sure the
account's email address is verified first — GitHub won't run Actions on an
unverified account.

1. **Create the repo.** Top-right **+** → *New repository* → name it
   `funding-tracker` → **Public** → tick *Add a README file* → Create.
2. **Upload the three root files.** *Add file → Upload files* → drag in
   `funding_tracker.py`, `requirements.txt`, and this `README.md` → *Commit
   changes* (it's fine that README gets replaced).
3. **Add the workflow.** *Add file → Create new file* → in the name box type
   exactly `.github/workflows/update.yml` (typing the slashes creates the
   folders) → paste the contents of `update.yml` → *Commit changes*.
4. **First run.** *Actions* tab → click **Daily funding tracker** in the left
   sidebar → **Run workflow** → green *Run workflow* button. It takes ~2–3
   minutes: it backfills the recent lookback window (NIH 10 days, NSF 14,
   USAspending 45) into the first column and commits `data/` + `docs/`.
   Click into the run and expand the *Run tracker* step to see per-source
   counts.
5. **Turn on Pages.** Settings → Pages → Source: *Deploy from a branch* →
   Branch: `main`, Folder: `/docs` → Save. (Do this *after* step 4 — the
   `/docs` folder only exists once the first run has committed it.)
6. **Open the page.** After ~1 minute, visit
   `https://sartoriusfunding.github.io/funding-tracker/` — that's the link to
   share with colleagues.

From then on it runs by itself every morning at 09:17 UTC (~5:17 AM ET).
No laptop needed.

## Maintenance notes

- **Keepalive:** GitHub disables scheduled workflows in public repos after 60
  days without commits. The daily data commit normally keeps it alive; if the
  Actions tab ever shows the schedule disabled, one click re-enables it.
- **Tuning:** all filters (NIH department include-list, bio keywords, NSF
  divisions, USAspending agencies, news queries) sit in the CONFIG block at
  the top of `funding_tracker.py`. If a USAspending agency logs 0 results
  forever, its name string likely needs adjusting there.
- **Resilience:** every source is wrapped independently — one failing (Tier-2
  scrapers especially) never blocks the rest.
- **Testing:** `python funding_tracker.py --selftest` runs an offline
  simulation (dedup, column preservation, rendering) with no network — only
  needed if you edit the code, and can run on any machine with Python (e.g.
  your personal laptop).
