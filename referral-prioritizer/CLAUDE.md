# CLAUDE.md

This file provides guidance to Claude Code when working with the
referral-prioritizer tooling.

## Purpose

Turn a LinkedIn connections export into a prioritized referral-ask list. The
pipeline is built in reviewable stages; implemented so far:

1. **Extract** (`referral_prioritizer/extract.py`) — LinkedIn `Connections.csv`
   → one row per distinct company (`company,n_connections,positions`).
2. **Board discovery** (`referral_prioritizer/discovery.py`) — enriches the
   companies CSV in place with `board_kind`, `board_slug`, `board_url`,
   `board_confidence`, `board_source`, `board_note`. Stage A probes the four
   slug-guessable board APIs for free; a hit is auto-accepted only when a
   display name covers the company name AND the board has postings (an empty
   same-name board is impostor bait). Stage B (Anthropic API, default
   `claude-opus-4-8`) verifies uncorroborated hits against sample job titles (no
   tools, ~1c each) and runs web-search discovery for misses and impostors
   (`web_search_20260209`, ~6c each), including workday `"hostprefix/site"` slug
   extraction. Every scannable-kind discovery claim is only the model's word —
   live testing surfaced hallucinated/stale slugs and real-but-internal workday
   tenants, all reported with high confidence — so each one is checked against
   the board's own API with one free request and demoted to `unknown` (with the
   evidence in the note) if not live and scannable. Structured outputs via
   `messages.parse`; `pause_turn` is resumed. The CSV is rewritten atomically
   per resolved row and rows with a `board_source` are skipped on re-runs —
   interrupted or credit-starved runs resume, and hand-prefilled
   `board_source=manual` rows are never touched (use that to opt out e.g.
   stealth placeholders). Both phases are concurrent — probes fan out across
   threads (workers only fetch; the main thread decides and writes), LLM calls
   fan out on asyncio under a semaphore with the CSV still written per
   completion. Flags: `--probe-only` (no key needed), `--dry-run` (counts + cost
   estimate, no writes), `--limit N`, `--model`, `--concurrency` (phase-B LLM
   calls, default 8).
3. **Pre-gate stats** (`referral_prioritizer/stats.py`) — adds/refreshes
   `n_postings_located` (postings passing a location filter) per scannable
   board, zero LLM spend. One-shot boards reuse the engine clients so counted
   location strings match scan semantics exactly. Workday prefers the server's
   own country facet (parameter names vary by tenant; matched by label,
   sometimes nested under `locationMainGroup`) and falls back to a parallel
   detail walk for facet-less tenants whose `locationsText` carries no geography
   at all (street addresses, campus names — worse than the City-ST gotcha).
   Flags: `--location-filter`, `--workday-location-filter` (defaults mirror a
   US+Canada scope), `--workers`.

4. **Bulk scan + rank** (`referral_prioritizer/scan.py`) — runs the
   job-description-scan engine (as a library) over every scannable board in the
   companies CSV, deduped by `(board_kind, board_slug)`, then a pairwise ranking
   per configured ladder, then writes a per-company `summary.csv` (fit-tier
   counts + top-3 per ladder) — the input to the later human gating pass.
   Generic orchestration only: extraction schemas, location filters, the
   cheap-model prefilter criterion, and ranking ladders all come from a
   **factory module in the consuming project** (`--boards scans.boards`,
   imported from cwd) exposing `make_scan(kind, slug) -> Scan` and
   `ladders() -> list[Ladder]`. Boards run in a thread pool
   (`--board-concurrency`, default 4), each with its own asyncio loop and
   per-board LLM fan-out (`--concurrency`, default 8). Per-board outputs under
   `--out-dir`: `<kind>-<slug>.jsonl` (written as `.partial`, renamed on
   completion — so existence means finished and re-runs skip unless `--force`),
   `-dropped.jsonl` (prefilter drops with the model's reasons — skim to validate
   the criterion), `.log`, and `-rank-<role>.jsonl` for ladders with ≥2 deduped
   clusters (round-robin ≤12 clusters, swiss above). The board fetch is cached
   in-process so ranking's content join doesn't re-fetch. `summary.csv` is
   rebuilt from disk artifacts every run, so resumed or `--only`-filtered runs
   stay coherent. Flags: `--only <substr>`, `--limit N` (smoke), `--model`,
   `--judge-model` (default `claude-opus-5`), `--skip-rank`, `--no-order-swap`,
   `--dry-run` (counts + cost estimate, no spend or HTTP).

5. **TheirStack census sweep** (`referral_prioritizer/theirstack.py`) — queries
   the TheirStack jobs API (needs `THEIRSTACK_API_KEY`; ~`--sample`+1 paid API
   credits per row, 1 credit per job returned) by case-insensitive company name
   and writes three columns in place: `n_postings_theirstack` (total over
   `--max-age-days`, default 30), `theirstack_companies` (>1 = ambiguous name
   match — eyeball before trusting), and `theirstack_hint` (board fingerprint
   from sampled jobs' `final_url` hosts, `kind:slug` when a native kind is
   recognizable, else the bare host). Corroborates discovery on scannable rows
   and surfaces ready-to-scan census fixes for custom/unknown/none rows — hints
   are advisory; `board_*` columns are never touched (applying them is a manual
   step). Resumable (rows with a count are skipped; `--force` re-queries); the
   CSV is rewritten atomically per resolved row; a 401/402/403 aborts the run
   instead of warning per row. Flags: `--kinds custom,unknown,none`,
   `--only <substr>`, `--limit-rows`, `--sample`, `--dry-run` (row count +
   credit estimate, no HTTP).

6. **TheirStack enrichment** (`referral_prioritizer/enrich.py`) — scans +
   rankings for census companies with NO scannable board, from TheirStack index
   pulls. Pull-once/compute-offline: each selected company's postings are pulled
   once (`theirstack-<slug>.raw.jsonl`, verbatim rows incl. LinkedIn URLs;
   per-company atomic billing since TheirStack checks the full credit price
   upfront; .partial→rename; cached pulls are skipped, so running out of credits
   parks the rest until the next run) and every downstream stage reads the cache
   through the engine's `StaticClient` — re-running extraction/ranking costs no
   credits. Selection: tail board_kinds with 0 < n_postings_theirstack <=
   `--max-postings` (default 1000; megacorp exclusion), minus `--exclude` name
   substrings, ordered (-n_connections, +count). Extraction uses the factory's
   workday-kind config (location_filter=None; the prefilter's geography clause
   cuts the free-text locations); rows whose `company` mismatches the census row
   are dropped at load time (ambiguous-name guard). Ranking reuses `_rank_board`
   verbatim. summary.csv rows carry `scan_source=theirstack` and `n_located` =
   the in-window index count — snapshots, not live boards.

Side tool: **provider liveness panel**
(`referral_prioritizer/provider_panel.py`) — measures job-data APIs (TheirStack,
fantastic.jobs via Apify) against native-client ground truth: id-level
recall/precision per board kind as a function of recency window. `--pull` caches
one widest-window pull per panel company (resumable; costs credits); `--report`
recomputes every window/anchor/is_closed variant offline from the cached rows,
free. Panel CSV lives in the consuming project.

Known limitation: a probe-accepted board can be genuine but _secondary_ (a
sub-org or test board on one ATS while the main careers system lives elsewhere).
The pre-gate stats stage will expose these via posting counts.

Roadmap (not yet implemented): LLM name/title normalization columns, a human
gating pass, and a human-judge Swiss + Bradley–Terry ranking over the gated
subset.

## Tooling here, data in the consuming project

This subproject holds only generic code. The connections export, the generated
companies CSV, and every enriched artifact are personal data and live in the
consuming (private) content project — passed in by path, never committed here.
Docs and examples use placeholders (`Jane Doe`, `acme`).

## Usage (from the content project)

```bash
uv run python -m referral_prioritizer.extract \
  --export data/Connections.csv --out data/Companies.csv

uv run python -m referral_prioritizer.discovery --companies data/Companies.csv --dry-run
uv run python -m referral_prioritizer.discovery --companies data/Companies.csv --probe-only
# Stage B needs ANTHROPIC_API_KEY (e.g. via direnv):
direnv exec . uv run python -m referral_prioritizer.discovery --companies data/Companies.csv

uv run python -m referral_prioritizer.stats --companies data/Companies.csv

# Bulk scan + rank (factory module + resume live in the consuming project):
uv run python -m referral_prioritizer.scan --companies data/Companies.csv --dry-run
direnv exec . uv run python -m referral_prioritizer.scan \
  --companies data/Companies.csv --boards scans.boards \
  --resume _output/resumes/rendered-variant.md
```

Output is deterministically ordered (`-n_connections`, then name): re-running
against an unchanged export is byte-identical, so the output can be a tracked
file with clean diffs. Rows with an empty Company are dropped (count printed).
