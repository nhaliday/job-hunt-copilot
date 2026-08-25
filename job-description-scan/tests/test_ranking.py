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


def _bt_units(n: int, utilities: list[float], m: int, seed: int = 0) -> list:
    """Synthetic judgment units sampled from a Bradley-Terry model over
    random pairs — planted ground truth for the stability monitor."""
    rng = random.Random(seed)
    units = []
    for _ in range(m):
        i, j = rng.sample(range(n), 2)
        p_i = 1 / (1 + pow(2.718281828, utilities[j] - utilities[i]))
        w = i if rng.random() < p_i else j
        units.append([{"a": i, "b": j, "winner": w}])
    return units


def test_topk_stability_separates_clear_top_from_mush():
    from job_description_scan.ranking import STABLE_AT, topk_stability

    # Items 0-2 tower over a near-tied tail: k=3 resolves fast, k=6 can't.
    utils = [8.0, 7.0, 6.0] + [0.05 * i for i in range(9)][::-1]
    stats = topk_stability(12, _bt_units(12, utils, 300), [3, 6])
    by_k = {s["k"]: s for s in stats}
    assert by_k[3]["stability"] >= STABLE_AT and by_k[3]["est_more"] == 0
    assert by_k[6]["stability"] < STABLE_AT
    assert by_k[6]["est_more"] is None or by_k[6]["est_more"] >= 1


def test_topk_stability_clamps_and_handles_empty():
    from job_description_scan.ranking import topk_stability

    units = _bt_units(4, [3.0, 2.0, 1.0, 0.0], 60)
    # k >= n and k <= 0 are dropped; duplicates collapse.
    assert [s["k"] for s in topk_stability(4, units, [0, 2, 2, 4, 9])] == [2]
    assert topk_stability(4, [], [2]) == [
        {"k": 2, "stability": 0.0, "est_more": None, "stable": False}
    ]


def test_topk_stability_is_deterministic():
    from job_description_scan.ranking import topk_stability

    units = _bt_units(8, [float(8 - i) for i in range(8)], 100)
    assert topk_stability(8, units, [3]) == topk_stability(8, units, [3])


def test_until_stable_stops_swiss_early(capsys):
    n = 16
    ranked = _run_kw(
        _cands(n), planted_judge(), schedule="swiss", topk=[1], until_stable=True
    )
    full = _run_kw(_cands(n), planted_judge(), schedule="swiss")
    spent = sum(r["comparisons"] for r in ranked)
    assert spent < sum(r["comparisons"] for r in full), "early stop must save calls"
    assert ranked[0]["posting_ids"][0] == "c00"
    assert "top-k stable:" in capsys.readouterr().out


def _run_kw(cands, judge, schedule="round-robin", **kw):
    return asyncio.run(
        run_ladder(
            cands,
            resume_text="",
            label="",
            model="unused-with-injected-judge",
            schedule=schedule,
            rounds=None,
            order_swap=True,
            concurrency=8,
            judge=judge,
            **kw,
        )
    )


def test_closure_topk_transitive_chain_certifies():
    from job_description_scan.ranking import closure_topk

    # A judged adjacent chain 0>1>...>5 certifies every prefix transitively.
    chain = [[{"a": i, "b": i + 1, "winner": i}] for i in range(5)]
    stats = closure_topk(6, chain, [1, 3])
    assert all(s["stable"] and s["est_more"] == 0 for s in stats)


def test_closure_topk_missing_links_are_counted():
    from job_description_scan.ranking import closure_topk

    units = [[{"a": 0, "b": 1, "winner": 0}], [{"a": 2, "b": 3, "winner": 2}]]
    (s,) = closure_topk(4, units, [1])
    assert not s["stable"] and 0 < s["stability"] < 1
    assert s["est_more"] >= 1


def test_closure_topk_tie_covers_both_directions():
    from job_description_scan.ranking import closure_topk

    units = [
        [{"a": 0, "b": 1, "winner": 0}, {"a": 0, "b": 1, "winner": 1}],  # tie
        [{"a": 1, "b": 2, "winner": 1}],
        [{"a": 0, "b": 2, "winner": 0}],
    ]
    (s,) = closure_topk(3, units, [1])
    assert s["stable"], "a boundary tie certifies either membership"
