"""Wide sweep: check ~15,900 public Greenhouse/Lever/Ashby boards for new matching jobs.

Usage:
    python sweep.py

The board lists in boards/ come from the Job Search Kit's slug cache. For each
board, the sweep downloads the job list and keeps postings that:
  - aren't stored yet,
  - were posted within `lookback_days` (feeds.yaml -> sweep),
  - have a title containing one of `title_keywords`, and
  - pass filters.yaml.
Jobs it keeps are stored with feed = "sweep" and go through scoring like any
other job. A full sweep takes roughly 15-20 minutes.

Because each board's whole list is downloaded, the sweep also knows which
stored jobs are no longer on their board, and closes them.
"""

import json
import random
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import db
from job import iso_utc
import filters
from sources import ashby, greenhouse, lever
from sources.http import get_json

ROOT = Path(__file__).parent
BOARDS_DIR = ROOT / "boards"
DEAD_BOARDS_PATH = ROOT / "data" / "dead_boards.json"
FEEDS_PATH = ROOT / "feeds.yaml"
FEED = "sweep"
DEAD_RETRY_DAYS = 30   # a board that 404'd is skipped this long, then tried again
REQUEST_PAUSE = 0.1    # seconds each worker waits after a request

LIST_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",  # no descriptions: ~20x smaller
    "lever": lever.API_URL,
    "ashby": ashby.API_URL,
}


def load_config() -> dict:
    config = (yaml.safe_load(FEEDS_PATH.read_text(encoding="utf-8")) or {}).get("sweep") or {}
    return {"title_keywords": config.get("title_keywords") or [],
            "lookback_days": config.get("lookback_days", 7),
            "workers": config.get("workers", 8)}


def load_boards() -> tuple[list[tuple[str, str]], int]:
    """Every (ats, slug) to check, minus boards that recently returned 404.

    Returns (boards, how many were skipped as dead).
    """
    dead = _load_dead()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEAD_RETRY_DAYS)).isoformat()
    boards, skipped = [], 0
    for ats in LIST_URLS:
        for slug in json.loads((BOARDS_DIR / f"{ats}_slugs.json").read_text(encoding="utf-8")):
            if dead.get(f"{ats}/{slug}", "") < cutoff:
                boards.append((ats, slug))
            else:
                skipped += 1
    # Mixing the three ATS hosts spreads the load instead of hitting one at a time.
    random.shuffle(boards)
    return boards, skipped


def _load_dead() -> dict:
    try:
        return json.loads(DEAD_BOARDS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _posted_at(ats: str, raw: dict) -> datetime | None:
    """When the posting went up, from each ATS's own field. None if unknown."""
    try:
        if ats == "greenhouse":
            return datetime.fromisoformat(raw.get("first_published") or raw["updated_at"])
        if ats == "lever":
            return datetime.fromtimestamp(raw["createdAt"] / 1000, tz=timezone.utc)
        return datetime.fromisoformat(raw["publishedAt"])
    except (KeyError, TypeError, ValueError):
        return None


def _check_board(ats: str, slug: str, keywords, rules, since: datetime, known: set) -> dict:
    """Runs in a worker thread: one board's list -> what's on it and what's new.

    Returns {"ids": every job ID on the board, "new": [Job, ...]}. Makes no
    database writes; the main thread does those.
    """
    try:
        payload = get_json(LIST_URLS[ats].format(slug=slug))
    finally:
        time.sleep(REQUEST_PAUSE)
    raw_jobs = payload["jobs"] if isinstance(payload, dict) else payload
    if ats == "ashby":
        raw_jobs = [r for r in raw_jobs if r.get("isListed", True)]

    ids, new, dates = [], [], {}
    for raw in raw_jobs:
        job_id = str(raw["id"])
        ids.append(job_id)
        title = (raw.get("title") or raw.get("text") or "").strip()
        location = (raw.get("location") or {}).get("name", "") if ats == "greenhouse" else ""
        posted = _posted_at(ats, raw)
        if posted:
            dates[job_id] = iso_utc(posted.isoformat())
        if (ats, slug, job_id) in known or (posted and posted < since):
            continue
        if not any(p.search(title) for _, p in keywords):
            continue
        # Build the full Job. Greenhouse's list has no description, so a
        # match costs one more request; Lever and Ashby lists already have it.
        if ats == "greenhouse":
            try:
                job = greenhouse.fetch_job(slug, job_id)
            except urllib.error.HTTPError:
                continue  # taken down between the list and this request
            finally:
                time.sleep(REQUEST_PAUSE)
        else:
            job = (lever if ats == "lever" else ashby)._to_job(raw, slug)
        # filters.yaml needs the location, which Lever/Ashby only give in full.
        if filters.check(job.title, job.location or location, rules) is None:
            new.append(job)
    return {"ids": ids, "new": new, "dates": dates}


def run_sweep(on_progress=None, should_stop=None) -> dict:
    """Run one sweep. Used by this command and the web app.

    on_progress(done, total, label) after each board; should_stop() between boards.
    """
    config = load_config()
    keywords = [(kw, filters._keyword_pattern(kw)) for kw in config["title_keywords"]]
    if not keywords:
        raise ValueError("feeds.yaml -> sweep -> title_keywords is empty")
    rules = filters.load_rules()
    since = datetime.now(timezone.utc) - timedelta(days=config["lookback_days"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    boards, skipped_dead = load_boards()

    conn = db.connect()
    known = db.known_keys(conn)
    dead = _load_dead()
    seen_on_board: dict[tuple[str, str], list[str]] = {}
    posted: dict[tuple[str, str, str], str] = {}  # posting dates, to fill in stored jobs
    added = failed = newly_dead = 0
    stopped = False
    lock = threading.Lock()  # `known` is read by workers while the main thread adds to it

    with ThreadPoolExecutor(max_workers=config["workers"]) as pool:
        futures = {pool.submit(_check_board, ats, slug, keywords, rules, since, known): (ats, slug)
                   for ats, slug in boards}
        for done, future in enumerate(as_completed(futures), 1):
            ats, slug = futures[future]
            try:
                result = future.result()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    dead[f"{ats}/{slug}"] = datetime.now(timezone.utc).isoformat()
                    newly_dead += 1
                else:
                    failed += 1
            except Exception:  # network trouble or an odd response: skip this board this run
                failed += 1
            else:
                seen_on_board[(ats, slug)] = result["ids"]
                posted.update({(ats, slug, jid): d for jid, d in result["dates"].items()})
                with lock:
                    fresh = [j for j in result["new"] if (j.source, j.company, j.ats_job_id) not in known]
                    known.update((j.source, j.company, j.ats_job_id) for j in fresh)
                if fresh:
                    db.insert_feed_jobs(conn, fresh, FEED, now)
                    added += len(fresh)
            if on_progress:
                on_progress(done, len(boards), f"{ats}/{slug}")
            if should_stop and should_stop():
                stopped = True
                pool.shutdown(cancel_futures=True)  # drop boards not started yet
                break

    DEAD_BOARDS_PATH.write_text(json.dumps(dead, indent=0, sort_keys=True), encoding="utf-8")

    # A board's list is the full truth about that board: stored jobs from it
    # that weren't on it are closed, and the ones that were are marked seen.
    present = [(ats, slug, jid) for (ats, slug), ids in seen_on_board.items() for jid in ids]
    db.touch_jobs(conn, [key for key in present if key in known], now, posted)
    closed = db.close_missing_board_jobs(conn, list(seen_on_board), now)

    results = [(r[0], filters.check(r[1], r[2], rules)) for r in db.feed_jobs_to_filter(conn, FEED)]
    db.set_filter_results(conn, results)
    conn.close()

    return {"boards": len(boards), "checked": len(seen_on_board), "failed": failed,
            "newly_dead": newly_dead, "skipped_dead": skipped_dead,
            "added": added, "closed": closed, "stopped": stopped,
            "lookback_days": config["lookback_days"]}


def main() -> None:
    started = time.time()
    summary = run_sweep(on_progress=lambda done, total, label:
                        print(f"\r  {done}/{total} boards  {label[:40]:<40}", end="", flush=True))
    print()
    s = summary
    print(f"\nWide sweep ({s['lookback_days']}-day lookback, {round((time.time() - started) / 60, 1)} min)")
    print(f"  boards checked:   {s['checked']} of {s['boards']}")
    print(f"  failed (retry):   {s['failed']}")
    print(f"  gone (404):       {s['newly_dead']} new, {s['skipped_dead']} skipped")
    print(f"  new jobs added:   {s['added']}")
    print(f"  closed:           {s['closed']}")
    if s["stopped"]:
        print("Stopped early.")
        sys.exit(2)


if __name__ == "__main__":
    main()
