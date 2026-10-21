"""Check that your best jobs are still open before you spend time on them.

Usage:
    python liveness.py              check up to 40 open jobs scored 7+
    python liveness.py --limit 100 --min-score 6

Postings close fast. This re-asks each job's ATS about the job directly:
gone (404, or no longer on its Ashby board) -> the job is closed. Any other
error leaves it alone, since a wrong "closed" would hide a live job.
Jobs checked longest ago go first, so repeated runs rotate through them.
Listing-only jobs are skipped: there's no API to ask.
"""

import argparse
import time
import urllib.error
from datetime import datetime, timezone

import db
from sources import ashby, greenhouse, lever

REQUEST_PAUSE = 0.3
FETCH_ONE = {"greenhouse": greenhouse.fetch_job, "lever": lever.fetch_job}


def _is_open(source: str, company: str, job_id: str, ashby_boards: dict) -> bool:
    """True/False from the ATS. Raises on network trouble (= unknown)."""
    try:
        if source == "ashby":
            if company not in ashby_boards:
                try:
                    ashby_boards[company] = {j.ats_job_id for j in ashby.fetch_jobs(company)}
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        raise
                    ashby_boards[company] = set()  # whole board is gone
            return job_id in ashby_boards[company]
        FETCH_ONE[source](company, job_id)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    finally:
        time.sleep(REQUEST_PAUSE)


def check_open(limit: int = 40, min_score: int = 7) -> dict:
    """Check up to `limit` open jobs: ones you've drafted or applied to first, then
    ones scored (or rated by you) at least `min_score`."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect()
    jobs = db.jobs_to_check(conn, min_score, limit)
    still_open = closed = unknown = 0
    ashby_boards: dict = {}
    for job in jobs:
        try:
            is_open = _is_open(job["source"], job["company"], job["ats_job_id"], ashby_boards)
        except Exception:  # network trouble or an odd response: leave the job alone
            unknown += 1
            continue
        db.record_check(conn, job["id"], is_open, now)
        if is_open:
            still_open += 1
        else:
            closed += 1
    conn.close()
    return {"checked": len(jobs), "open": still_open, "closed": closed, "unknown": unknown}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check that top jobs are still open.")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--min-score", type=int, default=7)
    args = parser.parse_args()
    s = check_open(args.limit, args.min_score)
    print(f"checked {s['checked']}: {s['open']} open, {s['closed']} closed, {s['unknown']} unknown")


if __name__ == "__main__":
    main()
