"""Warn (without failing) if the rendered PDF exceeds 1 page.

Usage: verify_pages.py <pdf>
"""

import sys
from pathlib import Path

import pdfplumber


def verify(pdf_path: Path) -> bool:
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
    ok = n == 1
    status = "OK" if ok else "WARN"
    print(f"    pages: {status} (expected 1, found {n})")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <pdf>", file=sys.stderr)
        sys.exit(2)
    verify(Path(sys.argv[1]))
    sys.exit(0)
