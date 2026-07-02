# CLAUDE.md

This file provides guidance to Claude Code when working with the
job-description-scan pipeline. All commands below run from this directory.

## Setup

```bash
uv sync
export ANTHROPIC_API_KEY=...      # or use direnv (.envrc here; gitignored at repo root)
```

No external system dependencies.

**When the API key is managed by direnv**, Claude Code's Bash tool runs
non-interactive shells and will NOT auto-load `.envrc` on `cd`. Wrap any
LLM-bound command with `direnv exec .` to inject the environment:

```bash
direnv exec . uv run python -m job_description_scan --scan scans.databricks
```

## Invocation

```bash
uv run python -m job_description_scan --scan scans.databricks
# Optional flags:
#   --resume ../resume-printer/_output/resumes/resume-tech-local-relocation.md  # comparison pass
#   --model claude-haiku-4-5                            # override scan default
#   --out _output/databricks.jsonl                      # default: _output/<scan_tail>.jsonl
#   --limit 5                                           # smoke test
#   --concurrency 20                                    # max concurrent LLM calls (default 20)
```

**Point `--resume` at the rendered variant, not the raw template.** The pipeline
reads the resume file verbatim into the (cached) system prompt — no Jinja
rendering. `resume-printer/resumes/resume-tech.md` is a template: its
`{% if location %}` / `{{ headlands_location }}` directives would reach the LLM
literally, and location/relocation would be _absent_. Use the instantiated
artifact `resume-printer/_output/resumes/resume-tech-local-relocation.md`
(produced by `./build.sh` in resume-printer; `local-relocation` = current
DC-Metro + open to relocation), so location and relocation frontmatter actually
inform the comparison/fit tier. Re-run `./build.sh` after editing the resume.

## Adding a new scan

1. Copy `scans/databricks.py` to `scans/<name>.py`
2. Edit the `Extraction` and (optional) `Comparison` Pydantic classes to match
   the role family you want to characterize
3. Edit the `scan = Scan(...)` block: source board (`greenhouse`, `ashby`, or
   `lever` — all three implemented), slug, model
4. Run: `uv run python -m job_description_scan --scan scans.<name>`

Pydantic `Field(description=...)` strings flow into the JSON schema sent to the
LLM, so use them to guide extraction at the field level.

## Adding a new board

1. Create `job_description_scan/boards/<name>.py` with a class implementing the
   `BoardClient` protocol — single method `iter_postings() -> Iterable[Posting]`
2. Register the class in `boards/__init__.py`'s `make_client` factory and add
   the kind to the `BoardKind` Literal in `config.py`
3. Map the URL pattern and response shape to
   `Posting(id, title, location, content_text, url, raw)`

## Architecture

- **Board fetch**: `boards/<kind>.py` → `Posting` dataclass. No title filtering;
  every posting flows to the LLM unless excluded by an optional
  `location_filter` (see below).
- **LLM pipeline**: `pipeline.py` builds a cached system prompt (instructions +
  schema + reference docs + optional resume), then issues a single composed-
  schema `client.messages.parse(...)` call per posting.
- **Output**: JSONL via `output.py`.

Per-scan inputs (`config.Scan`):

- `source`: `BoardSource(kind, slug)` — Greenhouse/Ashby/Lever
- `extraction`: Pydantic class for JD-only facts (always populated)
- `comparison`: optional Pydantic class for fit/gap fields (populated when
  `--resume` provided)
- `system_context_files`: list of paths inlined into the cached system prompt
  (e.g. `Levels.fyi Standard SWE Level Framework.md`)
- `model`: Anthropic model ID (default `claude-haiku-4-5`)
- `location_filter`: optional `re.Pattern` applied to `Posting.location` before
  the LLM call. Postings that don't match are skipped entirely (no extraction
  cost). See `scans/databricks.py` for a US-states example. Title content is
  never filtered — only location is, since location is structured metadata while
  titles encode role nuance worth letting the LLM judge.

## Concurrency

LLM calls run concurrently via `anthropic.AsyncAnthropic`. Default
`--concurrency 20`; bump freely if your rate limits allow (Haiku tier of 10K RPM
/ 10M ITPM gives ~2 orders of magnitude of headroom at typical scan sizes).

**Lead-then-fan-out**: call #1 is awaited sequentially so it writes the prompt
cache; the rest fan out under a `Semaphore(concurrency)`. Without this, every
concurrent call would pay `cache_creation_input_tokens` and cost would balloon
~3–5×.

**Row order** in the output JSONL is completion-order, not board-order. For
deterministic ordering, post-process with
`jq -s 'sort_by(.posting.id)' _output/<scan>.jsonl`.

**Retries** are handled by the SDK (`max_retries=8` set in the pipeline), which
retries 408/409/429/5xx with exponential backoff. No external retry library
needed — adding one duplicates the SDK's behavior.

## Caching

The system prompt is uniform across all postings in a single invocation, so
Anthropic prompt caching kicks in from posting 2 onward. Verify by inspecting
`_meta.cache_read_input_tokens > 0` in the output JSONL.

Minimum cacheable prefix is 4096 tokens on Haiku 4.5 and 2048 on Sonnet 4.6.
Small system prompts (no resume, short Levels.fyi-equivalent) may silently fall
under the threshold and cache nothing — the cost is still small at this scale
but check the meta if you see no cache reads.

## Output format

JSONL, one row per posting:

```json
{
  "posting": { "id": "...", "title": "...", "location": "...", "url": "..." },
  "result": { "extraction": { ... }, "comparison": { ... } },
  "_meta": {
    "model": "...",
    "input_tokens": N,
    "output_tokens": N,
    "cache_creation_input_tokens": N,
    "cache_read_input_tokens": N
  }
}
```

`result.comparison` is absent when `--resume` was not supplied or the scan did
not define a `Comparison` class. A row may instead carry an `error` field if the
LLM call failed; the pipeline continues past it.

## Ranking pass (second pass)

Pointwise `fit_tier` triages but orders poorly _within_ a tier. To pick a
best-of ordering, `ranking.py` runs an **LLM-as-judge pairwise tournament** over
one role family's strong+stretch pool and fits **Bradley-Terry** (`choix`) to
the outcomes. Run it once per role family — families are not comparable
head-to-head.

```bash
# preview cost (no API spend): rows -> clusters -> pairings -> judge calls
uv run python -m job_description_scan.ranking \
  --scan scans.palantir --results _output/palantir-fable.jsonl \
  --resume ../resume-printer/_output/resumes/resume-tech-local-relocation.md \
  --ladder swe --dry-run

# run a ladder (round-robin default; --schedule swiss is cheaper for large pools)
… --ladder swe
… --ladder post_sales_se
#   --ladder all            # every ladder in the scan's RankConfig
#   --no-order-swap         # halve calls (drops position-bias mitigation)
#   --judge-model …         # default claude-fable-5
```

**Case config lives in the scan module**, not the engine. A scan defines
`ranking = RankConfig(ladders=[Ladder(...)])` (see `scans/palantir.py`): one
`Ladder` per role family, each selecting `roles`/`tiers`, an optional
`exclude_title` regex (e.g. new-grad/internship), and a `label` role-framing
string slotted into the otherwise-generic judge prompt. The engine reads this
and stays free of any case-specific strings.

Mechanics:

- **Content dedup** (`rapidfuzz`, not embeddings): the scan JSONL has no JD
  body, so the ranker re-fetches the board and joins on `posting.id`. It strips
  the prefix/suffix shared across the pool (company blurb + EEO/benefits tail —
  else boilerplate inflates similarity) and merges postings with
  `token_set_ratio ≥ --dedup-threshold` (default 90). This collapses
  location-variant clones and near-duplicate titles into one competing entry;
  the canonical rep carries the member locations/ids.
- **Judge**: each pair is compared twice with A/B **swapped** (position-bias
  mitigation; `--no-order-swap` to halve cost). Consistent winner → one edge;
  disagreement → a tie (one edge each direction, which `choix` handles). The
  resume + role label form a cached system prefix (same lead-then-fan-out +
  caching as the scan pipeline, reused from `pipeline.py`).
- **Schedule**: `round-robin` (default) compares all pairs; `swiss`
  (`--schedule swiss`, `--rounds N`) is cheaper and concentrates comparisons
  near the top for large pools.
- **Output**: `_output/<scan>-rank-<role>.jsonl`, one row per cluster with
  `rank`, `utility` (Bradley-Terry), `wins`/`losses`/`ties`, and the member
  `locations`/`posting_ids`; plus a leaderboard to stdout.

**Cost**: round-robin is `2·C(n,2)` Fable calls — cheap for a small pool (~10
FDE clusters → ~90 calls), but a ~22-cluster SWE pool is ~460 calls. Always
`--dry-run` first; drop to `--no-order-swap` or `--schedule swiss` if that's
hotter than you want.
