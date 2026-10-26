"""Windows reminder: "time to apply" for jobs you reached out about.

When you mark a job "Reached out for a referral" and haven't applied
db.REMINDER_DAYS (2) days later, this shows a Windows notification for it,
once per reach-out. A Task Scheduler task ("Job Sourcer Reminders") runs it
every hour, so the alert comes within an hour of being due rather than at the
next nightly run. The app also shows a banner for these jobs on every page
until you mark them applied.

Which reminders were already shown is kept in data/reminded.json (keyed by
job and reach-out time, so reaching out again later gets a fresh reminder).

    python remind.py    # check now
"""
import json
from pathlib import Path

import db
import notify

REMINDED_PATH = Path(__file__).parent / "data" / "reminded.json"


def due_now() -> list:
    conn = db.connect()
    rows = db.apply_reminders(conn)
    conn.close()
    return rows


def notify_due() -> int:
    """Notify about reminders not shown before. Returns how many were shown."""
    try:
        shown = set(json.loads(REMINDED_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        shown = set()
    new = [r for r in due_now() if f"{r['id']}@{r['reached_at']}" not in shown]
    if not new:
        return 0
    if len(new) == 1:
        r = new[0]
        who = f" ({r['note']})" if r["note"] else ""
        notify.notify("Time to apply", f"No referral yet from {r['company']}{who}. "
                                       f"Apply directly: {r['title']}")
    else:
        names = ", ".join(r["company"] for r in new)
        notify.notify("Time to apply", f"{len(new)} jobs you reached out about: {names}. "
                                       "Apply directly, then mark them applied.")
    shown |= {f"{r['id']}@{r['reached_at']}" for r in new}
    REMINDED_PATH.write_text(json.dumps(sorted(shown), indent=1), encoding="utf-8")
    return len(new)


if __name__ == "__main__":
    print(f"{notify_due()} reminder(s) shown")
