"""Meta's careers site (metacareers.com), read through headless Edge.

Meta refuses plain requests (every URL answers 400) and builds its search
results with JavaScript, so pages are loaded in a real browser
(sources/browser.py). Meta has thousands of openings, so instead of reading
them all, this runs only the searches listed in companies.yaml
("university grad", "data scientist", ...) and treats the jobs they return as
the board.

Each job page carries a schema.org "JobPosting" block (JSON made for search
engines), which gives the title, description, qualifications and locations
reliably, without depending on the page's layout.

Same reader interface as sources/laserfiche.py.
"""

import html
import json
import re
import time
import urllib.parse

from job import Job, html_to_text, iso_utc
from sources import browser

BASE = "https://www.metacareers.com"
PAUSE = 1.0

_JOB_LINK = re.compile(r'href="/profile/job_details/(\d{8,})/?"[^>]*>(.*?)</a>', re.S)
_JSON_LD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class MetaError(Exception):
    """A page didn't come back as expected (blocked, or the site changed)."""


def _search(query: str) -> dict[str, str]:
    """{job id: title} for one search's results."""
    page = browser.render(f"{BASE}/jobsearch?q={urllib.parse.quote(query)}")
    if "Meta Careers" not in page:
        raise MetaError(f"the search page for {query!r} didn't load")
    found = {}
    for job_id, inner in _JOB_LINK.findall(page):
        # The link's first line of text is the title; the rest is locations and teams.
        text = html_to_text(inner).strip()
        found.setdefault(job_id, text.split("\n")[0].strip())
    return found


def _posting(job_id: str) -> dict:
    """The page's schema.org JobPosting block, as a dict."""
    page = browser.render(f"{BASE}/profile/job_details/{job_id}")
    for block in _JSON_LD.findall(page):
        try:
            data = json.loads(html.unescape(block))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    raise MetaError(f"job {job_id} has no JobPosting data")


def _locations(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    names = []
    for p in places:
        address = p.get("address") or {} if isinstance(p, dict) else {}
        name = (p.get("name") if isinstance(p, dict) else None) or address.get("addressLocality")
        if name and name not in names:
            names.append(name)
    return "; ".join(names)


def fetch_jobs(company: dict, wanted, known: set[str]) -> list[Job]:
    listed: dict[str, str] = {}
    for query in company.get("searches") or ["university grad"]:
        listed.update(_search(query))
        time.sleep(PAUSE)

    jobs = []
    for job_id, title in listed.items():
        location, description, posted = "", "", ""
        # Titles from the search are enough to decide; location comes with the details.
        if job_id not in known and wanted(title, ""):
            posting = _posting(job_id)
            title = html.unescape(posting.get("title") or title)
            location = _locations(posting)
            parts = [posting.get(k) or "" for k in ("description", "responsibilities", "qualifications")]
            description = "\n\n".join(html_to_text(html.unescape(p)).strip() for p in parts if p.strip())
            posted = iso_utc(posting.get("datePosted"))
            time.sleep(PAUSE)
        jobs.append(Job(source="meta", company=company["key"], ats_job_id=job_id, title=title,
                        location=location, url=f"{BASE}/profile/job_details/{job_id}",
                        description=description, posted_at=posted))
    return jobs
