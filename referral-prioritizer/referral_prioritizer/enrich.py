"""TheirStack enrichment for census companies with no scannable board.

Pull-once / compute-offline: each selected company's postings are pulled from
the TheirStack jobs API ONCE and cached verbatim (`theirstack-<slug>.raw.jsonl`
— rows keep their LinkedIn URLs and company metadata); every downstream stage
(prefilter, extraction, ranking, summary) reads the cache through the engine's
in-memory StaticClient and costs no credits to re-run. Pulls are per-company
atomic (TheirStack checks the full credit price upfront), written
.partial-then-rename, and skipped when cached — running out of credits
mid-sweep just parks the remaining companies until the next run.

Selection: census rows with a non-scannable board_kind and
0 < n_postings_theirstack <= --max-postings (megacorp exclusion), ordered by
(-n_connections, +count) so credit exhaustion hits the least valuable rows.
Extraction reuses the factory's workday-kind scan config: location_filter=None
with the prefilter's geography clause doing the US/Canada cut, since
TheirStack locations are free text ("London Area", "Chicago, IL").

Results are index snapshots, not live boards: summary.csv rows carry
scan_source=theirstack.

Needs THEIRSTACK_API_KEY (and ANTHROPIC_API_KEY) via direnv for live runs.
"""

import argparse
import asyncio
import csv
import importlib
import json
import os
import sys
from pathlib import Path

import httpx

from job_description_scan.boards import Posting, StaticClient
from job_description_scan.config import Ladder
from job_description_scan.pipeline import run_scan

from referral_prioritizer.provider_panel import _ts_request
from referral_prioritizer.scan import Board, _rank_board, load_boards, write_summary

_TAIL_KINDS = ("custom", "unknown", "none", "")


def _select(
    rows: list[dict],
    max_postings: int,
    exclude: list[str],
    only: str | None,
) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("company"):
            continue
        if (r.get("board_kind") or "") not in _TAIL_KINDS:
            continue
        count = r.get("n_postings_theirstack") or ""
        if not count.isdigit() or not 0 < int(count) <= max_postings:
            continue
        name = r["company"].lower()
        if any(e and e in name for e in exclude):
            continue
        if only and only.lower() not in name:
            continue
        out.append(r)
    out.sort(
        key=lambda r: (
            -int(r["n_connections"] or 0),
            int(r["n_postings_theirstack"]),
        )
    )
    return out


def _to_postings(rows: list[dict], company: str) -> tuple[list[Posting], int]:
    """Map raw TheirStack rows to Postings; drop rows whose company name
    doesn't match the census row (ambiguous-name guard). Raw rows stay on
    disk untouched — this runs at load time, every run, for free."""
    postings, strangers = [], 0
    for r in rows:
        if (r.get("company") or "").lower() != company.lower():
            strangers += 1
            continue
        postings.append(
            Posting(
                id=str(r["id"]),
                title=r.get("job_title") or "",
                location=r.get("location") or r.get("short_location") or "",
                content_text=r.get("description") or "",
                url=r.get("url") or "",
                raw={},
            )
        )
    return postings, strangers


def _pull(http: httpx.Client, company: str, max_age_days: int, path: Path) -> int:
    body = {
        "company_name_case_insensitive_or": [company],
        "posted_at_max_age_days": max_age_days,
        "limit": 500,  # the selected tail fits one page (--max-postings <= 1000
        # would need two; TheirStack's upfront credit check keeps it atomic)
        "include_total_results": True,
    }
    data = _ts_request(http, body).json()
    meta = data["metadata"]
    if meta["total_companies"] and meta["total_companies"] > 1:
        print(
            f"    WARN: name matched {meta['total_companies']} companies"
            " (stranger rows dropped at load time)"
        )
    rows = data.get("data") or []
    tmp = path.with_suffix(".partial")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.replace(path)
    return len(rows)


async def _scan(scan, postings: list[Posting], args, out_path: Path) -> None:
    counts = {"rows": 0, "dropped": 0}
    tmp = out_path.with_suffix(".partial")
    dropped_path = out_path.with_name(out_path.name.replace(".jsonl", "-dropped.jsonl"))
    tmp_drop = dropped_path.with_suffix(".partial")
    with open(tmp, "w") as f, open(tmp_drop, "w") as fd:
        async for row in run_scan(
            scan,
            StaticClient(postings),
            resume_path=args.resume,
            concurrency=args.concurrency,
        ):
            if row.get("_filter_stage"):
                fd.write(json.dumps(row) + "\n")
                counts["dropped"] += 1
            else:
                f.write(json.dumps(row) + "\n")
                counts["rows"] += 1
    tmp.replace(out_path)
    tmp_drop.replace(dropped_path)
    print(f"    scanned: {counts['rows']} rows, {counts['dropped']} prefilter-dropped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("_output/referral-scans"))
    ap.add_argument("--boards", default="scans.boards", help="factory module")
    ap.add_argument("--resume", type=Path, help="rendered resume markdown")
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument(
        "--max-postings",
        type=int,
        default=1000,
        help="skip rows above this n_postings_theirstack (megacorps)",
    )
    ap.add_argument("--exclude", default="", help="comma substrings on company name")
    ap.add_argument("--only", help="substring filter on company name")
    ap.add_argument("--force", action="store_true", help="redo scan/rank artifacts")
    ap.add_argument("--judge-model", default="claude-opus-5")
    ap.add_argument("--order-swap", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--skip-rank", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="rows + credits, no spend")
    args = ap.parse_args()

    sys.path.insert(0, str(Path.cwd()))
    factory = importlib.import_module(args.boards)
    ladders: list[Ladder] = factory.ladders()

    census = list(csv.DictReader(open(args.companies)))
    exclude = [e.strip().lower() for e in args.exclude.split(",") if e.strip()]
    selected = _select(census, args.max_postings, exclude, args.only)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    est = sum(
        int(r["n_postings_theirstack"])
        for r in selected
        if not (
            args.out_dir
            / f"{Board('theirstack', r['company'], r['company']).name}.raw.jsonl"
        ).exists()
    )
    print(f"{len(selected)} companies selected (~{est} credits for uncached pulls)")
    if args.dry_run:
        for r in selected:
            print(f"  {r['company']}: {r['n_postings_theirstack']} postings")
        return
    if not args.resume:
        raise SystemExit("--resume is required for a live run")

    ts_dead: str | None = None
    with httpx.Client(timeout=60) as http:
        for r in selected:
            company = r["company"]
            board = Board(kind="theirstack", slug=company, label=company)
            raw_path = args.out_dir / f"{board.name}.raw.jsonl"
            out_path = args.out_dir / f"{board.name}.jsonl"
            print(f"[{company}]")

            if raw_path.exists():
                print("    pull: cached")
            elif ts_dead:
                print(f"    pull: skipped ({ts_dead})")
                continue
            else:
                if not os.environ.get("THEIRSTACK_API_KEY"):
                    raise SystemExit("THEIRSTACK_API_KEY not set")
                try:
                    n = _pull(http, company, args.max_age_days, raw_path)
                except SystemExit as e:
                    ts_dead = str(e)
                    print(f"    pull: DEAD — {e}")
                    continue
                print(f"    pull: {n} rows ({n} credits)")

            raw_rows = [json.loads(l) for l in open(raw_path) if l.strip()]
            postings, strangers = _to_postings(raw_rows, company)
            if strangers:
                print(f"    dropped {strangers} stranger-company rows")
            if not postings:
                print("    no matching postings; skipping scan")
                continue

            if out_path.exists() and not args.force:
                print("    scan: cached")
            else:
                # workday-kind config on purpose: location_filter=None + the
                # prefilter's geography clause fits TheirStack's free-text
                # locations (see module docstring).
                scan = factory.make_scan("workday", board.name)
                asyncio.run(_scan(scan, postings, args, out_path))

            if not args.skip_rank:
                _rank_board(board, StaticClient(postings), ladders, args, out_path)

    path = write_summary(
        args.companies, load_boards(args.companies), ladders, args.out_dir
    )
    print(f"\n→ {path}", flush=True)


if __name__ == "__main__":
    main()
