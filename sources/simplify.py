"""SimplifyJobs New-Grad-Positions list (github.com/SimplifyJobs/New-Grad-Positions).

The repo's README table is generated from listings.json, a public file with
one entry per job: company, title, locations, category, active flag, and a
direct link to the company's own posting. We read that file, not the README.

Response shape: a JSON list of listings, about 20,000 including inactive ones.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from job import Job, iso_utc
from sources.http import get_json

FEED_URL = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
            "dev/.github/scripts/listings.json")

# Posting links we can turn into an ATS API call: (ats, pattern capturing slug and job ID).
# Greenhouse's EU boards (job-boards.eu.greenhouse.io) work with the same API.
LINK_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/([\w-]+)/jobs/(\d+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})")),
]


@dataclass
class Listing:
    id: str               # Simplify's own ID
    company_name: str
    title: str
    location: str
    url: str
    ats: str | None       # "greenhouse" / "lever" / "ashby", or None if we can't read it
    slug: str | None
    ats_job_id: str | None
    posted_at: str = ""   # Simplify's date_posted, as UTC ISO


def parse_link(url: str) -> tuple[str, str, str] | None:
    """(ats, slug, job_id) if the link points at a board we have an adapter for."""
    for ats, pattern in LINK_PATTERNS:
        match = pattern.search(url)
        if match:
            return ats, match.group(1), match.group(2)
    # Embedded Greenhouse form: boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>.
    # Some only have the token, with no board slug, so there's no API call to make.
    if "greenhouse.io/embed/job_app" in url:
        query = parse_qs(urlparse(url).query)
        if "for" in query and "token" in query:
            return "greenhouse", query["for"][0], query["token"][0]
    return None


def fetch_listings(categories: list[str]) -> list[Listing]:
    """Active listings in the given categories."""
    wanted = {c.lower() for c in categories}
    listings = []
    for raw in get_json(FEED_URL):
        if not raw.get("active") or not raw.get("is_visible", True):
            continue
        if (raw.get("category") or "").lower() not in wanted:
            continue
        ats, slug, job_id = parse_link(raw["url"]) or (None, None, None)
        listings.append(Listing(
            id=raw["id"],
            company_name=raw["company_name"].strip(),
            title=raw["title"].strip(),
            location="; ".join(raw.get("locations") or []),
            url=raw["url"],
            ats=ats, slug=slug, ats_job_id=job_id,
            posted_at=iso_utc(raw.get("date_posted")),
        ))
    return listings


def listing_only_job(listing: Listing) -> Job:
    """A Job for a listing whose ATS we can't read: everything but a description."""
    return Job(source="simplify", company=listing.company_name, ats_job_id=listing.id,
               title=listing.title, location=listing.location, url=listing.url,
               description="", posted_at=listing.posted_at)
