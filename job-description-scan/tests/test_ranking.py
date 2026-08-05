"""Hermetic tournament tests: run_ladder end-to-end with injected judges.

No network, no LLM spend. A seeded rng judge carries a planted ground truth
(lexicographically smaller Candidate.id wins), so the tests verify that the
schedules, order-swap handling, tie resolution, Bradley-Terry fit, and output
shape recover a known ordering. Everything is seeded (run_ladder's rng and the
judge's own), and aggregation is order-independent, so results are stable
across runs despite the concurrent fan-out.
"""

import asyncio
import random

from job_description_scan.ranking import Candidate, Judge, run_ladder

_ROW_KEYS = {
    "rank",
    "utility",
    "wins",
    "losses",
    "ties",
    "comparisons",
    "title",
    "tier",
    "level",
    "locations",
    "posting_ids",
    "url",
}


def _cands(n: int, shuffle_seed: int = 42) -> list[Candidate]:
    """Synthetic candidates with ids c00..c<n-1>, deliberately passed in
    scrambled order so a test can't pass by input order being preserved."""
    cands = [
        Candidate(
            id=f"c{i:02d}",
            title=f"Role {i:02d}",
            tier="strong" if i < n // 2 else "stretch",
            level="senior",
            url=f"https://example.invalid/c{i:02d}",
            content=f"Synthetic JD body for role {i:02d}.",
            locations=[f"City {i:02d}"],
            posting_ids=[f"c{i:02d}"],
            titles=[f"Role {i:02d}"],
        )
        for i in range(n)
    ]
    random.Random(shuffle_seed).shuffle(cands)
    return cands


def planted_judge(seed: int = 0, noise: float = 0.0) -> Judge:
    """Ground truth: smaller id wins; seeded rng flips with p=noise."""
    rng = random.Random(seed)

    async def judge(a: Candidate, b: Candidate):
        a_better = a.id < b.id
        if noise and rng.random() < noise:
            a_better = not a_better
        return "A" if a_better else "B"

    return judge


def _run(cands, judge, schedule="round-robin", rounds=None, order_swap=True):
    return asyncio.run(
        run_ladder(
            cands,
            resume_text="",
            label="",
            model="unused-with-injected-judge",
            schedule=schedule,
            rounds=rounds,
            order_swap=order_swap,
            concurrency=8,
            judge=judge,
        )
    )


def test_round_robin_recovers_planted_order():
    n = 10
    ranked = _run(_cands(n), planted_judge())
    ids = [r["posting_ids"][0] for r in ranked]
    assert ids == sorted(ids), "noise-free round-robin must recover the planted order"
    assert ranked[0]["wins"] == n - 1
    for row in ranked:
        assert set(row) == _ROW_KEYS
        assert row["comparisons"] == n - 1


def test_swiss_finds_planted_winner():
    n = 16
    ranked = _run(_cands(n), planted_judge(), schedule="swiss")
    assert ranked[0]["posting_ids"][0] == "c00"
    # Swiss doesn't guarantee a total order; require strong rank correlation.
    planted = {f"c{i:02d}": i for i in range(n)}
    d2 = sum((row["rank"] - 1 - planted[row["posting_ids"][0]]) ** 2 for row in ranked)
    spearman = 1 - 6 * d2 / (n * (n**2 - 1))
    assert spearman >= 0.8, f"spearman {spearman:.2f}"


def test_order_swap_disagreement_is_tie():
    n = 6

    async def position_biased(a: Candidate, b: Candidate):
        return "A"  # always prefers whichever is presented first

    ranked = _run(_cands(n), position_biased)
    for row in ranked:
        assert row["wins"] == 0 and row["losses"] == 0
        assert row["ties"] == n - 1


def test_judge_errors_are_non_fatal():
    n = 6
    bad = "c03"
    inner = planted_judge()

    async def flaky(a: Candidate, b: Candidate):
        if bad in (a.id, b.id):
            raise RuntimeError("boom")
        return await inner(a, b)

    ranked = _run(_cands(n), flaky)
    assert len(ranked) == n
    by_id = {r["posting_ids"][0]: r for r in ranked}
    assert by_id[bad]["comparisons"] == 0
    others = [r for r in ranked if r["posting_ids"][0] != bad]
    assert all(r["comparisons"] == n - 2 for r in others)
    ids = [r["posting_ids"][0] for r in others]
    assert ids == sorted(ids), "surviving comparisons must still order the rest"


def test_noisy_judge_still_ranks_planted_best_first():
    ranked = _run(_cands(10), planted_judge(seed=1, noise=0.15))
    assert ranked[0]["posting_ids"][0] == "c00"


def test_swiss_pairings_adjacent_and_no_repeats():
    from job_description_scan.ranking import swiss_pairings

    rng = random.Random(0)
    played: set[frozenset] = set()
    score = [3.0, 2.0, 1.0, 0.0]
    first = swiss_pairings(4, score, played, rng)
    assert sorted(sorted(p) for p in first) == [[0, 1], [2, 3]]  # adjacent standings
    second = swiss_pairings(4, score, played, rng)
    assert not (set(map(frozenset, first)) & set(map(frozenset, second)))
    # 4 items -> 3 distinct rounds possible, then exhaustion
    swiss_pairings(4, score, played, rng)
    assert swiss_pairings(4, score, played, rng) == []
