from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html


def _full_text(job: dict) -> str:
    parts = [job.get("descriptionPlain") or ""]
    for lst in job.get("lists") or []:
        header = lst.get("text") or ""
        body = strip_html(lst.get("content") or "")
        section = f"{header}\n{body}".strip()
        if section:
            parts.append(section)
    if job.get("additionalPlain"):
        parts.append(job["additionalPlain"])
    return "\n\n".join(p for p in parts if p)


def _location(job: dict) -> str:
    # Uniform, board-agnostic: the documented primary location, plus the
    # documented ISO-2 `country` as a bracketed tag so a scan's location_filter
    # can anchor on the exact country without the bare-"CA"/California collision.
    # No curated country-name map, no assumption that any board omits the country
    # from its human string — every posting is transformed identically. `country`
    # may be null (documented) → no tag; such a posting simply won't match a
    # country-based filter and is dropped as _filtered (conservative, and visible
    # in the CLI's `filtered:` count — not silent).
    loc = (job.get("categories") or {}).get("location") or ""
    country = job.get("country")
    return f"{loc} [{country}]" if country else loc


class LeverClient:
    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        url = f"https://api.lever.co/v0/postings/{self.slug}?mode=json"
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        for job in r.json():  # bare list, no .get("jobs")
            yield Posting(
                id=str(job["id"]),
                title=job.get("text", ""),
                location=_location(job),
                content_text=_full_text(job),
                url=job.get("hostedUrl", ""),
                raw=job,
            )
