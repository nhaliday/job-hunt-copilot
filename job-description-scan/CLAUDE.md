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
#   --resume ../resume-printer/resumes/resume-tech.md   # adds comparison pass
#   --model claude-haiku-4-5                            # override scan default
#   --out _output/databricks.jsonl                      # default: _output/<scan_tail>.jsonl
#   --limit 5                                           # smoke test
#   --concurrency 20                                    # max concurrent LLM calls (default 20)
```

## Adding a new scan

1. Copy `scans/databricks.py` to `scans/<name>.py`
2. Edit the `Extraction` and (optional) `Comparison` Pydantic classes to match
   the role family you want to characterize
3. Edit the `scan = Scan(...)` block: source board (`greenhouse` or `ashby`),
   slug, model
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
