# CLAUDE.md

This file provides guidance to Claude Code when working with the
referral-prioritizer tooling.

## Purpose

Turn a LinkedIn connections export into a prioritized referral-ask list. The
pipeline is built in reviewable stages; implemented so far:

1. **Extract** (`referral_prioritizer/extract.py`) — LinkedIn `Connections.csv`
   → one row per distinct company (`company,n_connections,positions`).

Roadmap (not yet implemented): board discovery (probe the job-board APIs
supported by `job-description-scan`, then LLM + web search for the misses), LLM
name/title normalization columns, free pre-gate posting stats, a human gating
pass, and a human-judge Swiss + Bradley–Terry ranking over the gated subset.

## Tooling here, data in the consuming project

This subproject holds only generic code. The connections export, the generated
companies CSV, and every enriched artifact are personal data and live in the
consuming (private) content project — passed in by path, never committed here.
Docs and examples use placeholders (`Jane Doe`, `acme`).

## Usage (from the content project)

```bash
uv run python -m referral_prioritizer.extract \
  --export data/Connections.csv --out data/Companies.csv
```

Output is deterministically ordered (`-n_connections`, then name): re-running
against an unchanged export is byte-identical, so the output can be a tracked
file with clean diffs. Rows with an empty Company are dropped (count printed).
