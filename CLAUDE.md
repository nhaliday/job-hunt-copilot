# CLAUDE.md

Generic job-hunt tooling. Two self-contained subprojects (own `pyproject.toml`,
`.python-version`, build scripts):

- **`resume-printer/`** — Markdown → PDF pipeline for resumes and cover letters.
  See [`resume-printer/CLAUDE.md`](resume-printer/CLAUDE.md).
- **`job-description-scan/`** — Greenhouse/Ashby/Lever job-board scanner with
  LLM-driven structured extraction, resume comparison, and a pairwise-ranking
  second pass. See
  [`job-description-scan/CLAUDE.md`](job-description-scan/CLAUDE.md).

## No personal content

This repo holds only reusable tooling. All personal material — actual resume and
letter text, per-company scan configs, and generated outputs — lives in a
separate **private content repo** that consumes this one:

- this repo is pinned there as a git submodule at `tools/` (revision pin);
- resumes/letters build via `tools/resume-printer/build.sh <content-root>` (the
  `SRC_ROOT` argument);
- the scan engine is installed as a uv path dependency on
  `tools/job-description-scan`; scan configs (`scans/<name>.py`) are imported
  from the content repo's cwd at runtime;
- outputs land in the content repo's gitignored `_output/`.

Keep the boundary absolute: no committed file here may contain personal data —
names, contact info, employers, target companies (except as neutral examples),
resume text, or fit assessments. Docs and code use placeholder examples
(`Jane Doe`, `scans.acme`).
