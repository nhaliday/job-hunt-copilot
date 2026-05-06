# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Build

```bash
uv sync                  # first time: create .venv, install weasyprint + pdfplumber
./build.sh               # build all resumes and letters (incremental, parallel)
./build.sh OUT           # custom output root (default: _output/)
```

External dependencies (not managed by uv): `pandoc` (3.x), `pdftotext`
(poppler).

Outputs land in `_output/resumes/<name>.pdf` and `_output/letters/<name>.pdf`.

Single resume, manually:

```bash
pandoc resumes/resume-teaching.md --lua-filter=filter.lua --template=template.html --css=style.css -o _output/resumes/resume-teaching.html
.venv/bin/python fit.py _output/resumes/resume-teaching.html _output/resumes/resume-teaching.pdf
.venv/bin/python verify_lines.py _output/resumes/resume-teaching.pdf resumes/resume-teaching.md
```

## Pipeline

1. **Pandoc** converts `resumes/*.md` and `letters/*.md` → intermediate HTML,
   using per-doc-type template + CSS (and `filter.lua` for resumes only)
2. **fit.py** binary-searches font size (10–12pt) to fit exactly 1 page, renders
   PDF via WeasyPrint
3. **post_build** runs after each PDF (or on cached PDFs that didn't need
   rebuilding):
   - **all**: `verify_pages.py` (warns if PDF exceeds 1 page; never fails the
     build)
   - **resume**: `smoke_test` (pdftotext checks for ATS readability — section
     headers, name, email, bullet markers, title/date alignment) +
     `verify_lines.py` (pdfplumber confirms h2 separator lines; this one DOES
     fail the build on mismatch)
   - **letter**: no extra checks beyond the page count (cover letters aren't
     ATS-filtered)

Builds are incremental (mtime of PDF vs source + per-type deps) and parallel
(background jobs with mkdir-based stdout mutex). Post-build checks run on cached
PDFs too, so editing `verify_pages.py`/`verify_lines.py`/the smoke test re-runs
on the next `./build.sh` without forcing a rebuild. Variants (via
`*.variants.toml` + `render_variants.py`) currently only used for resumes.

## Doc Types

| Type   | Source dir | Template               | CSS          | Filter       | Page size | Output                  |
| ------ | ---------- | ---------------------- | ------------ | ------------ | --------- | ----------------------- |
| resume | `resumes/` | `template.html`        | `style.css`  | `filter.lua` | A4        | `_output/resumes/*.pdf` |
| letter | `letters/` | `template-letter.html` | `letter.css` | (none)       | US Letter | `_output/letters/*.pdf` |

## Markdown Resume Format

```markdown
---
name: Jane Doe
email: jane.doe@example.com
phone: 555-123-4567
subtitle: Role-specific tagline
---

Optional intro paragraph.

## Section Name

### Job Title [Month YYYY – Month YYYY]{.date}

**_Organization Name_** [City, ST]{.location}

- Bullet point (rendered with ♦ marker)
```

- `[...]{.date}` and `[...]{.location}` are Pandoc span syntax, consumed by
  `filter.lua` to produce two-column HTML tables
- H2 = centered uppercase section headers with double-rule border
- H3 + following org/location paragraph = one entry, transformed into table rows
  by the Lua filter

## Markdown Cover Letter Format

```markdown
---
name: Jane Doe
email: jane.doe@example.com
phone: 555-123-4567
location: Washington DC Metro Area
date: May 5, 2026
---

Recipient Line One\
Recipient Line Two\
Recipient Line Three

Dear Hiring Manager,

Body paragraphs.

Sincerely,\
Jane Doe
```

- Frontmatter renders a small top-left letterhead (name + contact line) and the
  date
- Recipient block, salutation, body, and sign-off live in the body
- Use trailing `\` for hard line breaks (recipient block, signature) —
  `template-letter.html` does not consume Pandoc spans

## Key Details

- Python 3.14 pinned via `.python-version`; use `uv` for dependency management
- Both CSS files import EB Garamond from Google Fonts (network required on first
  build)
- Resumes: A4, 11mm/15mm margins. Letters: US Letter, 1in margins
- `_output/` is gitignored; `sample/` has committed reference output
