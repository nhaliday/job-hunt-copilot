"""Stage controllers, driven synchronously (no TUI): stage-1 selection and
ordering, rank-pool scheduling that skips logged pairs, and a planted-log
Bradley-Terry recovery through the same code path the real `rank` uses."""

from referral_prioritizer.judge import (
    RankController,
    ReferrersController,
    TierController,
)


def _drain(ctl, decide, limit=500):
    """Answer questions from `decide(payload)` until the controller is done."""
    for _ in range(limit):
        p = ctl.next()
        if p is None:
            return
        ctl.answer(p, decide(p))
    raise AssertionError("controller never finished")


def _conn(company, i):
    return {
        "id": f"u{i}",
        "name": f"Person {i}",
        "position": "Eng",
        "connected_on": "",
        "url": f"u{i}",
        "company": company,
    }


def test_referrers_stage_shortlist_then_order(tmp_path):
    groups = {
        "Solo": [_conn("Solo", 0)],  # single connection: no questions
        "Duo": [_conn("Duo", 1), _conn("Duo", 2)],
        "Big": [_conn("Big", i) for i in range(3, 9)],  # 6 -> shortlist 3
    }
    ctl = ReferrersController(tmp_path / "log.jsonl", groups)

    def decide(p):
        if p["mode"] == "select":
            return [0, 1, 2] if p["_company"] == "Big" else [0, 1]
        # planted: lower id wins
        return "a" if p["_a"] < p["_b"] else "b"

    _drain(ctl, decide)
    ctl.derive(tmp_path / "referrers.csv", groups)
    from referral_prioritizer.judge_data import read_derived

    rows = read_derived(tmp_path / "referrers.csv")
    by_company = {}
    for r in rows:
        by_company.setdefault(r["company"], []).append(r["name"])
    assert by_company["Solo"] == ["Person 0"]  # derived without judging
    assert by_company["Duo"] == ["Person 1", "Person 2"]
    assert by_company["Big"] == ["Person 3", "Person 4", "Person 5"]  # planted order


def test_tier_stage_resumes_and_derives(tmp_path):
    cards = [{"company": c} for c in ("Acme", "Beta", "Gamma")]
    log = tmp_path / "tiers.jsonl"
    ctl = TierController(log, cards, retier=False, tier_names=["A", "B", "C"])
    p = ctl.next()
    ctl.answer(p, "1")  # Acme -> A
    p2 = ctl.next()
    ctl.answer(p2, "x")  # Beta -> excluded
    # New controller (fresh session): only Gamma remains.
    ctl2 = TierController(log, cards, retier=False, tier_names=["A", "B", "C"])
    assert ctl2.next()["_company"] == "Gamma"
    ctl2.derive(tmp_path / "tiers.csv")
    from referral_prioritizer.judge_data import read_derived

    assert {r["company"]: r["tier"] for r in read_derived(tmp_path / "tiers.csv")} == {
        "Acme": "A",
        "Beta": "x",
    }


def test_targeted_retier_corrects_one_company(tmp_path):
    cards = [{"company": c} for c in ("Acme", "Beta", "Gamma")]
    log = tmp_path / "tiers.jsonl"
    ctl = TierController(log, cards, retier=False, tier_names=["A", "B", "C"])
    for _ in range(3):
        ctl.answer(ctl.next(), "1")  # everything tiered A
    # Correct just Acme without revisiting the rest.
    fix = TierController(
        log, cards, retier=True, tier_names=["A", "B", "C"], only="acme"
    )
    p = fix.next()
    assert p["_company"] == "Acme"
    fix.answer(p, "2")
    assert fix.next() is None  # nothing else queued
    fix.derive(tmp_path / "tiers.csv")
    from referral_prioritizer.judge_data import read_derived

    assert {r["company"]: r["tier"] for r in read_derived(tmp_path / "tiers.csv")} == {
        "Acme": "B",
        "Beta": "A",
        "Gamma": "A",
    }


def test_rank_stage_planted_recovery_and_no_repeats(tmp_path):
    companies = [f"C{i}" for i in range(8)]
    cards = [{"company": c} for c in companies]
    tiers = {c: "A" for c in companies}
    log = tmp_path / "ranking.jsonl"
    asked = []

    ctl = RankController(log, cards, "A", tiers, rounds=None)

    def decide(p):
        asked.append(frozenset((p["_a"], p["_b"])))
        return "a" if p["_a"] < p["_b"] else "b"  # planted: C0 best

    _drain(ctl, decide)
    assert len(asked) == len(set(asked)), "no pair asked twice"

    # Resume: a fresh controller schedules nothing further.
    ctl2 = RankController(log, cards, "A", tiers, rounds=None)
    assert ctl2.next() is None

    ctl2.derive(tmp_path / "company-ranking.csv")
    from referral_prioritizer.judge_data import read_derived

    rows = read_derived(tmp_path / "company-ranking.csv")
    assert rows[0]["company"] == "C0"
    # Swiss doesn't guarantee total order; require strong rank correlation.
    n = len(rows)
    d2 = sum((i - int(r["company"][1:])) ** 2 for i, r in enumerate(rows))
    assert 1 - 6 * d2 / (n * (n**2 - 1)) >= 0.8
