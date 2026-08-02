import re
from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html

# Empirical: the widgets endpoint honors large `size` values (a 300-row page
# worked on the probed tenant), but page conservatively — an undocumented cap
# would silently truncate a single-page walk.
_PAGE = 100
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _locations(row: dict) -> str:
    # List rows carry `multi_location` as full "City, State, Country" strings;
    # detail responses carry dicts with the same string under "location".
    # Either way every entry names its country, so a location_filter can
    # anchor on country names directly (no bare-"City, ST" workday problem).
    locs = row.get("multi_location") or []
    parts: list[str] = []
    for loc in locs:
        s = loc.get("location") if isinstance(loc, dict) else loc
        if s and s not in parts:
            parts.append(s)
    return " | ".join(parts) or row.get("cityStateCountry") or ""


class PhenomClient:
    """Phenom (CareerConnect) career sites — the JSON API behind branded
    careers.<company>.com portals. Everything rides one endpoint:
    `POST /widgets` with a `ddoKey` selecting the operation (`refineSearch`
    lists, `jobDetail` fetches one posting). List-then-detail: list rows carry
    only a teaser, so content costs one POST per posting; `location_filter`
    skips that POST for postings whose list-row locations can't match.

    Phenom fronts a separate ATS (the detail's `ats` field says which), so
    apply links may point off-site (e.g. into a Workday tenant whose own
    listing surface is disabled)."""

    def __init__(
        self, slug: str, location_filter: re.Pattern[str] | None = None
    ) -> None:
        # slug: the careers-site host, e.g. "careers.acme.org". Locale-prefixed
        # paths (/us/en) exist only for the human-facing pages; the widgets
        # endpoint is locale-independent at the root.
        self.host = slug
        self.url = f"https://{slug}/widgets"
        self.location_filter = location_filter

    def iter_postings(self) -> Iterable[Posting]:
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            for row in self._list_rows(http):
                try:
                    p = self._posting(http, row)
                except httpx.HTTPError as e:
                    # Persistent failure on ONE detail call — skip the posting
                    # loudly rather than abort the board.
                    print(
                        f"  phenom: skipping {row.get('jobId')}: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue
                if p is None:
                    print(
                        f"  phenom: skipping {row.get('jobId')}: delisted"
                        " (jobDetail answered without a job payload)"
                    )
                    continue
                yield p

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        """Targeted detail fetches by jobId. Lets the ranking join skip the
        full board walk. Delisted ids are skipped loudly; the caller sees them
        as missing and reports them dropped."""
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            for pid in ids:
                try:
                    p = self._detail_posting(http, pid, row=None)
                except httpx.HTTPError as e:
                    print(f"  phenom: skipping {pid}: {type(e).__name__}: {e}")
                    continue
                if p is None:
                    print(
                        f"  phenom: skipping {pid}: delisted"
                        " (jobDetail answered without a job payload)"
                    )
                    continue
                yield p

    def _list_rows(self, http: httpx.Client) -> list[dict]:
        # Materialize before detail fetches (see workday.py: offset pagination
        # over a churning board skips/duplicates rows at page boundaries).
        rows: dict[str, dict] = {}
        offset, total = 0, None
        while True:
            r = http.post(
                self.url,
                json={
                    "ddoKey": "refineSearch",
                    "from": offset,
                    "size": _PAGE,
                    "jobs": True,
                },
            )
            r.raise_for_status()
            data = r.json()["refineSearch"]  # fail loud: not a Phenom site
            if total is None:
                total = data["totalHits"]
                if total == 0:
                    # A wrong host that still serves a widgets endpoint would
                    # look like an empty board — raise instead of silently
                    # scanning nothing (mirrors the smartrecruiters client).
                    raise ValueError(f"Phenom site {self.host!r} returned 0 postings")
            page = data["data"]["jobs"]
            before = len(rows)
            for row in page:
                if row.get("jobId"):
                    rows[row["jobId"]] = row
            if not page or len(rows) == before:
                break
            offset += _PAGE
        if len(rows) != total:
            print(
                f"  phenom: collected {len(rows)} rows vs totalHits {total}"
                " (board churn mid-pagination)"
            )
        return list(rows.values())

    def _posting(self, http: httpx.Client, row: dict) -> Posting | None:
        pid = row["jobId"]
        loc = _locations(row)
        # List rows carry only a description teaser, so content requires the
        # jobDetail call; skip it when the list locations can't match. The
        # pipeline re-applies the filter to the final location string.
        if self.location_filter is not None and not self.location_filter.search(loc):
            return Posting(
                id=pid,
                title=row.get("title", ""),
                location=loc,
                content_text="",
                url=f"https://{self.host}/us/en/job/{pid}",
                raw=row,
            )
        return self._detail_posting(http, pid, row)

    def _detail_posting(
        self, http: httpx.Client, pid: str, row: dict | None
    ) -> Posting | None:
        """None means delisted: jobDetail answers 200 with the "job" key
        absent, an expected outcome at observed churn rates — so it is a
        return value, not an exception. The envelope keys stay hard-indexed:
        their absence is schema drift and must abort the walk loudly."""
        r = http.post(self.url, json={"ddoKey": "jobDetail", "jobId": pid})
        r.raise_for_status()
        detail = r.json()["jobDetail"]["data"].get("job")
        if detail is None:
            return None
        return Posting(
            id=pid,
            title=detail.get("title") or (row or {}).get("title", ""),
            location=_locations(detail) or _locations(row or {}),
            content_text=strip_html(detail.get("description") or ""),
            url=f"https://{self.host}/us/en/job/{pid}",
            raw=detail,
        )
