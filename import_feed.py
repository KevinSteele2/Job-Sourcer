"""Import jobs from the SimplifyJobs new-grad list: many companies in one run.

Usage:
    python import_feed.py

For each active listing in the categories from feeds.yaml:
  - already stored         -> just mark it seen (no request)
  - new, on Greenhouse/Lever/Ashby -> fetch the full job from that ATS
  - new, anywhere else     -> store as listing-only (no description, not scored)
Then close jobs from this list that it no longer includes, and run the
keyword filter from filters.yaml.

The first run makes a few hundred small requests (a few minutes, with a pause
between each). Later runs only fetch listings they haven't seen before.
"""

import sys
import time
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

import db
import filters
from sources import ashby, greenhouse, lever, simplify

FEED = "simplify"
FEEDS_PATH = Path(__file__).parent / "feeds.yaml"
REQUEST_DELAY = 0.3  # seconds between ATS requests, to be polite to their APIs

FETCH_ONE = {"greenhouse": greenhouse.fetch_job, "lever": lever.fetch_job}


def load_categories() -> list[str]:
    config = yaml.safe_load(FEEDS_PATH.read_text(encoding="utf-8")) or {}
    return (config.get(FEED) or {}).get("categories") or []


def _fetch_full_job(listing, ashby_boards: dict):
    """The full Job from its ATS, or None if the ATS no longer has it.

    Ashby has no per-job API, so each Ashby board is downloaded once per run
    and cached in ashby_boards (slug -> {job_id: Job}).
    """
    if listing.ats == "ashby":
        if listing.slug not in ashby_boards:
            try:
                ashby_boards[listing.slug] = {j.ats_job_id: j for j in ashby.fetch_jobs(listing.slug)}
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                ashby_boards[listing.slug] = {}  # board is gone: its listings are taken down
            finally:
                time.sleep(REQUEST_DELAY)
        board = ashby_boards[listing.slug]
        if board is None:
            raise ValueError(f"Ashby board {listing.slug} failed earlier this run")
        return board.get(listing.ats_job_id)

    try:
        return FETCH_ONE[listing.ats](listing.slug, listing.ats_job_id)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # listed on Simplify but already taken down
        raise
    finally:
        time.sleep(REQUEST_DELAY)


def run_import(on_progress=None, should_stop=None) -> dict:
    """Run one import. Used by this command and the web app.

    on_progress(i, total, label) is called before each ATS request;
    should_stop() is checked between them.
    """
    rules = filters.load_rules()
    categories = load_categories()
    # Downloaded before any database write: if it fails, nothing changes.
    listings = simplify.fetch_listings(categories)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = db.connect()
    known = db.known_keys(conn)

    # Sort every listing into: already stored / new and fetchable / new listing-only.
    # Dicts keyed by job identity drop any listing that appears twice.
    seen, to_fetch, listing_only = {}, {}, {}
    for listing in listings:
        if listing.ats:
            key = (listing.ats, listing.slug, listing.ats_job_id)
        else:
            key = ("simplify", listing.company_name, listing.id)
        if key in known:
            seen[key] = listing
        elif listing.ats:
            to_fetch[key] = listing
        else:
            listing_only[key] = simplify.listing_only_job(listing)

    db.touch_jobs(conn, list(seen), now, {key: l.posted_at for key, l in seen.items()})
    db.insert_feed_jobs(conn, list(listing_only.values()), FEED, now)

    added = unavailable = failed = 0
    stopped = False
    ashby_boards: dict = {}
    for i, listing in enumerate(to_fetch.values(), 1):
        if should_stop and should_stop():
            stopped = True
            break
        if on_progress:
            on_progress(i, len(to_fetch), f"{listing.company_name}: {listing.title}")
        try:
            job = _fetch_full_job(listing, ashby_boards)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            # Network trouble or an unexpected response. Not stored, so the
            # next run tries this listing again.
            if listing.ats == "ashby":
                ashby_boards.setdefault(listing.slug, None)  # don't retry that board this run
            failed += 1
            continue
        if job is None:
            unavailable += 1
            continue
        # Saved one at a time, so stopping midway keeps everything fetched so far.
        db.insert_feed_jobs(conn, [job], FEED, now)
        added += 1

    # Every job still on the list was marked seen above, so this only closes
    # jobs the list dropped. Unfetched new listings aren't stored yet, so a
    # stopped run can't close anything by mistake.
    closed = db.close_missing_feed_jobs(conn, FEED, now)

    results = [(row_id, filters.check(title, location, rules))
               for row_id, title, location in db.feed_jobs_to_filter(conn, FEED)]
    db.set_filter_results(conn, results)
    status_counts = dict(conn.execute("""
        SELECT status, COUNT(*) FROM jobs WHERE feed = ? AND closed_at IS NULL GROUP BY status
    """, (FEED,)).fetchall())
    conn.close()

    reasons = Counter(reason for _, reason in results if reason)
    return {
        "listings": len(listings), "already_stored": len(seen),
        "added": added, "listing_only_added": len(listing_only),
        "unavailable": unavailable, "failed": failed, "closed": closed,
        "not_fetched": len(to_fetch) - added - unavailable - failed,
        "stopped": stopped, "reasons": reasons.most_common(),
        "filtered_out": sum(reasons.values()), "status_counts": status_counts,
    }


def main() -> None:
    print("Downloading the Simplify list...")
    summary = run_import(
        on_progress=lambda i, n, label: print(f"\r  fetching {i}/{n}  {label[:60]:<60}", end="", flush=True))
    print()

    s = summary
    print(f"\nSimplify ({', '.join(load_categories())})")
    print(f"  active listings:       {s['listings']}")
    print(f"  already stored:        {s['already_stored']}")
    print(f"  new, full description: {s['added']}")
    print(f"  new, listing only:     {s['listing_only_added']}")
    print(f"  taken down already:    {s['unavailable']}")
    print(f"  failed (retry later):  {s['failed']}")
    print(f"  closed:                {s['closed']}")
    print(f"  filtered out:          {s['filtered_out']}")
    for reason, n in s["reasons"]:
        print(f"    {n:>5}  {reason}")
    counts = s["status_counts"]
    print(f"  ready to score:        {counts.get('to_score', 0)}")
    print(f"  listing only (kept):   {counts.get('no_description', 0)}")
    if s["stopped"]:
        print(f"Stopped early; {s['not_fetched']} new listing(s) will be fetched next run.")
        sys.exit(2)


if __name__ == "__main__":
    main()
