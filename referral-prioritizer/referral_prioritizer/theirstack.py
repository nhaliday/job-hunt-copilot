"""TheirStack census sweep: per-company posting counts + board hints.

For each companies-CSV row, queries the TheirStack jobs API by
case-insensitive company name and writes three columns in place:

- n_postings_theirstack — metadata.total_results over the recency window
- theirstack_companies — metadata.total_companies (>1 = the name matched
  several employers; eyeball before trusting the count)
- theirstack_hint — board fingerprint from the sampled jobs' final_url
  hosts, "kind:slug" when a native board kind is recognizable, else the
  bare host

`board_*` columns are never touched — applying hints to the census stays a
manual step. Costs ~(--sample + 1) API credits per row (1 credit per job
returned). Resumable: rows with n_postings_theirstack are skipped unless
--force; the CSV is rewritten atomically after every resolved row. Needs
THEIRSTACK_API_KEY (e.g. via direnv).
"""

import argparse
import collections
import csv
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

_API = "https://api.theirstack.com/v1/jobs/search"

_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
    "jobs.lever.co": "lever",
    "jobs.smartrecruiters.com": "smartrecruiters",
}
_WORKDAY_LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")
_COLUMNS = ("n_postings_theirstack", "theirstack_companies", "theirstack_hint")


def _board_hint(urls: list[str]) -> str:
    """Map sampled posting URLs to a board fingerprint; majority wins."""
    hints = []
    for u in urls:
        if not u:
            continue
        parsed = urlparse(u)
        host = parsed.netloc.lower()
        if not host:
            continue
        segs = [s for s in parsed.path.split("/") if s]
        kind = _ATS_HOSTS.get(host)
        if kind and segs:
            hints.append(f"{kind}:{segs[0]}")
        elif host.endswith(".myworkdayjobs.com"):
            prefix = host[: -len(".myworkdayjobs.com")]
            site = next((s for s in segs if not _WORKDAY_LOCALE.fullmatch(s)), None)
            hints.append(f"workday:{prefix}/{site}" if site else host)
        else:
            hints.append(host)
    if not hints:
        return ""
    return collections.Counter(hints).most_common(1)[0][0]


def _select(
    rows: list[dict], kinds: set[str] | None, only: str | None, force: bool
) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("company"):
            continue
        if kinds is not None and (r.get("board_kind") or "none") not in kinds:
            continue
        if only and only.lower() not in r["company"].lower():
            continue
        if r.get("n_postings_theirstack") and not force:
            continue
        out.append(r)
    return out


def _search(http: httpx.Client, company: str, max_age_days: int, sample: int) -> dict:
    body = {
        "company_name_case_insensitive_or": [company],
        "posted_at_max_age_days": max_age_days,
        "limit": sample,
        "include_total_results": True,
    }
    for attempt in range(4):
        r = http.post(_API, json=body)
        if r.status_code == 429 and attempt < 3:
            time.sleep(1.0 * 2**attempt)
            continue
        if r.status_code in (401, 402, 403):
            # Bad key or exhausted credits: abort the whole run rather than
            # warn once per row for the rest of the census.
            raise SystemExit(
                f"theirstack: HTTP {r.status_code} — bad key or out of"
                f" credits: {r.text[:200]}"
            )
        r.raise_for_status()
        return r.json()
    raise AssertionError("unreachable")


def _write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", type=Path, required=True)
    ap.add_argument(
        "--kinds", help="comma-separated board_kind filter, e.g. custom,unknown,none"
    )
    ap.add_argument("--only", help="substring filter on company name")
    ap.add_argument("--limit-rows", type=int)
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument(
        "--sample", type=int, default=3, help="jobs fetched per row for the board hint"
    )
    ap.add_argument(
        "--force", action="store_true", help="re-query rows that already have a count"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="row count + credit estimate, no HTTP"
    )
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.companies)))
    for r in rows:
        for col in _COLUMNS:
            r.setdefault(col, "")
    kinds = set(args.kinds.split(",")) if args.kinds else None
    selected = _select(rows, kinds, args.only, args.force)
    if args.limit_rows:
        selected = selected[: args.limit_rows]
    est = len(selected) * (args.sample + 1)
    print(f"{len(selected)} rows to sweep (~{est} credits at sample={args.sample})")
    if args.dry_run:
        return

    key = os.environ.get("THEIRSTACK_API_KEY")
    if not key:
        raise SystemExit("THEIRSTACK_API_KEY not set (load .envrc / direnv exec)")

    with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {key}"}) as http:
        for i, r in enumerate(selected, 1):
            try:
                resp = _search(http, r["company"], args.max_age_days, args.sample)
            except (httpx.HTTPError, ValueError) as e:  # per-row isolation
                print(f"  WARN {r['company']}: {type(e).__name__}: {e}")
                continue
            meta = resp["metadata"]
            jobs = resp.get("data") or []
            r["n_postings_theirstack"] = str(meta["total_results"])
            r["theirstack_companies"] = str(meta["total_companies"])
            r["theirstack_hint"] = _board_hint(
                [j.get("final_url") or j.get("url") or "" for j in jobs]
            )
            _write(args.companies, rows)
            print(
                f"  [{i}/{len(selected)}] {r['company']}: "
                f"{r['n_postings_theirstack']} postings"
                f" ({r['theirstack_companies']} companies)"
                f" {r['theirstack_hint']}"
            )


if __name__ == "__main__":
    main()
