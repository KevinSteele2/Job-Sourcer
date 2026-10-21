"""Load pages the way a browser does, for sites that refuse plain requests.

Some career sites (Meta's) answer a script with "400 Bad Request" and only
build their job lists with JavaScript. Headless Microsoft Edge, the same
browser that prints the resume PDFs, loads the page like any visitor would,
runs its JavaScript, and hands back the finished HTML (--dump-dom).
"""

import shutil
import subprocess
from pathlib import Path

EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def edge_path() -> Path:
    found = shutil.which("msedge")
    for path in ([Path(found)] if found else []) + EDGE_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("Microsoft Edge wasn't found.")


def render(url: str, wait_ms: int = 15000, timeout: int = 120) -> str:
    """The page's HTML after its scripts have run.

    --virtual-time-budget lets the page's scripts run for up to wait_ms of
    (simulated) time before the HTML is taken, so lists that load after the
    first paint are included.
    """
    proc = subprocess.run(
        [str(edge_path()), "--headless", "--disable-gpu", f"--virtual-time-budget={wait_ms}",
         "--dump-dom", url],
        capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.stdout.decode("utf-8", errors="replace")
