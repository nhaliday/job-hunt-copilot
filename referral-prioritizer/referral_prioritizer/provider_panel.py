"""Provider liveness panel: measure job-data APIs against native board truth.

Neither TheirStack nor fantastic.jobs can enumerate a board's live postings
directly; the usable knob is a recency window. This harness measures, per
provider x board kind x window size, id-level recall and precision against
the native clients' ground truth, so scans over aggregator-backed boards use
an evidence-based window.

Two phases, both driven by a panel CSV supplied by the consuming project
(columns: company, kind, slug, theirstack_filter [domain:X | name:X | skip],
fantastic_domain [domain | skip]):

- --pull: one widest-window pull per company per source, cached as raw files
  in --out-dir (resumable: existing files are skipped). Native ids are free;
  TheirStack costs ~1 credit per returned job (180-day window, no is_closed
  filter — rows carry the fields to evaluate every narrower window and the
  is_closed variant offline); fantastic goes through the Apify actor
  (~$4/1k jobs, timeRange 6m).
- --report: pure offline; recomputable without spend. Emits panel-curves.csv
  (long format) and panel-report.md with per-kind window recommendations.

Needs THEIRSTACK_API_KEY / APIFY_TOKEN for --pull (e.g. via direnv).
"""

import argparse
import csv
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

_TS_API = "https://api.theirstack.com/v1/jobs/search"
_FJ_COUNT = "https://data.fantastic.jobs/v1/active-ats-count"
_APIFY_RUN = (
    "https://api.apify.com/v2/acts/fantastic-jobs~career-site-job-listing-api"
    "/run-sync-get-dataset-items"
)

WINDOWS = (7, 14, 30, 60, 90, 180)
_ANCHORS = {
    # "reposted" = date_reposted falling back to date_posted: boards like
    # ashby re-post, and the repost date tracks liveness far better than the
    # original posting date.
    "theirstack": ("date_posted", "discovered_at", "reposted"),
    "fantastic": ("date_posted", "date_created"),
}
_ANCHOR_FIELDS = {"reposted": ("date_reposted", "date_posted")}

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
# Requisition ids appear as "..._R-00186224" (underscore = word char, so \b
# would never fire); require a non-letter before the R instead.
_WD_REQ = re.compile(r"(?<![A-Za-z])R-?\d{4,}\b")


def native_key(kind: str, posting_id: str) -> str | None:
    """Canonical match key for a native Posting.id."""
    if kind == "workday":
        # externalPath like /job/<City>/<Title>_R-00186224 — the requisition
        # token survives every URL skin (locale prefixes, custom fronts).
        m = _WD_REQ.search(posting_id)
        return m.group(0) if m else posting_id
    if kind in ("ashby", "lever"):
        return posting_id.lower()
    return posting_id  # greenhouse et al: already canonical strings


def provider_key(kind: str, url: str) -> str | None:
    """Board-native posting id extracted from a provider's posting URL;
    None = unattributable (e.g. a linkedin.com final_url)."""
    if not url:
        return None
    p = urlparse(url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    if kind == "greenhouse":
        gh_jid = parse_qs(p.query).get("gh_jid", [None])[0]
        if gh_jid and gh_jid.isdigit():
            return gh_jid
        if "greenhouse.io" in host and segs and segs[-1].isdigit():
            return segs[-1]
        return None
    if kind in ("ashby", "lever"):
        m = _UUID.search(url)
        return m.group(0).lower() if m else None
    if kind == "workday":
        m = _WD_REQ.search(url)
        return m.group(0) if m else None
    return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_curves(
    kind: str,
    native_ids: list[str],
    provider: str,
    rows: list[dict],
    pulled_at: datetime,
) -> list[dict]:
    """Long-format recall/precision rows across windows x anchors x filters.
    Pure: everything derives from the cached pull."""
    native = {native_key(kind, i) for i in native_ids} - {None}
    parsed = []
    for r in rows:
        key = provider_key(kind, r.get("final_url") or r.get("url") or "")
        dates = {}
        for a in _ANCHORS[provider]:
            fields = _ANCHOR_FIELDS.get(a, (a,))
            dates[a] = next((d for f in fields if (d := _parse_dt(r.get(f)))), None)
        parsed.append(
            {
                "key": key,
                "dates": dates,
                # closed_at is the field that actually appears on job rows
                # (is_closed is only a request filter).
                "open": not r.get("closed_at") and r.get("is_closed") is not True,
            }
        )
    filters = ("all", "open") if provider == "theirstack" else ("all",)
    out = []
    for anchor in _ANCHORS[provider]:
        for flt in filters:
            pool = [p for p in parsed if flt == "all" or p["open"]]
            for w in WINDOWS:
                cutoff = pulled_at - timedelta(days=w)
                inw = [
                    p
                    for p in pool
                    if p["dates"][anchor] and p["dates"][anchor] >= cutoff
                ]
                matched = {p["key"] for p in inw if p["key"] in native}
                unattr = sum(1 for p in inw if p["key"] is None)
                out.append(
                    {
                        "provider": provider,
                        "anchor": anchor,
                        "filter": flt,
                        "window_days": w,
                        "n_native": len(native),
                        "n_provider": len(inw),
                        "n_matched": len(matched),
                        "n_unattributable": unattr,
                        "recall": round(len(matched) / len(native), 3) if native else 0,
                        "precision": round(len(matched) / len(inw), 3) if inw else 0,
                    }
                )
    return out


# --------------------------------------------------------------------------- #
# Pull phase
# --------------------------------------------------------------------------- #
def _pull_native(kind: str, slug: str) -> list[str]:
    from typing import cast

    from job_description_scan.boards import make_client
    from job_description_scan.boards.workday import WorkdayClient
    from job_description_scan.config import BoardKind, BoardSource

    if kind == "workday":
        with httpx.Client(timeout=30) as http:
            return [r["externalPath"] for r in WorkdayClient(slug)._list_rows(http)]
    source = BoardSource(kind=cast("BoardKind", kind), slug=slug)
    return [p.id for p in make_client(source).iter_postings()]


def _ts_request(http: httpx.Client, body: dict) -> httpx.Response:
    for attempt in range(4):
        r = http.post(
            _TS_API,
            json=body,
            headers={"Authorization": f"Bearer {os.environ['THEIRSTACK_API_KEY']}"},
        )
        if r.status_code == 429 and attempt < 3:
            time.sleep(1.0 * 2**attempt)
            continue
        if r.status_code in (401, 402, 403):
            raise SystemExit(
                f"theirstack: HTTP {r.status_code} — bad key or out of"
                f" credits: {r.text[:200]}"
            )
        r.raise_for_status()
        return r
    raise AssertionError("unreachable")


def _pull_theirstack(http: httpx.Client, spec: str, max_jobs: int) -> list[dict]:
    mode, _, value = spec.partition(":")
    field = {
        "domain": "company_domain_or",
        "name": "company_name_case_insensitive_or",
    }[mode]
    rows: list[dict] = []
    page = 0
    while len(rows) < max_jobs:
        body = {
            field: [value],
            "posted_at_max_age_days": 180,
            "limit": min(500, max_jobs - len(rows)),
            "page": page,
            "include_total_results": page == 0,
        }
        data = _ts_request(http, body).json()
        if page == 0:
            meta = data["metadata"]
            print(
                f"    theirstack: total {meta['total_results']} in 180d"
                f" ({meta['total_companies']} companies)"
            )
        batch = data.get("data") or []
        if not batch:
            break
        for j in batch:
            j.pop("description", None)  # keep raw files small
            rows.append(j)
        page += 1
    return rows


def _pull_fantastic(http: httpx.Client, spec: str) -> list[dict]:
    # spec: "domain:X" or "org:X" (some companies are only findable by
    # organization name — their domain_derived doesn't match the real one);
    # a bare value means domain.
    mode, _, value = spec.rpartition(":")
    mode = mode or "domain"
    count = http.get(
        _FJ_COUNT,
        params={
            "time_frame": "6m",
            ("domain" if mode == "domain" else "organization"): value,
        },
        headers={
            "Authorization": f"Bearer {os.environ.get('FANTASTIC_JOBS_API_KEY', '')}"
        },
    )
    if count.status_code == 200:
        print(f"    fantastic: pre-sized count {count.json().get('count')}")
    actor_filter = (
        {"domainFilter": [value]}
        if mode == "domain"
        else {"organizationSearch": [value]}
    )
    r = http.post(
        _APIFY_RUN,
        params={"token": os.environ["APIFY_TOKEN"]},
        json={**actor_filter, "timeRange": "6m", "limit": 5000},
        timeout=280,
    )
    r.raise_for_status()
    rows = r.json()
    if isinstance(rows, dict):  # actor-level error object
        raise RuntimeError(f"apify: {rows}")
    for j in rows:
        j.pop("description_text", None)
        j.pop("description_html", None)
    return rows


def _pull(panel: list[dict], out_dir: Path, only: str | None, max_jobs: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60) as http:
        for row in panel:
            company = row["company"]
            if only and only.lower() not in company.lower():
                continue
            print(f"[{company}] ({row['kind']}/{row['slug']})")
            native_path = out_dir / f"{company}-native.json"
            if native_path.exists():
                print("    native: cached")
            else:
                ids = _pull_native(row["kind"], row["slug"])
                native_path.write_text(
                    json.dumps(
                        {
                            "pulled_at": datetime.now(timezone.utc).isoformat(),
                            "kind": row["kind"],
                            "ids": ids,
                        }
                    )
                )
                print(f"    native: {len(ids)} ids")
            for provider, spec in (
                ("theirstack", row["theirstack_filter"]),
                ("fantastic", row["fantastic_domain"]),
            ):
                path = out_dir / f"{company}-{provider}.jsonl"
                if spec == "skip" or path.exists():
                    print(f"    {provider}: {'skip' if spec == 'skip' else 'cached'}")
                    continue
                rows = (
                    _pull_theirstack(http, spec, max_jobs)
                    if provider == "theirstack"
                    else _pull_fantastic(http, spec)
                )
                with open(path, "w") as f:
                    for j in rows:
                        f.write(json.dumps(j) + "\n")
                print(f"    {provider}: {len(rows)} rows pulled")


# --------------------------------------------------------------------------- #
# Report phase
# --------------------------------------------------------------------------- #
def _report(panel: list[dict], out_dir: Path) -> None:
    curves = []
    headline = []
    for row in panel:
        company, kind = row["company"], row["kind"]
        native_path = out_dir / f"{company}-native.json"
        if not native_path.exists():
            continue
        native = json.loads(native_path.read_text())
        pulled_at = _parse_dt(native["pulled_at"])
        assert pulled_at is not None, f"bad pulled_at in {native_path}"
        for provider in ("theirstack", "fantastic"):
            path = out_dir / f"{company}-{provider}.jsonl"
            if not path.exists():
                continue
            rows = [json.loads(l) for l in open(path) if l.strip()]
            for c in compute_curves(kind, native["ids"], provider, rows, pulled_at):
                curves.append({"company": company, "kind": kind, **c})
        headline.append((company, kind, len(native["ids"])))

    curves_path = out_dir / "panel-curves.csv"
    with open(curves_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(curves[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(curves)

    # Per provider x kind x anchor x filter x window: mean recall/precision,
    # then recommend the max-F1 window on the best anchor/filter combo.
    grouped = defaultdict(list)
    for c in curves:
        grouped[
            (c["provider"], c["kind"], c["anchor"], c["filter"], c["window_days"])
        ].append(c)
    lines = ["# Provider liveness panel", ""]
    lines.append("| company | kind | native |")
    lines.append("|---|---|---|")
    for company, kind, n in headline:
        lines.append(f"| {company} | {kind} | {n} |")
    lines.append("")
    lines.append(
        "| provider | kind | anchor | filter | window | recall | precision | F1 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    best: dict = {}
    for (provider, kind, anchor, flt, w), cs in sorted(grouped.items()):
        rec = sum(c["recall"] for c in cs) / len(cs)
        prec = sum(c["precision"] for c in cs) / len(cs)
        f1 = 2 * rec * prec / (rec + prec) if rec + prec else 0
        lines.append(
            f"| {provider} | {kind} | {anchor} | {flt} | {w}d"
            f" | {rec:.2f} | {prec:.2f} | {f1:.2f} |"
        )
        k = (provider, kind)
        if k not in best or f1 > best[k][0]:
            best[k] = (f1, anchor, flt, w, rec, prec)
    lines.append("")
    lines.append("## Recommendations (max mean F1)")
    for (provider, kind), (f1, anchor, flt, w, rec, prec) in sorted(best.items()):
        lines.append(
            f"- **{provider} / {kind}**: window {w}d on `{anchor}`"
            f" (filter {flt}) — recall {rec:.2f}, precision {prec:.2f}, F1 {f1:.2f}"
        )
    report_path = out_dir / "panel-report.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"→ {curves_path}\n→ {report_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", help="substring filter on company name")
    ap.add_argument("--max-jobs", type=int, default=1000, help="theirstack cap/company")
    args = ap.parse_args()

    panel = list(csv.DictReader(open(args.panel)))
    if args.pull:
        _pull(panel, args.out_dir, args.only, args.max_jobs)
    if args.report:
        _report(panel, args.out_dir)
    if not (args.pull or args.report):
        raise SystemExit("nothing to do: pass --pull and/or --report")


if __name__ == "__main__":
    main()
