# job-hunt-copilot

Tooling for running a job hunt like an engineering project: a Markdown → PDF
pipeline for resumes and cover letters, and an LLM-driven job-board scanner that
turns hundreds of postings into a structured, ranked shortlist.

Two self-contained subprojects, each its own uv project:

## [`resume-printer/`](resume-printer/)

Markdown (with pandoc attributes) → styled one-page PDF via pandoc + WeasyPrint.

- **Jinja variants** — one resume source renders to multiple artifacts (e.g.
  with/without location and relocation lines) from a `variants.toml`.
- **Auto-fit** — binary-searches the body font size to fit a target page count.
- **Verification built into the build** — page-count check, an ATS smoke test
  (text extraction preserves name/contact/URLs), and PDF layout checks via
  pdfplumber.
- Content lives outside the pipeline: `build.sh <src-root>` builds any
  directory's `resumes/` and `letters/`.

## [`job-description-scan/`](job-description-scan/)

Scans a company's job board (Greenhouse, Ashby, or Lever) and runs each posting
through an LLM with a per-scan Pydantic schema.

- **Structured extraction** — role family, level, years-of-experience,
  required/desired quals; field descriptions in the schema steer the model.
- **Resume comparison** — pass `--resume` to also populate fit fields (missing
  quals, YoE gap, fit tier) against a concrete resume.
- **Cheap by construction** — deterministic location pre-filter before any LLM
  call; a shared cached system prompt with lead-then-fan-out concurrency so
  every call after the first reads the prompt cache (~3–5× cost reduction).
- **Ranking pass** — pointwise tiers order poorly within a tier, so a second
  pass runs a pairwise LLM-as-judge tournament (A/B order-swapped to control
  position bias) and fits Bradley–Terry to produce a ranking.
- Output is JSONL, one row per posting, with token/cache accounting per row.

Scan configs are plain Python modules (`scans/<company>.py`) kept in the
caller's project, not in the engine — see
[`job-description-scan/examples/example_scan.py`](job-description-scan/examples/example_scan.py).

## Usage shape

This repo holds only the generic tooling. It is designed to be consumed from a
separate (private) content project that holds actual resumes, letters, and scan
configs — pinned as a git submodule, with the scan engine installed as a uv path
dependency:

```bash
# in the content project
./build.sh                                   # wraps tools/resume-printer/build.sh
uv run python -m job_description_scan --scan scans.acme \
  --resume _output/resumes/resume.md
uv run python -m job_description_scan.ranking --scan scans.acme \
  --results _output/acme.jsonl --resume _output/resumes/resume.md \
  --ladder swe --dry-run
```

Requirements: Python 3.14+, [uv](https://docs.astral.sh/uv/), pandoc, poppler
(`pdftotext`), and an `ANTHROPIC_API_KEY` for the scanner.

## Development

Built primarily with AI coding agents (Claude Code), with every change reviewed.
The style that emerged: agents do the research (API documentation, schema
verification against live boards), the mechanical work, and the verification
harnesses; design decisions — what to pin, what to filter, what to verify — stay
human. `CLAUDE.md` files throughout are the agent-facing docs and double as
architecture notes.
