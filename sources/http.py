"""Shared HTTP helper for the ATS adapters."""

import json
import urllib.request


def get_json(url: str):
    """GET a URL and parse the response body as JSON."""
    request = urllib.request.Request(url, headers={"User-Agent": "job-sourcer/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)
