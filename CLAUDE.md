# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build

```bash
uv sync                  # first time: create .venv, install weasyprint + pdfplumber
./build.sh               # build all resumes (incremental, parallel)
./build.sh DIR OUT       # custom input/output dirs (defaults: resumes/ → _output/)
```

External dependencies (not managed by uv): `pandoc` (3.x), `pdftotext` (poppler).

Single resume, manually:
```bash
pandoc resumes/resume-teaching.md --lua-filter=filter.lua --template=template.html --css=style.css -o _output/resume-teaching.html
.venv/bin/python fit.py _output/resume-teaching.html _output/resume-teaching.pdf
.venv/bin/python verify_lines.py _output/resume-teaching.pdf resumes/resume-teaching.md
```

## Pipeline

1. **Pandoc** converts `resumes/*.md` → intermediate HTML via `filter.lua` (Lua AST filter) and `template.html`, linking `style.css`
2. **fit.py** binary-searches font size (10–12pt) to fit exactly 1 page, renders PDF via WeasyPrint
3. **smoke_test** (in build.sh) runs `pdftotext` checks for ATS readability: section headers, name, email, bullet markers, title/date alignment
4. **verify_lines.py** uses pdfplumber to confirm h2 separator lines rendered (counts full-width rects vs `##` headings in source)

Builds are incremental (mtime of PDF vs source + shared deps) and parallel (background jobs with mkdir-based stdout mutex).

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

***Organization Name*** [City, ST]{.location}

- Bullet point (rendered with ♦ marker)
```

- `[...]{.date}` and `[...]{.location}` are Pandoc span syntax, consumed by `filter.lua` to produce two-column HTML tables
- H2 = centered uppercase section headers with double-rule border
- H3 + following org/location paragraph = one entry, transformed into table rows by the Lua filter

## Key Details

- Python 3.14 pinned via `.python-version`; use `uv` for dependency management
- `style.css` imports EB Garamond from Google Fonts (network required on first build)
- Page size is A4 with 11mm/15mm margins
- `_output/` is gitignored; `sample/` has committed reference output
