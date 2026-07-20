# CLAUDE.md

Generic job-hunt tooling. Three self-contained subprojects (own
`pyproject.toml`, build scripts where applicable):

- **`resume-printer/`** — Markdown → PDF pipeline for resumes and cover letters.
  See [`resume-printer/CLAUDE.md`](resume-printer/CLAUDE.md).
- **`job-description-scan/`** — job-board scanner (Greenhouse, Ashby, Lever,
  Workday, SmartRecruiters) with LLM-driven structured extraction, resume
  comparison, and a pairwise-ranking second pass.
- **`referral-prioritizer/`** — LinkedIn-connections referral pipeline (company
  extraction, board discovery; ranking stages to come). See
  [`referral-prioritizer/CLAUDE.md`](referral-prioritizer/CLAUDE.md). See
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
