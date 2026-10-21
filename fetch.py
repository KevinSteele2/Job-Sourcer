"""Fetch one company's jobs, store them in SQLite, and run the rule filter.

Usage:
    python fetch.py <ats> <company-slug>

Examples:
    python fetch.py greenhouse anthropic
    python fetch.py lever palantir
    python fetch.py ashby ramp
"""

import sys
from collections import Counter
from datetime import datetime, timezone

import db
import filters
from sources import ADAPTERS


def run_fetch(ats: str, slug: str) -> dict:
    """Fetch one board, save it, filter it. Returns the summary counts.

    Used by both this command and the web app (app.py).
    """
    # Load rules before fetching, so a typo in filters.yaml fails fast.
    rules = filters.load_rules()

    # Fetch happens before any database write. If it raises (bad slug,
    # network error), the run stops here and no jobs get marked closed.
    jobs = ADAPTERS[ats](slug)

    # One timestamp for the whole run, so "last_seen < now" cleanly separates
    # jobs seen this run from jobs that disappeared.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = db.connect()
    counts = db.save_jobs(conn, ats, slug, jobs, now)

    results = [(row_id, filters.check(title, location, rules))
               for row_id, title, location in db.jobs_to_filter(conn, ats, slug)]
    db.set_filter_results(conn, results)
    conn.close()

    reasons = Counter(reason for _, reason in results if reason)
    passed = sum(1 for _, reason in results if reason is None)
    return {**counts, "filtered_out": sum(reasons.values()),
            "reasons": reasons.most_common(), "passed": passed}


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ADAPTERS:
        print(f"Usage: python fetch.py <{'|'.join(ADAPTERS)}> <company-slug>")
        sys.exit(1)
    ats, slug = sys.argv[1], sys.argv[2]
    summary = run_fetch(ats, slug)

    print(f"{ats}/{slug}")
    print(f"  fetched:      {summary['fetched']}")
    print(f"  new:          {summary['new']}")
    print(f"  closed:       {summary['closed']}")
    print(f"  filtered out: {summary['filtered_out']}")
    for reason, n in summary["reasons"]:
        print(f"    {n:>5}  {reason}")
    print(f"  passed:       {summary['passed']}")


if __name__ == "__main__":
    main()
