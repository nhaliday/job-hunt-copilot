import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, Protocol

from job_description_scan.config import BoardSource


@dataclass
class Posting:
    id: str
    title: str
    location: str
    content_text: str
    url: str
    raw: dict


class BoardClient(Protocol):
    def iter_postings(self) -> Iterable[Posting]: ...


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in ("p", "li", "div", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")


def strip_html(raw: str) -> str:
    decoded = html.unescape(raw)
    s = _Stripper()
    s.feed(decoded)
    text = "".join(s.parts)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def make_client(
    source: BoardSource, location_filter: re.Pattern[str] | None = None
) -> BoardClient:
    from .ashby import AshbyClient
    from .greenhouse import GreenhouseClient
    from .lever import LeverClient
    from .smartrecruiters import SmartRecruitersClient
    from .workday import WorkdayClient

    if source.kind == "greenhouse":
        return GreenhouseClient(source.slug)
    if source.kind == "ashby":
        return AshbyClient(source.slug)
    if source.kind == "lever":
        return LeverClient(source.slug)
    # List-then-detail boards: content costs one GET per posting, so only these
    # clients take the location filter — to skip detail fetches for postings
    # that can't match. Semantics are unchanged: every posting is still
    # yielded, and pipeline.run_scan applies the authoritative filter.
    if source.kind == "workday":
        return WorkdayClient(source.slug, location_filter)
    if source.kind == "smartrecruiters":
        return SmartRecruitersClient(source.slug, location_filter)
    raise ValueError(f"Unknown board kind: {source.kind!r}")
