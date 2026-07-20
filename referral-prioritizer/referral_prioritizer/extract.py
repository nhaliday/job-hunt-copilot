"""Extract distinct companies from a LinkedIn connections export.

The export ("Connections.csv" from LinkedIn's data download) starts with a
free-text Notes preamble; the real CSV begins at the "First Name,..." header.
Output is one row per distinct raw company name — no canonicalization (name
variants of one employer stay separate rows; a later normalization pass may
add columns for that) — deterministically ordered so re-runs against an
unchanged export are byte-identical.
"""

import argparse
import io
from pathlib import Path

import polars as pl


def parse_export(path: Path) -> pl.DataFrame:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("First Name,"))
    except StopIteration:
        raise SystemExit(
            f"{path}: no 'First Name,...' header — not a LinkedIn connections export?"
        )
    return pl.read_csv(io.StringIO("\n".join(lines[start:])))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--export", type=Path, required=True, help="LinkedIn Connections.csv"
    )
    ap.add_argument("--out", type=Path, required=True, help="output companies CSV")
    args = ap.parse_args()

    df = parse_export(args.export).with_columns(
        pl.col("Company", "Position").fill_null("").str.strip_chars()
    )
    dropped = df.filter(pl.col("Company") == "").height

    position = pl.col("Position")
    companies = (
        df.filter(pl.col("Company") != "")
        .group_by(pl.col("Company").alias("company"))
        .agg(
            pl.len().alias("n_connections"),
            position.filter(position != "")
            .unique()
            .sort()
            .str.join("; ")
            .alias("positions"),
        )
        .sort(["n_connections", "company"], descending=[True, False])
    )

    companies.write_csv(args.out)
    print(
        f"{args.out}: {companies.height} companies from {df.height} connections"
        f" ({dropped} without a company dropped)"
    )


if __name__ == "__main__":
    main()
