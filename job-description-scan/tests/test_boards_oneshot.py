"""Fixture-replay tests for the one-shot board clients (greenhouse, ashby,
lever): the full listing arrives in one request; fetch_postings is derived
from the walk via fetch_by_walk.

Synthetic payloads mirror the real response shapes (placeholder names only —
this repo holds no personal data). respx intercepts all HTTP: no network.
"""

import httpx
import pytest
import respx

from job_description_scan.boards import Posting, StaticClient
from job_description_scan.boards.ashby import AshbyClient
from job_description_scan.boards.greenhouse import GreenhouseClient
from job_description_scan.boards.lever import LeverClient

_GH = dict(host="boards-api.greenhouse.io", path="/v1/boards/acme/jobs")


def _gh_jobs(*jobs) -> httpx.Response:
    return httpx.Response(200, json={"jobs": list(jobs)})


@respx.mock(assert_all_called=False)
def test_greenhouse_parse(respx_mock):
    respx_mock.get(**_GH).mock(
        return_value=_gh_jobs(
            {
                "id": 101,
                "title": "Software Engineer",
                "location": {"name": "Anytown, ST"},
                "offices": [
                    {"name": "Anytown", "location": "Anytown, ST"},
                    {"name": "Remote", "location": None},
                ],
                # Greenhouse serves HTML-escaped HTML; strip_html must
                # unescape before stripping tags.
                "content": "&lt;p&gt;Build &amp; ship.&lt;/p&gt;",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            }
        )
    )
    (p,) = GreenhouseClient("acme").iter_postings()
    assert p.id == "101"
    assert p.title == "Software Engineer"
    assert p.location == "Anytown, ST | Anytown | Remote"
    assert p.content_text == "Build & ship."
    assert p.url == "https://boards.greenhouse.io/acme/jobs/101"


@respx.mock(assert_all_called=False)
def test_greenhouse_wrong_slug_fails_loud(respx_mock):
    # A wrong slug answers 200 with an error body, not a jobs list; the client
    # indexes ["jobs"] directly so this fails instead of scanning 0 postings.
    respx_mock.get(**_GH).mock(
        return_value=httpx.Response(200, json={"status": 404, "error": "not found"})
    )
    with pytest.raises(KeyError):
        list(GreenhouseClient("acme").iter_postings())


@respx.mock(assert_all_called=False)
def test_greenhouse_fetch_postings_is_filtered_walk(respx_mock):
    route = respx_mock.get(**_GH).mock(
        return_value=_gh_jobs(
            {"id": 101, "title": "A", "content": "x"},
            {"id": 102, "title": "B", "content": "y"},
        )
    )
    got = list(GreenhouseClient("acme").fetch_postings(["102", "999"]))
    assert [p.id for p in got] == ["102"]  # unknown id absent, not an error
    assert route.call_count == 1


def test_static_client():
    def p(i):
        return Posting(
            id=i, title=f"T{i}", location="X", content_text="c", url="", raw={}
        )

    client = StaticClient([p("1"), p("2")])
    assert [x.id for x in client.iter_postings()] == ["1", "2"]
    assert [x.id for x in client.fetch_postings(["2", "9"])] == ["2"]
    assert set(client.index()) == {"1", "2"}


@respx.mock(assert_all_called=False)
def test_ashby_parse(respx_mock):
    respx_mock.get(host="api.ashbyhq.com", path="/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "uuid-1",
                        "title": "Platform Engineer",
                        "location": "Anytown Hub",
                        "address": {
                            "postalAddress": {"addressCountry": "United States"}
                        },
                        "secondaryLocations": [
                            {
                                "location": "Remote - Canada",
                                "address": {
                                    "postalAddress": {"addressCountry": "Canada"}
                                },
                            }
                        ],
                        "descriptionPlain": "Own the platform.",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/uuid-1",
                    },
                    # Ashby documents every per-job field as optional.
                    {"id": "uuid-2", "title": "Minimal Role"},
                ]
            },
        )
    )
    full, minimal = AshbyClient("acme").iter_postings()
    assert full.location == "Anytown Hub | United States | Remote - Canada | Canada"
    assert full.content_text == "Own the platform."
    assert minimal.location == ""
    assert minimal.content_text == ""
    assert minimal.url == ""


@respx.mock(assert_all_called=False)
def test_lever_parse(respx_mock):
    respx_mock.get(host="api.lever.co", path="/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[  # bare list, no {"jobs": ...} wrapper
                {
                    "id": "ab12",
                    "text": "Backend Engineer",
                    "categories": {"location": "Anytown, ST"},
                    "country": "US",
                    "descriptionPlain": "About the role.",
                    "lists": [{"text": "Benefits", "content": "<li>Snacks</li>"}],
                    "additionalPlain": "Extra note.",
                    "hostedUrl": "https://jobs.lever.co/acme/ab12",
                },
                {
                    "id": "cd34",
                    "text": "No Country Role",
                    "categories": {"location": "Somewhere"},
                    "country": None,  # documented-nullable → no bracket tag
                },
            ],
        )
    )
    tagged, untagged = LeverClient("acme").iter_postings()
    assert tagged.location == "Anytown, ST [US]"
    assert tagged.content_text == "About the role.\n\nBenefits\nSnacks\n\nExtra note."
    assert untagged.location == "Somewhere"
    assert untagged.content_text == ""
