"""Pure-function tests for the TheirStack enrichment stage: raw-row→Posting
mapping (with the stranger-company guard) and census row selection/ordering."""

from referral_prioritizer.enrich import _select, _to_postings


def _raw(company="Acme", pid=1, title="Engineer"):
    return {
        "id": pid,
        "company": company,
        "job_title": title,
        "location": "Anytown, ST",
        "description": "Do things.",
        "url": f"https://www.linkedin.com/jobs/view/{pid}/",
    }


def test_to_postings_maps_and_guards():
    rows = [_raw(pid=1), _raw(pid=2, company="Acme Staffing"), _raw(pid=3)]
    postings, strangers = _to_postings(rows, "acme")  # case-insensitive
    assert [p.id for p in postings] == ["1", "3"]
    assert strangers == 1
    p = postings[0]
    assert p.title == "Engineer"
    assert p.location == "Anytown, ST"
    assert p.content_text == "Do things."
    assert p.url == "https://www.linkedin.com/jobs/view/1/"


def _row(company, kind="custom", conn=1, count="10"):
    return {
        "company": company,
        "board_kind": kind,
        "n_connections": str(conn),
        "n_postings_theirstack": count,
    }


def test_select_filters_and_orders():
    rows = [
        _row("BigCorp", count="4000"),  # over --max-postings
        _row("Native Co", kind="ashby"),  # scannable -> not tail
        _row("Zero Co", count="0"),
        _row("Unswept Co", count=""),
        _row("Stealth Startup", count="229"),
        _row("Cheap Co", conn=1, count="5"),
        _row("Pricey Co", conn=1, count="200"),
        _row("Connected Co", conn=2, count="150"),
    ]
    got = _select(rows, 1000, exclude=["stealth"], only=None)
    # 2-connection first, then 1-connection cheapest-first.
    assert [r["company"] for r in got] == ["Connected Co", "Cheap Co", "Pricey Co"]
    assert [r["company"] for r in _select(rows, 1000, [], "pricey")] == ["Pricey Co"]
