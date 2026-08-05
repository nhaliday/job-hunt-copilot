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

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]: ...


def fetch_by_walk(client: BoardClient, ids: Iterable[str]) -> Iterable[Posting]:
    """Derived fetch_postings for one-shot boards: the full listing arrives in
    one or a few requests, so a targeted fetch is just a filtered walk. Ids
    delisted since the scan simply don't appear; the caller sees them as
    missing and reports them dropped."""
    wanted = set(ids)
    return (p for p in client.iter_postings() if p.id in wanted)


class StaticClient:
    """In-memory BoardClient over pre-fetched postings (cached API pulls,
    tests). Serving from memory keeps paid/non-idempotent sources out of the
    client protocol: the pull happens once, upstream, and everything the
    pipeline does with the client stays free and repeatable."""

    def __init__(self, postings: Iterable[Posting]) -> None:
        self._postings = list(postings)

    def iter_postings(self) -> Iterable[Posting]:
        return iter(self._postings)

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        return fetch_by_walk(self, ids)

    def index(self) -> dict[str, Posting]:
        return {p.id: p for p in self._postings}


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
    from .eightfold import EightfoldClient
    from .greenhouse import GreenhouseClient
    from .lever import LeverClient
    from .phenom import PhenomClient
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
    if source.kind == "phenom":
        return PhenomClient(source.slug, location_filter)
    if source.kind == "eightfold":
        return EightfoldClient(source.slug, location_filter)
    raise ValueError(f"Unknown board kind: {source.kind!r}")
