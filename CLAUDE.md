# CLAUDE.md

Personal job-hunt copilot. Currently contains the resume-printer subproject;
additional subprojects (e.g. job-description scanning) may be added as siblings.

## Subprojects

- **`resume-printer/`** — Markdown → PDF pipeline for resumes and cover letters.
  See [`resume-printer/CLAUDE.md`](resume-printer/CLAUDE.md).

Each subproject is self-contained: its own `pyproject.toml`, `.python-version`,
build scripts, and outputs. Top-level state is limited to repo metadata
(`.git/`, `.gitignore`, `.claude/`).
