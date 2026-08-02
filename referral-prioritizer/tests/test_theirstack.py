"""Pure-function tests for the TheirStack census sweep: board-hint
fingerprinting and row selection. The HTTP layer is thin and per-row
isolated, so no request mocking is needed here.
"""

from referral_prioritizer.theirstack import _board_hint, _select


def test_hint_native_kinds():
    assert (
        _board_hint(["https://boards.greenhouse.io/acme/jobs/1"]) == "greenhouse:acme"
    )
    assert (
        _board_hint(["https://job-boards.greenhouse.io/acme/jobs/1"])
        == "greenhouse:acme"
    )
    assert _board_hint(["https://jobs.ashbyhq.com/acme/uuid-1"]) == "ashby:acme"
    assert _board_hint(["https://jobs.lever.co/acme/ab12"]) == "lever:acme"
    assert (
        _board_hint(["https://jobs.smartrecruiters.com/acme/123"])
        == "smartrecruiters:acme"
    )


def test_hint_workday_skips_locale_segment():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers/job/City/Eng_1"
    assert _board_hint([url]) == "workday:acme.wd5/Acme_Careers"


def test_hint_unknown_host_falls_back_to_host():
    assert (
        _board_hint(["https://careers.acme.example/jobs/1"]) == "careers.acme.example"
    )


def test_hint_majority_wins_and_empty_handled():
    urls = [
        "https://jobs.ashbyhq.com/acme/1",
        "https://jobs.ashbyhq.com/acme/2",
        "https://careers.acme.example/3",
        "",
    ]
    assert _board_hint(urls) == "ashby:acme"
    assert _board_hint([]) == ""
    assert _board_hint(["", None and "" or ""]) == ""


def _row(company="Acme", kind="custom", count=""):
    return {"company": company, "board_kind": kind, "n_postings_theirstack": count}


def test_select_filters_kinds_only_and_resume():
    rows = [
        _row("Acme", "custom"),
        _row("Beta", "greenhouse"),
        _row("Gamma", ""),  # empty kind counts as "none"
        _row("Delta", "custom", count="12"),  # already swept -> skipped
        {"company": "", "board_kind": "custom", "n_postings_theirstack": ""},
    ]
    assert [r["company"] for r in _select(rows, {"custom", "none"}, None, False)] == [
        "Acme",
        "Gamma",
    ]
    assert [r["company"] for r in _select(rows, None, "bet", False)] == ["Beta"]
    assert [r["company"] for r in _select(rows, {"custom"}, None, True)] == [
        "Acme",
        "Delta",
    ]
