"""Pure-function tests for the provider liveness panel: URL->id extraction
per board kind, and the offline window-curve computation on synthetic rows
with planted dates and liveness."""

from datetime import datetime, timedelta, timezone

from referral_prioritizer.provider_panel import (
    compute_curves,
    native_key,
    provider_key,
)

_UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def test_provider_key_greenhouse():
    assert (
        provider_key("greenhouse", "https://boards.greenhouse.io/acme/jobs/123456")
        == "123456"
    )
    assert (
        provider_key("greenhouse", "https://acme.example/careers?gh_jid=987654")
        == "987654"
    )
    assert provider_key("greenhouse", "https://www.linkedin.com/jobs/view/1") is None


def test_provider_key_uuid_kinds():
    assert provider_key("ashby", f"https://jobs.ashbyhq.com/acme/{_UUID}") == _UUID
    assert provider_key("lever", f"https://jobs.lever.co/acme/{_UUID.upper()}") == _UUID
    assert provider_key("ashby", "https://jobs.ashbyhq.com/acme") is None


def test_provider_key_workday_requisition():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/City/Engineer_R-004242"
    assert provider_key("workday", url) == "R-004242"
    # Custom career-site skins keep the requisition token too.
    assert (
        provider_key("workday", "https://careers.acme.example/jobs/R-004242/eng")
        == "R-004242"
    )


def test_native_key_alignment():
    assert native_key("workday", "/job/City/Engineer_R-004242") == "R-004242"
    assert native_key("ashby", _UUID.upper()) == _UUID
    assert native_key("greenhouse", "123456") == "123456"


def _row(uuid, days_old, closed=False):
    stamp = (
        datetime(2026, 8, 1, tzinfo=timezone.utc) - timedelta(days=days_old)
    ).isoformat()
    return {
        "final_url": f"https://jobs.ashbyhq.com/acme/{uuid}"
        if uuid
        else "https://www.linkedin.com/x",
        "date_posted": stamp,
        "discovered_at": stamp,
        "is_closed": closed,
    }


def test_compute_curves_planted():
    pulled_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    a, b, c = (
        "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12,
        "1" * 8 + "-" + "2" * 4 + "-" + "3" * 4 + "-" + "4" * 4 + "-" + "5" * 12,
        "9" * 8 + "-" + "8" * 4 + "-" + "7" * 4 + "-" + "6" * 4 + "-" + "5" * 12,
    )
    native = [a, b]  # two live postings
    rows = [
        _row(a, days_old=5),  # live, recent -> matched in every window
        _row(b, days_old=45),  # live, older -> matched only at >=60d
        _row(c, days_old=5),  # stale (not on board)
        _row(None, days_old=5),  # unattributable (linkedin)
        _row(c, days_old=5, closed=True),  # stale AND known-closed
    ]
    curves = {
        (r["anchor"], r["filter"], r["window_days"]): r
        for r in compute_curves("ashby", native, "theirstack", rows, pulled_at)
    }
    r7 = curves[("date_posted", "all", 7)]
    assert (r7["n_matched"], r7["recall"]) == (1, 0.5)
    assert r7["n_provider"] == 4 and r7["n_unattributable"] == 1
    r90 = curves[("date_posted", "all", 90)]
    assert (r90["n_matched"], r90["recall"]) == (2, 1.0)
    # the open-only filter drops the known-closed stale row
    assert curves[("date_posted", "open", 7)]["n_provider"] == 3
    # precision at 7d/all: 1 matched of 4 in-window
    assert r7["precision"] == 0.25
