from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html


def _enriched_location(job: dict) -> str:
    parts: list[str] = []
    name = (job.get("location") or {}).get("name")
    if name:
        parts.append(name)
    for off in job.get("offices") or []:
        for v in (off.get("name"), off.get("location")):
            if v and v not in parts:
                parts.append(v)
    return " | ".join(parts)


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
                location=_enriched_location(job),
                content_text=strip_html(job.get("content") or ""),
                url=job.get("absolute_url", ""),
                raw=job,
            )
