"""Cheap rule-based filter, driven by filters.yaml.

Deliberately permissive: it only removes obvious mismatches. Anything
ambiguous passes through to the AI scorer.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FILTERS_PATH = Path(__file__).parent / "filters.yaml"


def _keyword_pattern(keyword: str) -> re.Pattern:
    # (?<!\w) / (?!\w) mean "no letter, digit or _ directly before/after".
    # Unlike \b, this still works when the keyword starts or ends with a
    # symbol, e.g. "C++" or ".NET".
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)


@dataclass
class Rules:
    title_include: list[tuple[str, re.Pattern]]
    title_exclude: list[tuple[str, re.Pattern]]
    location_include: list[tuple[str, re.Pattern]]
    location_exclude: list[tuple[str, re.Pattern]]
    entry_level: list[tuple[str, re.Pattern]]
    target_roles: list[tuple[str, re.Pattern]]
    higher_level: list[tuple[str, re.Pattern]]


def load_rules(path: Path = FILTERS_PATH) -> Rules:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    title = config.get("title") or {}
    location = config.get("location") or {}
    priority_cfg = config.get("priority") or {}

    def compile_all(keywords):
        # `or []` handles a key that's present but empty in the YAML (None).
        return [(kw, _keyword_pattern(kw)) for kw in keywords or []]

    return Rules(
        title_include=compile_all(title.get("include")),
        title_exclude=compile_all(title.get("exclude")),
        location_include=compile_all(location.get("include")),
        location_exclude=compile_all(location.get("exclude")),
        # str(): YAML reads a bare 1 as a number
        entry_level=compile_all([str(k) for k in priority_cfg.get("entry_level") or []]),
        target_roles=compile_all([str(k) for k in priority_cfg.get("target_roles") or []]),
        higher_level=compile_all([str(k) for k in priority_cfg.get("higher_level") or []]),
    )


def check(title: str, location: str, rules: Rules) -> str | None:
    """Return why a job fails the rules, or None if it passes."""
    for keyword, pattern in rules.title_exclude:
        if pattern.search(title):
            return f'title excludes "{keyword}"'

    if rules.title_include and not any(p.search(title) for _, p in rules.title_include):
        return "title has no include keyword"

    # Multi-location jobs are stored as one string ("London; Remote (US)"),
    # so searching the whole string passes the job if ANY location matches.
    # A blank location passes: we can't tell it's a mismatch.
    if (rules.location_include and location.strip()
            and not any(p.search(location) for _, p in rules.location_include)):
        return "location not in include list"

    # Exclude only when EVERY listed location is excluded: "London; New York"
    # stays, "London; Dublin" goes. Locations are separated by ";" or "|".
    places = [p.strip() for p in re.split(r"[;|]", location) if p.strip()]
    if rules.location_exclude and places:
        matched = [next((kw for kw, p in rules.location_exclude if p.search(place)), None)
                   for place in places]
        if all(matched):
            return f'location excluded ("{matched[0]}")'

    return None


# "3+ years of experience", "3-5 years experience", "at least 3 years of
# professional experience": a number, "year(s)", then "experience" within a
# few words. The lowest number in the posting wins, so a posting asking for
# "1+ years of SQL" and "5+ years of Python" counts as 1.
YEARS_PATTERN = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*\+?\s*years?['’]?\s+(?:of\s+)?(?:\w+[\s,/-]+){0,5}?experience",
    re.IGNORECASE)


def min_years_asked(description: str) -> int | None:
    numbers = [int(m.group(1)) for m in YEARS_PATTERN.finditer(description or "")]
    numbers = [n for n in numbers if n < 20]  # skip things like "100 years of history"
    return min(numbers) if numbers else None


TARGET_BONUS = 10  # a target company's jobs are scored before everything else


def priority(title: str, description: str, feed: str | None, rules: Rules,
             target: bool = False) -> int:
    """Cheap guess at how promising a job is, for scoring order only.

    Points are explained in filters.yaml -> priority.
    """
    points = TARGET_BONUS if target else 0
    if any(p.search(title) for _, p in rules.entry_level):
        points += 3
    if any(p.search(title) for _, p in rules.target_roles):
        points += 1
    if feed in ("simplify", "sweep"):
        points += 1
    if any(p.search(title) for _, p in rules.higher_level):
        points -= 2
    years = min_years_asked(description)
    if years is not None and years >= 3:
        points -= 3
    return points


def refilter_all() -> int:
    """Re-run the current rules on every open job that hasn't been scored yet,
    and recompute each one's scoring priority.

    Called before each scoring run, so edits to filters.yaml always apply
    before anything is sent to Claude. Returns how many jobs were checked.
    """
    import db  # imported here: db is only needed for this function

    rules = load_rules()
    conn = db.connect()
    rows = db.all_jobs_to_filter(conn)
    db.set_filter_results(conn, [(r["id"], check(r["title"], r["location"], rules)) for r in rows])
    db.set_priorities(conn, [(r["id"], priority(r["title"], r["description"], r["feed"], rules,
                                                bool(r["target"])))
                             for r in rows])
    conn.close()
    return len(rows)
