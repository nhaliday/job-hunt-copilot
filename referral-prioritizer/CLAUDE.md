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
   extraction. Structured outputs via `messages.parse`; `pause_turn` is resumed.
   The CSV is rewritten atomically per resolved row and rows with a
   `board_source` are skipped on re-runs — interrupted or credit-starved runs
   resume, and hand-prefilled `board_source=manual` rows are never touched (use
   that to opt out e.g. stealth placeholders). Both phases are concurrent —
   probes fan out across threads (workers only fetch; the main thread decides
   and writes), LLM calls fan out on asyncio under a semaphore with the CSV
   still written per completion. Flags: `--probe-only` (no key needed),
   `--dry-run` (counts + cost estimate, no writes), `--limit N`, `--model`,
   `--concurrency` (phase-B LLM calls, default 8).

Known limitation: a probe-accepted board can be genuine but _secondary_ (a
sub-org or test board on one ATS while the main careers system lives elsewhere).
The pre-gate stats stage will expose these via posting counts.

Roadmap (not yet implemented): LLM name/title normalization columns, free
pre-gate posting stats, a human gating pass, and a human-judge Swiss +
Bradley–Terry ranking over the gated subset.

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
```

Output is deterministically ordered (`-n_connections`, then name): re-running
against an unchanged export is byte-identical, so the output can be a tracked
file with clean diffs. Rows with an empty Company are dropped (count printed).
