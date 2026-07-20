import re
from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html

# Empirical page cap — the Posting API documents limit/offset pagination but no
# maximum. If the server ever clamps lower than this, pages would skip rows; the
# collected-vs-totalFound warning in _list_rows is the tripwire.
_PAGE = 100
# The documented jobAd sections, minus the fifth ("videos") — video URLs aren't
# useful text for the LLM.
_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


def _location(row: dict) -> str:
    # fullLocation plus the ISO-2 country as a bracketed uppercase tag — same
    # rationale as the lever client: lets a filter anchor on the exact country
    # without the bare-"CA"/California collision.
    loc = row.get("location") or {}
    full = loc.get("fullLocation") or ""
    country = loc.get("country")
    return f"{full} [{country.upper()}]" if country else full


def _full_text(job_ad: dict) -> str:
    sections = job_ad["sections"]
    parts = (strip_html((sections.get(k) or {}).get("text") or "") for k in _SECTIONS)
    return "\n\n".join(p for p in parts if p)


class SmartRecruitersClient:
    """Official SmartRecruiters Posting API. List-then-detail: the paginated
    list carries no job body, so content costs one GET per posting;
    `location_filter` skips that GET for non-matching postings."""

    def __init__(
        self, slug: str, location_filter: re.Pattern[str] | None = None
    ) -> None:
        # slug is the API company identifier, which sometimes differs from the
        # careers-site slug (e.g. "linkedin3" for jobs.smartrecruiters.com/LinkedIn3).
        self.slug = slug
        self.location_filter = location_filter

    def iter_postings(self) -> Iterable[Posting]:
        base = f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings"
        with httpx.Client(timeout=30) as http:
            for row in self._list_rows(http, base):
                yield self._posting(http, base, row)

    def _list_rows(self, http: httpx.Client, base: str) -> list[dict]:
        # Materialize before detail fetches (see workday.py: offset pagination
        # over a churning board skips/duplicates rows at page boundaries).
        rows: dict[str, dict] = {}
        offset, total = 0, None
        while total is None or offset < total:
            r = http.get(base, params={"limit": _PAGE, "offset": offset})
            r.raise_for_status()
            data = r.json()
            total = data["totalFound"]  # fail loud on schema change
            if total == 0:
                # A wrong or API-disabled identifier is 200 + totalFound 0, not
                # a 404 — raise instead of silently scanning nothing.
                raise ValueError(
                    f"SmartRecruiters identifier {self.slug!r} returned 0 postings"
                    " (identifier may differ from the careers-site slug)"
                )
            for row in data["content"]:
                rows[str(row["id"])] = row
            offset += _PAGE
        if len(rows) != total:
            print(
                f"  smartrecruiters: collected {len(rows)} rows vs totalFound"
                f" {total} (board churn mid-pagination)"
            )
        return list(rows.values())

    def _posting(self, http: httpx.Client, base: str, row: dict) -> Posting:
        pid = str(row["id"])
        loc = _location(row)
        if self.location_filter is not None and not self.location_filter.search(loc):
            return Posting(
                id=pid,
                title=row.get("name", ""),
                location=loc,
                content_text="",
                url=f"https://jobs.smartrecruiters.com/{self.slug}/{pid}",
                raw=row,
            )
        r = http.get(f"{base}/{pid}")
        r.raise_for_status()
        detail = r.json()
        return Posting(
            id=pid,
            title=detail.get("name") or row.get("name", ""),
            location=loc,
            content_text=_full_text(detail["jobAd"]),  # fail loud on schema change
            url=detail.get("postingUrl", ""),
            raw=detail,
        )
