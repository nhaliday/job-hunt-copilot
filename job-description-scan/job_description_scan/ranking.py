"""Pairwise ranking pass: LLM-as-judge tournament + Bradley-Terry.

Generic engine. All case-specific selection (which roles/tiers compete, which
titles to exclude, role framing) lives in a scan module's `ranking = RankConfig`
(see examples/example_scan.py), never here.

Second pass after a scan: pointwise `fit_tier` triages but orders poorly within
a tier. This ranks the strong+stretch pool of one role family by having a judge
model compare postings head-to-head, then fits Bradley-Terry (choix) to the
pairwise outcomes. Run once per role family — families are not comparable.
"""

import argparse
import asyncio
import collections
import importlib
import itertools
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
import choix
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from job_description_scan.boards import Posting, make_client
from job_description_scan.config import Ladder, RankConfig, Scan
from job_description_scan.output import JsonlWriter
from job_description_scan.pipeline import cached_system, lead_then_fanout

_TIER_ORDER = {"strong": 0, "stretch": 1, "long_shot": 2, "blocked": 3}


class Verdict(BaseModel):
    reasoning: str = Field(
        description=(
            "One or two sentences naming the single dominant factor that makes "
            "the winner the better fit (YoE, required-qual match, domain/level, "
            "location, growth). Reason before choosing."
        )
    )
    winner: Literal["A", "B"] = Field(
        description="Which posting is the better overall fit for the candidate."
    )


@dataclass
class Candidate:
    """A cluster of near-duplicate postings that competes as one entry."""

    id: str  # canonical posting id
    title: str
    tier: str
    level: str
    url: str
    content: str  # canonical JD body, fed to the judge
    locations: list[str] = field(default_factory=list)
    posting_ids: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. Candidate selection + join
# --------------------------------------------------------------------------- #
def select_rows(results_path: Path, ladder: Ladder) -> list[dict]:
    rows = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "error" in r or "result" not in r:
            continue
        res = r["result"]
        if "comparison" not in res:
            continue
        if res["extraction"]["role"] not in ladder.roles:
            continue
        if res["comparison"]["fit_tier"] not in ladder.tiers:
            continue
        title = r["posting"]["title"]
        if ladder.exclude_title is not None and ladder.exclude_title.search(title):
            continue
        rows.append(r)
    return rows


def join_content(
    rows: list[dict], board: dict[str, Posting]
) -> tuple[list[dict], list[str]]:
    """Attach JD bodies from the re-fetched board; the scan JSONL has none.
    Returns (joined, dropped_ids) — ids missing from the current board."""
    joined, dropped = [], []
    for r in rows:
        pid = r["posting"]["id"]
        p = board.get(pid)
        if p is None or not p.content_text.strip():
            dropped.append(pid)
            continue
        joined.append({**r, "_content": p.content_text})
    return joined, dropped


# --------------------------------------------------------------------------- #
# 2. Content dedup (rapidfuzz, affix-aware)
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _strip_common_affixes(cores: list[str]) -> list[str]:
    """Remove the prefix and suffix shared by ALL texts before comparing.

    Boards reuse a company blurb (opening) and benefits/EEO tail (closing)
    across every posting; leaving them in inflates similarity and over-merges
    distinct roles. Stripping the common affixes isolates the role-specific
    middle — generic, no board-specific strings.
    """
    if len(cores) < 2:
        return cores
    lo, hi = min(cores), max(cores)
    pre = 0
    while pre < len(lo) and lo[pre] == hi[pre]:
        pre += 1
    lo_r, hi_r = lo[::-1], hi[::-1]
    suf = 0
    while suf < len(lo_r) and suf < len(lo) - pre and lo_r[suf] == hi_r[suf]:
        suf += 1
    return [c[pre : len(c) - suf] for c in cores]


def dedupe(joined: list[dict], threshold: float | None) -> list[Candidate]:
    """Cluster near-duplicate postings; `threshold` None means exact-only
    (string-identical cores), a float opts into fuzzy merging."""
    cores = _strip_common_affixes([_normalize(r["_content"]) for r in joined])
    clusters: list[list[int]] = []
    reps: list[str] = []
    rep_idx: list[int] = []
    for i, core in enumerate(cores):
        placed = False
        for c, rep in enumerate(reps):
            if core == rep:
                pass
            elif (
                threshold is not None
                and (score := fuzz.token_set_ratio(core, rep)) >= threshold
            ):
                # Non-identical texts merged on fuzzy similarity — surface it
                # so bad merges are visible in --dry-run. Note a score of 100
                # does NOT mean identical: token_set_ratio ignores word
                # order/multiplicity and scores near-subsets 100.
                pi, pr = joined[i]["posting"], joined[rep_idx[c]]["posting"]
                print(
                    f"  merge: {pi['id']} {pi['title']!r} -> "
                    f"{pr['id']} {pr['title']!r} "
                    f"(token_set_ratio={score:.0f}, non-identical text)"
                )
            else:
                continue
            clusters[c].append(i)
            placed = True
            break
        if not placed:
            clusters.append([i])
            reps.append(core)
            rep_idx.append(i)

    out: list[Candidate] = []
    for members in clusters:
        recs = [joined[i] for i in members]
        canon = min(recs, key=lambda r: r["posting"]["id"])
        best_tier = min(
            (r["result"]["comparison"]["fit_tier"] for r in recs),
            key=lambda t: _TIER_ORDER.get(t, 9),
        )
        cp = canon["posting"]
        out.append(
            Candidate(
                id=cp["id"],
                title=cp["title"],
                tier=best_tier,
                level=canon["result"]["extraction"].get("level", "unknown"),
                url=cp.get("url", ""),
                content=canon["_content"],
                locations=sorted({r["posting"].get("location", "") for r in recs}),
                posting_ids=[r["posting"]["id"] for r in recs],
                titles=sorted({r["posting"]["title"] for r in recs}),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 3. Judge
# --------------------------------------------------------------------------- #
def _system_prefix(resume_text: str, label: str) -> list[dict]:
    framing = f" for a {label} role" if label else ""
    instructions = (
        "You compare two job postings as career fit for one candidate, whose "
        "resume is below. Given posting A and posting B" + framing + ", decide "
        "which is the better OVERALL fit for THIS candidate — weigh required "
        "qualifications, years-of-experience gap, domain/vertical match, level, "
        "location and relocation, and growth trajectory. Reason first, naming "
        "the dominant differentiator, then pick the winner. If they are "
        "genuinely close, still choose the marginally better fit."
    )
    return cached_system([instructions, f"## Candidate resume\n\n{resume_text}"])


def _user_content(a: Candidate, b: Candidate) -> str:
    def block(tag: str, c: Candidate) -> str:
        # All member locations of the cluster (pipe-joined, matching the board
        # clients' list convention), so the judge weighs the role's true
        # geographic options rather than the canonical member's city.
        locs = " | ".join(loc for loc in c.locations if loc)
        return f"## Posting {tag}\nTitle: {c.title}\nLocation: {locs}\n\n{c.content}"

    return block("A", a) + "\n\n" + block("B", b)


async def _judge_call(
    anth: anthropic.AsyncAnthropic,
    model: str,
    system_blocks: list[dict],
    cands: list[Candidate],
    a_idx: int,
    b_idx: int,
) -> dict:
    """One directed comparison: candidate a_idx as 'A', b_idx as 'B'.
    Returns {a, b, winner} where winner is a global index or None on failure."""
    try:
        resp = await anth.messages.parse(
            model=model,
            max_tokens=12000,
            system=system_blocks,
            messages=[
                {"role": "user", "content": _user_content(cands[a_idx], cands[b_idx])}
            ],
            output_format=Verdict,
        )
        winner = a_idx if resp.parsed_output.winner == "A" else b_idx
        return {"a": a_idx, "b": b_idx, "winner": winner}
    except Exception as e:
        return {"a": a_idx, "b": b_idx, "winner": None, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 4. Schedules — produce directed comparisons (a_idx as A, b_idx as B)
# --------------------------------------------------------------------------- #
def _directed(matchups: list[tuple[int, int]], order_swap: bool, rng: random.Random):
    """Expand unordered matchups into directed comparisons. With swap, both
    orientations; without, one randomized orientation (avoids systematic A-bias)."""
    out = []
    for i, j in matchups:
        if order_swap:
            out.append((i, j))
            out.append((j, i))
        else:
            out.append((i, j) if rng.random() < 0.5 else (j, i))
    return out


async def _run_comparisons(
    anth, model, system_blocks, cands, directed, concurrency
) -> list[dict]:
    async def call(pair: tuple[int, int]) -> dict:
        return await _judge_call(anth, model, system_blocks, cands, pair[0], pair[1])

    return [row async for row in lead_then_fanout(directed, call, concurrency)]


def _swiss_rounds(n: int, override: int | None) -> int:
    return override if override else math.ceil(math.log2(n)) + 2 if n > 1 else 1


# --------------------------------------------------------------------------- #
# 5 + 6. Aggregate → Bradley-Terry → ranked output
# --------------------------------------------------------------------------- #
def _resolve(results: list[dict]) -> tuple[list[tuple[int, int]], dict]:
    """Group directed comparisons by unordered matchup; build BT win edges and
    per-candidate win/loss/tie tallies. A split (order-swap disagreement) is a
    tie: one edge each direction. A single-orientation result is one edge."""
    by_pair: dict[frozenset, list[int | None]] = {}
    for r in results:
        by_pair.setdefault(frozenset((r["a"], r["b"])), []).append(r["winner"])

    edges: list[tuple[int, int]] = []
    tally: dict = {}

    for pair, winners in by_pair.items():
        i, j = tuple(pair)
        wins = [w for w in winners if w is not None]
        if not wins:
            continue
        distinct = set(wins)
        if len(distinct) == 1:  # agreement (or single orientation)
            w = wins[0]
            loser = j if w == i else i
            edges.append((w, loser))
            tally.setdefault(w, {"wins": 0.0, "losses": 0.0, "ties": 0.0})["wins"] += 1
            tally.setdefault(loser, {"wins": 0.0, "losses": 0.0, "ties": 0.0})["losses"] += 1
        else:  # order-swap disagreement → tie
            edges.append((i, j))
            edges.append((j, i))
            for k in (i, j):
                tally.setdefault(k, {"wins": 0.0, "losses": 0.0, "ties": 0.0})["ties"] += 1
    return edges, tally


def rank(cands: list[Candidate], results: list[dict]) -> list[dict]:
    edges, tally = _resolve(results)
    n = len(cands)
    utilities = choix.ilsr_pairwise(n, edges, alpha=0.01) if edges else [0.0] * n
    order = sorted(range(n), key=lambda i: -utilities[i])
    out = []
    for pos, i in enumerate(order, 1):
        c = cands[i]
        t = tally.get(i, {"wins": 0.0, "losses": 0.0, "ties": 0.0})
        out.append(
            {
                "rank": pos,
                "utility": round(float(utilities[i]), 4),
                "wins": t["wins"],
                "losses": t["losses"],
                "ties": t["ties"],
                "comparisons": int(t["wins"] + t["losses"] + t["ties"]),
                "title": c.title,
                "tier": c.tier,
                "level": c.level,
                "locations": c.locations,
                "posting_ids": c.posting_ids,
                "url": c.url,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run_ladder(
    cands: list[Candidate],
    resume_text: str,
    label: str,
    model: str,
    schedule: str,
    rounds: int | None,
    order_swap: bool,
    concurrency: int,
    seed: int = 0,
) -> list[dict]:
    n = len(cands)
    rng = random.Random(seed)
    system_blocks = _system_prefix(resume_text, label)
    anth = anthropic.AsyncAnthropic(max_retries=8)
    results: list[dict] = []

    # Close the client before the caller's event loop does — otherwise its
    # pooled connections get finalized after asyncio.run() tears the loop
    # down and httpx raises "Event loop is closed" noise from __del__.
    try:
        if schedule == "round-robin":
            directed = _directed(
                list(itertools.combinations(range(n), 2)), order_swap, rng
            )
            results = await _run_comparisons(
                anth, model, system_blocks, cands, directed, concurrency
            )
        else:  # swiss
            played: set[frozenset] = set()
            score = [0.0] * n
            for _ in range(_swiss_rounds(n, rounds)):
                order = sorted(range(n), key=lambda i: (-score[i], rng.random()))
                matchups, used = [], set()
                for i in order:
                    if i in used:
                        continue
                    for j in order:
                        if j == i or j in used or frozenset((i, j)) in played:
                            continue
                        matchups.append((i, j))
                        used.update((i, j))
                        played.add(frozenset((i, j)))
                        break
                if not matchups:
                    break
                directed = _directed(matchups, order_swap, rng)
                round_results = await _run_comparisons(
                    anth, model, system_blocks, cands, directed, concurrency
                )
                results.extend(round_results)
                for r in round_results:  # update standings for next pairing
                    if r["winner"] is not None:
                        score[r["winner"]] += 1.0
    finally:
        await anth.close()

    _report_errors(results)
    return rank(cands, results)


def _report_errors(results: list[dict]) -> None:
    """Failed judge calls silently drop their edge; make that loud. Every
    failure carries an error string — summarize by type so a systemic cause
    (rate limit, refusal, truncation) is obvious, not just a thin tally."""
    errors = [r["error"] for r in results if r.get("error")]
    if not errors:
        return
    print(
        f"  WARNING: {len(errors)}/{len(results)} judge calls failed "
        "(no edge recorded):",
        flush=True,
    )
    for msg, count in collections.Counter(e[:160] for e in errors).most_common(5):
        print(f"    {count}x {msg}", flush=True)


def _load_ladders(scan_module: str, ladder_arg: str) -> tuple[Scan, list[Ladder]]:
    sys.path.insert(0, str(Path.cwd()))
    mod = importlib.import_module(scan_module)
    scan: Scan = mod.scan
    cfg: RankConfig | None = getattr(mod, "ranking", None)
    if cfg is None:
        raise SystemExit(f"{scan_module} defines no `ranking = RankConfig(...)`")
    if ladder_arg == "all":
        return scan, cfg.ladders
    chosen = [l for l in cfg.ladders if ladder_arg in l.roles]
    if not chosen:
        avail = ", ".join(sorted({r for l in cfg.ladders for r in l.roles}))
        raise SystemExit(f"no ladder for role {ladder_arg!r}; available: {avail}")
    return scan, chosen


def main() -> None:
    ap = argparse.ArgumentParser(prog="job-description-scan.ranking")
    ap.add_argument("--scan", required=True, help="scan module, e.g. scans.acme")
    ap.add_argument("--results", type=Path, required=True, help="scan JSONL output")
    ap.add_argument("--resume", type=Path, help="resume markdown (required unless --dry-run)")
    ap.add_argument("--ladder", required=True, help="role family (e.g. swe) or 'all'")
    ap.add_argument("--schedule", choices=["round-robin", "swiss"], default="round-robin")
    ap.add_argument("--rounds", type=int, help="swiss rounds (default ceil(log2 n)+2)")
    ap.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help="opt into fuzzy merging: token_set_ratio needed to merge "
        "non-identical cores (e.g. 90). Default: only string-identical "
        "cores merge.",
    )
    ap.add_argument("--judge-model", default="claude-fable-5")
    ap.add_argument("--order-swap", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="print counts, no API spend")
    ap.add_argument("--out", type=Path, help="output JSONL (default _output/<scan>-rank-<role>.jsonl)")
    args = ap.parse_args()

    scan, ladders = _load_ladders(args.scan, args.ladder)
    board = {
        p.id: p
        for p in make_client(scan.source, scan.location_filter).iter_postings()
    }
    scan_tail = args.scan.rsplit(".", 1)[-1]
    resume_text = args.resume.read_text(encoding="utf-8") if args.resume else ""

    for ladder in ladders:
        role_key = "_".join(ladder.roles)
        rows = select_rows(args.results, ladder)
        joined, dropped = join_content(rows, board)
        cands = dedupe(joined, args.dedup_threshold)
        n = len(cands)
        pairs = n * (n - 1) // 2 if args.schedule == "round-robin" else (
            _swiss_rounds(n, args.rounds) * (n // 2)
        )
        calls = pairs * (2 if args.order_swap else 1)
        print(
            f"[{role_key}] {len(rows)} rows -> {len(joined)} joined "
            f"({len(dropped)} dropped) -> {n} clusters -> ~{pairs} pairings "
            f"-> ~{calls} judge calls ({args.schedule})",
            flush=True,
        )
        if args.dry_run:
            continue
        if not args.resume:
            raise SystemExit("--resume is required for a live ranking run")
        if n < 2:
            print(f"[{role_key}] fewer than 2 candidates; nothing to rank", flush=True)
            continue

        ranked = asyncio.run(
            run_ladder(
                cands, resume_text, ladder.label, args.judge_model,
                args.schedule, args.rounds, args.order_swap, args.concurrency,
            )
        )
        out_path = args.out or Path("_output") / f"{scan_tail}-rank-{role_key}.jsonl"
        with JsonlWriter(out_path) as w:
            for row in ranked:
                w.write(row)
        print(f"\n[{role_key}] → {out_path}")
        for row in ranked:
            print(
                f"  #{row['rank']:<2} u={row['utility']:+.2f} "
                f"W{int(row['wins'])}/L{int(row['losses'])}/T{int(row['ties'])}  "
                f"[{row['tier']}/{row['level']}] {row['title']}  "
                f"@ {', '.join(row['locations'])}"
            )


if __name__ == "__main__":
    main()
