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

## Before any public release

This repo is private. Its history was path-filtered out of a personal monorepo:
file contents are clean (historical PII in doc examples was scrubbed with
`--replace-text`), but a few commit **messages** from formerly-mixed commits
still name personal files (e.g. cover-letter filenames). Before flipping public:
scrub messages (`git filter-repo --replace-message`), then re-run a full-history
grep audit for names/emails/companies.
