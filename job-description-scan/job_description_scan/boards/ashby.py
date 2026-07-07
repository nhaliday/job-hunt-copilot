from typing import Iterable

import httpx

from job_description_scan.boards import Posting


def _enriched_location(job: dict) -> str:
    # Primary `location` plus documented `secondaryLocations`, each followed by
    # its address's country name, so a scan's location_filter can anchor on
    # countries — primary strings are often bare city/hub names, and remote
    # variants ("Remote - Canada") may appear only as secondaries. Ashby
    # documents every per-job field as potentially missing, hence the guards.
    parts: list[str] = []
    pairs = [(job.get("location"), job.get("address"))] + [
        (sec.get("location"), sec.get("address"))
        for sec in job.get("secondaryLocations") or []
    ]
    for loc, addr in pairs:
        country = ((addr or {}).get("postalAddress") or {}).get("addressCountry")
        for v in (loc, country):
            if v and v not in parts:
                parts.append(v)
    return " | ".join(parts)


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
                location=_enriched_location(job),
                content_text=job.get("descriptionPlain") or "",
                url=job.get("jobUrl", ""),
                raw=job,
            )
