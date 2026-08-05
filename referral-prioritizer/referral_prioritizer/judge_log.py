"""Append-only judgment log: the source of truth for every human decision.

One JSONL file per judging stage. Event types:

- {"type": "shortlist", "company", "selected": [ids]}   (stage 1)
- {"type": "cmp", "pool", "a", "b", "result": "a|b|tie|skip"}
- {"type": "tier", "company", "tier"}                    (last one wins)
- {"type": "undo", "target": <ts of retracted event>}

Every event gets a nanosecond "ts" on append (the undo reference). Nothing is
ever mutated or deleted: undo appends a retraction, re-tiering appends a new
tier event. All derived artifacts (referrers.csv, tiers, rankings) recompute
from the effective event stream, so judging is resumable at any keypress and
re-fittable forever.
"""

import json
import os
import time
from pathlib import Path


def append(path: Path, event: dict) -> dict:
    event = {"ts": time.time_ns(), **event}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return event


def load(path: Path) -> list[dict]:
    """Effective events: undo retractions applied, order preserved."""
    if not path.exists():
        return []
    events = [json.loads(line) for line in open(path) if line.strip()]
    undone = {e["target"] for e in events if e.get("type") == "undo"}
    return [e for e in events if e.get("type") != "undo" and e["ts"] not in undone]


def effective_tiers(events: list[dict]) -> dict[str, str]:
    tiers: dict[str, str] = {}
    for e in events:
        if e.get("type") == "tier":
            tiers[e["company"]] = e["tier"]
    return tiers


def effective_shortlists(events: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in events:
        if e.get("type") == "shortlist":
            out[e["company"]] = e["selected"]
    return out


def played_pairs(events: list[dict], pool: str) -> set[frozenset]:
    """Pairs never to re-ask: every logged cmp, including skips (a skip is a
    standing 'no judgment' — re-asking would nag; undo it to re-open)."""
    return {
        frozenset((e["a"], e["b"]))
        for e in events
        if e.get("type") == "cmp" and e.get("pool") == pool
    }


def standings(events: list[dict], pool: str, key_to_idx: dict[str, int]) -> list[float]:
    """Swiss standings from logged comparisons: win 1.0, tie 0.5 each."""
    score = [0.0] * len(key_to_idx)
    for e in events:
        if e.get("type") != "cmp" or e.get("pool") != pool:
            continue
        a, b = key_to_idx.get(e["a"]), key_to_idx.get(e["b"])
        if a is None or b is None:
            continue
        if e["result"] == "a":
            score[a] += 1.0
        elif e["result"] == "b":
            score[b] += 1.0
        elif e["result"] == "tie":
            score[a] += 0.5
            score[b] += 0.5
    return score


def cmp_results(
    events: list[dict], pool: str, key_to_idx: dict[str, int]
) -> list[dict]:
    """Logged comparisons as ranking._resolve rows. A tie becomes one win in
    each direction (the same encoding order-swap disagreement produces);
    skips contribute nothing."""
    rows = []
    for e in events:
        if e.get("type") != "cmp" or e.get("pool") != pool:
            continue
        a, b = key_to_idx.get(e["a"]), key_to_idx.get(e["b"])
        if a is None or b is None:  # key left the pool (census edit)
            continue
        if e["result"] == "a":
            rows.append({"a": a, "b": b, "winner": a})
        elif e["result"] == "b":
            rows.append({"a": a, "b": b, "winner": b})
        elif e["result"] == "tie":
            rows.append({"a": a, "b": b, "winner": a})
            rows.append({"a": a, "b": b, "winner": b})
    return rows
