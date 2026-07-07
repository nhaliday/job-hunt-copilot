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
        # "jobs" is the documented top-level key; index directly so a wrong
        # slug or schema change fails loudly instead of scanning 0 postings.
        for job in r.json()["jobs"]:
            yield Posting(
                id=str(job["id"]),
                title=job.get("title", ""),
                location=job.get("location") or "",
                content_text=job.get("descriptionPlain") or "",
                url=job.get("jobUrl", ""),
                raw=job,
            )
