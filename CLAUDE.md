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

## Keep generic tooling and personal content separable

This repo mixes two categories:

1. **Generic, potentially-publishable tooling** — the reusable engines (the
   `job_description_scan/` package, the resume-printer build pipeline). No
   personal data; could be extracted into a public repo later.
2. **Personal / sensitive job-hunt content** — actual resume and cover-letter
   text, the specific target configs (`scans/<company>.py`), and all scan/rank
   outputs (fit assessments, per-company rankings, candid self-critique). This
   is career-strategy material you do **not** want a target company reading.

Rules, so the two never entangle (and a future public extraction stays a clean
directory copy + history filter):

- **Directory separation** — keep each category in its own directories; each
  subproject's `CLAUDE.md` names its own generic-vs-personal split.
- **Commit separation** — never touch both categories in one commit. A change to
  generic tooling and a change to personal content are always separate commits,
  so the sensitive half is easy to keep out of anything shareable and the
  generic half has clean, publishable history.
- **This GitHub repo stays private.** It contains candid gap analysis,
  per-company rankings, and resume tuning. To show the LLM/agent engineering
  publicly, extract the generic tooling into a separate sanitized repo rather
  than flipping this one public. Generated outputs already live under gitignored
  `_output/` and are never committed.
