# Biotech Department Funding Tracker (weekly, NIH-only)

A self-updating ledger of NIH awards granted each week to biotech-adjacent
university departments. Runs automatically **every Monday morning** on
GitHub's servers (right after NIH RePORTER's Sunday-night refresh) and
publishes a sortable, shareable table to GitHub Pages — no software needed
on your computer.

**Live page:** `https://sartoriusfunding.github.io/funding-tracker/`

## How it works

- Columns are completed **Sunday–Saturday weeks** ("Sep 6 – Sep 12"). An
  award lands in the week of its NIH **award-notice date**, exactly once —
  never double-counted. Each Monday run adds the just-finished week.
- Rows: **#** (position number that always follows the current sort/filter —
  the visible top row is always 1), University, Department (NIH's
  standardized name, or `--` when NIH reported none and the award matched
  the bio keywords), and Funder (the NIH institute, e.g. *NIH (NIGMS)*).
- Cells stack each award's amount (hyperlinked to its RePORTER project
  page), show a ruled unlinked **= total** when there are several, and list
  the contact **PI Name / PI Email / Institution** of the cell's largest
  award (institution = the awardee organization NIH lists).
- **Emails**, in order: (1) NIH's own project record — the same email the
  "View Email" button on a project page reveals; (2) the PI's recent PubMed
  publications (`.edu` addresses containing the PI's name); (3) `--` if
  neither had one. Hover an email to see its source. All lookups are cached
  in `data/pi_email_cache.json`.
- Top-right checkboxes: **Only show rows with a PI email / a PI name / an
  institution** — combinable; a row must satisfy every checked box. The search box filters by university, department,
  or funder. Every column header sorts (⇅ → ▲/▼).
- Scope: clinical/medical departments and companies (SBIR/STTR) are
  excluded; tune the include-list and keywords in the CONFIG block at the
  top of `funding_tracker.py`.

## Setup / updating

Browser-only, in the `funding-tracker` repo (account **SartoriusFunding**):

1. Replace `funding_tracker.py` and `.github/workflows/update.yml` with the
   current versions (open file → pencil icon → select-all → paste → *Commit
   directly to main*). Replace `requirements.txt` too (it's now just
   `requests`).
2. Actions → **Weekly funding tracker** → *Run workflow* once. The first run
   under this version automatically clears the old daily-format data and
   builds the most recently completed week; after that, Mondays are
   automatic. Old-format data needs no manual deletion.
3. Page refreshes ~1 minute after the run's green check (Ctrl+F5).

**Run time:** the workflow runs in two stages. Stage 1 pulls the week's
awards and publishes the page in ~2 minutes. Stage 2 looks up emails for
PIs never seen before (several in parallel, within NCBI's rate limit) and
publishes again — roughly 5–10 minutes on a normal Monday. Re-running on
the same week is quick: every PI already has a cached result, and only
lookups that hit a transient error are retried. The log ends with
`emails: reporter=X, pubmed=Y, none=Z`; if `reporter` sits at 0 with
repeated endpoint errors, report that log line for a fix.

**Notes:** the RePORTER project-info endpoint used for emails is
undocumented — if NIH changes it, the run logs it and PubMed carries on.
Weekly commits keep the schedule alive (GitHub disables schedules only
after 60 days with no commits). Optional: a free NCBI API key as an Actions
secret `NCBI_API_KEY` speeds the PubMed fallback. Offline test:
`python funding_tracker.py --selftest`.
