"""The common Job format every ATS adapter returns, plus shared helpers."""

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser


@dataclass
class Job:
    source: str       # which ATS: "greenhouse", "lever", or "ashby"
    company: str      # the board slug we fetched from
    ats_job_id: str   # the ATS's own job ID (unique within source + company)
    title: str
    location: str
    url: str
    description: str  # plain text, converted from the ATS's HTML
    posted_at: str = ""  # when the company posted it (UTC, ISO), "" if the source doesn't say


def iso_utc(value) -> str:
    """Any source's posting date as "YYYY-MM-DDTHH:MM:SSZ" (UTC), or "".

    Sources use different formats: ISO strings with an offset
    ("2026-07-30T20:39:09-04:00"), plain dates ("2026-10-05"), or Unix time
    in seconds (Simplify) or milliseconds (Lever).
    """
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value  # milliseconds vs seconds
            moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return ""


# Tags that start a new line when rendered, so the text doesn't run together.
BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    """Collects the text between tags, adding line breaks at block tags."""

    def __init__(self):
        super().__init__()  # convert_charrefs=True by default, so &amp; etc. become characters
        self.parts: list[str] = []

    def _last_part(self) -> str:
        """The most recent part, skipping space-only parts from between tags."""
        for part in reversed(self.parts):
            if part.strip(" "):
                return part
        return ""

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            # One line break before a bullet, not two: Ashby closes a </p>
            # inside the previous <li>, which already broke the line.
            if not self._last_part().endswith("\n"):
                self.parts.append("\n")
            self.parts.append("- ")
        elif tag in BLOCK_TAGS and not self._last_part().endswith("- "):
            # Skipped right after a bullet, so <li><p>text</p> stays "- text"
            # on one line instead of "-" and "text" on separate lines.
            self.parts.append("\n")

    def handle_endtag(self, tag):
        # Skip </li>: the next <li> or </ul> already breaks the line, and a
        # second break here would put a blank line between every bullet.
        if tag in BLOCK_TAGS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data):
        # HTML treats any run of whitespace (including newlines in the source)
        # as a single space; str.split() with no args also catches \xa0 (&nbsp;).
        text = " ".join(data.split())
        if not text:
            self.parts.append(" ")
            return
        # Keep a space at either edge, or "our <a>Blog</a> for" becomes "ourBlogfor".
        if data[0].isspace():
            text = " " + text
        if data[-1].isspace():
            text += " "
        self.parts.append(text)


def html_to_text(html: str) -> str:
    """Convert an HTML job description to readable plain text."""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]

    # Drop blank lines that repeat, so there's at most one blank line in a row.
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()
