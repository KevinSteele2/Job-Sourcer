"""Check your target companies (companies.yaml): every open posting, every night.

Usage:
    python targets.py

For each company, its reader (see companies.yaml -> source) lists every
opening. Detail pages are only fetched for titles that pass filters.yaml and
aren't already stored, so a big list (LCS has 800+ community jobs) costs a
handful of requests. Then, like fetching one board: save, close what
disappeared, filter. Jobs at these companies are marked target = 1 (also ones
found by the Simplify list or the sweep), which puts them first in scoring.
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml

import db
import filters
from sources import ADAPTERS, laserfiche, meta, oracle

CONFIG_PATH = Path(__file__).parent / "companies.yaml"

# Readers for sites that aren't one of the three ATS boards. Each takes
# (company, wanted, known) and returns every listed Job.
SCRAPERS = {"laserfiche": laserfiche.fetch_jobs, "oracle": oracle.fetch_jobs, "meta": meta.fetch_jobs}


def load_companies() -> list[dict]:
    """companies.yaml, with each company's storage key worked out.

    key is what's stored in jobs.company: the board slug for an ATS board
    (so the sweep's copies of those jobs are the same rows), otherwise the
    name in lowercase.
    """
    if not CONFIG_PATH.exists():
        return []
    companies = (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("companies") or []
    for c in companies:
        c["connection"] = c.get("connection") or ""
        if c["source"] in ADAPTERS:
            c["key"] = c["board"]
        elif c["source"] in SCRAPERS:
            c["key"] = c["name"].lower().replace(" ", "")
        else:
            raise ValueError(f"companies.yaml: unknown source {c['source']!r} for {c['name']}")
    return companies


def target_names(companies: list[dict]) -> set[str]:
    """Lowercased names and keys: how a job's company is matched to a target."""
    return {c["name"].lower() for c in companies} | {c["key"].lower() for c in companies}


def run_targets(on_progress=None) -> dict:
    """Check every target company. Returns {company name: summary counts}."""
    companies = load_companies()
    rules = filters.load_rules()

    def wanted(title: str, location: str) -> bool:
        return filters.check(title, location, rules) is None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect()
    summary = {}
    for i, c in enumerate(companies, 1):
        if on_progress:
            on_progress(i, len(companies), c["name"])
        source, key = c["source"], c["key"]
        try:
            if source in ADAPTERS:
                jobs = ADAPTERS[source](key)  # board APIs include descriptions
            else:
                jobs = SCRAPERS[source](c, wanted, db.ids_with_description(conn, source, key))
        except Exception as e:  # one company failing mustn't stop the rest
            summary[c["name"]] = {"error": f"{type(e).__name__}: {e}"[:200]}
            continue
        open_now = db.open_job_count(conn, source, key)
        if not jobs and open_now:
            # An empty list when there were open jobs is almost always a page
            # that didn't load. Saving it would close every job, so skip it.
            summary[c["name"]] = {"error": "came back empty; nothing was changed"}
            continue
        counts = db.save_jobs(conn, source, key, jobs, now)
        results = [(row_id, filters.check(title, location, rules))
                   for row_id, title, location in db.jobs_to_filter(conn, source, key)]
        db.set_filter_results(conn, results)
        summary[c["name"]] = {**counts, "passed": sum(1 for _, r in results if r is None)}

    db.mark_targets(conn, target_names(companies))
    conn.close()
    return summary


def main() -> None:
    summary = run_targets(on_progress=lambda i, n, name: print(f"[{i}/{n}] {name}...", flush=True))
    for name, s in summary.items():
        if "error" in s:
            print(f"  {name}: FAILED, {s['error']}")
        else:
            print(f"  {name}: {s['fetched']} open, {s['new']} new, {s['closed']} closed, "
                  f"{s['passed']} passed the filter")


if __name__ == "__main__":
    main()
