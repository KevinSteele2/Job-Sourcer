"""ATS adapters. Each one exposes fetch_jobs(slug) -> list[Job]."""

from sources import ashby, greenhouse, lever

ADAPTERS = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
}
