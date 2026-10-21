"""Rate scored jobs yourself, so disagreements can become feedback rules.

Usage:
    python rate.py              rate unrated jobs (highest score first, some low ones mixed in)
    python rate.py --limit 10   stop after 10 jobs
    python rate.py --export     write every rating to data/ratings.csv

At each job: type 1-10 to rate, then optionally say why.
Other keys: d = show description, o = open in browser, s = skip, q = quit.
Each rating is saved as soon as you enter it, so quitting never loses work.
"""

import argparse
import csv
import json
import textwrap
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import db

EXPORT_PATH = Path(__file__).parent / "data" / "ratings.csv"

# Every LOW_SCORE_EVERY-th job comes from the bottom of the list, so you also
# catch good jobs the model under-scored, not just over-scored top picks.
LOW_SCORE_EVERY = 4


def rating_order(jobs: list[tuple]) -> list[tuple]:
    """Interleave: highest scores first, with one lowest-score job every few."""
    queue = deque(jobs)  # already sorted highest score first
    ordered = []
    while queue:
        take_low = len(ordered) % LOW_SCORE_EVERY == LOW_SCORE_EVERY - 1
        ordered.append(queue.pop() if take_low else queue.popleft())
    return ordered


def show(job: tuple, index: int, total: int) -> None:
    _, company, title, location, url, score, reason, matched, gaps, _ = job
    print(f"\n{'=' * 70}\n[{index}/{total}] {title}  ({company})")
    print(f"Location: {location}\n{url}")
    print(f"\nModel score: {score}/10")
    print(textwrap.fill(reason, width=70, initial_indent="  ", subsequent_indent="  "))
    print(f"Matched: {', '.join(json.loads(matched)) or '-'}")
    print(f"Gaps:    {', '.join(json.loads(gaps)) or '-'}")


def ask_rating(job: tuple) -> int | str:
    """Return a rating 1-10, or 's' (skip) / 'q' (quit)."""
    while True:
        answer = input("\nYour rating 1-10 (d=description, o=open, s=skip, q=quit): ").strip().lower()
        if answer == "d":
            print("\n" + job[9])
        elif answer == "o":
            webbrowser.open(job[4])
        elif answer in ("s", "q"):
            return answer
        elif answer.isdigit() and 1 <= int(answer) <= 10:
            return int(answer)
        else:
            print("Enter a number from 1 to 10, or d / o / s / q.")


def export(conn) -> None:
    headers, rows = db.all_ratings(conn)
    # utf-8-sig adds a byte-order mark so Excel opens accented characters correctly.
    with EXPORT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    print(f"Exported {len(rows)} ratings to {EXPORT_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rate scored jobs.")
    parser.add_argument("--limit", type=int, help="rate at most this many jobs")
    parser.add_argument("--export", action="store_true", help="write ratings to data/ratings.csv and exit")
    args = parser.parse_args()

    conn = db.connect()
    if args.export:
        export(conn)
        return

    jobs = rating_order(db.jobs_to_rate(conn))[:args.limit]
    if not jobs:
        print("Nothing to rate. Score some jobs first: python score.py --limit 20")
        return

    rated = 0
    for i, job in enumerate(jobs, 1):
        show(job, i, len(jobs))
        answer = ask_rating(job)
        if answer == "q":
            break
        if answer == "s":
            continue
        diff = answer - job[5]
        hint = " (you disagree; your reason is what becomes a rule)" if abs(diff) >= 2 else ""
        comment = input(f"Why?{hint} Enter to skip: ").strip()
        db.save_rating(conn, job[0], answer, comment,
                       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        rated += 1

    conn.close()
    print(f"\nRated {rated} job(s) this session.")


if __name__ == "__main__":
    main()
