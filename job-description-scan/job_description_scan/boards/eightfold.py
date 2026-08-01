import re
from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _locations(row: dict) -> str:
    # `locations` is a list of strings on both list rows and details:
    # "City, ST, US" domestically, "International - <Country>" abroad — every
    # entry carries a country token a location_filter can anchor on.
    parts: list[str] = []
    for loc in row.get("locations") or []:
        if loc and loc not in parts:
            parts.append(loc)
    return " | ".join(parts)


class EightfoldClient:
    """Eightfold AI career sites, PCSX experience — the JSON API behind
    self-hosted careers portals. List-then-detail: list rows carry no job
    body, so content costs one GET per posting; `location_filter` skips that
    GET for postings whose list-row locations can't match.

    The classic documented endpoint (/api/apply/v2/jobs) answers
    "Not authorized for PCSX" on these deployments; the operative pair is
    /api/pcsx/search and /api/pcsx/position_details. Eightfold fronts a
    separate ATS (rows carry an atsJobId), so a company may have e.g. a
    Workday tenant behind it."""

    def __init__(
        self, slug: str, location_filter: re.Pattern[str] | None = None
    ) -> None:
        # slug: "host/domain", e.g. "searchcareers.acme.com/acme.com" — the
        # careers host plus the `domain` query param every API call requires
        # (shown in the page's own config; usually the company's main domain).
        self.host, self.domain = slug.split("/", 1)
        self.location_filter = location_filter

    def iter_postings(self) -> Iterable[Posting]:
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            for row in self._list_rows(http):
                try:
                    yield self._posting(http, row)
                except httpx.HTTPError as e:
                    # Persistent failure on ONE detail call — skip the posting
                    # loudly rather than abort the board.
                    print(
                        f"  eightfold: skipping {row.get('id')}: "
                        f"{type(e).__name__}: {e}"
                    )

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        """Targeted detail fetches by position id. Lets the ranking join skip
        the full board walk. Delisted ids (404) are skipped loudly; the caller
        sees them as missing and reports them dropped."""
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            for pid in ids:
                try:
                    yield self._detail_posting(http, pid, row=None)
                except httpx.HTTPError as e:
                    print(f"  eightfold: skipping {pid}: {type(e).__name__}: {e}")

    def _list_rows(self, http: httpx.Client) -> list[dict]:
        # Materialize before detail fetches (see workday.py: offset pagination
        # over a churning board skips/duplicates rows at page boundaries).
        # Sort keys are coarse (postedTs is day-granular), so huge tie blocks
        # reshuffle on EVERY request and one offset walk both repeats and
        # skips rows — a single pass misses ~13% of the board (observed
        # 1392/1595). Misses are random per pass, so unioning repeated walks
        # converges fast; stop when a pass adds nothing new.
        rows: dict[str, dict] = {}
        total = 0
        for _ in range(5):
            before = len(rows)
            total = self._walk_once(http, rows)
            if len(rows) >= total or len(rows) == before:
                break
        if len(rows) != total:
            print(
                f"  eightfold: collected {len(rows)} rows vs count {total}"
                " (board churn or tie-shuffle residue)"
            )
        return list(rows.values())

    def _walk_once(self, http: httpx.Client, rows: dict[str, dict]) -> int:
        url = f"https://{self.host}/api/pcsx/search"
        offset, total = 0, None
        while True:
            r = http.get(
                url,
                params={
                    "domain": self.domain,
                    "query": "",
                    "location": "",
                    "start": offset,
                    # Fewer between-request reorders than the default
                    # relevance sort (solrScore).
                    "sort_by": "timestamp",
                },
            )
            r.raise_for_status()
            data = r.json()["data"]  # fail loud: not a PCSX Eightfold site
            if total is None:
                total = data["count"]
                if total == 0 and not rows:
                    # A wrong host/domain pair can still answer with an empty
                    # result set — raise instead of silently scanning nothing.
                    raise ValueError(
                        f"Eightfold site {self.host!r} returned 0 postings"
                    )
            page = data["positions"]
            if not page:
                break
            for row in page:
                if row.get("id") is not None:
                    rows[str(row["id"])] = row
            # Page size is server-fixed (observed 10; a num param is ignored),
            # so advance by whatever the server actually returned. No
            # no-new-rows termination (unlike workday/phenom): tie-shuffled
            # pages legitimately repeat mid-walk. The server count is the
            # bound — deep offsets serve fine here.
            offset += len(page)
            if offset >= total:
                break
        return total

    def _posting(self, http: httpx.Client, row: dict) -> Posting:
        pid = str(row["id"])
        loc = _locations(row)
        if self.location_filter is not None and not self.location_filter.search(loc):
            return Posting(
                id=pid,
                title=row.get("name", ""),
                location=loc,
                content_text="",
                url=row.get("positionUrl") or f"https://{self.host}/careers/job/{pid}",
                raw=row,
            )
        return self._detail_posting(http, pid, row)

    def _detail_posting(
        self, http: httpx.Client, pid: str, row: dict | None
    ) -> Posting:
        r = http.get(
            f"https://{self.host}/api/pcsx/position_details",
            params={"domain": self.domain, "position_id": pid},
        )
        r.raise_for_status()
        detail = r.json()["data"]  # fail loud on schema change
        return Posting(
            id=pid,
            title=detail.get("name") or (row or {}).get("name", ""),
            location=_locations(detail) or _locations(row or {}),
            content_text=strip_html(detail.get("jobDescription") or ""),
            url=detail.get("publicUrl") or f"https://{self.host}/careers/job/{pid}",
            raw=detail,
        )
