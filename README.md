# Bounty Scope Tracker

Ranks every public program on **HackerOne, YesWeHack and Intigriti** by
**probability of actually paying out**, and feeds you the **most recent scope
changes** — because fresh attack surface is where untested bugs (and bounties) live.

Runs once a day as a GitHub Actions cron job. No server, no API keys, no database:
data lives in `data/*.json` inside the repo, and the dashboard is a static page
served by GitHub Pages.

## What it tracks

- **Score (0–100)** per program: recent scope additions, recent resolved reports
  (proof of payout), weighted scope breadth (wildcards/APIs/apps count more),
  response efficiency, bounty stats, and a penalty for mostly-out-of-scope programs.
- **Changes feed**: every asset added to a scope in the last 120 days, plus removed
  assets and resolved-report deltas from daily diffs (from the second run onward).
- **Filters**: min score, scope changed ≤ 90d, resolved ≤ 60d, pays bounties,
  has wildcards, free-text search.

## How the data is collected

`scraper.py` uses only public, unauthenticated endpoints:

1. **HackerOne** — `GET /directory` for a CSRF token, `/programs/search` for the
   program list, then anonymous `POST /graphql` (3 teams per request — H1 caps
   aliases) for bounty stats, response efficiency, resolved counts and structured
   scopes **including each asset's `created_at` timestamp**. That timestamp is
   what makes "recently changed scope" work from the very first run.
2. **YesWeHack** — `api.yeswehack.com/programs` list + per-program detail:
   full scope lists, real average/max rewards, and reports received in the last
   7 days. No per-asset timestamps, so scope changes are detected by daily diff
   (useful from run 2 onward).
3. **Intigriti** — `app.intigriti.com/api/core/public/programs`: program-level
   stats only (bounty range, last program update, last submission). Scope detail
   requires a logged-in account, so breadth columns show "—" for Intigriti.

A snapshot of everything is kept in `data/db.json`; each run diffs against it,
so from day 2 onward you also see removed assets, per-day report deltas, and
Intigriti "program updated" events.

## Tuning

Everything is at the top of `scraper.py`:

- `TYPE_WEIGHT` — how much each asset type counts toward breadth.
- `CHANGE_WINDOW_DAYS` — feed horizon (default 120).
- `score_program()` — the scoring formula itself.

## Notes & etiquette

- The scraper makes ~230 requests with delays and retries; a full run takes
  ~10 minutes. That's gentle, but don't schedule it more than once a day.
- Data reflects *public* programs only. Invite-only programs aren't visible
  without an account (Intigriti's invite-only programs are excluded; their
  public programs show limited scope detail for the same reason).
- Scores are heuristics, not promises — always read the actual policy before
  testing anything.
