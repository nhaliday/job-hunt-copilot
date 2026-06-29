import html
from html.parser import HTMLParser
from typing import Iterable

import httpx

from job_description_scan.boards import Posting


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


def _strip_html(raw: str) -> str:
    decoded = html.unescape(raw)
    s = _Stripper()
    s.feed(decoded)
    text = "".join(s.parts)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


class GreenhouseClient:
    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        url = (
            f"https://boards-api.greenhouse.io/v1/boards/{self.slug}"
            "/jobs?content=true"
        )
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        for job in data.get("jobs", []):
            yield Posting(
                id=str(job["id"]),
                title=job.get("title", ""),
                location=(job.get("location") or {}).get("name", ""),
                content_text=_strip_html(job.get("content") or ""),
                url=job.get("absolute_url", ""),
                raw=job,
            )
