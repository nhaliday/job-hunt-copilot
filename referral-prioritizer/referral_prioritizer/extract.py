"""Extract distinct companies from a LinkedIn connections export.

The export ("Connections.csv" from LinkedIn's data download) starts with a
free-text Notes preamble; the real CSV begins at the "First Name,..." header.
Output is one row per distinct raw company name — no canonicalization (name
variants of one employer stay separate rows; a later normalization pass may
add columns for that) — deterministically ordered so re-runs against an
unchanged export are byte-identical.
"""

import argparse
import collections
import csv
from pathlib import Path


def parse_export(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("First Name,"))
    except StopIteration:
        raise SystemExit(
            f"{path}: no 'First Name,...' header — not a LinkedIn connections export?"
        )
    return list(csv.DictReader(lines[start:]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--export", type=Path, required=True, help="LinkedIn Connections.csv"
    )
    ap.add_argument("--out", type=Path, required=True, help="output companies CSV")
    args = ap.parse_args()

    rows = parse_export(args.export)
    positions_at: dict[str, list[str]] = collections.defaultdict(list)
    dropped = 0
    for r in rows:
        company = (r.get("Company") or "").strip()
        if not company:
            dropped += 1
            continue
        positions_at[company].append((r.get("Position") or "").strip())

    out_rows = [
        {
            "company": company,
            "n_connections": len(positions),
            "positions": "; ".join(sorted({p for p in positions if p})),
        }
        for company, positions in positions_at.items()
    ]
    out_rows.sort(key=lambda r: (-r["n_connections"], r["company"]))

    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["company", "n_connections", "positions"])
        w.writeheader()
        w.writerows(out_rows)
    print(
        f"{args.out}: {len(out_rows)} companies from {len(rows)} connections"
        f" ({dropped} without a company dropped)"
    )


if __name__ == "__main__":
    main()
