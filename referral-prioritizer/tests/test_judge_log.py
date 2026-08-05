"""Judgment log: append/load, undo retraction, last-wins tiers, played
pairs (skips included), _resolve-shaped cmp rows, standings weights."""

from referral_prioritizer import judge_log


def test_append_load_and_undo(tmp_path):
    log = tmp_path / "log.jsonl"
    e1 = judge_log.append(log, {"type": "tier", "company": "Acme", "tier": "A"})
    judge_log.append(log, {"type": "tier", "company": "Beta", "tier": "B"})
    judge_log.append(log, {"type": "undo", "target": e1["ts"]})
    events = judge_log.load(log)
    assert [e["company"] for e in events] == ["Beta"]
    assert judge_log.effective_tiers(events) == {"Beta": "B"}


def test_tier_last_wins(tmp_path):
    log = tmp_path / "log.jsonl"
    judge_log.append(log, {"type": "tier", "company": "Acme", "tier": "A"})
    judge_log.append(log, {"type": "tier", "company": "Acme", "tier": "C"})
    assert judge_log.effective_tiers(judge_log.load(log)) == {"Acme": "C"}


def _cmp(pool, a, b, result):
    return {"type": "cmp", "pool": pool, "a": a, "b": b, "result": result}


def test_played_includes_skips_and_results_exclude_them(tmp_path):
    log = tmp_path / "log.jsonl"
    judge_log.append(log, _cmp("p", "x", "y", "a"))
    judge_log.append(log, _cmp("p", "x", "z", "skip"))
    judge_log.append(log, _cmp("other", "x", "y", "b"))
    events = judge_log.load(log)
    assert judge_log.played_pairs(events, "p") == {
        frozenset(("x", "y")),
        frozenset(("x", "z")),
    }
    idx = {"x": 0, "y": 1, "z": 2}
    rows = judge_log.cmp_results(events, "p", idx)
    assert rows == [{"a": 0, "b": 1, "winner": 0}]  # skip contributes nothing


def test_tie_encoding_and_standings(tmp_path):
    log = tmp_path / "log.jsonl"
    judge_log.append(log, _cmp("p", "x", "y", "tie"))
    judge_log.append(log, _cmp("p", "x", "z", "a"))
    events = judge_log.load(log)
    idx = {"x": 0, "y": 1, "z": 2}
    rows = judge_log.cmp_results(events, "p", idx)
    assert rows[:2] == [
        {"a": 0, "b": 1, "winner": 0},
        {"a": 0, "b": 1, "winner": 1},
    ]  # a tie is one win each direction — the shape _resolve reads as a tie
    assert judge_log.standings(events, "p", idx) == [1.5, 0.5, 0.0]
