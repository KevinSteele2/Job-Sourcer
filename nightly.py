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


# Windows power flags for SetThreadExecutionState. Task Scheduler wakes the PC
# to start the run, but nothing stops it going back to sleep partway through
# (a 20-minute run can turn into hours). ES_SYSTEM_REQUIRED says "this program
# needs the computer awake"; ES_CONTINUOUS keeps that in force until it's
# cleared. The display can still turn off.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _stay_awake(on: bool) -> None:
    """Ask Windows not to sleep while the run is going (on=False clears it).
    Best effort: on other systems, or if the call fails, nothing happens."""
    try:
        import ctypes
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def is_running() -> bool:
    """A nightly run holds the lock (and it's not left over from a crash).
    The app checks this so a manual scoring run can't score the same jobs
    at the same time."""
    return LOCK_PATH.exists() and (time.time() - LOCK_PATH.stat().st_mtime) / 3600 < STALE_LOCK_HOURS


def _acquire_lock() -> bool:
    """Make sure only one nightly run happens at a time."""
    if is_running():
        return False
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


# Volume checks: (step, field, fraction). Tonight's value below this fraction
# of the median of the last 7 runs means something broke quietly (a network
# outage, a source that stopped answering) even though no step failed.
VOLUME_CHECKS = [("sweep", "checked", 0.5), ("import_simplify", "listings", 0.5),
                 ("sweep", "added", 0.25)]   # new jobs vary night to night, so a lower bar


def volume_warnings(record: dict) -> list[str]:
    """Steps whose volume tonight is far below the recent median."""
    try:
        past = [json.loads(line) for line in RUNS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    past = [r for r in past if r.get("started") != record.get("started")][-7:]
    warnings = []
    for step, field, fraction in VOLUME_CHECKS:
        values = sorted(v for r in past if isinstance(v := (r.get(step) or {}).get(field), (int, float)))
        tonight = (record.get(step) or {}).get(field)
        if len(values) < 3 or tonight is None:
            continue   # not enough history, or the step didn't run
        median = values[len(values) // 2]
        if median and tonight < median * fraction:
            warnings.append(f"{step} {field} only {tonight} (usually ~{median})")
    return warnings


def summary_message(record: dict, started: str) -> tuple[str, str]:
    """(title, message) for the end-of-run notification."""
    import db  # imported here, like the steps in run_nightly
    score = record.get("score") or {}
    if "login expired" in (score.get("stopped_reason") or ""):
        # Every Claude call will fail until this is fixed, so say only that.
        return ("Job Sourcer: Claude login expired",
                "Nothing was scored. Open a terminal and run: claude /login")
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
    if score:
        conn = db.connect()
        good = conn.execute("""
            SELECT COUNT(*) FROM jobs WHERE scored_at >= ? AND score >= 7
              AND duplicate_of IS NULL AND closed_at IS NULL
        """, (started,)).fetchone()[0]
        day_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        cost = sum(u["cost_usd"] or 0 for u in db.usage_summary(conn, day_ago))
        conn.close()
        failed_note = f" ({score['failed']} failed)" if score.get("failed") else ""
        parts.append(f"scored {score.get('scored', 0)}{failed_note}, {good} at 7+, "
                     f"{score.get('remaining', 0)} still waiting")
        if score.get("stopped_reason"):
            parts.append(f"scoring stopped ({score['stopped_reason'][:60]})")
        parts.append(f"Claude today ${cost:.2f}")
    closed = (record.get("check_open") or {}).get("closed", 0)
    if closed:
        parts.append(f"{closed} postings closed")
    conn = db.connect()
    due = len(db.apply_reminders(conn))  # reached out 2+ days ago, not applied yet
    conn.close()
    if due:
        parts.insert(0, f"{due} job{'s' if due > 1 else ''} to apply to (no referral reply)")
    low = volume_warnings(record)
    if low:
        parts.insert(0, "⚠ low volume: " + "; ".join(low))
    if record["status"] == "ok" and not low:
        return "Job Sourcer: nightly run done", " · ".join(parts)
    status = "" if record["status"] == "ok" else f"{record['status']}. "
    return ("Job Sourcer: nightly run had problems",
            status + " · ".join(parts) + ". See data/logs/.")


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
        _stay_awake(True)
        try:
            outcome = run_nightly()
        finally:
            _stay_awake(False)
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
