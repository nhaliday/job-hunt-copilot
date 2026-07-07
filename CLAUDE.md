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

This repo is private. Its history was path-filtered out of a personal monorepo,
then rewritten once more (2026-07-07) to drop `reference.docx`, a historical
binary that embedded a real resume — text greps don't see inside zip containers,
so any future audit must account for binaries explicitly (none remain: every
path ever tracked is a text format).

Audited state of the full history:

- **File contents: clean.** PII in doc examples was scrubbed with
  `--replace-text` (verified zero matches for name/phone/email across
  `git rev-list --all`); the one binary carrier is filtered out entirely.
- **Commit messages: retain mild personal context.** A few formerly-mixed
  commits name target companies and personal filenames (e.g. a cover-letter
  trim, `scans/palantir`). Assessed low-sensitivity: the latest-commit-per-file
  view (what GitHub's landing page shows) is entirely clean, and the buried
  messages only confirm a job hunt the repo's purpose already implies. Scrub
  with `git filter-repo --replace-message` only if that assessment changes — any
  rewrite must go to a **fresh** GitHub repo (force-pushed-away objects remain
  fetchable) and requires re-pinning the content repo's submodule.

Before flipping public: re-run the grep audit (names/emails/companies) over
blobs _and_ messages as a final check.
