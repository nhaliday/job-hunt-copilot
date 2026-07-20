import re
from typing import Iterable

import httpx

from job_description_scan.boards import Posting, strip_html

_PAGE = 20  # server-enforced page cap
# locationsText for a multi-location posting is an aggregate like "2 Locations";
# the true locations exist only in the detail response.
_AGGREGATE = re.compile(r"^\d+ Locations$")


def _enriched_location(info: dict) -> str:
    # Detail location + additionalLocations + country descriptor, deduped and
    # " | "-joined (same shape as the ashby client) so a location_filter can
    # anchor on any of them.
    country = info.get("country")
    if isinstance(country, dict):
        country = country.get("descriptor")
    parts: list[str] = []
    for v in [info.get("location"), *(info.get("additionalLocations") or []), country]:
        if v and v not in parts:
            parts.append(v)
    return " | ".join(parts)


class WorkdayClient:
    """Unofficial Workday CxS endpoints — the JSON API behind every
    myworkdayjobs.com career site. List-then-detail: the paginated list carries
    no job body, so content costs one GET per posting; `location_filter` skips
    that GET for postings whose list-row location can't match."""

    def __init__(
        self, slug: str, location_filter: re.Pattern[str] | None = None
    ) -> None:
        # slug: "hostprefix/site", e.g. "acme.wd5/Acme_Careers". A hostprefix
        # containing ".myworkday" is taken as a full host (covers the rarer
        # myworkdaysite.com variant); otherwise ".myworkdayjobs.com" is appended.
        prefix, self.site = slug.split("/", 1)
        self.host = prefix if ".myworkday" in prefix else f"{prefix}.myworkdayjobs.com"
        self.tenant = prefix.split(".", 1)[0]
        self.location_filter = location_filter

    def iter_postings(self) -> Iterable[Posting]:
        with httpx.Client(timeout=30) as http:
            for row in self._list_rows(http):
                yield self._posting(http, row)

    def _list_rows(self, http: httpx.Client) -> list[dict]:
        # Materialize the whole list before any detail fetch: offset pagination
        # over a live board can skip or duplicate rows at page boundaries when
        # postings churn mid-walk, so keep the walk short and dedupe by path.
        url = f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"
        rows: dict[str, dict] = {}
        offset, total = 0, None
        while True:
            r = http.post(
                url,
                json={
                    "appliedFacets": {},
                    "limit": _PAGE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            r.raise_for_status()
            data = r.json()
            if total is None:
                total = data["total"]  # fail loud on schema change
            # Terminate on an empty or no-new-rows page, NOT on the reported
            # total: some tenants report total only on page 0 (0 afterwards),
            # and page-0 totals can be display-capped — trusting them either
            # truncates the walk or ends it two pages in.
            before = len(rows)
            for row in data["jobPostings"]:
                rows[row["externalPath"]] = row
            if not data["jobPostings"] or len(rows) == before:
                break
            offset += _PAGE
        if len(rows) != total:
            print(
                f"  workday: collected {len(rows)} rows vs page-0 total {total}"
                " (board churn or capped/omitted totals)"
            )
        return list(rows.values())

    def _posting(self, http: httpx.Client, row: dict) -> Posting:
        path = row["externalPath"]  # stable across fetches → ranking re-join key
        loc = row.get("locationsText") or ""
        hosted_url = f"https://{self.host}/{self.site}{path}"
        # Detail is worth fetching iff the list location matches the filter or
        # is an aggregate whose true locations only the detail reveals. The
        # pipeline re-applies the filter to the final location string either way.
        if not (
            self.location_filter is None
            or self.location_filter.search(loc)
            or _AGGREGATE.match(loc)
        ):
            return Posting(
                id=path,
                title=row.get("title", ""),
                location=loc,
                content_text="",
                url=hosted_url,
                raw=row,
            )
        r = http.get(f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}{path}")
        r.raise_for_status()
        info = r.json()["jobPostingInfo"]  # fail loud on schema change
        return Posting(
            id=path,
            title=info.get("title") or row.get("title", ""),
            location=_enriched_location(info),
            content_text=strip_html(info.get("jobDescription") or ""),
            url=info.get("externalUrl") or hosted_url,
            raw=info,
        )
