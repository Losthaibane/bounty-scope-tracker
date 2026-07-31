#!/usr/bin/env python3
"""
bounty-scope-tracker scraper
============================
Pulls public bug bounty programs from HackerOne, YesWeHack and Intigriti
(no API keys needed anywhere), scores them by payout probability, detects
scope changes, and writes:

  data/db.json        full snapshot (diff baseline for the next run)
  data/programs.json  scored program table for the dashboard
  data/changes.json   recent scope-change / new-program / update feed

Pure stdlib. Safe to run daily from GitHub Actions.

Platform coverage notes:
- HackerOne   : full stats + per-asset scope list with created_at timestamps.
- YesWeHack   : full stats + scope list (no per-asset timestamps -> diff-based
                change detection from run 2 onward).
- Intigriti   : program-level stats only (bounty range, last update,
                last submission). Scope detail requires a logged-in account.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = {"User-Agent": "bounty-scope-tracker/1.1 (+github-actions)"}
DATA_DIR = Path(__file__).parent / "data"
NOW = datetime.now(timezone.utc)

# --- tunables ---------------------------------------------------------------
H1_BATCH = 3               # teams per GraphQL request (H1 caps aliases at 3)
H1_MAX_SCOPE_PAGES = 3     # up to 300 assets per program
SLEEP = 0.30               # base delay between requests
CHANGE_WINDOW_DAYS = 120   # feed horizon

TYPE_WEIGHT = {
    "WILDCARD": 5, "CIDR": 5, "API": 4, "MOBILE_APPLICATION": 4,
    "APPLICATION": 4, "HARDWARE": 3, "SOURCE_CODE": 3, "EXECUTABLE": 3,
    "OTHER": 2, "SMART_CONTRACT": 3, "DOMAIN": 1, "URL": 1,
    "GOOGLE_PLAY_APP_ID": 4, "APPLE_STORE_APP_ID": 4,
}
# YesWeHack scope_type -> our asset_type
YWH_TYPE_MAP = {
    "api": "API", "mobile-application": "MOBILE_APPLICATION",
    "mobile": "MOBILE_APPLICATION", "thick-client": "EXECUTABLE",
    "iot": "HARDWARE", "hardware": "HARDWARE", "web-application": "URL",
    "website": "URL", "other": "OTHER",
}
H1_TEAM_FIELDS = """name handle offers_bounties submission_state currency
minimum_bounty average_bounty_lower_amount average_bounty_upper_amount
response_efficiency_percentage resolved_report_count
last_report_resolved_at started_accepting_at"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def http_json(url, method="GET", payload=None, headers=None, retries=3):
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload).encode() if payload is not None else None
    if body:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  {e.code}, retrying in {wait}s ...", flush=True)
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code} for {url} — skipping", flush=True)
            return None
    return None


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def days_ago(dt):
    return (NOW - dt).days if dt else None


# --------------------------------------------------------------------------- #
# HackerOne
# --------------------------------------------------------------------------- #
def h1_csrf():
    req = urllib.request.Request("https://hackerone.com/directory", headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        html = r.read().decode(errors="replace")
    m = re.search(r'csrf-token" content="([^"]+)', html)
    if not m:
        sys.exit("hackerone: no csrf token")
    return m.group(1)


def h1_gql(token, query):
    return http_json("https://hackerone.com/graphql", method="POST",
                     payload={"query": query},
                     headers={"X-CSRF-Token": token})


def fetch_hackerone():
    token = h1_csrf()
    # directory
    handles, page = [], 1
    while True:
        d = http_json(f"https://hackerone.com/programs/search?query=type%3Ahackerone"
                      f"&sort=published_at%3Adescending&page={page}",
                      headers={"Accept": "application/json",
                               "X-Requested-With": "XMLHttpRequest"})
        results = (d or {}).get("results", [])
        handles += [p["handle"] for p in results]
        if len(handles) >= (d or {}).get("total", 0) or not results:
            break
        page += 1
        time.sleep(SLEEP)
    print(f"hackerone: {len(handles)} programs in directory", flush=True)

    records = []
    for i in range(0, len(handles), H1_BATCH):
        batch = handles[i:i + H1_BATCH]
        parts = []
        for j, h in enumerate(batch):
            parts.append(
                f't{j}: team(handle: "{h}") {{ {H1_TEAM_FIELDS} '
                f'structured_scopes(first: 100) {{ edges {{ node {{ asset_identifier '
                f'asset_type eligible_for_submission created_at }} }} '
                f'pageInfo {{ hasNextPage endCursor }} }} }}')
        d = h1_gql(token, "query { " + " ".join(parts) + " }")
        if (d or {}).get("errors") and not (d or {}).get("data"):
            print(f"  h1 batch error: {d['errors'][0].get('message')}", flush=True)
        for team in ((d or {}).get("data") or {}).values():
            if not team:
                continue
            sc = team.get("structured_scopes") or {}
            edges, pi, extra = sc.get("edges", []), sc.get("pageInfo", {}), 0
            while pi.get("hasNextPage") and extra < H1_MAX_SCOPE_PAGES - 1:
                time.sleep(SLEEP)
                q = (f'query {{ team(handle: "{team["handle"]}") {{ '
                     f'structured_scopes(first: 100, after: "{pi["endCursor"]}") {{ '
                     f'edges {{ node {{ asset_identifier asset_type '
                     f'eligible_for_submission created_at }} }} '
                     f'pageInfo {{ hasNextPage endCursor }} }} }} }}')
                more = (((h1_gql(token, q) or {}).get("data") or {}).get("team") or {})
                s2 = more.get("structured_scopes") or {}
                edges += s2.get("edges", [])
                pi = s2.get("pageInfo", {})
                extra += 1
            scopes = [{"asset_identifier": e["node"]["asset_identifier"],
                       "asset_type": e["node"].get("asset_type"),
                       "eligible_for_submission": e["node"].get("eligible_for_submission"),
                       "created_at": e["node"].get("created_at")} for e in edges]
            records.append({
                "platform": "hackerone", "handle": team["handle"],
                "name": team["name"], "url": f"https://hackerone.com/{team['handle']}",
                "offers_bounties": bool(team.get("offers_bounties")),
                "submission_state": team.get("submission_state"),
                "currency": team.get("currency") or "usd",
                "min_bounty": team.get("minimum_bounty"),
                "max_bounty": None,
                "avg_bounty_lo": team.get("average_bounty_lower_amount"),
                "avg_bounty_hi": team.get("average_bounty_upper_amount"),
                "response_efficiency": team.get("response_efficiency_percentage"),
                "resolved_count": team.get("resolved_report_count") or 0,
                "last_resolved_at": team.get("last_report_resolved_at"),
                "started_at": team.get("started_accepting_at"),
                "program_updated_at": None,
                "reports_7d": None,
                "scopes": scopes,
            })
        print(f"hackerone batch {i // H1_BATCH + 1}: {len(records)} fetched", flush=True)
        time.sleep(SLEEP)
    return records


# --------------------------------------------------------------------------- #
# YesWeHack
# --------------------------------------------------------------------------- #
def fetch_yeswehack():
    items, page = [], 1
    while True:
        d = http_json(f"https://api.yeswehack.com/programs?page={page}")
        if not d:
            break
        items += d.get("items", [])
        if page >= d.get("pagination", {}).get("nb_pages", 1):
            break
        page += 1
        time.sleep(SLEEP)
    print(f"yeswehack: {len(items)} programs listed", flush=True)

    records = []
    for it in items:
        if it.get("disabled") or it.get("archived"):
            continue
        time.sleep(SLEEP)
        d = http_json(f"https://api.yeswehack.com/programs/{it['slug']}")
        if not d:
            continue
        stats = d.get("stats") or {}
        scopes = []
        for s in d.get("scopes") or []:
            ident = s.get("scope", "")
            atype = YWH_TYPE_MAP.get((s.get("scope_type") or "").lower(), "URL")
            if ident.startswith("*.") or "*." in ident:
                atype = "WILDCARD"
            scopes.append({"asset_identifier": ident, "asset_type": atype,
                           "eligible_for_submission": True, "created_at": None})
        for s in d.get("out_of_scope") or []:
            # OOS list mixes prose lines with assets; keep asset-looking ones
            if isinstance(s, str) and " " not in s.strip():
                scopes.append({"asset_identifier": s.strip(), "asset_type": None,
                               "eligible_for_submission": False, "created_at": None})
        avg = stats.get("average_reward")
        records.append({
            "platform": "yeswehack", "handle": it["slug"],
            "name": d.get("title") or it["title"],
            "url": f"https://yeswehack.com/programs/{it['slug']}",
            "offers_bounties": bool(it.get("bounty")),
            "submission_state": "open",
            "currency": "eur",
            "min_bounty": d.get("bounty_reward_min"),
            "max_bounty": d.get("bounty_reward_max"),
            "avg_bounty_lo": round(avg / 100) if avg else None,   # cents -> EUR
            "avg_bounty_hi": None,
            "response_efficiency": None,
            "resolved_count": d.get("reports_count") or 0,
            "last_resolved_at": None,
            "started_at": None,
            "program_updated_at": None,
            "reports_7d": stats.get("total_reports_last7_days"),
            "scopes": scopes,
        })
    print(f"yeswehack: {len(records)} programs fetched", flush=True)
    return records


# --------------------------------------------------------------------------- #
# Intigriti (program-level only; scope detail needs a logged-in account)
# --------------------------------------------------------------------------- #
def fetch_intigriti():
    d = http_json("https://app.intigriti.com/api/core/public/programs",
                  headers={"Accept": "application/json"})
    if not d:
        return []
    records = []
    for p in d:
        if p.get("status") != 3:          # 3 = active
            continue
        key = f"{p['companyHandle']}/{p['handle']}"
        records.append({
            "platform": "intigriti", "handle": key,
            "name": p.get("name") or key,
            "url": f"https://app.intigriti.com/programs/{key}",
            "offers_bounties": (p.get("maxBounty") or {}).get("value", 0) > 0,
            "submission_state": "open",
            "currency": (p.get("maxBounty") or {}).get("currency", "EUR").lower(),
            "min_bounty": (p.get("minBounty") or {}).get("value") or None,
            "max_bounty": (p.get("maxBounty") or {}).get("value") or None,
            "avg_bounty_lo": None, "avg_bounty_hi": None,
            "response_efficiency": None,
            "resolved_count": 0,
            "last_resolved_at": iso(p.get("lastSubmissionAt")),
            "started_at": iso(p.get("createdAt")),
            "program_updated_at": iso(p.get("lastUpdatedAt")),
            "reports_7d": None,
            "scopes": [],
        })
    print(f"intigriti: {len(records)} active programs fetched", flush=True)
    return records


# --------------------------------------------------------------------------- #
# scoring (works with whatever each platform provides)
# --------------------------------------------------------------------------- #
def score(rec):
    scopes = rec["scopes"]
    in_scope = [s for s in scopes if s.get("eligible_for_submission")]
    out_scope = [s for s in scopes if not s.get("eligible_for_submission")]
    wild = sum(1 for s in in_scope if s.get("asset_type") in ("WILDCARD", "CIDR"))
    breadth = sum(TYPE_WEIGHT.get(s.get("asset_type"), 1) for s in in_scope)
    newest_asset = max((parse_dt(s.get("created_at")) for s in scopes
                        if s.get("created_at")), default=None)
    # Intigriti has no per-asset dates; program update is the closest proxy
    if newest_asset is None and rec.get("program_updated_at"):
        newest_asset = parse_dt(rec["program_updated_at"])
    last_resolved = parse_dt(rec.get("last_resolved_at"))

    sc = 0.0
    na = days_ago(newest_asset)
    if na is not None:
        sc += max(0, 30 * (1 - na / 90)) if na <= 90 else 0
    lr = days_ago(last_resolved)
    if lr is not None:
        sc += 15 if lr <= 14 else 8 if lr <= 60 else 2 if lr <= 180 else 0
    elif rec.get("reports_7d") is not None:      # YesWeHack activity signal
        sc += 15 if rec["reports_7d"] > 10 else 8 if rec["reports_7d"] > 0 else 0
    sc += min(25, breadth / 4)
    rep = rec.get("response_efficiency")
    if rep is not None:
        sc += 10 if rep >= 80 else 5 if rep >= 50 else 0
    if rec.get("offers_bounties"):
        sc += 15
        avg = rec.get("avg_bounty_lo")
        mx = rec.get("max_bounty")
        if (avg and avg >= 500) or (avg is None and mx and mx >= 2000):
            sc += 5
    total = len(in_scope) + len(out_scope)
    if total and len(out_scope) / total > 0.7:
        sc -= 10

    return {
        "score": round(sc, 1),
        "in_scope_count": len(in_scope) if scopes else None,
        "out_scope_count": len(out_scope) if scopes else None,
        "wildcard_count": wild,
        "breadth": breadth,
        "newest_asset_at": newest_asset.isoformat() if newest_asset else None,
        "last_resolved_at": last_resolved.isoformat() if last_resolved else None,
        "started_at": rec.get("started_at"),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    DATA_DIR.mkdir(exist_ok=True)
    records = fetch_hackerone() + fetch_yeswehack() + fetch_intigriti()

    prev = {}
    db_path = DATA_DIR / "db.json"
    had_baseline = db_path.exists()
    if had_baseline:
        prev = json.loads(db_path.read_text()).get("programs", {})

    cutoff = NOW - timedelta(days=CHANGE_WINDOW_DAYS)
    out, changes = [], []

    for rec in records:
        key = f"{rec['platform']}:{rec['handle']}"
        metrics = score(rec)
        p0 = prev.get(key, {})
        prev_assets = set(p0.get("assets", []))
        cur_assets = sorted({s["asset_identifier"] for s in rec["scopes"]
                             if s.get("eligible_for_submission")})

        for s in rec["scopes"]:
            ca = parse_dt(s.get("created_at"))
            if ca and ca >= cutoff and s.get("eligible_for_submission"):
                changes.append({"date": ca.isoformat(), "type": "scope_added",
                                "platform": rec["platform"], "program": rec["name"],
                                "handle": rec["handle"],
                                "asset": s["asset_identifier"],
                                "asset_type": s.get("asset_type")})
        for gone in sorted(prev_assets - set(cur_assets)):
            changes.append({"date": NOW.isoformat(), "type": "scope_removed",
                            "platform": rec["platform"], "program": rec["name"],
                            "handle": rec["handle"], "asset": gone,
                            "asset_type": None})
        for new in sorted(set(cur_assets) - prev_assets):
            # diff-based additions (YesWeHack, and H1 assets lacking timestamps)
            if p0 and not any(parse_dt(s.get("created_at")) for s in rec["scopes"]
                              if s["asset_identifier"] == new):
                changes.append({"date": NOW.isoformat(), "type": "scope_added",
                                "platform": rec["platform"], "program": rec["name"],
                                "handle": rec["handle"], "asset": new,
                                "asset_type": None})
        if rec.get("program_updated_at") and p0.get("updated_at") \
                and p0["updated_at"] != rec["program_updated_at"]:
            changes.append({"date": rec["program_updated_at"], "type": "program_updated",
                            "platform": rec["platform"], "program": rec["name"],
                            "handle": rec["handle"], "asset": None,
                            "asset_type": None})
        st = parse_dt(rec.get("started_at"))
        if key not in prev:
            if st and st >= cutoff:
                # platform exposes a real launch date (HackerOne, Intigriti)
                changes.append({"date": st.isoformat(), "type": "new_program",
                                "platform": rec["platform"], "program": rec["name"],
                                "handle": rec["handle"], "asset": None,
                                "asset_type": None})
            elif had_baseline:
                # no launch date available (YesWeHack): report first sighting,
                # but never on the very first run (that would flood the feed)
                changes.append({"date": NOW.isoformat(), "type": "new_program",
                                "platform": rec["platform"], "program": rec["name"],
                                "handle": rec["handle"], "asset": None,
                                "asset_type": None})

        out.append({
            "platform": rec["platform"], "handle": rec["handle"],
            "name": rec["name"], "url": rec["url"],
            "offers_bounties": rec["offers_bounties"],
            "submission_state": rec["submission_state"],
            "currency": rec["currency"],
            "min_bounty": rec.get("min_bounty"),
            "max_bounty": rec.get("max_bounty"),
            "avg_bounty_lo": rec.get("avg_bounty_lo"),
            "avg_bounty_hi": rec.get("avg_bounty_hi"),
            "response_efficiency": rec.get("response_efficiency"),
            "resolved_count": rec.get("resolved_count") or 0,
            "resolved_delta": (rec.get("resolved_count") or 0)
                              - p0.get("resolved_count", rec.get("resolved_count") or 0),
            "reports_7d": rec.get("reports_7d"),
            **metrics,
        })

    out.sort(key=lambda p: p["score"], reverse=True)
    changes.sort(key=lambda c: c["date"], reverse=True)

    (DATA_DIR / "programs.json").write_text(json.dumps({
        "generated_at": NOW.isoformat(), "programs": out}, indent=1))
    (DATA_DIR / "changes.json").write_text(json.dumps({
        "generated_at": NOW.isoformat(), "changes": changes[:600]}, indent=1))
    (DATA_DIR / "db.json").write_text(json.dumps({
        "generated_at": NOW.isoformat(),
        "programs": {f"{r['platform']}:{r['handle']}": {
            "assets": sorted({s["asset_identifier"] for s in r["scopes"]
                              if s.get("eligible_for_submission")}),
            "resolved_count": r.get("resolved_count") or 0,
            "updated_at": r.get("program_updated_at"),
        } for r in records}}, indent=1))

    print(f"\ndone: {len(out)} programs, {len(changes)} change events", flush=True)
    for p in out[:5]:
        print(f"  {p['score']:6.1f}  [{p['platform']}] {p['name']}", flush=True)


if __name__ == "__main__":
    main()
