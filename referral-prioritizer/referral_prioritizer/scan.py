"""Bulk scan + rank over an enriched companies CSV.

Runs the job-description-scan engine over every scannable board in the CSV
(deduped by (board_kind, board_slug)), then a pairwise ranking per configured
ladder, and finally writes a per-company summary CSV — the input to a later
human gating/ranking pass.

Generic orchestration only: all case content (extraction schemas, location
filters, the cheap-model prefilter criterion, ranking ladders) comes from a
factory module in the consuming project, passed as a dotted path and imported
from the cwd (like the engine CLI's --scan). The factory supplies:

    make_scan(kind: str, slug: str) -> job_description_scan.config.Scan
    ladders() -> list[job_description_scan.config.Ladder]

Outputs, per board, under --out-dir (all generated, never committed):
    <kind>-<slug>.jsonl           scan results (written atomically: .partial
                                  until complete, so an existing file means a
                                  finished board and re-runs skip it)
    <kind>-<slug>-dropped.jsonl   prefilter/title_precut drops with reasons —
                                  skim to validate the criterion text
    <kind>-<slug>.log             per-posting progress lines + final counts
    <kind>-<slug>-rank-<role>.jsonl   ranking ladder (boards with >=2 clusters)
    summary.csv                   one row per company: fit-tier counts and
                                  top-3 per ladder (rebuilt from disk every
                                  run, so resumed/partial runs stay coherent)
"""

import argparse
import asyncio
import csv
import importlib
import json
import math
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from job_description_scan.boards import Posting, make_client
from job_description_scan.config import Ladder, Scan
from job_description_scan.output import JsonlWriter
from job_description_scan.pipeline import run_scan
from job_description_scan.ranking import (
    _TIER_ORDER,
    dedupe,
    join_content,
    run_ladder,
    select_rows,
)

SCANNABLE = ("greenhouse", "ashby", "lever", "workday", "smartrecruiters")

# Ladder pools above this many clusters rank with the cheaper swiss schedule
# instead of round-robin (which is quadratic in judge calls).
SWISS_THRESHOLD = 12

# $/1M tokens (input, output) for the --dry-run estimate only.
_PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


@dataclass
class Board:
    kind: str
    slug: str
    label: str  # highest-connection company sharing this board
    companies: list[str] = field(default_factory=list)
    n_located: int = 0  # from the companies CSV (pre-gate stats column)

    @property
    def name(self) -> str:
        """Filesystem-safe output basename."""
        return f"{self.kind}-{re.sub(r'[^A-Za-z0-9._-]+', '-', self.slug)}"


def load_boards(companies_csv: Path) -> list[Board]:
    """Scannable boards, deduped by (kind, slug). The CSV is sorted by
    -n_connections, so the first company seen becomes the board's label."""
    boards: dict[tuple[str, str], Board] = {}
    with open(companies_csv, newline="") as f:
        for r in csv.DictReader(f):
            kind, slug = r.get("board_kind", ""), r.get("board_slug", "")
            if kind not in SCANNABLE or not slug:
                continue
            b = boards.setdefault(
                (kind, slug), Board(kind=kind, slug=slug, label=r["company"])
            )
            b.companies.append(r["company"])
            n = r.get("n_postings_located", "")
            b.n_located = max(b.n_located, int(n) if n else 0)
    return [b for b in boards.values() if b.n_located > 0]


class _CachingClient:
    """Records the postings a full iteration yields so the ranking join can
    reuse them instead of re-fetching the board (the scan JSONL has no JD
    bodies; ranking joins content back in by posting id)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.postings: list[Posting] | None = None

    def iter_postings(self):
        if self.postings is not None:
            yield from self.postings
            return
        acc: list[Posting] = []
        for p in self._inner.iter_postings():
            acc.append(p)
            yield p
        self.postings = acc  # set only after complete iteration

    def index(self) -> dict[str, Posting]:
        if self.postings is None:
            self.postings = list(self._inner.iter_postings())
        return {p.id: p for p in self.postings}


async def _scan_board(
    board: Board,
    scan_cfg: Scan,
    client: _CachingClient,
    args: argparse.Namespace,
    out_path: Path,
) -> None:
    tmp_out = out_path.with_name(out_path.name + ".partial")
    dropped_path = out_path.with_name(f"{board.name}-dropped.jsonl")
    tmp_drop = dropped_path.with_name(dropped_path.name + ".partial")
    log_path = out_path.with_name(f"{board.name}.log")

    counts: Counter[str] = Counter()
    pf_stats: dict | None = None
    with (
        JsonlWriter(tmp_out) as w,
        JsonlWriter(tmp_drop) as wd,
        open(log_path, "w", encoding="utf-8") as log,
    ):
        async for row in run_scan(
            scan_cfg, client, args.resume, args.model, args.limit, args.concurrency
        ):
            if "_prefilter_stats" in row:
                pf_stats = row["_prefilter_stats"]
                continue
            if row.get("_filtered"):
                stage = row.get("_filter_stage", "location")
                counts[stage] += 1
                if stage != "location":  # location drops are bulk noise
                    wd.write(row)
                continue
            counts["extracted"] += 1
            if "error" in row:
                counts["errors"] += 1
            w.write(row)
            p = row["posting"]
            log.write(f"{p['title']} @ {p['location']}\n")
            log.flush()
        log.write(f"\ncounts: {dict(counts)}\nprefilter: {pf_stats}\n")

    tmp_out.replace(out_path)
    tmp_drop.replace(dropped_path)
    line = (
        f"[{board.label}] {counts['extracted']} extracted "
        f"({counts['errors']} errors), {counts['prefilter']} prefiltered, "
        f"{counts['title_precut']} precut, {counts['location']} location-filtered"
    )
    if pf_stats and (pf_stats["batch_errors"] or pf_stats["unechoed_ids"]):
        line += (
            f" — prefilter WARN: {pf_stats['batch_errors']} failed batches, "
            f"{pf_stats['unechoed_ids']} unechoed ids (kept)"
        )
    print(line, flush=True)


def _rank_board(
    board: Board,
    client: _CachingClient,
    ladders: list[Ladder],
    args: argparse.Namespace,
    out_path: Path,
) -> None:
    resume_text = args.resume.read_text(encoding="utf-8")
    board_index: dict[str, Posting] | None = None
    for ladder in ladders:
        role_key = "_".join(ladder.roles)
        rank_path = out_path.with_name(f"{board.name}-rank-{role_key}.jsonl")
        if rank_path.exists() and not args.force:
            continue
        rows = select_rows(out_path, ladder)
        if len(rows) < 2:  # 0 or 1 candidates: summary handles without a file
            continue
        if board_index is None:
            board_index = client.index()
        joined, dropped = join_content(rows, board_index)
        if dropped:
            print(
                f"[{board.label}] {role_key}: {len(dropped)} rows missing from "
                "board re-fetch (posting vanished?)",
                flush=True,
            )
        cands = dedupe(joined, None)
        if len(cands) < 2:
            continue
        schedule = "swiss" if len(cands) > SWISS_THRESHOLD else "round-robin"
        print(
            f"[{board.label}] ranking {role_key}: {len(cands)} clusters "
            f"({schedule})",
            flush=True,
        )
        ranked = asyncio.run(
            run_ladder(
                cands,
                resume_text,
                ladder.label,
                args.judge_model,
                schedule,
                None,
                args.order_swap,
                args.concurrency,
            )
        )
        with JsonlWriter(rank_path) as w:
            for row in ranked:
                w.write(row)
        top = ranked[0]
        print(
            f"[{board.label}] {role_key} → {rank_path.name} "
            f"(#1: {top['title']})",
            flush=True,
        )


def process_board(
    board: Board, factory, ladders: list[Ladder], args: argparse.Namespace
) -> None:
    try:
        scan_cfg: Scan = factory.make_scan(board.kind, board.slug)
        client = _CachingClient(
            make_client(scan_cfg.source, scan_cfg.location_filter)
        )
        out_path = args.out_dir / f"{board.name}.jsonl"
        if out_path.exists() and not args.force:
            print(f"[{board.label}] scan exists, skipping", flush=True)
        else:
            print(
                f"[{board.label}] scanning {board.kind}/{board.slug} "
                f"(~{board.n_located} located)",
                flush=True,
            )
            asyncio.run(_scan_board(board, scan_cfg, client, args, out_path))
        if not args.skip_rank and out_path.exists():
            _rank_board(board, client, ladders, args, out_path)
    except Exception as e:  # noqa: BLE001 — per-board isolation, reported
        print(f"[{board.label}] WARN: {type(e).__name__}: {e}", flush=True)


# --------------------------------------------------------------------------- #
# Summary — rebuilt from disk artifacts every run
# --------------------------------------------------------------------------- #
def _read_results(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _top3(out_path: Path, rank_path: Path, ladder: Ladder) -> list[str]:
    """Top-3 'title | url' cells: from the ranked ladder when a tournament
    ran, else tier-ordered from the scan rows (covers 0/1-candidate boards
    and --skip-rank runs)."""
    if rank_path.exists():
        ranked = _read_results(rank_path)[:3]
        return [f"{r['title']} | {r['url']}" for r in ranked]
    rows = select_rows(out_path, ladder)
    rows.sort(
        key=lambda r: (
            _TIER_ORDER.get(r["result"]["comparison"]["fit_tier"], 9),
            r["posting"]["title"],
        )
    )
    return [
        f"{r['posting']['title']} | {r['posting'].get('url', '')}"
        for r in rows[:3]
    ]


def write_summary(
    companies_csv: Path,
    boards: list[Board],
    ladders: list[Ladder],
    out_dir: Path,
) -> Path:
    by_key = {(b.kind, b.slug): b for b in boards}
    tiers = ("strong", "stretch", "long_shot", "blocked")
    fields = [
        "company",
        "n_connections",
        "board_kind",
        "board_slug",
        "n_located",
        "n_scanned",
        "n_precut",
        "n_prefiltered",
    ]
    role_keys = ["_".join(l.roles) for l in ladders]
    for rk in role_keys:
        fields += [f"{rk}_{t}" for t in tiers]
        fields += [f"{rk}_top{i}" for i in (1, 2, 3)]

    out_rows = []
    with open(companies_csv, newline="") as f:
        for r in csv.DictReader(f):
            board = by_key.get((r.get("board_kind", ""), r.get("board_slug", "")))
            if board is None:
                continue
            out_path = out_dir / f"{board.name}.jsonl"
            row = {
                "company": r["company"],
                "n_connections": r["n_connections"],
                "board_kind": board.kind,
                "board_slug": board.slug,
                "n_located": r.get("n_postings_located", ""),
            }
            if out_path.exists():
                results = [x for x in _read_results(out_path) if "result" in x]
                dropped_path = out_dir / f"{board.name}-dropped.jsonl"
                drops = (
                    Counter(
                        x.get("_filter_stage", "")
                        for x in _read_results(dropped_path)
                    )
                    if dropped_path.exists()
                    else Counter()
                )
                row["n_scanned"] = len(results)
                row["n_precut"] = drops["title_precut"]
                row["n_prefiltered"] = drops["prefilter"]
                for ladder, rk in zip(ladders, role_keys):
                    counted = Counter(
                        x["result"]["comparison"]["fit_tier"]
                        for x in results
                        if x["result"]["extraction"].get("role") in ladder.roles
                        and "comparison" in x["result"]
                    )
                    for t in tiers:
                        row[f"{rk}_{t}"] = counted[t]
                    rank_path = out_dir / f"{board.name}-rank-{rk}.jsonl"
                    top = _top3(out_path, rank_path, ladder)
                    for i in (1, 2, 3):
                        row[f"{rk}_top{i}"] = top[i - 1] if len(top) >= i else ""
            out_rows.append(row)

    path = out_dir / "summary.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)
    return path


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
def _est_cost(model: str, in_tok: float, out_tok: float) -> float:
    pi, po = _PRICES.get(model, (5.0, 25.0))
    return (in_tok * pi + out_tok * po) / 1e6


def dry_run(boards: list[Board], factory, args: argparse.Namespace) -> None:
    KEEP_RATE = 0.15  # assumed prefilter survival — stated, not measured
    total_located = sum(b.n_located for b in boards)
    triage_calls = triage_cost = extract_cost = 0.0
    for b in boards:
        cfg: Scan = factory.make_scan(b.kind, b.slug)
        model = args.model or cfg.model
        if cfg.prefilter is not None:
            batches = math.ceil(b.n_located / cfg.prefilter.batch_size)
            triage_calls += batches
            # ~1.2K in (criterion + ~40 title lines) / ~800 out per batch
            triage_cost += _est_cost(cfg.prefilter.model, batches * 1200, batches * 800)
            survivors = b.n_located * KEEP_RATE
        else:
            survivors = b.n_located
        # ~2.5K uncached input (JD) + ~600 output per extraction call
        extract_cost += _est_cost(model, survivors * 2500, survivors * 600)
    print(f"{len(boards)} boards, {total_located} located postings")
    print(
        f"triage: ~{int(triage_calls)} batch calls, ~${triage_cost:.2f}"
    )
    print(
        f"extraction: ~${extract_cost:.2f} "
        f"(assumes {KEEP_RATE:.0%} prefilter survival; cache reads not modeled)"
    )
    print("ranking: not estimated (depends on fit-tier distribution)")
    for b in sorted(boards, key=lambda b: -b.n_located):
        print(f"  {b.n_located:>6}  {b.label}  ({b.kind}/{b.slug})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", type=Path, required=True)
    ap.add_argument(
        "--boards",
        default="scans.boards",
        help="factory module (make_scan + ladders), imported from cwd",
    )
    ap.add_argument("--resume", type=Path, help="rendered resume markdown")
    ap.add_argument("--out-dir", type=Path, default=Path("_output/referral-scans"))
    ap.add_argument("--only", help="substring filter on board slug or label")
    ap.add_argument("--force", action="store_true", help="redo existing outputs")
    ap.add_argument("--board-concurrency", type=int, default=4)
    ap.add_argument(
        "--concurrency", type=int, default=8, help="LLM calls per board"
    )
    ap.add_argument("--model", help="override the factory's extraction model")
    ap.add_argument("--judge-model", default="claude-opus-4-8")
    ap.add_argument("--limit", type=int, help="postings per board (smoke tests)")
    ap.add_argument("--skip-rank", action="store_true")
    ap.add_argument(
        "--order-swap", action=argparse.BooleanOptionalAction, default=True
    )
    ap.add_argument("--dry-run", action="store_true", help="counts + cost, no spend")
    args = ap.parse_args()

    sys.path.insert(0, str(Path.cwd()))
    factory = importlib.import_module(args.boards)
    ladders: list[Ladder] = factory.ladders()

    boards = load_boards(args.companies)
    if args.only:
        needle = args.only.lower()
        boards = [
            b
            for b in boards
            if needle in b.slug.lower() or needle in b.label.lower()
        ]
    if not boards:
        raise SystemExit("no matching scannable boards")

    if args.dry_run:
        dry_run(boards, factory, args)
        return
    if not args.resume:
        raise SystemExit("--resume is required for a live run")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.board_concurrency) as ex:
        list(ex.map(lambda b: process_board(b, factory, ladders, args), boards))

    path = write_summary(args.companies, boards, ladders, args.out_dir)
    print(f"\n→ {path}", flush=True)


if __name__ == "__main__":
    main()
