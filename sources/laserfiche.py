"""Laserfiche's own jobs site (jobs.laserfiche.com), read from its HTML.

There's no public API: /jobs is a plain server-built page listing every
opening (title, department, location), and /Jobs/Details/<id> has the full
description. Only one company is read this way, so it's a few requests a night.

Like the other target-company readers (targets.py), it takes:
  wanted(title, location) -> bool   only these get their detail page fetched
  known                              job IDs whose description is already stored
and returns every listed job; the ones not fetched have description "" (which
db.save_jobs treats as "keep what's stored").
"""

import html
import re
import time
import urllib.request

from job import Job, html_to_text

BASE = "https://jobs.laserfiche.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAUSE = 1.0  # seconds between detail pages

# One listing: the title link, then (before the next listing) its location.
_LISTING = re.compile(r'href="/Jobs/Details/(\w+)">([^<]+)</a>(.*?)(?=href="/Jobs/Details/|<hr)', re.S)
_LOCATION = re.compile(r'job_location[^>]*>(.*?)</div>', re.S)


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def _description(page: str) -> str:
    """The text from the "Description" heading to the copyright footer."""
    text = html_to_text(page)
    start = text.find("\nDescription")
    end = text.find("© 20", start)
    return text[start:end].strip() if start != -1 else ""


def fetch_jobs(company: dict, wanted, known: set[str]) -> list[Job]:
    jobs = []
    for job_id, title, rest in _LISTING.findall(_get(f"{BASE}/jobs")):
        title = _clean(title)
        location = "; ".join(_clean(x) for x in _LOCATION.findall(rest))
        description = ""
        if job_id not in known and wanted(title, location):
            page = _get(f"{BASE}/Jobs/Details/{job_id}")
            description = _description(page)
            # "2 Locations" in the list; the page names them after the department.
            m = re.search(r"\|\s*([^\n]+)\n", html_to_text(page)[:3000])
            if location.endswith("Locations") and m:
                location = m.group(1).strip().replace(" & ", "; ")
            time.sleep(PAUSE)
        jobs.append(Job(source="laserfiche", company=company["key"], ats_job_id=job_id,
                        title=title, location=location,
                        url=f"{BASE}/Jobs/Details/{job_id}", description=description))
    return jobs
