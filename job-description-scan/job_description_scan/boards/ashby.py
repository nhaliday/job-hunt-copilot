from typing import Iterable

import httpx

from job_description_scan.boards import Posting


class AshbyClient:
    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        url = (
            f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}"
            "?includeCompensation=true"
        )
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        for job in data.get("jobs", []):
            yield Posting(
                id=str(job["id"]),
                title=job.get("title", ""),
                location=job.get("location") or "",
                content_text=job.get("descriptionPlain") or "",
                url=job.get("jobUrl", ""),
                raw=job,
            )
