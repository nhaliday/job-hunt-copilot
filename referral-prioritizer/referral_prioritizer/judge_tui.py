"""Textual UI for human judging: one card component, four modes.

The app is deliberately dumb: a controller (see judge.py) supplies question
payloads ({"mode": "compare"|"select"|"tier", ...card data...}) one at a
time and receives answers; all log I/O and scheduling stay in the controller,
so the UI is swappable and the logic testable without a terminal. BrowseApp
renders the finished ranking with the same card.

Keys — compare: a/b pick a side, t tie, s skip; select: j/k move, space
toggle, enter commit; tier: 1/2/3 = A/B/C, x exclude, s skip; everywhere:
u undo, q quit (resumable — the log has everything).
"""

from typing import Protocol

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


def render_card(card: dict, extra: dict | None = None) -> str:
    """Rich-markup card for a company (or a connection: has 'name')."""
    lines: list[str] = []
    if "name" in card:  # connection card (stage 1)
        lines.append(f"[b]{card['name']}[/b]")
        if card.get("position"):
            lines.append(card["position"])
        if card.get("connected_on"):
            lines.append(f"[dim]connected {card['connected_on']}[/dim]")
        if card.get("url"):
            lines.append(f"[link='{card['url']}']linkedin[/link]")
        return "\n".join(lines)

    lines.append(f"[b]{card['company']}[/b]")
    src = card.get("scan_source") or "unscanned"
    badge = {"native": "green", "theirstack": "yellow", "unscanned": "dim"}.get(
        src, "dim"
    )
    lines.append(
        f"[{badge}]{src}[/{badge}]  board: {card.get('board_kind') or '?'}"
        f"  connections: {card.get('n_connections') or '?'}"
    )
    if extra:
        lines.append(
            f"[b]#{extra['rank']}[/b]  tier {extra.get('tier', '?')}"
            f"  u={extra['utility']:+.2f}"
            f"  {extra['wins']}W/{extra['losses']}L/{extra['ties']}T"
        )
    if card.get("positions"):
        lines.append(f"[dim]you know: {card['positions'][:100]}[/dim]")
    for ref in card.get("referrers", []):
        lines.append(f"  ref #{ref['rank']}: {ref['name']} — {ref['position'][:60]}")
    for role, counts in (card.get("tier_counts") or {}).items():
        if any(v not in ("", "0") for v in counts.values()):
            lines.append(
                f"{role} fit: "
                + "  ".join(
                    f"{t}={counts[t]}" for t in counts if counts[t] not in ("", "0")
                )
            )
    for role, tops in (card.get("role_tops") or {}).items():
        for i, top in enumerate(tops, 1):
            title, _, url = (part.strip() for part in top.partition("|"))
            shown = f"[link='{url}']{title}[/link]" if url else title
            lines.append(f"  {role} top{i}: {shown}")
    if card.get("board_note"):
        lines.append(f"[dim]{card['board_note'][:160]}[/dim]")
    return "\n".join(lines)


class Controller(Protocol):
    def next(self) -> dict | None: ...
    def answer(self, payload: dict, response) -> None: ...
    def undo(self) -> dict | None: ...


class JudgeApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    .panel { width: 1fr; border: solid $primary; padding: 1 2; }
    #status { dock: top; height: 1; background: $boost; }
    #help { dock: bottom; height: 1; color: $text-muted; }
    """

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        self.payload: dict | None = None
        self.cursor = 0
        self.selected: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal():
            yield Static("", id="left", classes="panel")
            yield Static("", id="right", classes="panel")
        yield Static("", id="help")

    def on_mount(self) -> None:
        self._advance()

    def _advance(self) -> None:
        self.payload = self.controller.next()
        if self.payload is None:
            self.exit()
            return
        self._show()

    def _show(self) -> None:
        p = self.payload
        assert p is not None
        self.query_one("#status", Static).update(p.get("heading", ""))
        left = self.query_one("#left", Static)
        right = self.query_one("#right", Static)
        if p["mode"] == "compare":
            left.update("[b]\\[A][/b]\n\n" + render_card(p["left"]))
            right.update("[b]\\[B][/b]\n\n" + render_card(p["right"]))
            right.display = True
            help_text = "a/b pick · t tie · s skip · u undo · q quit"
        elif p["mode"] == "select":
            left.update(render_card(p["card"]))
            self.cursor = min(self.cursor, len(p["options"]) - 1)
            rows = []
            for i, opt in enumerate(p["options"]):
                mark = "[green]x[/green]" if i in self.selected else " "
                cur = ">" if i == self.cursor else " "
                rows.append(f"{cur} \\[{mark}] {opt['name']} — {opt['position'][:50]}")
            right.update("\n".join(rows))
            right.display = True
            help_text = "j/k move · space toggle · enter commit · u undo · q quit"
        else:  # tier
            left.update(render_card(p["card"]))
            right.display = False
            help_text = p.get(
                "help", "1/2/3 tier · x exclude · s skip · u undo · q quit"
            )
        self.query_one("#help", Static).update(help_text)

    def on_key(self, event) -> None:
        p = self.payload
        if p is None:
            return
        key = event.key
        if key == "q":
            self.exit()
            return
        if key == "u":
            reshow = self.controller.undo()
            if reshow is not None:
                self.payload = reshow
                self.cursor, self.selected = 0, set()
                self._show()
            return
        if p["mode"] == "compare" and key in ("a", "b", "t", "s"):
            self.controller.answer(p, {"t": "tie", "s": "skip"}.get(key, key))
            self._advance()
        elif p["mode"] == "tier" and key in p.get("_valid", {"1", "2", "3", "x", "s"}):
            self.controller.answer(p, key)
            self._advance()
        elif p["mode"] == "select":
            if key in ("j", "down"):
                self.cursor = min(self.cursor + 1, len(p["options"]) - 1)
            elif key in ("k", "up"):
                self.cursor = max(self.cursor - 1, 0)
            elif key == "space":
                self.selected ^= {self.cursor}
            elif key == "enter":
                self.controller.answer(p, sorted(self.selected))
                self.cursor, self.selected = 0, set()
                self._advance()
                return
            self._show()


class BrowseApp(App):
    """Rank-ordered list + card detail; j/k to walk, q to quit."""

    CSS = JudgeApp.CSS

    def __init__(self, entries: list[tuple[dict, dict | None]]) -> None:
        # entries: (card, extra) in display order; extra carries rank/tier.
        super().__init__()
        self.entries = entries
        self.cursor = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal():
            yield Static("", id="left", classes="panel")
            yield Static("", id="right", classes="panel")
        yield Static("j/k move · q quit", id="help")

    def on_mount(self) -> None:
        self._show()

    def _show(self) -> None:
        rows = []
        last_tier = None
        for i, (card, extra) in enumerate(self.entries):
            tier = (extra or {}).get("tier")
            if tier != last_tier:
                rows.append(f"[b]— tier {tier or '?'} —[/b]")
                last_tier = tier
            cur = ">" if i == self.cursor else " "
            rank = f"#{extra['rank']:<3}" if extra and "rank" in extra else "    "
            rows.append(f"{cur} {rank} {card['company']}")
        self.query_one("#left", Static).update("\n".join(rows))
        card, extra = self.entries[self.cursor]
        self.query_one("#right", Static).update(render_card(card, extra))
        self.query_one("#status", Static).update(
            f"{self.cursor + 1}/{len(self.entries)}"
        )

    def on_key(self, event) -> None:
        if event.key == "q":
            self.exit()
        elif event.key in ("j", "down"):
            self.cursor = min(self.cursor + 1, len(self.entries) - 1)
            self._show()
        elif event.key in ("k", "up"):
            self.cursor = max(self.cursor - 1, 0)
            self._show()
