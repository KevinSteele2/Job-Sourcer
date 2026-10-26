"""Local web app for Job Sourcer: fetch, score, rate, and manage feedback.

Start it by double-clicking start.bat (or `python app.py`). It opens
http://127.0.0.1:5050 in your browser. Close the console window to stop it.

Each page calls the same functions the command-line scripts use
(fetch.run_fetch, score.score_jobs, condense.propose_rules), so the two
never drift apart.
"""

import csv
import io
import json
import os
import re
import secrets
import socket
import threading
import urllib.error
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

import condense
import db
import drafter
import fetch
import import_feed
import liveness
import nightly
import score
import sweep
import targets
import scorer
from sources import ADAPTERS

HOST, PORT = "127.0.0.1", 5050
DATA_DIR = db.DB_PATH.parent
MODELS = [("sonnet", "Sonnet (balanced)"), ("haiku", "Haiku (uses the least quota)"),
          ("opus", "Opus"), ("", "Claude Code default")]
VIEW_LABELS = {"scored": "Scored", "to_score": "To score", "filtered_out": "Filtered out",
               "no_description": "Listing only", "closed": "Closed", "applied": "Applied & reached out",
               "targets": "★ Target companies", "old": f"Older than {db.MAX_AGE_DAYS} days"}

app = Flask(__name__)
# Flask signs its flash-message cookie with this. A fresh random key each
# start is fine for a local, single-user app (and nothing is hardcoded).
app.secret_key = secrets.token_hex(16)


@app.template_filter("days_ago")
def days_ago(value):
    """Calendar days (in your local time) since a UTC ISO timestamp, or None.

    Calendar days, not 24-hour blocks: something posted yesterday evening is
    "1d", not "today".
    """
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now().astimezone().date() - then.astimezone().date()).days)


@app.template_filter("local_date")
def local_date(value):
    """A UTC ISO timestamp as your local calendar date (YYYY-MM-DD)."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return (value or "")[:10]


@app.template_filter("fromjson")
def fromjson(value):
    return json.loads(value) if value else []


@app.template_filter("plus_days")
def plus_days(value, days):
    """A UTC ISO timestamp moved forward by `days`, still as an ISO timestamp."""
    when = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@app.context_processor
def inject_reminders():
    """Jobs you reached out about REMINDER_DAYS+ days ago and still haven't
    applied to: shown as a banner on every page until you mark them applied."""
    conn = db.connect()
    reminders = db.apply_reminders(conn)
    conn.close()
    return {"apply_reminders": reminders}


# A global (not a context variable), so imported macros like tracker() see it too.
app.jinja_env.globals["reminder_days"] = db.REMINDER_DAYS


@app.context_processor
def nav_counts():
    """Makes `counts` available in every template, for the nav badges."""
    conn = db.connect()
    counts = db.view_counts(conn)
    conn.close()
    return {"counts": counts, "scoring": SCORING.running,
            "importing": IMPORTING.running or SWEEPING.running, "sweeping": SWEEPING.running}


# ---------- Jobs ----------

@app.route("/")
def jobs():
    view = request.args.get("view", "scored")
    if view not in db.JOB_VIEWS:
        view = "scored"
    company = request.args.get("company", "")
    search = request.args.get("q", "").strip()
    sort = "newest" if request.args.get("sort") == "newest" else "score"

    conn = db.connect()
    rows = db.list_jobs(conn, view, company, search, sort)
    companies = db.companies(conn)
    conn.close()
    return render_template("jobs.html", rows=rows, view=view, views=VIEW_LABELS,
                           company=company, companies=companies, search=search, sort=sort)


@app.route("/job/<int:job_id>")
def job_detail(job_id):
    conn = db.connect()
    job = db.get_job(conn, job_id)
    has_application = db.get_application(conn, job_id) is not None
    events = db.app_events(conn, job_id)
    copies = db.duplicates(conn, job_id)
    conn.close()
    if job is None:
        flash("That job doesn't exist.", "error")
        return redirect(url_for("jobs"))
    return render_template("job.html", job=job, queue=None, has_application=has_application,
                           events=events, copies=copies)


# ---------- Application tracker ----------

@app.post("/job/<int:job_id>/status")
def job_status(job_id):
    status = request.form.get("status", "")
    conn = db.connect()
    job = db.get_job(conn, job_id)
    first_apply = status == "applied" and job is not None and not job["applied_at"]
    try:
        db.set_app_status(conn, job_id, status, request.form.get("note", "").strip(),
                          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        flash(f"Marked as {status.replace('_', ' ')}.", "ok")
    except ValueError:
        flash("Pick a status.", "error")
        first_apply = False
    conn.close()
    if first_apply:
        # Keep exactly what was sent: a later redraft overwrites the files.
        sent = drafter.snapshot_sent(job_id)
        if sent:
            flash(f"Saved a copy of the files you sent in \"{Path(sent).name}\".", "ok")
    # Back to whichever page the button was on (job page or draft page).
    return redirect(request.referrer or url_for("job_detail", job_id=job_id))


@app.post("/job/<int:job_id>/status/clear")
def job_status_clear(job_id):
    conn = db.connect()
    db.clear_app_status(conn, job_id)
    conn.close()
    flash("Application status cleared.", "ok")
    return redirect(request.referrer or url_for("job_detail", job_id=job_id))


@app.post("/job/<int:job_id>/rate")
def rate_job(job_id):
    rating = request.form.get("rating", type=int)
    if rating is None or not 1 <= rating <= 10:
        flash("Pick a rating from 1 to 10.", "error")
        return redirect(request.referrer or url_for("job_detail", job_id=job_id))

    conn = db.connect()
    db.save_rating(conn, job_id, rating, request.form.get("comment", "").strip(),
                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.close()

    # Came from the Rate queue: go on to the next job.
    if request.form.get("queue_n") is not None:
        return redirect(url_for("rate_queue", n=int(request.form["queue_n"]) + 1,
                                skip=request.form.get("queue_skip", "")))
    flash("Rating saved.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


# ---------- Rate queue ----------

@app.route("/rate")
def rate_queue():
    """Show the next unrated scored job.

    Same order as rate.py: highest score first, but every 4th job comes from
    the lowest scores. `n` counts jobs shown this session to know when it's
    a low one; `skip` holds the IDs you skipped, so they don't come straight back.
    """
    n = request.args.get("n", 0, type=int)
    skip_param = request.args.get("skip", "")
    skipped = {int(s) for s in skip_param.split(",") if s.isdigit()}

    conn = db.connect()
    waiting = [row for row in db.jobs_to_rate(conn) if row["id"] not in skipped]
    if not waiting:
        conn.close()
        return render_template("rate_done.html", skipped=len(skipped))

    pick = waiting[-1] if n % 4 == 3 else waiting[0]
    job = db.get_job(conn, pick["id"])
    conn.close()
    queue = {"n": n, "skip": skip_param, "remaining": len(waiting),
             "skip_url": url_for("rate_queue", n=n + 1,
                                 skip=",".join(str(i) for i in skipped | {job["id"]}))}
    return render_template("job.html", job=job, queue=queue)


# ---------- Fetch ----------

@app.route("/fetch", methods=["GET", "POST"])
def fetch_page():
    summary = None
    if request.method == "POST":
        ats = request.form.get("ats", "")
        slug = request.form.get("slug", "").strip().lower()
        if ats not in ADAPTERS or not slug:
            flash("Pick an ATS and enter a company slug.", "error")
        else:
            try:
                summary = fetch.run_fetch(ats, slug)
                summary["board"] = f"{ats}/{slug}"
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    flash(f'No {ats} board called "{slug}". Check the slug in the company\'s job board URL.', "error")
                else:
                    flash(f"{ats} returned HTTP {e.code} for {slug}.", "error")
            except urllib.error.URLError as e:
                flash(f"Network error: {e.reason}", "error")

    conn = db.connect()
    boards = db.boards(conn)
    feed = conn.execute("""
        SELECT SUM(closed_at IS NULL) AS open, MAX(last_seen) AS last_import
        FROM jobs WHERE feed = ?
    """, (import_feed.FEED,)).fetchone()
    sweep_info = conn.execute("""
        SELECT SUM(closed_at IS NULL) AS open, MAX(first_seen) AS last_sweep
        FROM jobs WHERE feed = ?
    """, (sweep.FEED,)).fetchone()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = db.usage_summary(conn, week_ago)
    target_rows = {r["company"]: r for r in db.target_summary(conn)}
    conn.close()
    return render_template("fetch.html", boards=boards, summary=summary, ats_names=list(ADAPTERS),
                           feed=feed, feed_categories=import_feed.load_categories(), sweep=sweep_info,
                           nightly=_last_nightly_run(), usage=usage,
                           targets=targets.load_companies(), target_rows=target_rows)


def _last_nightly_run() -> dict | None:
    """The latest line of data/nightly_runs.jsonl, written by nightly.py."""
    path = DATA_DIR / "nightly_runs.jsonl"
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1]) if lines else None


@app.post("/import/start")
def import_start():
    def work(task):
        def on_progress(i, total, label):
            task.update(index=i, total=total, current=label)
        return import_feed.run_import(on_progress, should_stop=task.should_stop)

    if not IMPORTING.start(work):
        flash("An import is already going.", "error")
    return redirect(url_for("fetch_page"))


@app.post("/import/stop")
def import_stop():
    IMPORTING.update(stop=True)
    return redirect(url_for("fetch_page"))


def _targets_by_key() -> dict:
    """companies.yaml by lowercased key and name, for badges and connection notes."""
    try:
        companies = targets.load_companies()
    except Exception:  # a typo in companies.yaml shouldn't break every page
        return {}
    return {k: c for c in companies for k in (c["key"].lower(), c["name"].lower())}


@app.context_processor
def inject_targets():
    return {"target_companies": _targets_by_key()}


# ---------- People you know ----------

def _company_key(name: str) -> str:
    """Lowercase letters and digits only: "LCS Living" -> "lcsliving"."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _word_prefixes(name: str) -> set[str]:
    """Keys of the name's leading words: "LCS Living" -> {"lcs", "lcsliving"}."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {"".join(words[:i]) for i in range(1, len(words) + 1)}


def _same_company(job_company: str, contact_company: str) -> bool:
    """Does a job's company (often a board name like "lcs") match a contact's
    company as you typed it ("LCS Living")?

    They match when one, ignoring case and punctuation, equals the other or
    its first few whole words: "lcs" matches "LCS Living", "Acme Robotics"
    matches "Acme". Partial words don't, so "meta" doesn't match "Metabase".
    """
    a, b = _company_key(job_company), _company_key(contact_company)
    if not a or not b:
        return False
    return a in _word_prefixes(contact_company) or b in _word_prefixes(job_company)


@app.context_processor
def inject_people():
    """people_at(company) in every template: the contacts you have there."""
    conn = db.connect()
    contacts = db.list_contacts(conn)
    conn.close()
    return {"people_at": lambda company: [c for c in contacts if _same_company(company, c["company"])]}


@app.route("/people")
def people():
    conn = db.connect()
    contacts = db.list_contacts(conn)
    counts = db.open_job_counts(conn)
    conn.close()
    # For each contact, the job boards at their company with open jobs:
    # [(board company name, count)], linked to the Jobs page filtered to it.
    jobs_at = {c["id"]: [(name, n) for name, n in counts.items() if _same_company(name, c["company"])]
               for c in contacts}
    editing = next((c for c in contacts if str(c["id"]) == request.args.get("edit")), None)
    return render_template("people.html", contacts=contacts, jobs_at=jobs_at, editing=editing)


@app.post("/people")
def people_save():
    if not request.form.get("name", "").strip() or not request.form.get("company", "").strip():
        flash("A contact needs a name and a company.", "error")
        return redirect(url_for("people"))
    conn = db.connect()
    db.save_contact(conn, request.form, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    request.form.get("id", type=int))
    conn.close()
    flash("Contact saved.", "ok")
    return redirect(url_for("people"))


@app.post("/people/<int:contact_id>/delete")
def people_delete(contact_id):
    conn = db.connect()
    db.delete_contact(conn, contact_id)
    conn.close()
    flash("Contact removed.", "ok")
    return redirect(url_for("people"))


@app.post("/targets")
def targets_check():
    """Synchronous, like the liveness check: a minute or two (Meta pages load in Edge)."""
    summary = targets.run_targets()
    parts = []
    for name, s in summary.items():
        parts.append(f"{name}: failed ({s['error']})" if "error" in s
                     else f"{name}: {s['fetched']} open, {s['new']} new, {s['passed']} passed the filter")
    flash("Target companies checked. " + "; ".join(parts) + ".",
          "error" if any("error" in s for s in summary.values()) else "ok")
    return redirect(url_for("fetch_page"))


@app.post("/liveness")
def liveness_check():
    """Synchronous: about 40 small requests, so it finishes in under a minute."""
    s = liveness.check_open()
    flash(f"Checked {s['checked']} top jobs: {s['open']} still open, {s['closed']} closed"
          + (f", {s['unknown']} couldn't be checked" if s["unknown"] else "") + ".", "ok")
    return redirect(url_for("fetch_page"))


@app.post("/sweep/start")
def sweep_start():
    def work(task):
        def on_progress(done, total, label):
            task.update(index=done, total=total, current=label)
        return sweep.run_sweep(on_progress, should_stop=task.should_stop)

    if not SWEEPING.start(work):
        flash("A sweep is already going.", "error")
    return redirect(url_for("fetch_page"))


@app.post("/sweep/stop")
def sweep_stop():
    SWEEPING.update(stop=True)
    return redirect(url_for("fetch_page"))


@app.route("/sweep/status")
def sweep_status():
    """Polled every 2 seconds by the Fetch page while a sweep is going."""
    return jsonify(SWEEPING.status())


@app.route("/import/status")
def import_status():
    """Polled every 2 seconds by the Fetch page while an import is going."""
    return jsonify(IMPORTING.status())


# ---------- Background tasks (scoring, importing) ----------

class BackgroundTask:
    """One long-running job in a background thread, with progress for the page.

    The thread writes progress into `state`; the page polls status() every
    2 seconds. The lock makes sure a request never reads `state` halfway
    through an update. Only one run per task at a time.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = self._fresh(running=False)

    @staticmethod
    def _fresh(running: bool) -> dict:
        return {"running": running, "stop": False, "index": 0, "total": 0, "current": None,
                "log": [], "summary": None, "error": None}

    def start(self, work) -> bool:
        """Run work(self) in a new thread. Returns False if already running."""
        with self.lock:
            if self.state["running"]:
                return False
            self.state = self._fresh(running=True)
        # daemon=True: closing the app doesn't wait for the run to finish.
        threading.Thread(target=self._run, args=(work,), daemon=True).start()
        return True

    def _run(self, work):
        try:
            self.update(summary=work(self))
        except Exception as e:  # show any crash on the page instead of losing it
            self.update(error=f"{type(e).__name__}: {e}")
        finally:
            self.update(running=False, current=None)

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def log(self, line: str):
        with self.lock:
            self.state["log"].append(line)

    def should_stop(self) -> bool:
        return self.state["stop"]

    @property
    def running(self) -> bool:
        return self.state["running"]

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "log": list(self.state["log"])}


SCORING = BackgroundTask()
IMPORTING = BackgroundTask()
SWEEPING = BackgroundTask()
DRAFTING = BackgroundTask()


# ---------- Score ----------

@app.route("/score")
def score_page():
    return render_template("score.html", models=MODELS,
                           profile_missing=not scorer.PROFILE_PATH.exists())


@app.post("/score/start")
def score_start():
    if not scorer.PROFILE_PATH.exists():
        flash("profile.md is missing. Scoring needs it.", "error")
        return redirect(url_for("score_page"))
    if nightly.is_running():
        # Both would pull the same jobs from jobs_to_score and score them twice.
        flash("The nightly run is going right now (it scores too). Try again when it finishes.", "error")
        return redirect(url_for("score_page"))
    limit = request.form.get("limit", type=int)
    batch_size = request.form.get("batch_size", score.DEFAULT_BATCH_SIZE, type=int)
    model = request.form.get("model") or None

    def work(task):
        def on_batch(i, n_batches, size):
            task.update(index=i, total=n_batches, current=f"{size} jobs")

        def on_result(label, outcome):
            task.log(f"{outcome:>3}  {label}")

        return score.score_jobs(limit, model, batch_size, on_batch, on_result,
                                should_stop=task.should_stop)

    if not SCORING.start(work):
        flash("A scoring run is already going.", "error")
    return redirect(url_for("score_page"))


@app.post("/score/stop")
def score_stop():
    SCORING.update(stop=True)
    return redirect(url_for("score_page"))


@app.route("/score/status")
def score_status():
    """Polled every 2 seconds by the Score page while a run is going."""
    return jsonify(SCORING.status())


# ---------- Applications (resume + cover letter drafts) ----------

# Plain-English names for scorer.GATE_CAPS, for the warning before drafting.
GATE_LABELS = {
    "experience_5plus": "Requires 5+ years of experience",
    "experience_3to4": "Requires 3-4 years of experience",
    "clearance": "Requires an active security clearance",
    "credential": "Requires a license, certification, or advanced degree you don't have",
    "management": "Requires people-management experience",
    "language": "Requires fluency in a language other than English",
    "location": "Requires living or being authorized to work outside the US",
    "enrollment": "Only for current students or later graduates",
}


@app.post("/job/<int:job_id>/draft")
def draft_start(job_id):
    model = request.form.get("model") or drafter.DEFAULT_MODEL
    notes = request.form.get("notes", "")
    resume_only = request.form.get("resume_only") == "1"  # an unchecked box isn't sent
    if DRAFTING.running:
        flash("Another draft is still being written. Try again when it finishes.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    # Step 0: a requirement the scorer found, with its quote verified in the
    # posting, that the profile can't meet (years, clearance, current
    # student...). Show the posting's own words and ask before spending a
    # draft on it. "gates_ok" is set by that page's "Draft anyway" button.
    if request.form.get("gates_ok") != "1" and request.form.get("gaps_done") != "1":
        conn = db.connect()
        job = db.get_job(conn, job_id)
        conn.close()
        gates = [g for g in json.loads(job["hard_gates"] or "[]") if g.get("verified")] if job else []
        if gates:
            return render_template("draft_gates.html", job=job, gates=gates, labels=GATE_LABELS,
                                   form=request.form)

    if request.form.get("gaps_done") == "1":
        _save_gap_answers(job_id, request.form)
    elif request.form.get("check_gaps") == "1":
        # Step 1: ask about skills the posting wants that the profile doesn't show.
        # This waits for one quick Sonnet call (the button says so while it runs).
        try:
            gaps = drafter.find_gaps(job_id)
        except Exception as e:  # the check helps, but never blocks a draft
            gaps = []
            flash(f"The skill check didn't work ({str(e)[:200]}), so drafting went ahead without it.", "error")
        if gaps:
            conn = db.connect()
            job = db.get_job(conn, job_id)
            conn.close()
            return render_template("draft_gaps.html", job=job, gaps=gaps, model=model,
                                   notes=notes, resume_only=resume_only)

    def work(task):
        task.update(total=job_id)  # which job is being drafted, for the status page
        return drafter.make_draft(job_id, model, on_step=lambda m: task.update(current=m), notes=notes,
                                  resume_only=resume_only)

    if not DRAFTING.start(work):
        flash("Another draft is still being written. Try again when it finishes.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    return redirect(url_for("application_page", job_id=job_id))


def _save_gap_answers(job_id: int, form) -> None:
    """Record each answered skill question; "yes" ones also go into full-profile.md.

    Form fields per question i: skill_i, answer_i ("yes"/"no", absent if left
    unanswered) and details_i. Unanswered questions aren't saved, so they're
    asked again next time.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect()
    job = db.get_job(conn, job_id)
    confirmed = []
    for i in range(int(form.get("gap_count", 0))):
        skill, answer = form.get(f"skill_{i}", "").strip(), form.get(f"answer_{i}")
        details = form.get(f"details_{i}", "").strip()
        if not skill or answer not in ("yes", "no"):
            continue
        db.save_skill_answer(conn, skill, answer, details, job_id, now)
        if answer == "yes":
            confirmed.append((skill, details))
    conn.close()
    if confirmed:
        drafter.add_confirmed_skills(confirmed, job)
        flash(f"Added to full-profile.md: {', '.join(s for s, _ in confirmed)}.", "ok")


@app.route("/draft/status")
def draft_status():
    """Polled every 2 seconds while a draft is being written."""
    return jsonify(DRAFTING.status())


@app.route("/applications")
def applications():
    conn = db.connect()
    rows = db.list_applications(conn)
    conn.close()
    return render_template("applications.html", rows=rows)


@app.route("/application/<int:job_id>")
def application_page(job_id):
    conn = db.connect()
    job = db.get_job(conn, job_id)
    app_row = db.get_application(conn, job_id)
    conn.close()
    if job is None:
        abort(404)
    drafting_this = DRAFTING.running and DRAFTING.state["total"] == job_id
    status = DRAFTING.status() if DRAFTING.state["total"] == job_id else None
    if app_row is None or drafting_this:
        return render_template("application_progress.html", job=job, status=status)
    # A feedback or revision run for this job that failed: show it once.
    if status and status["error"]:
        flash(f"That didn't finish: {status['error']}", "error")
        DRAFTING.update(error=None)
    rendered = None
    if app_row["folder"]:
        folder = Path(app_row["folder"])
        name = drafter.contact_info(drafter.read_profile())["name"]
        rendered = {kind: f for kind, f in (("resume", f"{name} Resume.pdf"),
                                            ("cover", f"{name} Cover Letter.pdf"),
                                            ("resume_docx", f"{name} Resume.docx"),
                                            ("cover_docx", f"{name} Cover Letter.docx"))
                    if (folder / f).exists()}
    conn = db.connect()
    events = db.app_events(conn, job_id)
    versions = db.versions(conn, job_id)
    conn.close()
    draft = json.loads(app_row["draft"])
    drafter.upgrade_resume(draft["resume"])  # drafts from before bullets open in the new editor
    edits = _hand_edits(versions, draft)
    return render_template("application.html", job=job, app=app_row, events=events,
                           draft=draft,
                           review=json.loads(app_row["review"] or "{}"),
                           rendered=rendered, models=MODELS, versions=versions, edits=edits,
                           edits_changed=sum(1 for l in edits or [] if l[:1] in "+-"
                                             and not l.startswith(("+++", "---"))))


def _draft_lines(draft: dict) -> list[str]:
    """A draft as plain lines of what gets printed, for comparing versions."""
    resume = drafter.upgrade_resume(json.loads(json.dumps(draft["resume"])))
    lines = [resume["summary"]]
    for p in resume["projects"]:
        lines += [f"{p['name']} | {p.get('tech', '')}", *(f"- {b}" for b in p["bullets"])]
    for e in resume["experience"]:
        lines += [e["heading"], *(f"- {b}" for b in e["bullets"])]
    lines += [f"{s['label']}: {', '.join(s['items'])}" for s in resume["skills"]]
    if draft.get("cover_letter"):
        lines += draft["cover_letter"]["paragraphs"]
    return lines


def _hand_edits(versions, current: dict) -> list[str] | None:
    """What you changed by hand: the latest generated (or revised) version
    against the sent version, or against the current draft if not sent yet.
    A unified diff, or None if there's nothing to compare."""
    import difflib
    made = [v for v in versions if v["kind"] in ("generated", "revised")]
    if not made:
        return None
    sent = [v for v in versions if v["kind"] == "sent"]
    after = json.loads(sent[-1]["draft"]) if sent else current
    diff = list(difflib.unified_diff(_draft_lines(json.loads(made[-1]["draft"])), _draft_lines(after),
                                     "as generated", "as sent" if sent else "now", lineterm="", n=0))
    return [l for l in diff if not l.startswith("@@")]


def _draft_from_form(form, old: dict) -> dict:
    """Rebuild the draft from the editor. A blank item is removed."""
    def text(name):
        # Browsers send textarea line breaks as \r\n; normalize to \n.
        return form.get(name, "").replace("\r\n", "\n")

    def lines(name, sep="\n"):
        return [x.strip() for x in text(name).split(sep) if x.strip()]

    def items(prefix, key, extra=()):
        """Entries with a name/heading, optional extra text fields, and a
        bullets box (one bullet per line). No name or no bullets = removed."""
        out, i = [], 0
        while f"{prefix}_{key}_{i}" in form:
            item = {key: form.get(f"{prefix}_{key}_{i}", "").strip(),
                    **{f: form.get(f"{prefix}_{f}_{i}", "").strip() for f in extra},
                    "bullets": lines(f"{prefix}_bullets_{i}")}
            if item[key] and item["bullets"]:
                out.append(item)
            i += 1
        return out

    def skill_lines():
        # "Languages: Python, SQL" -> {"label": "Languages", "items": [...]};
        # a line with no colon is filed under "Skills".
        out = []
        for line in lines("skills"):
            label, colon, rest = line.partition(":")
            label, rest = (label.strip(), rest) if colon else ("Skills", label)
            entries = [x.strip() for x in rest.split(",") if x.strip()]
            if entries:
                out.append({"label": label, "items": entries})
        return out

    draft = dict(old)
    draft["resume"] = {
        "summary": form.get("summary", "").strip(),
        "projects": items("project", "name", ("tech",)),
        "experience": items("exp", "heading"),
        "skills": skill_lines(),
        "education": form.get("education", "").strip(),
        "coursework": lines("coursework", ","),
    }
    if not old.get("cover_letter"):
        return draft  # resume only: the editor has no letter fields
    # Paragraphs are separated by a blank line in the editor.
    paragraphs = [p.strip() for p in text("paragraphs").split("\n\n") if p.strip()]
    draft["cover_letter"] = {"greeting": form.get("greeting", "").strip(), "paragraphs": paragraphs,
                             "closing": form.get("closing", "").strip() or "Sincerely,",
                             # An unchecked checkbox isn't sent at all, so absent means off.
                             "show_header": form.get("show_header") == "1"}
    return draft


@app.post("/application/<int:job_id>/save")
def application_save(job_id):
    conn = db.connect()
    app_row = db.get_application(conn, job_id)
    if app_row is None:
        abort(404)
    draft = _draft_from_form(request.form, json.loads(app_row["draft"]))
    # Your edits can't drift from the fixed lines either: a known heading or
    # project name is put back to its canonical form, anything else is flagged.
    for warning in drafter.canonical_checks(drafter.read_profile(), draft["resume"]):
        flash(warning, "error")
    db.update_draft(conn, job_id, draft, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.close()

    dashes = drafter.em_dash_count(draft)
    if dashes:
        flash(f"Your edits contain {dashes} em dash(es); you don't use them. "
              "Replace with a comma, period, or parentheses.", "error")
    colons = drafter.letter_colon_count(draft)
    if colons > drafter.MAX_LETTER_COLONS:
        flash(f"The letter has {colons} colons; your rule is at most "
              f"{drafter.MAX_LETTER_COLONS}.", "error")
    if request.form.get("action") == "feedback":
        # Review the letter as just saved, in the background (about a minute).
        def work(task):
            task.update(total=job_id)
            return drafter.review_letter(job_id, on_step=lambda m: task.update(current=m))
        if not DRAFTING.start(work):
            flash("Claude is busy with another draft. Try again when it finishes.", "error")
        return redirect(url_for("application_page", job_id=job_id))
    if request.form.get("action") != "render":
        flash("Draft saved.", "ok")
        return redirect(url_for("application_page", job_id=job_id))
    try:
        out = drafter.render(job_id)
    except (drafter.DraftError, FileNotFoundError) as e:
        flash(f"Couldn't make the files: {e}", "error")
        return redirect(url_for("application_page", job_id=job_id))
    if out["resume_pages"] > 1:
        flash(f"The resume runs to {out['resume_pages']} pages. Shorten a few lines and generate again.", "error")
    else:
        flash("PDF and Word files ready.", "ok")
    return redirect(url_for("application_page", job_id=job_id))


@app.post("/application/<int:job_id>/revise")
def application_revise(job_id):
    """Rewrite only the cover letter with the feedback you chose (background)."""
    feedback = request.form.get("feedback", "").replace("\r\n", "\n").strip()
    if not feedback:
        flash("Write or keep at least one change for the revision.", "error")
        return redirect(url_for("application_page", job_id=job_id))

    def work(task):
        task.update(total=job_id)
        return drafter.revise_letter(job_id, feedback, on_step=lambda m: task.update(current=m))

    if not DRAFTING.start(work):
        flash("Claude is busy with another draft. Try again when it finishes.", "error")
    return redirect(url_for("application_page", job_id=job_id))


@app.route("/application/<int:job_id>/file/<kind>")
def application_file(job_id, kind):
    conn = db.connect()
    app_row = db.get_application(conn, job_id)
    conn.close()
    fmt = request.args.get("format", "pdf")
    if app_row is None or not app_row["folder"] or kind not in ("resume", "cover") or fmt not in ("pdf", "docx"):
        abort(404)
    name = drafter.contact_info(drafter.read_profile())["name"]
    filename = f"{name} Resume.{fmt}" if kind == "resume" else f"{name} Cover Letter.{fmt}"
    # A .docx can't show in the browser, so it downloads; a PDF opens in a tab.
    return send_from_directory(app_row["folder"], filename, as_attachment=fmt == "docx")


@app.post("/application/<int:job_id>/open-folder")
def application_open_folder(job_id):
    conn = db.connect()
    app_row = db.get_application(conn, job_id)
    conn.close()
    if app_row and app_row["folder"] and Path(app_row["folder"]).exists():
        os.startfile(app_row["folder"])  # opens it in File Explorer (Windows only)
    return redirect(url_for("application_page", job_id=job_id))


@app.route("/score/preview")
def score_preview():
    return render_template("preview.html", prompt=score.preview_prompt())


# ---------- Feedback ----------

def _read_feedback() -> str:
    path = scorer.FEEDBACK_PATH
    return path.read_text(encoding="utf-8") if path.exists() else ""


@app.route("/feedback")
def feedback_page():
    threshold = request.args.get("threshold", 2, type=int)
    conn = db.connect()
    stats = db.rating_stats(conn, threshold)
    conn.close()
    return render_template("feedback.html", current=_read_feedback(), stats=stats,
                           threshold=threshold, models=MODELS, proposal=None)


@app.post("/feedback/propose")
def feedback_propose():
    threshold = request.form.get("threshold", 2, type=int)
    proposal = None
    try:
        info = condense.propose_rules(threshold, request.form.get("model") or None)
        if info["rules"]:
            proposal = info
        else:
            flash(f"No rated jobs with a comment differ from the model by {threshold}+ yet. "
                  "Rate more jobs, and say why when you disagree.", "error")
    except scorer.UsageLimitError as e:
        flash(f"Usage limit reached, try later: {e}", "error")
    except scorer.ScoringError as e:
        flash(f"Claude call failed: {e}", "error")

    conn = db.connect()
    stats = db.rating_stats(conn, threshold)
    conn.close()
    return render_template("feedback.html", current=_read_feedback(), stats=stats,
                           threshold=threshold, models=MODELS, proposal=proposal)


@app.post("/feedback/save")
def feedback_save():
    # Browsers send textarea line breaks as \r\n; normalize to \n.
    text = request.form.get("text", "").replace("\r\n", "\n").strip()
    scorer.FEEDBACK_PATH.write_text(text + "\n" if text else "", encoding="utf-8")
    flash("Saved feedback.md. New scores will use these rules.", "ok")
    return redirect(url_for("feedback_page"))


@app.route("/ratings.csv")
def ratings_csv():
    conn = db.connect()
    headers, rows = db.all_ratings(conn)
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    # utf-8-sig adds a byte-order mark so Excel opens accented characters correctly.
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ratings.csv"})


# ---------- Start ----------

def _already_running() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if _already_running():
        # Double-clicked start.bat twice: just open the existing app.
        print(f"Job Sourcer is already running at {url}")
        webbrowser.open(url)
    else:
        print(f"Job Sourcer running at {url}. Close this window to stop it.")
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        # 127.0.0.1 = only this computer can reach the app.
        app.run(host=HOST, port=PORT, debug=False)
