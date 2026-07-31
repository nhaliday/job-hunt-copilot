"""Fixture-replay tests for the list-then-detail board clients (workday,
smartrecruiters, phenom): paginated list + one detail request per posting.

These pin the edge handling live APIs can't serve on demand: pagination
termination and dedupe, delisted postings (workday/smartrecruiters: 404;
phenom: 200 with the "job" key absent), the empty-board fail-loud sentinels,
transient-error retry, and the location_filter pushdown that skips detail
requests. Synthetic payloads, placeholder names, no network.
"""

import json
import re

import httpx
import pytest
import respx

from job_description_scan.boards import workday as workday_module
from job_description_scan.boards.phenom import PhenomClient
from job_description_scan.boards.smartrecruiters import SmartRecruitersClient
from job_description_scan.boards.workday import WorkdayClient

# --------------------------------------------------------------------------- #
# workday
# --------------------------------------------------------------------------- #
_WD_HOST = "acme.wd5.myworkdayjobs.com"
_WD_LIST = "/wday/cxs/acme/Acme_Careers/jobs"


def _wd_client(location_filter=None) -> WorkdayClient:
    return WorkdayClient("acme.wd5/Acme_Careers", location_filter)


def _wd_pages(rows: list[dict], total: int) -> list[httpx.Response]:
    # Page 0 with rows, then an empty page: the walk terminates on
    # empty/no-new pages, never on the reported total.
    return [
        httpx.Response(200, json={"total": total, "jobPostings": rows}),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),
    ]


def _wd_row(path: str, loc: str, title: str = "Engineer") -> dict:
    return {"externalPath": path, "locationsText": loc, "title": title}


def _wd_detail(**info) -> httpx.Response:
    return httpx.Response(200, json={"jobPostingInfo": info})


@respx.mock(assert_all_called=False)
def test_workday_pagination_dedupe_and_warnings(respx_mock, capsys):
    row_a = _wd_row("/job/A/Eng_1", "Anytown, ST")
    rows = [row_a, _wd_row("/job/B/Eng_2", "Elsewhere"), row_a, {"bulletFields": ["x"]}]
    respx_mock.post(host=_WD_HOST, path=_WD_LIST).mock(side_effect=_wd_pages(rows, 99))
    for path in ("/job/A/Eng_1", "/job/B/Eng_2"):
        respx_mock.get(host=_WD_HOST, path=f"{_WD_LIST[: -len('/jobs')]}{path}").mock(
            return_value=_wd_detail(jobDescription="<p>Work.</p>")
        )
    postings = list(_wd_client().iter_postings())
    assert [p.id for p in postings] == ["/job/A/Eng_1", "/job/B/Eng_2"]
    out = capsys.readouterr().out
    assert "skipped 1 list rows without externalPath" in out
    assert "collected 2 rows vs page-0 total 99" in out


@respx.mock(assert_all_called=False)
def test_workday_filter_pushdown(respx_mock):
    rows = [
        _wd_row("/job/A/Eng_1", "Anytown, ST"),
        _wd_row("/job/B/Eng_2", "Faraway City"),
        _wd_row("/job/C/Eng_3", "2 Locations"),  # aggregate → detail anyway
    ]
    respx_mock.post(host=_WD_HOST, path=_WD_LIST).mock(side_effect=_wd_pages(rows, 3))
    base = _WD_LIST[: -len("/jobs")]
    detail_a = respx_mock.get(host=_WD_HOST, path=f"{base}/job/A/Eng_1").mock(
        return_value=_wd_detail(location="Anytown, ST", jobDescription="<p>A.</p>")
    )
    detail_b = respx_mock.get(host=_WD_HOST, path=f"{base}/job/B/Eng_2").mock(
        return_value=_wd_detail(jobDescription="<p>B.</p>")
    )
    detail_c = respx_mock.get(host=_WD_HOST, path=f"{base}/job/C/Eng_3").mock(
        return_value=_wd_detail(
            location="Anytown, ST",
            additionalLocations=["Other Town, ST"],
            jobDescription="<p>C.</p>",
        )
    )
    by_id = {p.id: p for p in _wd_client(re.compile("Anytown")).iter_postings()}
    assert by_id["/job/B/Eng_2"].content_text == ""  # skipped, still yielded
    assert not detail_b.called
    assert detail_a.called
    assert detail_c.called  # aggregate row resolved despite filter mismatch
    assert by_id["/job/C/Eng_3"].location == "Anytown, ST | Other Town, ST"


@respx.mock(assert_all_called=False)
def test_workday_detail_parse(respx_mock):
    rows = [
        _wd_row("/job/A/Eng_1", "Anytown, ST", title="Row Title"),
        _wd_row("/job/B/Eng_2", "Anytown, ST"),
    ]
    respx_mock.post(host=_WD_HOST, path=_WD_LIST).mock(side_effect=_wd_pages(rows, 2))
    base = _WD_LIST[: -len("/jobs")]
    respx_mock.get(host=_WD_HOST, path=f"{base}/job/A/Eng_1").mock(
        return_value=_wd_detail(
            # no title → falls back to the list row's
            location="Anytown, ST",
            additionalLocations=["Remote US", "Anytown, ST"],
            country={"descriptor": "United States"},
            jobDescription="<p>Do <b>things</b>.</p>",
            externalUrl="https://acme.example/apply/1",
        )
    )
    respx_mock.get(host=_WD_HOST, path=f"{base}/job/B/Eng_2").mock(
        return_value=_wd_detail(title="Detail Title", jobDescription="<p>B.</p>")
        # no externalUrl → falls back to the hosted url
    )
    a, b = _wd_client().iter_postings()
    assert a.title == "Row Title"
    assert a.location == "Anytown, ST | Remote US | United States"  # deduped
    assert a.content_text == "Do things."
    assert a.url == "https://acme.example/apply/1"
    assert b.title == "Detail Title"
    assert b.url == f"https://{_WD_HOST}/Acme_Careers/job/B/Eng_2"


@respx.mock(assert_all_called=False)
def test_workday_delisted_skipped_loudly(respx_mock, capsys):
    rows = [_wd_row("/job/A/Eng_1", "Anytown"), _wd_row("/job/B/Eng_2", "Anytown")]
    respx_mock.post(host=_WD_HOST, path=_WD_LIST).mock(side_effect=_wd_pages(rows, 2))
    base = _WD_LIST[: -len("/jobs")]
    respx_mock.get(host=_WD_HOST, path=f"{base}/job/A/Eng_1").mock(
        return_value=httpx.Response(404)
    )
    respx_mock.get(host=_WD_HOST, path=f"{base}/job/B/Eng_2").mock(
        return_value=_wd_detail(jobDescription="<p>B.</p>")
    )
    assert [p.id for p in _wd_client().iter_postings()] == ["/job/B/Eng_2"]
    targeted = list(_wd_client().fetch_postings(["/job/A/Eng_1", "/job/B/Eng_2"]))
    assert [p.id for p in targeted] == ["/job/B/Eng_2"]
    assert capsys.readouterr().out.count("workday: skipping /job/A/Eng_1") == 2


@respx.mock(assert_all_called=False)
def test_workday_transient_error_retried(respx_mock, monkeypatch):
    monkeypatch.setattr(workday_module.time, "sleep", lambda s: None)
    rows = [_wd_row("/job/A/Eng_1", "Anytown")]
    respx_mock.post(host=_WD_HOST, path=_WD_LIST).mock(side_effect=_wd_pages(rows, 1))
    detail = respx_mock.get(
        host=_WD_HOST, path=f"{_WD_LIST[: -len('/jobs')]}/job/A/Eng_1"
    ).mock(
        side_effect=[
            httpx.Response(502),
            _wd_detail(jobDescription="<p>Recovered.</p>"),
        ]
    )
    (p,) = _wd_client().iter_postings()
    assert p.content_text == "Recovered."
    assert detail.call_count == 2


# --------------------------------------------------------------------------- #
# smartrecruiters
# --------------------------------------------------------------------------- #
_SR_HOST = "api.smartrecruiters.com"
_SR_LIST = "/v1/companies/acme/postings"
_SR_LOCATION = {
    "fullLocation": "Anytown, State, United States",
    "country": "us",
}
_SR_DETAIL = {
    "id": 744000001,
    "name": "Data Engineer",
    "location": _SR_LOCATION,
    "jobAd": {
        "sections": {
            "companyDescription": {"text": "<p>About acme.</p>"},
            "jobDescription": {"text": "<p>Do data.</p>"},
            "qualifications": {"text": "<p>SQL</p>"},
            "additionalInformation": {"text": "<p>EEO</p>"},
            "videos": {"text": "<p>watch this</p>"},  # excluded from content
        }
    },
    "postingUrl": "https://jobs.smartrecruiters.com/acme/744000001",
}


@respx.mock(assert_all_called=False)
def test_smartrecruiters_empty_board_fails_loud(respx_mock):
    # A wrong or API-disabled identifier is 200 + totalFound 0, not a 404.
    respx_mock.get(host=_SR_HOST, path=_SR_LIST).mock(
        return_value=httpx.Response(200, json={"totalFound": 0, "content": []})
    )
    with pytest.raises(ValueError, match="returned 0 postings"):
        list(SmartRecruitersClient("acme").iter_postings())


@respx.mock(assert_all_called=False)
def test_smartrecruiters_parse(respx_mock):
    list_route = respx_mock.get(host=_SR_HOST, path=_SR_LIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {"id": 744000001, "name": "Data Engineer", "location": _SR_LOCATION}
                ],
            },
        )
    )
    respx_mock.get(host=_SR_HOST, path=f"{_SR_LIST}/744000001").mock(
        return_value=httpx.Response(200, json=_SR_DETAIL)
    )
    (p,) = SmartRecruitersClient("acme").iter_postings()
    assert p.location == "Anytown, State, United States [US]"
    assert p.content_text == "About acme.\n\nDo data.\n\nSQL\n\nEEO"
    assert p.url == "https://jobs.smartrecruiters.com/acme/744000001"
    assert list_route.call_count == 1  # offset 100 >= totalFound 1 → done


@respx.mock(assert_all_called=False)
def test_smartrecruiters_targeted_fetch_and_delisted(respx_mock, capsys):
    respx_mock.get(host=_SR_HOST, path=f"{_SR_LIST}/744000001").mock(
        return_value=httpx.Response(200, json=_SR_DETAIL)
    )
    respx_mock.get(host=_SR_HOST, path=f"{_SR_LIST}/744000404").mock(
        return_value=httpx.Response(404)
    )
    got = list(
        SmartRecruitersClient("acme").fetch_postings(["744000001", "744000404"])
    )
    assert [p.id for p in got] == ["744000001"]
    # No list row available: location must come from the detail response.
    assert got[0].location == "Anytown, State, United States [US]"
    assert "smartrecruiters: skipping 744000404" in capsys.readouterr().out


@respx.mock(assert_all_called=False)
def test_smartrecruiters_filter_pushdown(respx_mock):
    respx_mock.get(host=_SR_HOST, path=_SR_LIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {"id": 744000001, "name": "Data Engineer", "location": _SR_LOCATION}
                ],
            },
        )
    )
    detail = respx_mock.get(host=_SR_HOST, path=f"{_SR_LIST}/744000001")
    (p,) = SmartRecruitersClient("acme", re.compile(r"\[CA\]")).iter_postings()
    assert p.content_text == ""  # skipped, still yielded for the audit trail
    assert not detail.called


# --------------------------------------------------------------------------- #
# phenom
# --------------------------------------------------------------------------- #
_PH_HOST = "careers.acme.org"


def _phenom_handler(jobs: list[dict], details: dict[str, dict]):
    """One /widgets endpoint serves both operations; dispatch on ddoKey.
    `details[id]` missing the "job" key models a delisted posting (Phenom
    answers 200 either way). `handler.seen` records every request body so
    tests can assert which jobDetail calls were (not) made."""
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["ddoKey"] == "refineSearch":
            page = jobs if body["from"] == 0 else []
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "totalHits": len(jobs),
                        "data": {"jobs": page},
                    }
                },
            )
        return httpx.Response(
            200, json={"jobDetail": {"data": details.get(body["jobId"], {})}}
        )

    handle.seen = seen
    return handle


def _ph_detail_calls(handler) -> list[str]:
    return [b["jobId"] for b in handler.seen if b["ddoKey"] == "jobDetail"]


@respx.mock(assert_all_called=False)
def test_phenom_empty_board_fails_loud(respx_mock):
    # A wrong host that still serves /widgets looks like an empty board.
    respx_mock.post(host=_PH_HOST, path="/widgets").mock(
        side_effect=_phenom_handler([], {})
    )
    with pytest.raises(ValueError, match="returned 0 postings"):
        list(PhenomClient(_PH_HOST).iter_postings())


@respx.mock(assert_all_called=False)
def test_phenom_parse(respx_mock):
    handler = _phenom_handler(
        # List rows carry multi_location as bare strings...
        [
            {
                "jobId": "R100",
                "title": "ML Engineer",
                "multi_location": ["Anytown, State, United States"],
            }
        ],
        # ...detail responses as dicts with the string under "location".
        {
            "R100": {
                "job": {
                    "title": "ML Engineer",
                    "multi_location": [
                        {"location": "Anytown, State, United States"},
                        {"location": "Other Town, State, United States"},
                    ],
                    "description": "<p>Train models.</p>",
                }
            }
        },
    )
    respx_mock.post(host=_PH_HOST, path="/widgets").mock(side_effect=handler)
    (p,) = PhenomClient(_PH_HOST).iter_postings()
    assert p.id == "R100"
    assert p.location == (
        "Anytown, State, United States | Other Town, State, United States"
    )
    assert p.content_text == "Train models."
    assert p.url == f"https://{_PH_HOST}/us/en/job/R100"


@respx.mock(assert_all_called=False)
def test_phenom_delisted_skipped_loudly(respx_mock, capsys):
    live = {
        "job": {"title": "ML Engineer", "description": "<p>Live.</p>"}
    }
    handler = _phenom_handler(
        [
            {"jobId": "R100", "title": "ML Engineer", "multi_location": ["A, B, C"]},
            {"jobId": "R404", "title": "Gone Role", "multi_location": ["A, B, C"]},
        ],
        {"R100": live},  # R404 delisted: jobDetail 200 without "job"
    )
    respx_mock.post(host=_PH_HOST, path="/widgets").mock(side_effect=handler)
    # Mid-walk delisting must not abort the board (KeyError, not HTTPError).
    assert [p.id for p in PhenomClient(_PH_HOST).iter_postings()] == ["R100"]
    targeted = list(PhenomClient(_PH_HOST).fetch_postings(["R100", "R404"]))
    assert [p.id for p in targeted] == ["R100"]
    assert capsys.readouterr().out.count("phenom: skipping R404: KeyError") == 2


@respx.mock(assert_all_called=False)
def test_phenom_filter_pushdown(respx_mock):
    handler = _phenom_handler(
        [
            {
                "jobId": "R100",
                "title": "ML Engineer",
                "multi_location": ["Anytown, State, United States"],
            },
            {
                "jobId": "R200",
                "title": "Elsewhere Role",
                "multi_location": ["Elsewhere City, Nowhere"],
            },
        ],
        {"R100": {"job": {"description": "<p>US role.</p>"}}},
    )
    respx_mock.post(host=_PH_HOST, path="/widgets").mock(side_effect=handler)
    client = PhenomClient(_PH_HOST, re.compile("United States"))
    by_id = {p.id: p for p in client.iter_postings()}
    assert by_id["R200"].content_text == ""  # skipped, still yielded
    assert _ph_detail_calls(handler) == ["R100"]  # no jobDetail for R200
