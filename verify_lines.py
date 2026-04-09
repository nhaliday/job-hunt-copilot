"""Verify that each ## heading in the Markdown source has a corresponding
horizontal separator line in the rendered PDF.

WeasyPrint renders CSS borders as filled rectangles. A `border-bottom: Xpt solid`
on an h2 produces a pair of full-width rects at the same top position: one for
the content box and one Xpt taller including the border. We detect h2 borders
by grouping full-width rects by top position and counting groups (excluding the
header top-border group near the page top).
"""

import re
import sys
from pathlib import Path

import pdfplumber

HEADER_Y_THRESHOLD = 100  # header border is near top of page


def count_md_h2(md_path: Path) -> int:
    text = md_path.read_text()
    return len(re.findall(r"^## ", text, re.MULTILINE))


def count_h2_borders(pdf_path: Path, min_width: float = 400) -> int:
    """Count groups of full-width rects that represent h2 borders."""
    groups: list[float] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for rect in page.rects:
                w = abs(rect["x1"] - rect["x0"])
                if w < min_width:
                    continue
                top = rect["top"]
                # Skip header border (near top of page)
                if top < HEADER_Y_THRESHOLD:
                    continue
                # Group rects within 2pt of each other
                merged = False
                for i, g in enumerate(groups):
                    if abs(top - g) < 2:
                        merged = True
                        break
                if not merged:
                    groups.append(top)
    return len(groups)


def main():
    resumes_dir = Path(__file__).parent / "resumes"
    output_dir = Path(__file__).parent / "_output"
    ok = True

    for md in sorted(resumes_dir.glob("*.md")):
        pdf = output_dir / f"{md.stem}.pdf"
        if not pdf.exists():
            print(f"  {md.stem}: SKIP (no PDF)")
            continue

        expected = count_md_h2(md)
        actual = count_h2_borders(pdf)

        status = "OK" if actual == expected else "FAIL"
        if actual != expected:
            ok = False
        print(f"  {md.stem}: {status} (expected {expected} h2 borders, found {actual})")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
