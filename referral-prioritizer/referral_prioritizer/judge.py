"""Human-judge referral ranking: shortlist -> tier -> Swiss+Bradley-Terry.

Subcommands (python -m referral_prioritizer.judge <cmd>):

- referrers  stage 1: per multi-connection company, shortlist the plausible
             referrers, then order the shortlist pairwise -> referrers.csv
             (single-connection companies derive automatically).
- tier       stage 2: pointwise-tier every census company (A/B/C/exclude)
             on the full enrichment card -> company-tiers.csv.
- rank       stage 3: Swiss + Bradley-Terry within a tier (default A), on
             two-card comparisons -> company-ranking.csv.
- browse     walk the finished ranking on the same card.

Every judgment appends to a JSONL log under --judgments-dir (see judge_log);
derived CSVs recompute from the log on every exit, so quitting mid-stage
loses nothing and --derive-only rebuilds artifacts without opening the UI.
"""

import argparse
import itertools
import random
from collections import deque
from pathlib import Path

import choix

from job_description_scan.ranking import _resolve, _swiss_rounds, swiss_pairings

from referral_prioritizer import judge_data, judge_log
from referral_prioritizer.judge_tui import BrowseApp, JudgeApp


def _fit_utilities(n: int, results: list[dict]) -> list[float]:
    edges, _ = _resolve(results)
    if not edges:
        return [0.0] * n
    return list(choix.ilsr_pairwise(n, edges, alpha=0.01))


class _Controller:
    """Shared log/undo plumbing. Subclasses implement _build (recompute all
    pending work from the effective log) and expose questions via a deque."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.history: list[tuple[dict, dict]] = []
        self.queue: deque = deque()
        self._dirty = True

    def _log(self, payload: dict, event: dict) -> None:
        self.history.append((judge_log.append(self.log_path, event), payload))

    def undo(self) -> dict | None:
        if not self.history:
            return None
        ev, payload = self.history.pop()
        judge_log.append(self.log_path, {"type": "undo", "target": ev["ts"]})
        self._dirty = True
        return payload

    def next(self) -> dict | None:
        if self._dirty:
            self.queue.clear()
            self._build()
            self._dirty = False
        return self.queue.popleft() if self.queue else self._refill()

    def _refill(self) -> dict | None:
        return None

    def _build(self) -> None:
        raise NotImplementedError


class ReferrersController(_Controller):
    def __init__(self, log_path: Path, groups: dict[str, list[dict]]) -> None:
        super().__init__(log_path)
        # Multi-connection companies only, biggest pools first.
        self.groups = {c: conns for c, conns in groups.items() if len(conns) >= 2}
        self.order = sorted(self.groups, key=lambda c: -len(self.groups[c]))

    def _build(self) -> None:
        events = judge_log.load(self.log_path)
        shortlists = judge_log.effective_shortlists(events)
        for company in self.order:
            conns = self.groups[company]
            if company not in shortlists:
                self.queue.append(
                    {
                        "mode": "select",
                        "heading": f"shortlist referrers — {company}",
                        "card": {"company": company, "n_connections": len(conns)},
                        "options": conns,
                        "_company": company,
                    }
                )
                continue
            by_id = {c["id"]: c for c in conns}
            keys = [k for k in shortlists[company] if k in by_id]
            if len(keys) < 2:
                continue
            pool = f"referrers:{company}"
            played = judge_log.played_pairs(events, pool)
            idx = {k: i for i, k in enumerate(keys)}
            if len(keys) <= 4:
                pairs = [
                    (a, b)
                    for a, b in itertools.combinations(keys, 2)
                    if frozenset((a, b)) not in played
                ]
            else:
                score = judge_log.standings(events, pool, idx)
                target = _swiss_rounds(len(keys), None) * (len(keys) // 2)
                pairs = []
                played_keys = {
                    frozenset((idx[a], idx[b]))
                    for a, b in (tuple(p) for p in played)
                    if a in idx and b in idx
                }
                rng = random.Random(0)
                while len(played_keys) < target:
                    round_pairs = swiss_pairings(len(keys), score, played_keys, rng)
                    if not round_pairs:
                        break
                    pairs += [(keys[i], keys[j]) for i, j in round_pairs]
            for a, b in pairs:
                self.queue.append(
                    {
                        "mode": "compare",
                        "heading": f"better referrer — {company}",
                        "left": by_id[a],
                        "right": by_id[b],
                        "_pool": pool,
                        "_a": a,
                        "_b": b,
                    }
                )

    def answer(self, payload: dict, response) -> None:
        if payload["mode"] == "select":
            selected = [payload["options"][i]["id"] for i in response]
            self._log(
                payload,
                {
                    "type": "shortlist",
                    "company": payload["_company"],
                    "selected": selected,
                },
            )
            self._dirty = True  # the shortlist spawns comparison work
        else:
            self._log(
                payload,
                {
                    "type": "cmp",
                    "pool": payload["_pool"],
                    "a": payload["_a"],
                    "b": payload["_b"],
                    "result": response,
                },
            )

    def derive(self, out_path: Path, all_groups: dict[str, list[dict]]) -> None:
        events = judge_log.load(self.log_path)
        shortlists = judge_log.effective_shortlists(events)
        rows = []
        for company, conns in sorted(all_groups.items()):
            if len(conns) == 1:
                ordered = conns
            elif company in shortlists:
                by_id = {c["id"]: c for c in conns}
                keys = [k for k in shortlists[company] if k in by_id]
                idx = {k: i for i, k in enumerate(keys)}
                results = judge_log.cmp_results(events, f"referrers:{company}", idx)
                utils = _fit_utilities(len(keys), results)
                ordered = [by_id[k] for k in sorted(keys, key=lambda k: -utils[idx[k]])]
            else:
                continue  # multi-connection, not yet judged
            for rank, c in enumerate(ordered, 1):
                rows.append(
                    {
                        "company": company,
                        "rank": rank,
                        "name": c["name"],
                        "position": c["position"],
                        "url": c["url"],
                    }
                )
        judge_data.write_referrers(out_path, rows)


class TierController(_Controller):
    def __init__(
        self,
        log_path: Path,
        cards: list[dict],
        retier: bool,
        tier_names: list[str],
    ) -> None:
        super().__init__(log_path)
        self.cards = cards
        self.retier = retier
        self.key_map = {str(i + 1): name for i, name in enumerate(tier_names)}
        self.help = (
            " · ".join(f"{k}={v}" for k, v in self.key_map.items())
            + " · x exclude · s skip · u undo · q quit"
        )

    def _build(self) -> None:
        tiers = judge_log.effective_tiers(judge_log.load(self.log_path))
        todo = [c for c in self.cards if self.retier or c["company"] not in tiers]
        for i, card in enumerate(todo):
            self.queue.append(
                {
                    "mode": "tier",
                    "heading": f"tier {i + 1}/{len(todo)} — {card['company']}",
                    "card": card,
                    "help": self.help,
                    "_valid": set(self.key_map) | {"x", "s"},
                    "_company": card["company"],
                }
            )

    def answer(self, payload: dict, response) -> None:
        if response == "s":
            return  # defer: no event, reappears next session
        self._log(
            payload,
            {
                "type": "tier",
                "company": payload["_company"],
                "tier": self.key_map.get(response, "x"),
            },
        )

    def derive(self, out_path: Path) -> None:
        judge_data.write_tiers(
            out_path, judge_log.effective_tiers(judge_log.load(self.log_path))
        )


class RankController(_Controller):
    def __init__(
        self,
        log_path: Path,
        cards: list[dict],
        tier: str,
        tiers: dict[str, str],
        rounds: int | None,
    ) -> None:
        super().__init__(log_path)
        self.cards_by_company = {c["company"]: c for c in cards}
        self.tiers = tiers
        self.tier = tier
        self.keys = sorted(c for c, t in tiers.items() if t == tier)
        self.idx = {k: i for i, k in enumerate(self.keys)}
        self.pool = f"rank:{tier}"
        n = len(self.keys)
        self.target = _swiss_rounds(n, rounds) * (n // 2) if n > 1 else 0
        self.rng = random.Random(0)

    def _build(self) -> None:
        pass  # questions come from _refill so standings stay fresh per round

    def _refill(self) -> dict | None:
        events = judge_log.load(self.log_path)
        played = {
            frozenset((self.idx[a], self.idx[b]))
            for a, b in (tuple(p) for p in judge_log.played_pairs(events, self.pool))
            if a in self.idx and b in self.idx
        }
        if len(played) >= self.target:
            return None
        score = judge_log.standings(events, self.pool, self.idx)
        matchups = swiss_pairings(len(self.keys), score, played, self.rng)
        if not matchups:
            return None
        for i, j in matchups:
            self.queue.append(
                {
                    "mode": "compare",
                    "heading": (
                        f"tier {self.tier} ranking — "
                        f"{len(played)}/{self.target} comparisons"
                    ),
                    "left": self.cards_by_company[self.keys[i]],
                    "right": self.cards_by_company[self.keys[j]],
                    "_pool": self.pool,
                    "_a": self.keys[i],
                    "_b": self.keys[j],
                }
            )
        return self.queue.popleft() if self.queue else None

    def answer(self, payload: dict, response) -> None:
        self._log(
            payload,
            {
                "type": "cmp",
                "pool": payload["_pool"],
                "a": payload["_a"],
                "b": payload["_b"],
                "result": response,
            },
        )

    def derive(self, out_path: Path) -> None:
        events = judge_log.load(self.log_path)
        rows = []
        ranked_tiers = sorted({t for t in self.tiers.values() if t != "x"})
        for tier in ranked_tiers + ["x"]:
            keys = sorted(c for c, t in self.tiers.items() if t == tier)
            if not keys:
                continue
            idx = {k: i for i, k in enumerate(keys)}
            results = judge_log.cmp_results(events, f"rank:{tier}", idx)
            utils = _fit_utilities(len(keys), results)
            _, tally = _resolve(results)
            ordered = sorted(keys, key=lambda k: -utils[idx[k]])
            for rank, k in enumerate(ordered, 1):
                card = self.cards_by_company.get(k, {"company": k})
                t = tally.get(idx[k], {"wins": 0.0, "losses": 0.0, "ties": 0.0})
                rows.append(
                    {
                        "tier": tier,
                        "rank": rank if results else "",
                        "utility": round(utils[idx[k]], 4),
                        "wins": int(t["wins"]),
                        "losses": int(t["losses"]),
                        "ties": int(t["ties"]),
                        "company": k,
                        "n_connections": card.get("n_connections", ""),
                        "scan_source": card.get("scan_source", ""),
                        "referrers": "; ".join(
                            r["name"] for r in card.get("referrers", [])
                        ),
                        "top_posting": (card.get("top_postings") or [""])[0],
                    }
                )
        judge_data.write_ranking(out_path, rows)


def _browse_entries(args) -> list[tuple[dict, dict | None]]:
    cards = {
        c["company"]: c
        for c in judge_data.company_cards(
            args.companies, args.summary, args.out_dir / "referrers.csv"
        )
    }
    ranking_csv = args.out_dir / "company-ranking.csv"
    if ranking_csv.exists():
        entries = []
        for r in judge_data.read_derived(ranking_csv):
            extra = {
                "tier": r["tier"],
                "rank": int(r["rank"]) if r["rank"] else 0,
                "utility": float(r["utility"] or 0),
                "wins": r["wins"],
                "losses": r["losses"],
                "ties": r["ties"],
            }
            entries.append((cards.get(r["company"], {"company": r["company"]}), extra))
        return entries
    return [(c, None) for c in cards.values()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["referrers", "tier", "rank", "browse"])
    ap.add_argument("--companies", type=Path, default=Path("data/Companies.csv"))
    ap.add_argument("--connections", type=Path, default=Path("data/Connections.csv"))
    ap.add_argument(
        "--summary", type=Path, default=Path("_output/referral-scans/summary.csv")
    )
    ap.add_argument("--judgments-dir", type=Path, default=Path("data/judgments"))
    ap.add_argument("--out-dir", type=Path, default=Path("data"))
    ap.add_argument(
        "--tiers",
        default="A,B,C",
        help="tier: comma-separated tier names mapped to keys 1..N",
    )
    ap.add_argument("--tier", default="A", help="rank: which tier to compare")
    ap.add_argument("--rounds", type=int, help="rank: swiss rounds override")
    ap.add_argument("--retier", action="store_true", help="tier: revisit all")
    ap.add_argument(
        "--derive-only", action="store_true", help="rebuild CSVs from logs, no UI"
    )
    args = ap.parse_args()

    referrers_csv = args.out_dir / "referrers.csv"
    if args.cmd == "referrers":
        groups = judge_data.load_connections(args.connections)
        ctl = ReferrersController(args.judgments_dir / "referrers.jsonl", groups)
        if not args.derive_only:
            JudgeApp(ctl).run()
        ctl.derive(referrers_csv, groups)
        print(f"→ {referrers_csv}")
    elif args.cmd == "tier":
        cards = judge_data.company_cards(args.companies, args.summary, referrers_csv)
        ctl = TierController(
            args.judgments_dir / "tiers.jsonl",
            cards,
            args.retier,
            [t.strip() for t in args.tiers.split(",") if t.strip()],
        )
        if not args.derive_only:
            JudgeApp(ctl).run()
        ctl.derive(args.out_dir / "company-tiers.csv")
        print(f"→ {args.out_dir / 'company-tiers.csv'}")
    elif args.cmd == "rank":
        cards = judge_data.company_cards(args.companies, args.summary, referrers_csv)
        tiers = judge_log.effective_tiers(
            judge_log.load(args.judgments_dir / "tiers.jsonl")
        )
        if not tiers:
            raise SystemExit("no tiers logged yet — run `judge tier` first")
        ctl = RankController(
            args.judgments_dir / "ranking.jsonl",
            cards,
            args.tier,
            tiers,
            args.rounds,
        )
        if not args.derive_only:
            JudgeApp(ctl).run()
        ctl.derive(args.out_dir / "company-ranking.csv")
        print(f"→ {args.out_dir / 'company-ranking.csv'}")
    else:  # browse
        BrowseApp(_browse_entries(args)).run()


if __name__ == "__main__":
    main()
