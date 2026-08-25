from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_CONFIG_URL = "https://myjobs.adp.com/public/staffing/v1/career-site/{slug}"
_API = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1"
_PAGE = 100


class AdpClient:
    """ADP hosted career sites — the myjobs.adp.com "CX" front-end for ADP
    Workforce Now recruiting. List-then-detail: list rows carry no job body,
    so content costs one GET per posting.

    slug is the site's path segment (myjobs.adp.com/<slug>/cx/job-listing).
    Every data call needs the tenant's `orgoid` header, fetched once from the
    public career-site config — which is also the slug gate (400 "Careersite
    not found" on a wrong slug; the requisition endpoints key off the orgoid
    alone and ignore careerSiteId). Pagination is OData-style $top/$skip;
    limit/offset are silently ignored and always serve page one.

    No location_filter pushdown: observed tenants leave every structured
    location field empty (workLocations/postingLocations/requisitionLocations
    all []), with locations only in JD prose — scans should pair this client
    with location_filter=None and let the prefilter's geography clause cut.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def _orgoid(self, http: httpx.Client) -> str:
        r = http.get(_CONFIG_URL.format(slug=self.slug))
        r.raise_for_status()  # 400 Careersite not found on a wrong slug
        return r.json()["orgoid"]

    def iter_postings(self) -> Iterable[Posting]:
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            orgoid = self._orgoid(http)
            for row in self._list_rows(http, orgoid):
                pid = str(row["reqId"])
                try:
                    yield self._detail_posting(http, orgoid, pid, row)
                except httpx.HTTPError as e:
                    # Persistent failure on ONE detail call — skip the posting
                    # loudly rather than abort the board.
                    print(f"  adp: skipping {pid}: {type(e).__name__}: {e}")

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        """Targeted detail fetches by reqId. Delisted ids (404) are skipped
        loudly; the caller sees them as missing and reports them dropped."""
        with httpx.Client(timeout=30, headers=_HEADERS) as http:
            orgoid = self._orgoid(http)
            for pid in ids:
                try:
                    yield self._detail_posting(http, orgoid, pid, None)
                except httpx.HTTPError as e:
                    print(f"  adp: skipping {pid}: {type(e).__name__}: {e}")

    def _list_rows(self, http: httpx.Client, orgoid: str) -> list[dict]:
        # Materialize before detail fetches (see workday.py: offset pagination
        # over a churning board skips/duplicates rows at page boundaries).
        rows: dict[str, dict] = {}
        skip, count = 0, None
        while True:
            r = http.get(
                f"{_API}/job-requisitions",
                params={"careerSiteId": self.slug, "$top": _PAGE, "$skip": skip},
                headers={"orgoid": orgoid},
            )
            r.raise_for_status()
            data = r.json()
            if count is None:
                count = data["count"]  # fail loud: not an ADP CX payload
                if count == 0:
                    raise ValueError(f"ADP site {self.slug!r} returned 0 postings")
            page = data.get("jobRequisitions") or []
            if not page:
                break
            before = len(rows)
            for row in page:
                if row.get("reqId"):
                    rows[str(row["reqId"])] = row
            skip += len(page)
            # No-new-rows guard alongside the count bound: churn at page
            # boundaries can repeat rows, and a shrunken board must not loop.
            if skip >= count or len(rows) == before:
                break
        if len(rows) != count:
            print(f"  adp: collected {len(rows)} rows vs count {count}")
        return list(rows.values())

    def _detail_posting(
        self, http: httpx.Client, orgoid: str, pid: str, row: dict | None
    ) -> Posting:
        r = http.get(
            f"{_API}/job-requisitions/{pid}",
            params={"careerSiteId": self.slug},
            headers={"orgoid": orgoid},
        )
        r.raise_for_status()  # delisted → 404
        detail = r.json()["jobRequisitions"][0]  # fail loud on schema change
        parts = [
            strip_html(detail.get(field) or "")
            for field in ("jobDescription", "jobQualifications")
        ]
        return Posting(
            id=pid,
            title=detail.get("publishedJobTitle")
            or detail.get("jobTitle")
            or (row or {}).get("jobTitle", ""),
            location="",  # structured locations unpopulated on ADP tenants
            content_text="\n\n".join(p for p in parts if p),
            url=f"https://myjobs.adp.com/{self.slug}/cx/job-details?reqId={pid}",
            raw=detail,
        )
