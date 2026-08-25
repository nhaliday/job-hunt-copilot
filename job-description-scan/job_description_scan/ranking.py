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
from typing import Awaitable, Callable, Literal

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


# The tournament's only contract with a judge: given two candidates in a fixed
# presentation order, which wins? The LLM judge is one implementation
# (_llm_judge); tests inject a seeded rng judge, and the roadmap's human-judge
# ranking will inject a terminal prompt. Orientation (order-swap) is expressed
# by argument order, so judges stay oblivious to it.
Judge = Callable[["Candidate", "Candidate"], Awaitable[Literal["A", "B"]]]


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


def _llm_judge(
    anth: anthropic.AsyncAnthropic,
    model: str,
    system_blocks: list[dict],
) -> Judge:
    """The default Judge. All LLM specifics live here; it knows nothing about
    indices, schedules, or error rows."""

    async def judge(a: Candidate, b: Candidate) -> Literal["A", "B"]:
        resp = await anth.messages.parse(
            model=model,
            max_tokens=12000,
            system=system_blocks,
            messages=[{"role": "user", "content": _user_content(a, b)}],
            output_format=Verdict,
        )
        return resp.parsed_output.winner

    return judge


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
    judge: Judge, cands: list[Candidate], directed, concurrency
) -> list[dict]:
    """Tournament semantics around the judge: A/B → global index, and any
    judge failure → {winner: None, error} (the row _resolve skips and
    _report_errors surfaces)."""

    async def call(pair: tuple[int, int]) -> dict:
        i, j = pair
        try:
            winner = i if await judge(cands[i], cands[j]) == "A" else j
            return {"a": i, "b": j, "winner": winner}
        except Exception as e:
            return {"a": i, "b": j, "winner": None, "error": f"{type(e).__name__}: {e}"}

    return [row async for row in lead_then_fanout(directed, call, concurrency)]


def _swiss_rounds(n: int, override: int | None) -> int:
    return override if override else math.ceil(math.log2(n)) + 2 if n > 1 else 1


def swiss_pairings(
    n: int,
    score: list[float],
    played: set[frozenset],
    rng: random.Random,
) -> list[tuple[int, int]]:
    """One Swiss round: pair adjacent standings (random tiebreak), skipping
    pairs already played. Mutates `played` with the pairings it emits. Shared
    by the LLM tournament (run_ladder) and the human-judge driver."""
    order = sorted(range(n), key=lambda i: (-score[i], rng.random()))
    matchups: list[tuple[int, int]] = []
    used: set[int] = set()
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
    return matchups


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
            tally.setdefault(loser, {"wins": 0.0, "losses": 0.0, "ties": 0.0})[
                "losses"
            ] += 1
        else:  # order-swap disagreement → tie
            edges.append((i, j))
            edges.append((j, i))
            for k in (i, j):
                tally.setdefault(k, {"wins": 0.0, "losses": 0.0, "ties": 0.0})[
                    "ties"
                ] += 1
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
# 7. Top-k stability (anytime progress signal for the tournament)
# --------------------------------------------------------------------------- #
# Comparison selection stays k-free (swiss pairs adjacent standings, which
# works every rank boundary at once); k enters only here, as measurement.
STABLE_AT = 0.95


def topk_stability(
    n: int,
    units: list[list[dict]],
    ks: list[int],
    *,
    n_boot: int = 200,
    seed: int = 0,
    z_target: float = 1.96,
) -> list[dict]:
    """Bootstrap stability of the top-k prefixes of the BT ranking.

    `units` are independent judgment units, each a list of _resolve-shaped
    rows ({"a", "b", "winner"}) — one LLM judge call, or one human keypress
    (a tie is one unit of two rows). Resampling units with replacement and
    refitting measures how often the top-k SET survives (set, not order:
    "which items make the cut" is the actionable question; within-prefix
    order keeps refining as comparisons accrue).

    Per k returns {"k", "stability" (fraction of refits whose top-k set
    matches the point estimate's), "est_more" (rough additional judgments
    until stable: 1/sqrt(m) extrapolation of the k-boundary utility gap's
    z-score to z_target; 0 = already stable, None = no signal yet)}. ks are
    deduped and clamped to 0 < k < n (the full set is trivially stable).

    Interpretation caveat: standings-adjacent items are separated only by
    their direct game(s) — shared-opponent records carry no signal — so a
    boundary a no-repeat schedule judged exactly once plateaus below
    STABLE_AT no matter how many further rounds run (order-swap's two calls
    lift the ceiling to ~0.9). A plateau with growing est_more means "this
    schedule cannot certify k further", not "keep going".
    """
    ks = sorted({k for k in ks if 0 < k < n})
    rows = [r for u in units for r in u if r.get("winner") is not None]
    edges, _ = _resolve(rows)
    if not ks or not edges:
        return [
            {"k": k, "stability": 0.0, "est_more": None, "stable": False} for k in ks
        ]
    utils = choix.ilsr_pairwise(n, edges, alpha=0.01)
    order = sorted(range(n), key=lambda i: -utils[i])
    topsets = {k: frozenset(order[:k]) for k in ks}
    boundary = {k: (order[k - 1], order[k]) for k in ks}

    rng = random.Random(seed)
    matches = {k: 0 for k in ks}
    gaps: dict[int, list[float]] = {k: [] for k in ks}
    m = len(units)
    for _ in range(n_boot):
        sample = [
            r
            for _ in range(m)
            for r in units[rng.randrange(m)]
            if r.get("winner") is not None
        ]
        b_edges, _ = _resolve(sample)
        if not b_edges:
            continue  # counts as a mismatch for every k
        try:
            b_utils = choix.ilsr_pairwise(n, b_edges, alpha=0.01)
        except Exception:
            continue  # degenerate resample: mismatch
        b_order = sorted(range(n), key=lambda i: -b_utils[i])
        for k in ks:
            if frozenset(b_order[:k]) == topsets[k]:
                matches[k] += 1
            i, j = boundary[k]
            gaps[k].append(float(b_utils[i]) - float(b_utils[j]))

    out = []
    for k in ks:
        stability = matches[k] / n_boot
        i, j = boundary[k]
        gap = float(utils[i]) - float(utils[j])
        gs = gaps[k]
        se = 0.0
        if len(gs) >= 2:
            mean = sum(gs) / len(gs)
            se = (sum((g - mean) ** 2 for g in gs) / (len(gs) - 1)) ** 0.5
        est_more: int | None
        if stability >= STABLE_AT:
            est_more = 0
        elif se <= 0 or gap <= 0 or gap / se < 0.5:
            est_more = None  # boundary too unresolved to extrapolate from
        else:
            # se shrinks ~1/sqrt(m); solve m' where gap/se' = z_target. The
            # boundary pair is the dominant but not only instability source,
            # so floor at 1 while the set still flips.
            est_more = max(1, math.ceil(m * ((z_target * se / gap) ** 2 - 1)))
        out.append(
            {
                "k": k,
                "stability": stability,
                "est_more": est_more,
                "stable": stability >= STABLE_AT,
            }
        )
    return out


def closure_topk(n: int, units: list[list[dict]], ks: list[int]) -> list[dict]:
    """Top-k certification for a NON-stochastic judge (a human): judgments
    are treated as ground truth, so the question is coverage, not sampling
    error. The top-k set (by the BT fit) is certified when every member
    reaches every outsider through a directed path of actual judgments
    (winner->loser edges; a tie is an edge each way — "no worse than", which
    covers a boundary in both directions).

    Same row shape as topk_stability: "stability" is the covered fraction of
    the k*(n-k) crossing pairs, "stable" means fully covered, "est_more" the
    number of unjudged standings-adjacent pairs spanning some uncovered
    crossing (judging them completes the chains) — a lower bound that
    reprices as answers land. Bootstrap resampling is wrong for this judge:
    a no-repeat schedule leaves every adjacent boundary on a single
    judgment, which caps bootstrap stability near coin-flip territory no
    matter how many rounds run.
    """
    ks = sorted({k for k in ks if 0 < k < n})
    rows = [r for u in units for r in u if r.get("winner") is not None]
    edges, _ = _resolve(rows)
    if not ks or not edges:
        return [
            {"k": k, "stability": 0.0, "est_more": None, "stable": False} for k in ks
        ]
    utils = choix.ilsr_pairwise(n, edges, alpha=0.01)
    order = sorted(range(n), key=lambda i: -utils[i])
    pos = {item: p for p, item in enumerate(order)}

    adj: dict[int, set[int]] = {}
    for w, loser in edges:
        adj.setdefault(w, set()).add(loser)
    reach: dict[int, set[int]] = {}
    for start in range(n):
        seen = {start}
        frontier = [start]
        while frontier:
            nxt = [t for f in frontier for t in adj.get(f, ()) if t not in seen]
            seen.update(nxt)
            frontier = nxt
        reach[start] = seen - {start}
    judged = {frozenset((r["a"], r["b"])) for r in rows}

    out = []
    for k in ks:
        uncovered = [(i, j) for i in order[:k] for j in order[k:] if j not in reach[i]]
        crossing = k * (n - k)
        needed: set[int] = set()
        stubborn = 0
        for i, j in uncovered:
            slots = [
                r
                for r in range(pos[i], pos[j])
                if frozenset((order[r], order[r + 1])) not in judged
            ]
            if slots:
                needed.update(slots)
            else:  # chain judged but contradicts the fit — only a direct
                stubborn += 1  # (i, j) comparison can settle it
        covered = 1.0 - len(uncovered) / crossing
        out.append(
            {
                "k": k,
                "stability": covered,
                "est_more": (len(needed) + stubborn) if uncovered else 0,
                "stable": not uncovered,
            }
        )
    return out


def format_stability(stats: list[dict]) -> str:
    """One-line summary: 'top-k stable: k5=1.00✓ k10=0.82(~+18) k20=0.41(?)'."""
    parts = []
    for s in stats:
        if s["stable"]:
            tail = "✓"
        elif s["est_more"] is None:
            tail = "(?)"
        else:
            tail = f"(~+{s['est_more']})"
        parts.append(f"k{s['k']}={s['stability']:.2f}{tail}")
    return "top-k stable: " + " ".join(parts) if parts else ""


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
    *,
    judge: Judge | None = None,
    topk: list[int] | None = None,
    until_stable: bool = False,
) -> list[dict]:
    n = len(cands)
    rng = random.Random(seed)
    results: list[dict] = []

    def _stability_check(tag: str) -> bool:
        """Report top-k stability; True once every requested k is stable."""
        if not topk:
            return False
        units = [[r] for r in results if r["winner"] is not None]
        stats = topk_stability(n, units, topk)
        print(
            f"  {tag}: {len(units)} judgments · {format_stability(stats)}", flush=True
        )
        return bool(stats) and all(s["stable"] for s in stats)

    # Default judge is the LLM (resume_text/label/model are its inputs); an
    # injected judge makes those parameters inert and needs no client.
    anth = None
    if judge is None:
        anth = anthropic.AsyncAnthropic(max_retries=8)
        judge = _llm_judge(anth, model, _system_prefix(resume_text, label))

    # Close the client before the caller's event loop does — otherwise its
    # pooled connections get finalized after asyncio.run() tears the loop
    # down and httpx raises "Event loop is closed" noise from __del__.
    try:
        if schedule == "round-robin":
            directed = _directed(
                list(itertools.combinations(range(n), 2)), order_swap, rng
            )
            results = await _run_comparisons(judge, cands, directed, concurrency)
            _stability_check("final")
        else:  # swiss
            played: set[frozenset] = set()
            score = [0.0] * n
            total_rounds = _swiss_rounds(n, rounds)
            for rd in range(total_rounds):
                matchups = swiss_pairings(n, score, played, rng)
                if not matchups:
                    break
                directed = _directed(matchups, order_swap, rng)
                round_results = await _run_comparisons(
                    judge, cands, directed, concurrency
                )
                results.extend(round_results)
                for r in round_results:  # update standings for next pairing
                    if r["winner"] is not None:
                        score[r["winner"]] += 1.0
                if _stability_check(f"round {rd + 1}/{total_rounds}") and until_stable:
                    break
    finally:
        if anth is not None:
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
    ap.add_argument(
        "--resume", type=Path, help="resume markdown (required unless --dry-run)"
    )
    ap.add_argument("--ladder", required=True, help="role family (e.g. swe) or 'all'")
    ap.add_argument(
        "--schedule", choices=["round-robin", "swiss"], default="round-robin"
    )
    ap.add_argument("--rounds", type=int, help="swiss rounds (default ceil(log2 n)+2)")
    ap.add_argument(
        "--topk",
        help="comma-separated k values (e.g. 5,10,20): report bootstrap "
        "top-k stability per swiss round (round-robin: once at the end)",
    )
    ap.add_argument(
        "--until-stable",
        action="store_true",
        help="swiss: stop rounds early once every --topk prefix is stable "
        f"(bootstrap stability >= {STABLE_AT})",
    )
    ap.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help="opt into fuzzy merging: token_set_ratio needed to merge "
        "non-identical cores (e.g. 90). Default: only string-identical "
        "cores merge.",
    )
    ap.add_argument("--judge-model", default="claude-opus-5")
    ap.add_argument("--order-swap", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="print counts, no API spend")
    ap.add_argument(
        "--out",
        type=Path,
        help="output JSONL (default _output/<scan>-rank-<role>.jsonl)",
    )
    args = ap.parse_args()

    topk = [int(k) for k in args.topk.split(",")] if args.topk else None
    if args.until_stable and not topk:
        raise SystemExit("--until-stable needs --topk")
    if args.until_stable and args.schedule != "swiss":
        raise SystemExit("--until-stable only applies to --schedule swiss")

    scan, ladders = _load_ladders(args.scan, args.ladder)
    selected = [(ladder, select_rows(args.results, ladder)) for ladder in ladders]
    # Fetch only the pool's ids: targeted detail GETs on list-then-detail
    # boards (a full walk is one GET per posting — 10+ min on a ~2k board),
    # a filtered walk on one-shot boards. The empty-pool guard stays here
    # because a one-shot fetch_postings([]) would still pay the listing
    # request(s) before filtering everything out.
    needed = sorted({r["posting"]["id"] for _, rows in selected for r in rows})
    if not needed:
        board: dict[str, Posting] = {}
    else:
        client = make_client(scan.source, scan.location_filter)
        board = {p.id: p for p in client.fetch_postings(needed)}
        print(
            f"content join: fetched {len(board)}/{len(needed)} postings",
            flush=True,
        )
    scan_tail = args.scan.rsplit(".", 1)[-1]
    resume_text = args.resume.read_text(encoding="utf-8") if args.resume else ""

    for ladder, rows in selected:
        role_key = "_".join(ladder.roles)
        joined, dropped = join_content(rows, board)
        cands = dedupe(joined, args.dedup_threshold)
        n = len(cands)
        pairs = (
            n * (n - 1) // 2
            if args.schedule == "round-robin"
            else (_swiss_rounds(n, args.rounds) * (n // 2))
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
                cands,
                resume_text,
                ladder.label,
                args.judge_model,
                args.schedule,
                args.rounds,
                args.order_swap,
                args.concurrency,
                topk=topk,
                until_stable=args.until_stable,
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
