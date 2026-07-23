"""Add free pre-gate stats to an enriched companies CSV.

Counts, per scannable board, the postings whose location matches a filter —
no LLM spend, list-level requests only. Location semantics mirror the
job-description-scan board clients: for one-shot boards (greenhouse, ashby,
lever) the clients themselves are reused, so the counted `Posting.location`
strings are exactly what a scan's location_filter would see. The paginated
kinds are counted from list rows (smartrecruiters: `fullLocation [CC]`;
workday: `locationsText`, with detail fetched only for aggregate
"N Locations" rows — the same rows the scan client resolves).

Writes/refreshes one column in place: n_postings_located. Rows without a
scannable board_kind are left blank; per-board errors leave the previous
value and print a warning.
"""

import argparse
import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from job_description_scan.boards import make_client
from job_description_scan.boards.smartrecruiters import _location as sr_location
from job_description_scan.boards.workday import _enriched_location
from job_description_scan.config import BoardSource

SCANNABLE = ("greenhouse", "ashby", "lever", "workday", "smartrecruiters")

# Case defaults live with the caller via flags; these mirror a US+Canada scope.
DEFAULT_FILTER = r"\b(USA?|United States|Canada)\b"
# Workday list rows are bare "City, ST" with no country (see the
# job-description-scan CLAUDE.md gotcha), so match state/province codes too.
DEFAULT_WORKDAY_FILTER = r", [A-Z]{2}\b|United States|Canada|\bRemote\b"


def _find_country_facet(facets: list) -> list | None:
    # Country facets vary by tenant: `locationCountry` top-level, nested
    # inside a locationMainGroup wrapper, or a custom parameter (observed:
    # "CF_-_REC_-_..._Country_...") — but the parameter or display label
    # says "country" in every observed case. Match on that.
    for f in facets:
        label = f"{f.get('facetParameter') or ''} {f.get('descriptor') or ''}".lower()
        vals = f.get("values") or []
        inner = [v for v in vals if isinstance(v, dict) and "facetParameter" in v]
        if "country" in label and vals and not inner:
            return vals
        if inner:
            found = _find_country_facet(inner)
            if found is not None:
                return found
    return None


def _count_workday(http: httpx.Client, slug: str, country_pattern: re.Pattern,
                   row_pattern: re.Pattern) -> int:
    prefix, _, site = slug.partition("/")
    host = prefix if ".myworkday" in prefix else f"{prefix}.myworkdayjobs.com"
    tenant = prefix.split(".", 1)[0]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

    r = http.post(url, json={**body, "limit": 1})
    r.raise_for_status()
    first = r.json()
    # Preferred: the server's own per-country counts — exact regardless of the
    # tenant's locationsText format (campus names, "State - City" styles, ...).
    countries = _find_country_facet(first.get("facets") or [])
    if countries is not None:
        return sum(v.get("count", 0) for v in countries
                   if country_pattern.search(v.get("descriptor") or ""))

    # Fallback for tenants without a country facet: their locationsText is
    # typically country-less too (street addresses, campus names), so rows the
    # row_pattern doesn't already match need their detail's enriched location
    # (which carries the country descriptor). Free, but one GET per unmatched
    # row — parallelized within the board.
    # Same pagination rules as the engine's WorkdayClient: dedupe by path and
    # terminate on an empty/no-new page — per-page totals are unreliable
    # (some tenants report total only on page 0, and page-0 totals can be
    # display-capped).
    by_path: dict[str, dict] = {}
    offset = 0
    while True:
        r = http.post(url, json={**body, "offset": offset})
        r.raise_for_status()
        page = r.json()["jobPostings"]
        before = len(by_path)
        for row in page:
            # Degenerate rows with only bulletFields (no path) exist; skip.
            if row.get("externalPath"):
                by_path[row["externalPath"]] = row
        if not page or len(by_path) == before:
            break
        offset += 20
    rows = list(by_path.values())

    matched = [row for row in rows if row_pattern.search(row.get("locationsText") or "")]
    unmatched = [row for row in rows if row not in matched]

    def detail_matches(row: dict) -> bool:
        d = http.get(f"https://{host}/wday/cxs/{tenant}/{site}{row['externalPath']}")
        if d.status_code != 200:
            return False
        enriched = _enriched_location(d.json()["jobPostingInfo"])
        return bool(country_pattern.search(enriched) or row_pattern.search(enriched))

    with ThreadPoolExecutor(max_workers=8) as ex:
        return len(matched) + sum(ex.map(detail_matches, unmatched))


def _count_smartrecruiters(http: httpx.Client, slug: str, pattern: re.Pattern) -> int:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    n = offset = 0
    total = None
    while True:
        r = http.get(base, params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        if total is None:
            total = data["totalFound"]
        if not data["content"]:
            break
        n += sum(1 for row in data["content"] if pattern.search(sr_location(row)))
        offset += 100
        if offset >= total:
            break
    return n


def count_located(http: httpx.Client, kind: str, slug: str,
                  pattern: re.Pattern, workday_pattern: re.Pattern) -> int:
    if kind == "workday":
        return _count_workday(http, slug, pattern, workday_pattern)
    if kind == "smartrecruiters":
        return _count_smartrecruiters(http, slug, pattern)
    client = make_client(BoardSource(kind=kind, slug=slug))
    return sum(1 for p in client.iter_postings() if pattern.search(p.location))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", type=Path, required=True)
    ap.add_argument("--location-filter", default=DEFAULT_FILTER)
    ap.add_argument("--workday-location-filter", default=DEFAULT_WORKDAY_FILTER)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    pattern = re.compile(args.location_filter, re.IGNORECASE)
    workday_pattern = re.compile(args.workday_location_filter)

    rows = list(csv.DictReader(open(args.companies)))
    for r in rows:
        r.setdefault("n_postings_located", "")
    scannable = [r for r in rows if r.get("board_kind") in SCANNABLE]
    print(f"{len(scannable)} scannable boards")

    def job(r: dict) -> None:
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            try:
                n = count_located(http, r["board_kind"], r["board_slug"],
                                  pattern, workday_pattern)
            except Exception as e:  # noqa: BLE001 — per-board isolation, reported
                print(f"  WARN {r['company']}: {type(e).__name__}: {e}")
                return
        r["n_postings_located"] = str(n)
        print(f"  {r['company']}: {n}")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(job, scannable))

    tmp = args.companies.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(args.companies)
    done = sum(1 for r in scannable if r["n_postings_located"] != "")
    print(f"wrote counts for {done}/{len(scannable)} boards")


if __name__ == "__main__":
    main()
