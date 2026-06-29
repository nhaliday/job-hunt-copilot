# CLAUDE.md

This file provides guidance to Claude Code when working with the
job-description-scan pipeline. All commands below run from this directory.

## Setup

```bash
uv sync
export ANTHROPIC_API_KEY=...
```

No external system dependencies.

## Invocation

```bash
uv run python -m job_description_scan --scan scans.databricks
# Optional flags:
#   --resume ../resume-printer/resumes/resume-tech.md   # adds comparison pass
#   --model claude-haiku-4-5                            # override scan default
#   --out _output/databricks.jsonl                      # default: _output/<scan_tail>.jsonl
#   --limit 5                                           # smoke test
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
  every posting flows to the LLM.
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
