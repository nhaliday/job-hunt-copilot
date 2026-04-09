"""Verify that each ## heading in the Markdown source has a corresponding
horizontal separator line in the rendered PDF.

WeasyPrint renders CSS borders as filled rectangles. A `border-bottom: Xpt`
on an h2 produces a pair of full-width rects at the same top position: one for
the content box and one Xpt taller including the border. We detect h2 borders
by grouping full-width rects by top position and counting groups (excluding the
header top-border group near the page top).

Usage: verify_lines.py <pdf> <md>
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
                if top < HEADER_Y_THRESHOLD:
                    continue
                merged = False
                for g in groups:
                    if abs(top - g) < 2:
                        merged = True
                        break
                if not merged:
                    groups.append(top)
    return len(groups)


def verify(pdf_path: Path, md_path: Path) -> bool:
    expected = count_md_h2(md_path)
    actual = count_h2_borders(pdf_path)
    ok = actual == expected
    status = "OK" if ok else "FAIL"
    print(f"    separators: {status} (expected {expected}, found {actual})")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <pdf> <md>", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if verify(Path(sys.argv[1]), Path(sys.argv[2])) else 1)
