"""write_summary contract: the summary covers every board it is given,
populated from whatever artifacts exist — a board with no artifact still
gets its census row (blank counts). main() passes the UNFILTERED board
list, so an --only run must never truncate other companies' rows (the
regression that once collapsed summary.csv to a single row).
"""

import csv
import json

from job_description_scan.config import Ladder

from referral_prioritizer.scan import Board, write_summary

_CSV = """company,n_connections,board_kind,board_slug,n_postings_located,n_postings_theirstack
Acme,5,greenhouse,acme,10,
Beta,2,ashby,beta,4,
Gamma Fund,1,custom,,,42
Delta,1,none,,,7
Emptied,1,lever,emptied,0,
"""


def _result_row(pid, role, tier):
    return {
        "posting": {"id": pid, "title": f"T{pid}", "location": "X", "url": ""},
        "result": {
            "extraction": {"role": role},
            "comparison": {"fit_tier": tier},
        },
    }


def test_summary_covers_all_boards_even_without_artifacts(tmp_path):
    companies = tmp_path / "companies.csv"
    companies.write_text(_CSV)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    boards = [
        Board(kind="greenhouse", slug="acme", label="Acme"),
        Board(kind="ashby", slug="beta", label="Beta"),
    ]
    # Only Acme has been scanned (the --only case).
    with open(out_dir / f"{boards[0].name}.jsonl", "w") as f:
        f.write(json.dumps(_result_row("1", "swe", "strong")) + "\n")
        f.write(json.dumps(_result_row("2", "swe", "blocked")) + "\n")

    # "Emptied" is scannable but 0-located and NOT in the boards list — its
    # historical artifact must still earn a summary row.
    emptied = Board(kind="lever", slug="emptied", label="Emptied")
    with open(out_dir / f"{emptied.name}.jsonl", "w") as f:
        f.write(json.dumps(_result_row("9", "swe", "stretch")) + "\n")

    path = write_summary(
        companies, boards, [Ladder(roles=("swe",), label="swe")], out_dir
    )
    rows = {r["company"]: r for r in csv.DictReader(open(path))}

    assert set(rows) == {"Acme", "Beta", "Emptied"}
    assert rows["Acme"]["swe_strong"] == "1"
    assert rows["Acme"]["swe_blocked"] == "1"
    assert rows["Acme"]["scan_source"] == "native"
    assert rows["Beta"]["n_scanned"] == ""  # blank counts, not dropped
    assert rows["Beta"]["n_located"] == "4"
    assert rows["Emptied"]["swe_stretch"] == "1"  # artifact data preserved
    assert rows["Emptied"]["n_located"] == "0"


def test_summary_includes_theirstack_enrichment_rows(tmp_path):
    companies = tmp_path / "companies.csv"
    companies.write_text(_CSV)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    boards = [Board(kind="greenhouse", slug="acme", label="Acme")]
    # Gamma Fund has an enrichment artifact; Delta does not.
    name = Board(kind="theirstack", slug="Gamma Fund", label="Gamma Fund").name
    with open(out_dir / f"{name}.jsonl", "w") as f:
        f.write(json.dumps(_result_row("9", "swe", "stretch")) + "\n")

    path = write_summary(
        companies, boards, [Ladder(roles=("swe",), label="swe")], out_dir
    )
    rows = {r["company"]: r for r in csv.DictReader(open(path))}

    # Delta (unscannable, no artifact) gets no row; scannable Beta/Emptied
    # get blank native rows under the membership rule even when unscanned.
    assert set(rows) == {"Acme", "Beta", "Emptied", "Gamma Fund"}
    g = rows["Gamma Fund"]
    assert g["scan_source"] == "theirstack"
    assert g["board_kind"] == "custom"  # census provenance kept, not faked
    assert g["n_located"] == "42"  # the theirstack in-window count
    assert g["swe_stretch"] == "1"
