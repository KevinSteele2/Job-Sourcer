"""SQLite storage for jobs and your ratings."""

import json
import sqlite3
from pathlib import Path

from job import Job

DB_PATH = Path(__file__).parent / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    company        TEXT NOT NULL,
    ats_job_id     TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT NOT NULL,
    url            TEXT NOT NULL,
    description    TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    closed_at      TEXT,
    status         TEXT NOT NULL,
    filter_reason  TEXT,
    UNIQUE (source, company, ats_job_id)
)
"""

# Your own ratings, one per job. model_score and prompt_version are copied
# from the job at rating time, so a later re-score can't change what you
# were disagreeing with.
RATINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ratings (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL UNIQUE REFERENCES jobs (id),
    my_rating       INTEGER NOT NULL,
    comment         TEXT,
    model_score     INTEGER,
    prompt_version  TEXT,
    rated_at        TEXT NOT NULL
)
"""

# Drafted resume + cover letter, one per job. `draft` is the JSON the drafter
# produced (and you edited); `review` holds the truth check's findings.
APPLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL UNIQUE REFERENCES jobs (id),
    draft        TEXT NOT NULL,
    review       TEXT,
    model        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    folder       TEXT,
    rendered_at  TEXT
)
"""

# Your application history for a job: one row per status change, with an
# optional note ("phone screen 10/12"). jobs.app_status holds the latest one.
APPLICATION_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS application_events (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES jobs (id),
    status  TEXT NOT NULL,
    note    TEXT,
    at      TEXT NOT NULL
)
"""

# One row per `claude -p` call: what it was for and what it used. Filled in
# from the JSON envelope Claude Code returns. cost_usd is what the call would
# cost on the API; on a Claude plan it's a measure of usage, not a bill.
CLAUDE_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_calls (
    id                  INTEGER PRIMARY KEY,
    at                  TEXT NOT NULL,
    stage               TEXT NOT NULL,
    model               TEXT,
    effort              TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    cost_usd            REAL,
    duration_ms         INTEGER,
    ok                  INTEGER
)
"""

# Web research on a company (research.py), cached by board name so several
# roles at one company share one research run. `research` is JSON.
COMPANY_RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_research (
    company   TEXT PRIMARY KEY,
    research  TEXT NOT NULL,
    at        TEXT NOT NULL
)
"""

# Your answers to "Have you used X?" before a draft (drafter.find_gaps).
# A "no" here means the skill isn't asked about again on later drafts; a "yes"
# is also written into full-profile.md. `skill` is stored lowercased.
SKILL_ANSWERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_answers (
    skill       TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,       -- 'yes' or 'no'
    details     TEXT,
    job_id      INTEGER,             -- the draft it was asked for
    answered_at TEXT NOT NULL
)
"""

# Every version of a job's draft: "generated" (what the drafter produced,
# before your edits), "revised" (after a letter revision), "rendered" (what
# Save & generate made files from), and "sent" (the rendered version current
# when you marked the job applied). Comparing generated with sent shows what
# you change by hand, which is the best signal for improving the prompts.
DRAFT_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_versions (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES jobs (id),
    kind    TEXT NOT NULL,
    draft   TEXT NOT NULL,       -- the draft JSON at that moment
    files   TEXT,                -- for "sent": folder holding copies of the files sent
    at      TEXT NOT NULL
)
"""

# People you know, and where they work (the People page). Jobs at a matching
# company show them (see app.people_at for how names are matched).
CONTACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    company   TEXT NOT NULL,
    role      TEXT,              -- their job, e.g. "Talent acquisition"
    how       TEXT,              -- how you know them
    notes     TEXT,
    added_at  TEXT NOT NULL
)
"""

# Columns added after the table was first created. SQLite's CREATE TABLE IF
# NOT EXISTS won't change an existing table, so connect() adds any of these
# that are missing with ALTER TABLE. New columns go here, not in SCHEMA.
ADDED_COLUMNS = {
    "score": "INTEGER",
    "reason": "TEXT",
    "matched_skills": "TEXT",   # JSON array, e.g. '["Python", "SQL"]'
    "gaps": "TEXT",             # JSON array
    "model": "TEXT",
    "scored_at": "TEXT",
    "prompt_version": "TEXT",
    "feed": "TEXT",             # which job list it was imported from ("simplify"), NULL for board fetches
    "fit_score": "INTEGER",     # the model's score before hard-gate caps
    "hard_gates": "TEXT",       # JSON: [{"gate", "quote", "verified"}]
    "checked_at": "TEXT",       # last time liveness.py confirmed the posting was open
    "priority": "INTEGER",      # scoring order: higher is scored first (filters.priority)
    "app_status": "TEXT",       # where your application stands: applied, interviewing, offer, rejected
    "applied_at": "TEXT",       # when you marked it applied (UTC)
    "duplicate_of": "INTEGER",  # id of the copy this one repeats (mark_duplicates)
    "target": "INTEGER",        # 1 = a company in companies.yaml (targets.mark_targets)
    "posted_at": "TEXT",        # when the company posted it (UTC); NULL if the source doesn't say
}

# Statuses the rule-based filter is allowed to set or overwrite. Anything later
# in the pipeline (scored, applied, ...) is left alone. "no_description" is a
# listing-only job (no description to score); it can still be filtered out.
FILTERABLE_STATUSES = ("to_score", "filtered_out", "no_description")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path)
    # Rows can be read by position (row[0]) or by column name (row["title"]).
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(RATINGS_SCHEMA)
    conn.execute(APPLICATIONS_SCHEMA)
    conn.execute(APPLICATION_EVENTS_SCHEMA)
    conn.execute(CLAUDE_CALLS_SCHEMA)
    conn.execute(COMPANY_RESEARCH_SCHEMA)
    conn.execute(SKILL_ANSWERS_SCHEMA)
    conn.execute(CONTACTS_SCHEMA)
    conn.execute(DRAFT_VERSIONS_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, sql_type in ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
    conn.commit()
    return conn


def save_jobs(conn: sqlite3.Connection, source: str, company: str,
              jobs: list[Job], now: str) -> dict:
    """Insert new jobs, refresh existing ones, and close jobs that disappeared.

    Only touches rows for this one source + company, so fetching one board
    never closes another board's jobs. Returns counts for the run summary.
    """
    existing = {row[0] for row in conn.execute(
        "SELECT ats_job_id FROM jobs WHERE source = ? AND company = ?", (source, company))}

    with conn:  # one transaction: commits on success, rolls back on error
        for job in jobs:
            # "Upsert": insert, or if (source, company, ats_job_id) already
            # exists, update that row instead. first_seen and status are only
            # set on insert, so they survive later runs.
            conn.execute("""
                INSERT INTO jobs (source, company, ats_job_id, title, location, url,
                                  description, first_seen, last_seen, status, posted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'to_score', NULLIF(?, ''))
                ON CONFLICT (source, company, ats_job_id) DO UPDATE SET
                    title = excluded.title,
                    -- An empty location or description means "not fetched this
                    -- time" (the target-company readers skip detail pages they
                    -- already have), so it never wipes out a stored one.
                    location = CASE WHEN excluded.location = '' THEN jobs.location
                                    ELSE excluded.location END,
                    url = excluded.url,
                    description = CASE WHEN excluded.description = '' THEN jobs.description
                                       ELSE excluded.description END,
                    last_seen = excluded.last_seen,
                    posted_at = COALESCE(excluded.posted_at, jobs.posted_at),
                    closed_at = NULL
            """, (job.source, job.company, job.ats_job_id, job.title, job.location,
                  job.url, job.description, now, now, job.posted_at))

        # Every job on the board just got last_seen = now, so any open job
        # from this board with an older last_seen wasn't on the board.
        closed = conn.execute("""
            UPDATE jobs SET closed_at = ?
            WHERE source = ? AND company = ? AND closed_at IS NULL AND last_seen < ?
        """, (now, source, company, now)).rowcount

    new = sum(1 for job in jobs if job.ats_job_id not in existing)
    return {"fetched": len(jobs), "new": new, "closed": closed}


def jobs_to_filter(conn: sqlite3.Connection, source: str, company: str) -> list[tuple]:
    """Open jobs from this board that haven't moved past the filter stage."""
    placeholders = ", ".join("?" for _ in FILTERABLE_STATUSES)
    return conn.execute(f"""
        SELECT id, title, location FROM jobs
        WHERE source = ? AND company = ? AND closed_at IS NULL
          AND status IN ({placeholders})
    """, (source, company, *FILTERABLE_STATUSES)).fetchall()


def set_filter_results(conn: sqlite3.Connection, results: list[tuple[int, str | None]]) -> None:
    """results: (row id, reason). reason None means the job passed.

    A passing job goes to to_score, unless it has no description: then it
    stays no_description, since there's nothing for Claude to score.
    """
    with conn:
        conn.executemany("""
            UPDATE jobs SET
                status = CASE
                    WHEN ?1 IS NOT NULL THEN 'filtered_out'
                    WHEN description = '' THEN 'no_description'
                    ELSE 'to_score'
                END,
                filter_reason = ?1
            WHERE id = ?2
        """, [(reason, row_id) for row_id, reason in results])
    mark_duplicates(conn)


# ---- Duplicate postings ----
# Some companies post one job once per city (one posting appeared 49 times).
# Copies with the same company, title and description are one job: only the
# primary copy is scored and listed, and its score is copied to the others.

DUPLICATE_STATUSES = ("to_score", "scored")

_SCORE_FIELDS = ("status", "score", "fit_score", "reason", "matched_skills", "gaps",
                 "hard_gates", "model", "scored_at", "prompt_version")


def _duplicate_key(row) -> tuple[str, str, str]:
    """Company, title, and a fingerprint of the description, ignoring case and spacing.

    The description has to match exactly: copies that differ even a little
    (one Palantir copy required a clearance, another didn't) can deserve
    different scores, so they stay separate jobs.
    """
    import hashlib
    description = " ".join(row["description"].split())
    return (row["company"].lower(), " ".join(row["title"].lower().split()),
            hashlib.sha1(description.encode("utf-8")).hexdigest())


def mark_duplicates(conn: sqlite3.Connection) -> int:
    """Regroup open jobs into duplicates. Returns how many copies point at another.

    Recomputed from scratch each time, so if a primary copy closes, one of its
    copies (which already has the score) simply becomes the new primary.
    A copy you've rated, drafted, or tracked is never hidden behind another.
    """
    placeholders = ", ".join("?" for _ in DUPLICATE_STATUSES)
    rows = conn.execute(f"""
        SELECT j.id, j.company, j.title, j.description, j.status,
               (r.id IS NOT NULL OR a.id IS NOT NULL OR j.app_status IS NOT NULL) AS active
        FROM jobs j
        LEFT JOIN ratings r ON r.job_id = j.id
        LEFT JOIN applications a ON a.job_id = j.id
        WHERE j.closed_at IS NULL AND j.status IN ({placeholders})
        ORDER BY j.id
    """, DUPLICATE_STATUSES).fetchall()
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault(_duplicate_key(row), []).append(row)

    links = []  # (copy id, primary id)
    for members in groups.values():
        if len(members) < 2:
            continue
        # Primary: one you've acted on, else one already scored, else the oldest.
        primary = min(members, key=lambda r: (not r["active"], r["status"] != "scored", r["id"]))
        links += [(r["id"], primary["id"]) for r in members
                  if r["id"] != primary["id"] and not r["active"]]

    copy = ", ".join(f"{f} = p.{f}" for f in _SCORE_FIELDS)
    with conn:
        conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL")
        conn.executemany("UPDATE jobs SET duplicate_of = ? WHERE id = ?",
                         [(primary, copy_id) for copy_id, primary in links])
        # Copies take the primary's score, so the whole group always agrees.
        conn.execute(f"""
            UPDATE jobs SET {copy}
            FROM jobs p WHERE jobs.duplicate_of = p.id AND p.status = 'scored'
        """)
    return len(links)


# ---- Job-list feeds (import_feed.py) ----
# A feed lists jobs from many companies. Unlike a board fetch, it only covers
# some of each board's jobs, so it must never use save_jobs' per-board closing.

def known_keys(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """(source, company, ats_job_id) of every job already stored."""
    return {(r[0], r[1], r[2]) for r in conn.execute("SELECT source, company, ats_job_id FROM jobs")}


def touch_jobs(conn: sqlite3.Connection, keys: list[tuple[str, str, str]], now: str,
               posted: dict | None = None) -> None:
    """Mark already-stored jobs as seen this run (and reopen them if closed).

    posted: {key: posting date} from the list just read. Fills in the date for
    jobs stored without one (everything stored before dates were kept).
    """
    with conn:
        conn.executemany("""
            UPDATE jobs SET last_seen = ?, closed_at = NULL
            WHERE source = ? AND company = ? AND ats_job_id = ?
        """, [(now, *key) for key in keys])
        conn.executemany("""
            UPDATE jobs SET posted_at = ?
            WHERE source = ? AND company = ? AND ats_job_id = ? AND posted_at IS NULL
        """, [(posted[key], *key) for key in keys if (posted or {}).get(key)])


def insert_feed_jobs(conn: sqlite3.Connection, jobs: list[Job], feed: str, now: str) -> None:
    """Insert newly discovered jobs. A job without a description is listing-only."""
    with conn:
        conn.executemany("""
            INSERT INTO jobs (source, company, ats_job_id, title, location, url,
                              description, first_seen, last_seen, status, feed, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''))
            ON CONFLICT (source, company, ats_job_id) DO NOTHING
        """, [(j.source, j.company, j.ats_job_id, j.title, j.location, j.url, j.description,
               now, now, "to_score" if j.description else "no_description", feed, j.posted_at)
              for j in jobs])


def close_missing_feed_jobs(conn: sqlite3.Connection, feed: str, now: str) -> int:
    """Close jobs imported from this feed that it no longer lists."""
    with conn:
        return conn.execute("""
            UPDATE jobs SET closed_at = ?
            WHERE feed = ? AND closed_at IS NULL AND last_seen < ?
        """, (now, feed, now)).rowcount


def all_jobs_to_filter(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every open job that hasn't moved past the filter stage, from any source."""
    placeholders = ", ".join("?" for _ in FILTERABLE_STATUSES)
    return conn.execute(f"""
        SELECT id, title, location, description, feed, target FROM jobs
        WHERE closed_at IS NULL AND status IN ({placeholders})
    """, FILTERABLE_STATUSES).fetchall()


def close_missing_board_jobs(conn: sqlite3.Connection, boards: list[tuple[str, str]],
                             now: str) -> int:
    """Close open jobs from these boards that weren't seen on them this run.

    Only for boards whose full list was just downloaded (the sweep); every job
    still on them was given last_seen = now first.
    """
    with conn:
        cursor = conn.executemany("""
            UPDATE jobs SET closed_at = ?
            WHERE source = ? AND company = ? AND closed_at IS NULL AND last_seen < ?
        """, [(now, ats, slug, now) for ats, slug in boards])
        return cursor.rowcount


def set_priorities(conn: sqlite3.Connection, priorities: list[tuple[int, int]]) -> None:
    """priorities: (row id, priority)."""
    with conn:
        conn.executemany("UPDATE jobs SET priority = ? WHERE id = ?",
                         [(p, row_id) for row_id, p in priorities])


def feed_jobs_to_filter(conn: sqlite3.Connection, feed: str) -> list[tuple]:
    """Open jobs from this feed that haven't moved past the filter stage."""
    placeholders = ", ".join("?" for _ in FILTERABLE_STATUSES)
    return conn.execute(f"""
        SELECT id, title, location FROM jobs
        WHERE feed = ? AND closed_at IS NULL AND status IN ({placeholders})
    """, (feed, *FILTERABLE_STATUSES)).fetchall()


def jobs_to_score(conn: sqlite3.Connection, limit: int | None = None) -> list[tuple]:
    """Open jobs waiting for AI scoring, most promising first (then newest).

    Jobs older than MAX_AGE_DAYS are skipped (FRESH), so no usage goes to them.
    """
    sql = f"""
        SELECT id, company, title, location, description FROM jobs j
        WHERE status = 'to_score' AND closed_at IS NULL AND duplicate_of IS NULL AND {FRESH}
        ORDER BY COALESCE(priority, 0) DESC, first_seen DESC, id
    """
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def jobs_to_recheck(conn: sqlite3.Connection, stamp: str, min_score: int,
                    limit: int | None) -> list[sqlite3.Row]:
    """Good jobs scored with an older prompt or profile (prompt_version != stamp).

    Best first. Skips jobs you've already applied to: a new score wouldn't
    change anything there.
    """
    sql = f"""
        SELECT id, company, title, location, description, score FROM jobs j
        WHERE status = 'scored' AND closed_at IS NULL AND duplicate_of IS NULL
          AND app_status IS NULL AND score >= ? AND {FRESH}
          AND (prompt_version IS NULL OR prompt_version != ?)
        ORDER BY score DESC, first_seen DESC
    """
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (min_score, stamp, limit)).fetchall()
    return conn.execute(sql, (min_score, stamp)).fetchall()


def save_score(conn: sqlite3.Connection, row_id: int, result: dict,
               model: str, scored_at: str, prompt_version: str) -> None:
    """Save a job's score, and copy it to the job's duplicate copies."""
    with conn:
        conn.execute("""
            UPDATE jobs SET
                status = 'scored', score = ?, fit_score = ?, reason = ?, matched_skills = ?,
                gaps = ?, hard_gates = ?, model = ?, scored_at = ?, prompt_version = ?
            WHERE id = ? OR duplicate_of = ?
        """, (result["score"], result["fit_score"], result["reason"],
              json.dumps(result["matched_skills"]), json.dumps(result["gaps"]),
              json.dumps(result["hard_gates"]), model, scored_at, prompt_version, row_id, row_id))


def jobs_to_rate(conn: sqlite3.Connection) -> list[tuple]:
    """Open scored jobs you haven't rated yet, highest score first."""
    return conn.execute(f"""
        SELECT j.id, j.company, j.title, j.location, j.url, j.score, j.reason,
               j.matched_skills, j.gaps, j.description
        FROM jobs j LEFT JOIN ratings r ON r.job_id = j.id
        WHERE j.status = 'scored' AND j.closed_at IS NULL AND r.id IS NULL
          AND j.duplicate_of IS NULL AND j.app_status IS NULL AND {FRESH}
        ORDER BY j.score DESC, j.first_seen DESC
    """).fetchall()


def save_rating(conn: sqlite3.Connection, job_id: int, my_rating: int,
                comment: str, rated_at: str) -> None:
    with conn:
        # Copy the model's score and prompt version into the rating row.
        # ON CONFLICT lets you re-rate a job: the new rating replaces the old.
        conn.execute("""
            INSERT INTO ratings (job_id, my_rating, comment, model_score, prompt_version, rated_at)
            SELECT id, ?, ?, score, prompt_version, ? FROM jobs WHERE id = ?
            ON CONFLICT (job_id) DO UPDATE SET
                my_rating = excluded.my_rating, comment = excluded.comment,
                model_score = excluded.model_score,
                prompt_version = excluded.prompt_version, rated_at = excluded.rated_at
        """, (my_rating, comment or None, rated_at, job_id))


def disagreements(conn: sqlite3.Connection, threshold: int) -> list[tuple]:
    """Rated jobs where |your rating - model score| >= threshold, with a comment."""
    return conn.execute("""
        SELECT j.company, j.title, j.location, r.model_score, j.reason,
               r.my_rating, r.comment
        FROM ratings r JOIN jobs j ON j.id = r.job_id
        WHERE ABS(r.my_rating - r.model_score) >= ? AND r.comment IS NOT NULL
        ORDER BY r.rated_at
    """, (threshold,)).fetchall()


def all_ratings(conn: sqlite3.Connection) -> tuple[list[str], list[tuple]]:
    """Every rating joined with its job, for CSV export. Returns (headers, rows)."""
    cursor = conn.execute("""
        SELECT j.id AS job_id, j.company, j.title, j.location, j.url,
               r.model_score, r.my_rating, r.my_rating - r.model_score AS difference,
               r.comment, j.reason AS model_reason, r.prompt_version, r.rated_at
        FROM ratings r JOIN jobs j ON j.id = r.job_id
        ORDER BY r.rated_at
    """)
    return [col[0] for col in cursor.description], cursor.fetchall()


# ---- Queries for the web app (app.py) ----

# Postings older than this leave the working lists (Scored, To score, Listing
# only) for the "Older than N days" tab, and aren't scored or offered for
# rating: by then most have hundreds of applicants or are filled. Age is from
# the posting date, or when it was first found if the source gives none.
# Jobs at your target companies are kept whatever their age.
MAX_AGE_DAYS = 14
_CUTOFF = f"strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{MAX_AGE_DAYS} days')"
FRESH = f"(COALESCE(j.posted_at, j.first_seen) >= {_CUTOFF} OR j.target = 1)"
STALE = f"(COALESCE(j.posted_at, j.first_seen) < {_CUTOFF} AND COALESCE(j.target, 0) = 0)"

# The tabs on the Jobs page. Closed jobs get their own tab whatever their status.
JOB_VIEWS = {
    # Duplicate copies are hidden; their primary copy lists their locations.
    # Jobs you've marked applied leave these lists (they're on the Applied tab
    # and the Applications page).
    "scored": "j.status = 'scored' AND j.closed_at IS NULL AND j.duplicate_of IS NULL"
              f" AND j.app_status IS NULL AND {FRESH}",
    "to_score": "j.status = 'to_score' AND j.closed_at IS NULL AND j.duplicate_of IS NULL"
                f" AND j.app_status IS NULL AND {FRESH}",
    "filtered_out": "j.status = 'filtered_out' AND j.closed_at IS NULL AND j.app_status IS NULL",
    "no_description": "j.status = 'no_description' AND j.closed_at IS NULL AND j.app_status IS NULL"
                      f" AND {FRESH}",
    # Open, unapplied jobs past MAX_AGE_DAYS (kept, with their scores, not deleted).
    "old": "j.status IN ('scored', 'to_score', 'no_description') AND j.closed_at IS NULL"
           f" AND j.duplicate_of IS NULL AND j.app_status IS NULL AND {STALE}",
    "closed": "j.closed_at IS NOT NULL",
    "applied": "j.app_status IS NOT NULL",  # whatever else is true of the job
    # Open jobs at your target companies that passed the filter (the filtered-out
    # ones, e.g. LCS's community nursing jobs, are on the Filtered out tab).
    "targets": "j.target = 1 AND j.status != 'filtered_out' AND j.closed_at IS NULL"
               " AND j.duplicate_of IS NULL AND j.app_status IS NULL",
}


def view_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {view: conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}").fetchone()[0]
            for view, where in JOB_VIEWS.items()}


# When a job was posted, or failing that, when it was first found.
POSTED = "COALESCE(j.posted_at, j.first_seen)"


def list_jobs(conn: sqlite3.Connection, view: str, company: str = "",
              search: str = "", sort: str = "score") -> list[sqlite3.Row]:
    sql = f"""
        SELECT j.id, j.score, r.my_rating, j.title, j.company, j.location,
               j.status, j.filter_reason, j.first_seen, j.priority,
               j.app_status, j.applied_at, j.target, j.posted_at,
               (SELECT COUNT(*) FROM jobs d WHERE d.duplicate_of = j.id) AS copies
        FROM jobs j LEFT JOIN ratings r ON r.job_id = j.id
        WHERE {JOB_VIEWS[view]}
    """
    params = []
    if company:
        sql += " AND LOWER(j.company) = LOWER(?)"
        params.append(company)
    if search:
        sql += " AND j.title LIKE ?"  # LIKE is case-insensitive for ASCII in SQLite
        params.append(f"%{search}%")
    # Scored jobs by score; waiting jobs in the order they'll be scored.
    if view == "applied":
        # Reached out (not applied yet) first, then most recent applications.
        sql += " ORDER BY j.app_status = 'reached_out' DESC, j.applied_at DESC"
    elif sort == "newest":
        sql += f" ORDER BY {POSTED} DESC"
    elif view == "to_score":
        sql += f" ORDER BY j.priority DESC NULLS LAST, {POSTED} DESC"  # the order they'll be scored
    else:
        # Best score first; among equal scores, the most recently posted.
        sql += f" ORDER BY j.score DESC NULLS LAST, {POSTED} DESC"
    return conn.execute(sql, params).fetchall()


def duplicates(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """The other copies of this job (same posting in other places)."""
    return conn.execute("""
        SELECT id, location, url FROM jobs WHERE duplicate_of = ? ORDER BY location
    """, (job_id,)).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("""
        SELECT j.*, r.my_rating, r.comment
        FROM jobs j LEFT JOIN ratings r ON r.job_id = j.id
        WHERE j.id = ?
    """, (job_id,)).fetchone()


def boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Boards you fetched directly (not just via a feed), for one-click refresh."""
    return conn.execute("""
        SELECT source, company, COUNT(*) AS total,
               SUM(closed_at IS NULL) AS open, MAX(last_seen) AS last_fetched
        FROM jobs GROUP BY source, company
        HAVING SUM(feed IS NULL) > 0
        ORDER BY company
    """).fetchall()


def companies(conn: sqlite3.Connection) -> list[str]:
    """Every company with a job stored, from boards and feeds."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs ORDER BY company COLLATE NOCASE")]


def rating_stats(conn: sqlite3.Connection, threshold: int) -> dict[str, int]:
    row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(ABS(my_rating - model_score) >= ? AND comment IS NOT NULL) AS usable,
               SUM(ABS(my_rating - model_score) >= ? AND comment IS NULL) AS no_comment
        FROM ratings
    """, (threshold, threshold)).fetchone()
    # SUM over zero rows is NULL, hence `or 0`.
    return {key: row[key] or 0 for key in ("total", "usable", "no_comment")}


# ---- Open-posting check (liveness.py) ----

def jobs_to_check(conn: sqlite3.Connection, min_score: int, limit: int) -> list[sqlite3.Row]:
    """Open jobs worth confirming, least recently checked first.

    Jobs you're acting on (drafted, or applied/interviewing) come first,
    whatever their score: you want to know before you spend time on them.
    Then jobs scored or rated min_score+.
    """
    return conn.execute("""
        SELECT j.id, j.source, j.company, j.ats_job_id FROM jobs j
        LEFT JOIN ratings r ON r.job_id = j.id
        LEFT JOIN applications a ON a.job_id = j.id
        WHERE j.closed_at IS NULL AND j.source IN ('greenhouse', 'lever', 'ashby')
          AND j.duplicate_of IS NULL
          AND (j.score >= ? OR r.my_rating >= ? OR a.id IS NOT NULL
               OR j.app_status IN ('applied', 'interviewing'))
        ORDER BY (a.id IS NOT NULL OR j.app_status IN ('applied', 'interviewing')) DESC,
                 j.checked_at IS NOT NULL, j.checked_at, j.score DESC
        LIMIT ?
    """, (min_score, min_score, limit)).fetchall()


def record_check(conn: sqlite3.Connection, job_id: int, is_open: bool, now: str) -> None:
    with conn:
        if is_open:
            conn.execute("UPDATE jobs SET checked_at = ? WHERE id = ?", (now, job_id))
        else:
            conn.execute("UPDATE jobs SET checked_at = ?, closed_at = ? WHERE id = ?",
                         (now, now, job_id))


# ---- Applications (drafter.py) ----

def save_draft(conn: sqlite3.Connection, job_id: int, draft: dict, review: dict,
               model: str, now: str) -> None:
    """Store a new draft for a job, replacing any earlier one."""
    with conn:
        conn.execute("""
            INSERT INTO applications (job_id, draft, review, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                draft = excluded.draft, review = excluded.review, model = excluded.model,
                created_at = excluded.created_at, updated_at = excluded.updated_at,
                folder = NULL, rendered_at = NULL
        """, (job_id, json.dumps(draft), json.dumps(review), model, now, now))


def update_draft(conn: sqlite3.Connection, job_id: int, draft: dict, now: str) -> None:
    """Save your edits to a draft."""
    with conn:
        conn.execute("UPDATE applications SET draft = ?, updated_at = ? WHERE job_id = ?",
                     (json.dumps(draft), now, job_id))


def update_review(conn: sqlite3.Connection, job_id: int, review: dict) -> None:
    """Replace the review notes (truth check findings, letter feedback)."""
    with conn:
        conn.execute("UPDATE applications SET review = ? WHERE job_id = ?",
                     (json.dumps(review), job_id))


def set_rendered(conn: sqlite3.Connection, job_id: int, folder: str, now: str) -> None:
    with conn:
        conn.execute("UPDATE applications SET folder = ?, rendered_at = ? WHERE job_id = ?",
                     (folder, now, job_id))


def get_application(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def list_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every job with a draft or an application status. Applied ones first."""
    return conn.execute("""
        SELECT j.id AS job_id, a.model, a.updated_at, a.rendered_at, a.folder,
               j.title, j.company, j.score, j.url, j.closed_at,
               j.app_status, j.applied_at, a.id IS NOT NULL AS has_draft,
               (SELECT MAX(e.at) FROM application_events e WHERE e.job_id = j.id) AS status_at
        FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
        WHERE a.id IS NOT NULL OR j.app_status IS NOT NULL
        ORDER BY j.app_status IS NULL, COALESCE(status_at, a.updated_at) DESC
    """).fetchall()


# ---- Application tracker ----

# "reached_out" = you asked someone there for a referral and haven't applied
# yet. After REMINDER_DAYS without applying, apply_reminders() lists the job.
APP_STATUSES = ("reached_out", "applied", "interviewing", "offer", "rejected")
REMINDER_DAYS = 2


def set_app_status(conn: sqlite3.Connection, job_id: int, status: str, note: str, now: str) -> None:
    """Record a status change. applied_at is set the first time a job is marked
    anything past "reached_out" (reaching out isn't applying)."""
    if status not in APP_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with conn:
        conn.execute("INSERT INTO application_events (job_id, status, note, at) VALUES (?, ?, ?, ?)",
                     (job_id, status, note or None, now))
        conn.execute("""UPDATE jobs SET app_status = ?,
                            applied_at = CASE WHEN ? = 'reached_out' THEN applied_at
                                              ELSE COALESCE(applied_at, ?) END
                        WHERE id = ?""", (status, status, now, job_id))


def apply_reminders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Jobs still at "reached_out" REMINDER_DAYS or more after you reached out:
    no referral came through, so it's time to apply directly. Oldest first.

    `reached_at` and `note` come from the latest reached_out event (the note
    usually says who you asked).
    """
    return conn.execute(f"""
        SELECT j.id, j.title, j.company, e.at AS reached_at, e.note
        FROM jobs j JOIN application_events e ON e.id = (
            SELECT MAX(id) FROM application_events WHERE job_id = j.id AND status = 'reached_out')
        WHERE j.app_status = 'reached_out'
          AND e.at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-{REMINDER_DAYS} days')
        ORDER BY e.at
    """).fetchall()


def clear_app_status(conn: sqlite3.Connection, job_id: int) -> None:
    """Undo: forget this job's application history entirely."""
    with conn:
        conn.execute("DELETE FROM application_events WHERE job_id = ?", (job_id,))
        conn.execute("UPDATE jobs SET app_status = NULL, applied_at = NULL WHERE id = ?", (job_id,))


def app_events(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT status, note, at FROM application_events WHERE job_id = ? ORDER BY at, id",
                        (job_id,)).fetchall()


# ---- Claude usage (scorer.call_claude) ----

def log_claude_call(conn: sqlite3.Connection, call: dict) -> None:
    from datetime import datetime, timezone
    with conn:
        conn.execute("""
            INSERT INTO claude_calls (at, stage, model, effort, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, cost_usd,
                                      duration_ms, ok)
            VALUES (:at, :stage, :model, :effort, :input_tokens, :output_tokens,
                    :cache_read_tokens, :cache_write_tokens, :cost_usd, :duration_ms, :ok)
        """, {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **call})


def usage_summary(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Claude usage per stage since a UTC timestamp, biggest first."""
    return conn.execute("""
        SELECT stage, COUNT(*) AS calls,
               SUM(input_tokens + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0))
                   AS input_tokens,
               SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd,
               SUM(duration_ms) / 1000 AS seconds
        FROM claude_calls WHERE at >= ?
        GROUP BY stage ORDER BY cost_usd DESC
    """, (since,)).fetchall()


# ---- Company research (research.py) ----

def get_company_research(conn: sqlite3.Connection, company: str) -> dict | None:
    row = conn.execute("SELECT research, at FROM company_research WHERE company = ?",
                       (company,)).fetchone()
    return {"research": json.loads(row["research"]), "at": row["at"]} if row else None


def save_company_research(conn: sqlite3.Connection, company: str, research: dict, at: str) -> None:
    with conn:
        conn.execute("""
            INSERT INTO company_research (company, research, at) VALUES (?, ?, ?)
            ON CONFLICT (company) DO UPDATE SET research = excluded.research, at = excluded.at
        """, (company, json.dumps(research), at))


# ---- Skill gap answers (drafter.find_gaps) ----

def answered_skills(conn: sqlite3.Connection) -> set[str]:
    """Skills you've already answered about (lowercased), so they aren't asked again."""
    return {r[0] for r in conn.execute("SELECT skill FROM skill_answers")}


def save_skill_answer(conn: sqlite3.Connection, skill: str, answer: str, details: str,
                      job_id: int, at: str) -> None:
    with conn:
        conn.execute("""
            INSERT INTO skill_answers (skill, answer, details, job_id, answered_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (skill) DO UPDATE SET answer = excluded.answer, details = excluded.details,
                job_id = excluded.job_id, answered_at = excluded.answered_at
        """, (skill.strip().lower(), answer, details, job_id, at))


# ---- Draft versions ----

def save_version(conn: sqlite3.Connection, job_id: int, kind: str, draft: dict, at: str,
                 files: str | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO draft_versions (job_id, kind, draft, files, at) VALUES (?, ?, ?, ?, ?)",
                     (job_id, kind, json.dumps(draft), files, at))


def versions(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """A job's draft versions, oldest first."""
    return conn.execute("SELECT id, kind, files, at, draft FROM draft_versions WHERE job_id = ? ORDER BY at, id",
                        (job_id,)).fetchall()


# ---- People you know (the People page) ----

CONTACT_FIELDS = ("name", "company", "role", "how", "notes")


def list_contacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contacts ORDER BY company COLLATE NOCASE, name COLLATE NOCASE").fetchall()


def save_contact(conn: sqlite3.Connection, fields: dict, at: str, contact_id: int | None = None) -> None:
    """Add a contact, or update the one with contact_id."""
    values = [fields.get(f, "").strip() or None for f in CONTACT_FIELDS]
    with conn:
        if contact_id:
            conn.execute("UPDATE contacts SET name = ?, company = ?, role = ?, how = ?, notes = ? WHERE id = ?",
                         (*values, contact_id))
        else:
            conn.execute("INSERT INTO contacts (name, company, role, how, notes, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (*values, at))


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))


def open_job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Jobs per company on the Scored tab (open, recent, not applied), for the People page."""
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT j.company, COUNT(*) FROM jobs j WHERE {JOB_VIEWS['scored']} GROUP BY j.company")}


# ---- Target companies (targets.py) ----

def ids_with_description(conn: sqlite3.Connection, source: str, company: str) -> set[str]:
    """Job IDs on this board whose description is already stored (no need to fetch again)."""
    return {r[0] for r in conn.execute(
        "SELECT ats_job_id FROM jobs WHERE source = ? AND company = ? AND description != ''",
        (source, company))}


def open_job_count(conn: sqlite3.Connection, source: str, company: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE source = ? AND company = ? AND closed_at IS NULL",
                        (source, company)).fetchone()[0]


def mark_targets(conn: sqlite3.Connection, names: set[str]) -> None:
    """target = 1 for jobs whose company matches a target (by name or board), else 0.

    Matches jobs from every source, so a target's jobs found by the Simplify
    list or the sweep are marked too.
    """
    placeholders = ", ".join("?" for _ in names) or "NULL"
    with conn:
        conn.execute(f"UPDATE jobs SET target = (LOWER(company) IN ({placeholders}))", list(names))


def target_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per target company: open jobs, how many passed the filter, how many scored 7+."""
    return conn.execute("""
        SELECT LOWER(company) AS company, COUNT(*) AS open,
               SUM(status != 'filtered_out') AS passed, SUM(score >= 7) AS good,
               MAX(last_seen) AS last_seen
        FROM jobs WHERE target = 1 AND closed_at IS NULL AND duplicate_of IS NULL
        GROUP BY LOWER(company)
    """).fetchall()
