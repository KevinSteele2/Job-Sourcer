"""Score jobs with status to_score using Claude Code, several jobs per call.

Usage:
    python score.py --dry-run                 print the prompt for one job, no Claude call
    python score.py --limit 50                score up to 50 jobs (the per-run cap)
    python score.py --batch-size 10           jobs per Claude call (default 10)
    python score.py --model haiku ...         pick the model (default: Claude Code's default)
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
import filters
import scorer

ERROR_LOG = Path(__file__).parent / "data" / "score_errors.log"
DEFAULT_BATCH_SIZE = 10
RECHECK_MIN_SCORE = 6  # re-check scores this high when the prompt or profile changes


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_failure(job: dict, error: str) -> None:
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{utc_now()}  job {job['id']} ({job['title']}): {error}\n")


def _as_dicts(rows) -> list[dict]:
    return [{"id": r["id"], "company": r["company"], "title": r["title"],
             "location": r["location"], "description": r["description"]} for r in rows]


def preview_prompt() -> str | None:
    """The full prompt for the newest unscored job (a batch of one), or None."""
    profile, feedback = scorer.read_context(require_profile=False)
    conn = db.connect()
    jobs = _as_dicts(db.jobs_to_score(conn, 1))
    conn.close()
    return scorer.build_prompt(profile, feedback, jobs) if jobs else None


def score_jobs(limit: int | None, model: str | None, batch_size: int = DEFAULT_BATCH_SIZE,
               on_batch=None, on_result=None, should_stop=None,
               recheck_limit: int | None = None, recheck_min_score: int = RECHECK_MIN_SCORE) -> dict:
    """Score up to `limit` jobs, `batch_size` per Claude call.

    New (to_score) jobs come first. Then, within the same limit, up to
    `recheck_limit` jobs scored `recheck_min_score`+ are re-checked if they
    were scored before the last change to the prompt, profile.md or
    feedback.md (their stamp doesn't match). By default a third of the
    limit, so re-checks never crowd out new jobs entirely.

    Used by this command and the web app. Optional hooks:
      on_batch(i, n_batches, size)   before each batch
      on_result(label, outcome)      per job: the score, or "failed (logged)"
      should_stop() -> bool          checked between batches; True ends the run

    Each batch gets two tries. Jobs that fail the first try (a bad entry, or
    missing from the reply) are retried together; after that they're logged
    and stay to_score for the next run. A usage limit stops the run at once.
    """
    profile, feedback = scorer.read_context()  # raises if profile.md is missing
    stamp = scorer.prompt_stamp(profile, feedback)
    filters.refilter_all()  # so the current filters.yaml applies before anything is scored
    conn = db.connect()
    # Re-checks get a reserved share of the limit (with hundreds of new jobs
    # waiting they'd otherwise never run); new jobs fill the rest. When nothing
    # needs re-checking, new jobs get the whole limit.
    if recheck_limit is None and limit is not None:
        recheck_limit = limit // 3
    if limit is not None:
        recheck_limit = min(recheck_limit, limit)
    stale = db.jobs_to_recheck(conn, stamp, recheck_min_score, recheck_limit)
    old_scores = {r["id"]: r["score"] for r in stale}
    new_limit = None if limit is None else limit - len(stale)
    jobs = _as_dicts(db.jobs_to_score(conn, new_limit)) + _as_dicts(stale)
    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]

    scored = failed = rechecked = 0
    stopped_reason = None
    for b, batch in enumerate(batches, 1):
        if should_stop and should_stop():
            stopped_reason = "stopped by you"
            break
        if on_batch:
            on_batch(b, len(batches), len(batch))

        pending, errors = batch, {}
        try:
            for _attempt in (1, 2):
                if not pending:
                    break
                prompt = scorer.build_prompt(profile, feedback, pending)
                try:
                    text, used_model = scorer.call_claude(prompt, model)
                    results, errors = scorer.parse_batch(text, pending)
                except scorer.ScoringError as e:
                    results, errors = {}, {str(j["id"]): str(e) for j in pending}
                # Save each success right away, so a later usage limit
                # can't lose work that's already done.
                for job in pending:
                    result = results.get(str(job["id"]))
                    if result:
                        db.save_score(conn, job["id"], result, used_model, utc_now(), stamp)
                        scored += 1
                        outcome = str(result["score"])
                        if job["id"] in old_scores:
                            rechecked += 1
                            outcome = f"re-check {old_scores[job['id']]} -> {outcome}"
                        if on_result:
                            on_result(f"{job['company']}: {job['title']}", outcome)
                pending = [j for j in pending if str(j["id"]) in errors]
        except scorer.UsageLimitError as e:
            stopped_reason = f"usage limit: {e}"
            break  # everything unscored stays to_score for the next run

        for job in pending:  # still failing after the retry
            log_failure(job, errors[str(job["id"])])
            failed += 1
            if on_result:
                on_result(f"{job['company']}: {job['title']}", "failed (logged)")

    remaining = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'to_score' AND closed_at IS NULL"
        " AND duplicate_of IS NULL").fetchone()[0]
    stale_left = len(db.jobs_to_recheck(conn, stamp, recheck_min_score, None))
    conn.close()
    return {"scored": scored, "failed": failed, "remaining": remaining,
            "rechecked": rechecked, "recheck_remaining": stale_left,
            "stopped_reason": stopped_reason}


def main() -> None:
    parser = argparse.ArgumentParser(description="Score to_score jobs with Claude Code.")
    parser.add_argument("--limit", type=int, help="score at most this many jobs this run")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"jobs per Claude call (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt for one job without calling Claude")
    parser.add_argument("--model", help="model alias or name passed to claude --model")
    args = parser.parse_args()

    if args.dry_run:
        print(preview_prompt() or "No jobs with status to_score.")
        return

    summary = score_jobs(
        args.limit, args.model, args.batch_size,
        on_batch=lambda i, n, size: print(f"batch {i}/{n} ({size} jobs)..."),
        on_result=lambda label, outcome: print(f"  {outcome:>15}  {label}"))

    print(f"\nscored: {summary['scored']} (re-checks: {summary['rechecked']})   "
          f"failed: {summary['failed']}   still to_score: {summary['remaining']}   "
          f"re-checks waiting: {summary['recheck_remaining']}")
    if summary["failed"]:
        print(f"failure details: {ERROR_LOG}")
    if summary["stopped_reason"]:
        print(f"Stopped early, {summary['stopped_reason']}")
        sys.exit(2)


if __name__ == "__main__":
    main()
