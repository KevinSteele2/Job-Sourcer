"""The nightly run: import -> sweep -> open-posting check -> capped scoring.

Started by Windows Task Scheduler (see README), or by hand:
    python nightly.py

Each step runs on its own: one failing doesn't stop the rest, and every
failure is recorded. Steps and the scoring cap are set in nightly.yaml.
Output goes to data/logs/nightly-<date>.log, and a one-line summary of each
run is appended to data/nightly_runs.jsonl (the app's Fetch page shows the latest).
When it finishes, a Windows notification says how it went (notify.py).
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

import notify

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "nightly.yaml"
LOG_DIR = ROOT / "data" / "logs"
RUNS_PATH = ROOT / "data" / "nightly_runs.jsonl"
LOCK_PATH = ROOT / "data" / "nightly.lock"
STALE_LOCK_HOURS = 3   # a lock older than this is from a crashed run


def _acquire_lock() -> bool:
    """Make sure only one nightly run happens at a time."""
    if LOCK_PATH.exists():
        age_hours = (time.time() - LOCK_PATH.stat().st_mtime) / 3600
        if age_hours < STALE_LOCK_HOURS:
            return False
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def summary_message(record: dict, started: str) -> tuple[str, str]:
    """(title, message) for the end-of-run notification."""
    import db  # imported here, like the steps in run_nightly
    parts = []
    found = sum((record.get(step) or {}).get("added", 0) for step in ("import_simplify", "sweep"))
    parts.append(f"{found} new jobs")
    if record.get("targets") is not None:
        conn = db.connect()
        new_targets = conn.execute("""
            SELECT COUNT(*) FROM jobs WHERE target = 1 AND first_seen >= ?
              AND status != 'filtered_out' AND duplicate_of IS NULL
        """, (started,)).fetchone()[0]
        conn.close()
        failed = [name for name, s in record["targets"].items() if isinstance(s, dict) and "error" in s]
        if new_targets:
            parts.insert(0, f"★ {new_targets} new at your target companies")
        if failed:
            parts.append(f"couldn't check {', '.join(failed)}")
    score = record.get("score") or {}
    if score:
        conn = db.connect()
        good = conn.execute("""
            SELECT COUNT(*) FROM jobs WHERE scored_at >= ? AND score >= 7
              AND duplicate_of IS NULL AND closed_at IS NULL
        """, (started,)).fetchone()[0]
        day_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        cost = sum(u["cost_usd"] or 0 for u in db.usage_summary(conn, day_ago))
        conn.close()
        parts.append(f"scored {score.get('scored', 0)}, {good} at 7+")
        if score.get("stopped_reason"):
            parts.append(f"scoring stopped ({score['stopped_reason'][:60]})")
        parts.append(f"Claude today ${cost:.2f}")
    closed = (record.get("check_open") or {}).get("closed", 0)
    if closed:
        parts.append(f"{closed} postings closed")
    if record["status"] == "ok":
        return "Job Sourcer: nightly run done", " · ".join(parts)
    return ("Job Sourcer: nightly run had problems",
            f"{record['status']}. " + " · ".join(parts) + ". See data/logs/.")


def run_nightly() -> dict:
    # Imported here so a broken module shows up in the log, not as a silent crash.
    import import_feed
    import liveness
    import score
    import sweep
    import targets

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    steps = config.get("steps") or {}
    score_cfg = config.get("score") or {}

    plan = [
        ("targets", lambda: targets.run_targets()),
        ("import_simplify", lambda: import_feed.run_import()),
        ("sweep", lambda: sweep.run_sweep()),
        ("check_open", lambda: liveness.check_open()),
        ("score", lambda: score.score_jobs(score_cfg.get("limit", 60), score_cfg.get("model") or None,
                                           score_cfg.get("batch_size", score.DEFAULT_BATCH_SIZE),
                                           recheck_limit=score_cfg.get("recheck_limit"),
                                           recheck_min_score=score_cfg.get("recheck_min_score",
                                                                           score.RECHECK_MIN_SCORE))),
    ]
    results, failed = {}, []
    for name, step in plan:
        if not steps.get(name, True):
            print(f"[{name}] skipped (off in nightly.yaml)")
            continue
        started = time.time()
        print(f"[{name}] starting", flush=True)
        try:
            summary = step()
            # Keep the log readable: drop long lists from the summaries.
            results[name] = {k: v for k, v in summary.items() if not isinstance(v, list)}
            print(f"[{name}] done in {time.time() - started:.0f}s: {results[name]}", flush=True)
        except Exception:
            failed.append(name)
            print(f"[{name}] FAILED:\n{traceback.format_exc()}", flush=True)
    return {"results": results, "failed": failed}


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    log_path = LOG_DIR / f"nightly-{started.strftime('%Y-%m-%d')}.log"

    # Under Task Scheduler there's no console (pythonw), so everything printed
    # goes to the day's log file instead.
    with log_path.open("a", encoding="utf-8") as log:
        sys.stdout = sys.stderr = log
        print(f"\n=== Nightly run {started.isoformat()} ===", flush=True)
        if not _acquire_lock():
            print("Another nightly run is still going; exiting.", flush=True)
            return
        try:
            outcome = run_nightly()
        finally:
            LOCK_PATH.unlink(missing_ok=True)

        status = "ok" if not outcome["failed"] else "failed: " + ", ".join(outcome["failed"])
        record = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "minutes": round((datetime.now(timezone.utc) - started).total_seconds() / 60, 1),
                  "status": status, **outcome["results"]}
        with RUNS_PATH.open("a", encoding="utf-8") as runs:
            runs.write(json.dumps(record) + "\n")
        print(f"=== Finished: {status} ===", flush=True)
        try:
            notify.notify(*summary_message(record, record["started"]))
        except Exception:
            print(f"Notification failed:\n{traceback.format_exc()}", flush=True)


if __name__ == "__main__":
    main()
