"""Lever adapter.

Response shape: a bare JSON list of postings (no wrapper object).
"""

from job import Job, html_to_text, iso_utc
from sources.http import get_json

API_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
JOB_URL = "https://api.lever.co/v0/postings/{slug}/{job_id}"


def fetch_jobs(slug: str) -> list[Job]:
    postings = get_json(API_URL.format(slug=slug))
    return [_to_job(raw, slug) for raw in postings]


def fetch_job(slug: str, job_id: str) -> Job:
    """One posting by ID. Same shape as an entry in the board list."""
    return _to_job(get_json(JOB_URL.format(slug=slug, job_id=job_id)), slug)


def _to_job(raw: dict, slug: str) -> Job:
    categories = raw.get("categories") or {}
    locations = categories.get("allLocations") or [categories.get("location", "")]
    return Job(
        source="lever",
        company=slug,
        ats_job_id=raw["id"],
        title=raw["text"].strip(),
        location="; ".join(loc for loc in locations if loc),
        url=raw["hostedUrl"],
        description=html_to_text(_full_description_html(raw)),
        posted_at=iso_utc(raw.get("createdAt")),
    )


def _full_description_html(raw: dict) -> str:
    """Stitch Lever's split-up description back together.

    Lever puts the intro/body in `description`, the bulleted sections
    (responsibilities, requirements, ...) in `lists`, and the closing text in
    `additional`. Using only `description` would drop the requirements.
    """
    parts = [raw.get("description") or ""]
    for section in raw.get("lists") or []:
        parts.append(f"<h3>{section.get('text', '')}</h3><ul>{section.get('content', '')}</ul>")
    parts.append(raw.get("additional") or "")
    return "".join(parts)
