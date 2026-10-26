"""Find the new home of boards the sweep marks dead (404).

When a company's board 404s, it has usually either closed, moved to another
ATS (Greenhouse -> Ashby is common), or renamed its board (often a small
change: "acme" -> "acmehq", "acme-ai" -> "acmeai"). None of the three ATSs
redirects an old board name to a new one, so this tries likely names
directly:

  - the same name on the other two ATSs
  - simple variants on all three: hyphens removed, a common suffix
    ("hq", "inc", "ai", "labs") dropped or added

Every guess is one request to the same public endpoint the sweep uses. A
guess that returns 200 is a live board. Each find is added to that ATS's list
in boards/ (so the next sweep checks it) and recorded in
data/moved_boards.json with the dead name it replaced. Dead names stay in
dead_boards.json, which skips them, so nothing is lost.

A found board is very likely the same company, but not certain (a different
company can own "acmehq"). That's low-risk here: a wrong board only adds
postings that go through the usual filter and scoring.

    python find_moved_boards.py --sample 300 --dry-run   # try a few, change nothing
    python find_moved_boards.py                          # every dead board
    python find_moved_boards.py --variants               # also renamed forms (slower)
"""
import argparse
import json
import random
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sweep

ROOT = Path(__file__).parent
MOVED_PATH = ROOT / "data" / "moved_boards.json"
# Endings dropped from a name ("acmehq" -> "acme"). "ai" only with its hyphen:
# without it, "openai" would become "open".
STRIP = (r"-?hq", r"-?inc", r"-?labs", r"-ai")
ADD = ("hq", "inc")  # endings added to a name ("acme" -> "acmehq")
PAUSE = 0.25   # seconds between requests in each worker
WORKERS = 6


def load_lists() -> dict[str, list[str]]:
    return {ats: json.loads((sweep.BOARDS_DIR / f"{ats}_slugs.json").read_text(encoding="utf-8"))
            for ats in sweep.LIST_URLS}


def variants(slug: str) -> list[str]:
    """Likely renamed forms of a slug, the slug itself first."""
    base = {slug, slug.replace("-", "")}
    for s in list(base):
        for ending in STRIP:
            stripped = re.sub(rf"{ending}$", "", s)
            if len(stripped) >= 3 and stripped != s:
                base.add(stripped)
    out = [slug] + sorted(base - {slug})
    for s in list(out):
        if not s.endswith(ADD):
            out += [s + x for x in ADD]
    return list(dict.fromkeys(out))


def guesses(dead_key: str, known: dict[str, set], use_variants: bool = False) -> list[tuple[str, str, str]]:
    """(ats, slug, kind) to try for one dead board, skipping boards already listed.

    By default only the same name on the other two ATSs: in a 300-board test
    that found 13 of 14 moves, while the name variants cost ~6 more requests
    per board for 1 more find.
    """
    ats, slug = dead_key.split("/", 1)
    out = []
    for i, v in enumerate(variants(slug) if use_variants else [slug]):
        for other in sweep.LIST_URLS:
            if (other, v) == (ats, slug) or v in known[other]:
                continue
            kind = ("same name, other ATS" if i == 0 else
                    "variant, same ATS" if other == ats else "variant, other ATS")
            out.append((other, v, kind))
    return out


def alive(ats: str, slug: str) -> bool | None:
    """True = live board, False = 404, None = couldn't tell (network error)."""
    request = urllib.request.Request(sweep.LIST_URLS[ats].format(slug=slug),
                                     headers={"User-Agent": "job-sourcer (personal job search)"})
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 400) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    finally:
        time.sleep(PAUSE)
    # All three answer an unknown board with 404, so any JSON reply is a real
    # board (possibly with no open jobs right now).
    return body is not None


def check(dead_key: str, known: dict[str, set], use_variants: bool = False) -> dict | None:
    """Try the guesses for one dead board in order; stop at the first live one."""
    for ats, slug, kind in guesses(dead_key, known, use_variants):
        if alive(ats, slug):
            return {"dead": dead_key, "found": f"{ats}/{slug}", "kind": kind}
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", type=int, help="check only this many dead boards, chosen at random")
    parser.add_argument("--variants", action="store_true",
                        help='also try renamed forms ("acme" -> "acmehq"); ~4x the requests')
    parser.add_argument("--dry-run", action="store_true", help="report finds without changing boards/")
    args = parser.parse_args()

    lists = load_lists()
    known = {ats: set(slugs) for ats, slugs in lists.items()}
    dead = sweep._load_dead()
    # Skip dead boards whose name is already a listed board on another ATS:
    # those are already covered by the sweep.
    todo = [k for k in dead if not any(k.split("/", 1)[1] in known[o]
                                       for o in known if o != k.split("/", 1)[0])]
    moved = json.loads(MOVED_PATH.read_text(encoding="utf-8")) if MOVED_PATH.exists() else []
    done = {m["dead"] for m in moved}
    todo = [k for k in todo if k not in done]
    if args.sample:
        random.seed(0)
        todo = random.sample(todo, min(args.sample, len(todo)))
    n_requests = sum(len(guesses(k, known, args.variants)) for k in todo)
    print(f"{len(todo)} dead boards to check, up to {n_requests} requests")

    found, started = [], time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        for i, result in enumerate(pool.map(lambda k: check(k, known, args.variants), todo), 1):
            if result:
                found.append(result)
                print(f"  {result['dead']:45} -> {result['found']}  ({result['kind']})")
            if i % 200 == 0:
                print(f"  ... {i}/{len(todo)} checked, {len(found)} found, {time.time() - started:.0f}s")

    kinds = {}
    for f in found:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"\n{len(found)} of {len(todo)} found a new board in {time.time() - started:.0f}s: {kinds}")
    if args.dry_run or not found:
        return
    for f in found:
        ats, slug = f["found"].split("/", 1)
        if slug not in known[ats]:
            lists[ats].append(slug)
            known[ats].add(slug)
    for ats, slugs in lists.items():
        (sweep.BOARDS_DIR / f"{ats}_slugs.json").write_text(json.dumps(sorted(slugs), indent=2), encoding="utf-8")
    MOVED_PATH.write_text(json.dumps(moved + found, indent=1), encoding="utf-8")
    print(f"Added to boards/ and recorded in {MOVED_PATH.name}.")


if __name__ == "__main__":
    main()
