# CLAUDE.md

Personal job-hunt copilot. Subprojects are self-contained; additional ones may
be added as siblings.

## Subprojects

- **`resume-printer/`** — Markdown → PDF pipeline for resumes and cover letters.
  See [`resume-printer/CLAUDE.md`](resume-printer/CLAUDE.md).
- **`job-description-scan/`** — Greenhouse/Ashby job-board scanner with
  LLM-driven structured extraction and optional resume comparison. See
  [`job-description-scan/CLAUDE.md`](job-description-scan/CLAUDE.md).

Each subproject is self-contained: its own `pyproject.toml`, `.python-version`,
build scripts, and outputs. Top-level state is limited to repo metadata
(`.git/`, `.gitignore`, `.claude/`).
