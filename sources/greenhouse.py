"""Greenhouse adapter.

Response shape: {"jobs": [...], "meta": {...}}, all jobs in one response.
"""

import html

from job import Job, html_to_text, iso_utc
from sources.http import get_json

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"


def fetch_jobs(slug: str) -> list[Job]:
    payload = get_json(API_URL.format(slug=slug))
    return [_to_job(raw, slug) for raw in payload["jobs"]]


def fetch_job(slug: str, job_id: str) -> Job:
    """One job by ID. The response has the same shape as a board-list entry."""
    return _to_job(get_json(JOB_URL.format(slug=slug, job_id=job_id)), slug)


def _to_job(raw: dict, slug: str) -> Job:
    # Greenhouse double-encodes the description ("&lt;p&gt;"), so unescape it
    # into real HTML first, then strip the tags.
    description_html = html.unescape(raw.get("content") or "")
    return Job(
        source="greenhouse",
        company=slug,
        ats_job_id=str(raw["id"]),
        title=raw["title"].strip(),
        location=(raw.get("location") or {}).get("name", ""),
        url=raw["absolute_url"],
        description=html_to_text(description_html),
        posted_at=iso_utc(raw.get("first_published") or raw.get("updated_at")),
    )
