"""Headless TUI smokes: each mode mounts and routes a keypress to the
controller. No pixel assertions — logic lives in the controllers."""

import pytest

from referral_prioritizer.judge_tui import BrowseApp, JudgeApp, render_card


class _StubController:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.answers = []

    def next(self):
        return self.payloads.pop(0) if self.payloads else None

    def answer(self, payload, response):
        self.answers.append((payload["mode"], response))

    def undo(self):
        return None


def _company_card(name="Acme"):
    return {
        "company": name,
        "n_connections": "2",
        "board_kind": "custom",
        "scan_source": "theirstack",
        "referrers": [{"rank": "1", "name": "Jane Doe", "position": "Eng"}],
        "tier_counts": {
            "swe": {"strong": "1", "stretch": "0", "long_shot": "0", "blocked": "0"}
        },
        "role_tops": {"swe": ["Title | https://x/1"]},
    }


def test_window_lines_slides_with_cursor():
    from referral_prioritizer.judge_tui import window_lines

    lines = [f"L{i}" for i in range(100)]
    assert window_lines(lines, 5, 200) == lines  # fits: untouched
    top = window_lines(lines, 0, 10)
    assert top[0] == "L0" and "below" in top[-1] and len(top) == 10
    mid = window_lines(lines, 50, 10)
    assert "above" in mid[0] and "below" in mid[-1] and "L50" in mid
    bottom = window_lines(lines, 99, 10)
    assert bottom[-1] == "L99" and "above" in bottom[0]


def test_render_card_company_and_connection():
    text = render_card(_company_card())
    assert "Acme" in text and "theirstack" in text and "Jane Doe" in text
    conn = render_card(
        {"name": "Jane Doe", "position": "Eng", "url": "u", "connected_on": "2020"}
    )
    assert "Jane Doe" in conn


@pytest.mark.asyncio
async def test_compare_mode_routes_keys():
    ctl = _StubController(
        [
            {
                "mode": "compare",
                "heading": "h",
                "left": _company_card("A"),
                "right": _company_card("B"),
            },
            {
                "mode": "compare",
                "heading": "h",
                "left": _company_card("C"),
                "right": _company_card("D"),
            },
        ]
    )
    app = JudgeApp(ctl)
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.press("t")
    assert ctl.answers == [("compare", "a"), ("compare", "tie")]


@pytest.mark.asyncio
async def test_select_and_tier_modes():
    ctl = _StubController(
        [
            {
                "mode": "select",
                "heading": "h",
                "card": {"company": "Acme"},
                "options": [
                    {"name": "P1", "position": "x"},
                    {"name": "P2", "position": "y"},
                ],
                "_company": "Acme",
            },
            {
                "mode": "tier",
                "heading": "h",
                "card": _company_card(),
                "_company": "Acme",
            },
        ]
    )
    app = JudgeApp(ctl)
    async with app.run_test() as pilot:
        await pilot.press("space")  # select P1
        await pilot.press("j")
        await pilot.press("space")  # select P2
        await pilot.press("enter")
        await pilot.press("2")  # tier B
    assert ctl.answers == [("select", [0, 1]), ("tier", "2")]


@pytest.mark.asyncio
async def test_browse_mode_navigates():
    entries = [
        (
            _company_card("A"),
            {"tier": "A", "rank": 1, "utility": 1.0, "wins": 2, "losses": 0, "ties": 0},
        ),
        (
            _company_card("B"),
            {"tier": "A", "rank": 2, "utility": 0.5, "wins": 1, "losses": 1, "ties": 0},
        ),
    ]
    app = BrowseApp(entries)
    async with app.run_test() as pilot:
        await pilot.press("j")
        assert app.cursor == 1
        await pilot.press("k")
        assert app.cursor == 0
        await pilot.press("q")
