"""Oracle Recruiting Cloud career sites (e.g. LCS), through their public JSON API.

An Oracle career site (https://<host>/hcmUI/CandidateExperience/en/sites/<site>/jobs)
loads its jobs from a REST API on the same host that needs no login:
  recruitingCEJobRequisitions        the list, up to 200 jobs a request
  recruitingCEJobRequisitionDetails  one job's full description
Same reader interface as sources/laserfiche.py: details are fetched only for
`wanted` titles not already `known`.
"""

import json
import time
import urllib.parse
import urllib.request

from job import Job, html_to_text, iso_utc

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_SIZE = 200
PAUSE = 0.5


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_jobs(company: dict, wanted, known: set[str]) -> list[Job]:
    host, site = company["host"], company["site"]
    api = f"https://{host}/hcmRestApi/resources/latest"
    listings, offset = [], 0
    while True:
        finder = urllib.parse.quote(f"findReqs;siteNumber={site},limit={PAGE_SIZE},offset={offset}",
                                    safe=";=,")
        page = _get(f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                    f"&finder={finder}")["items"][0]
        listings += page["requisitionList"]
        offset += PAGE_SIZE
        if not page["requisitionList"] or offset >= page["TotalJobsCount"]:
            break
        time.sleep(PAUSE)

    jobs = []
    for r in listings:
        job_id, title = str(r["Id"]), r["Title"].strip()
        location = r.get("PrimaryLocation") or ""
        description = ""
        if job_id not in known and wanted(title, location):
            finder = urllib.parse.quote(f'ById;Id="{job_id}",siteNumber={site}', safe=";=,")
            detail = _get(f"{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                          f"&finder={finder}")["items"][0]
            parts = [detail.get(k) or "" for k in ("ExternalDescriptionStr",
                                                     "ExternalResponsibilitiesStr",
                                                     "ExternalQualificationsStr")]
            description = "\n\n".join(html_to_text(p).strip() for p in parts if p.strip())
            time.sleep(PAUSE)
        jobs.append(Job(source="oracle", company=company["key"], ats_job_id=job_id,
                        title=title, location=location,
                        url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}",
                        description=description, posted_at=iso_utc(r.get("PostedDate"))))
    return jobs
