"""Ashby adapter.

Response shape: {"jobs": [...], "apiVersion": ...}, all jobs in one response.
"""

from job import Job, html_to_text, iso_utc
from sources.http import get_json

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch_jobs(slug: str) -> list[Job]:
    payload = get_json(API_URL.format(slug=slug))
    # Ashby can return unlisted postings; skip anything not meant to be public.
    return [_to_job(raw, slug) for raw in payload["jobs"] if raw.get("isListed", True)]


def _to_job(raw: dict, slug: str) -> Job:
    locations = [raw.get("location", "")]
    locations += [loc.get("location", "") for loc in raw.get("secondaryLocations") or []]
    return Job(
        source="ashby",
        company=slug,
        ats_job_id=raw["id"],
        title=raw["title"].strip(),
        location="; ".join(loc for loc in locations if loc),
        url=raw["jobUrl"],
        description=html_to_text(raw.get("descriptionHtml") or ""),
        posted_at=iso_utc(raw.get("publishedAt")),
    )
