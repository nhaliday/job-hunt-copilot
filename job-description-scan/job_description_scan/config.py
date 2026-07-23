import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

BoardKind = Literal["greenhouse", "ashby", "lever", "workday", "smartrecruiters"]


@dataclass(frozen=True)
class BoardSource:
    kind: BoardKind
    slug: str


@dataclass(frozen=True)
class Prefilter:
    """Optional cheap-model triage of postings before extraction.

    Titles + locations go to an inexpensive model in batches, with the
    case-supplied `criterion` text verbatim in the prompt; postings judged
    clearly out of scope are dropped before any extraction spend. Recall-biased
    by contract: the prompt says keep-on-uncertain, and every failure mode
    (batch call error, missing/duplicate echoed ids) falls open to keeping.
    Drops are yielded as `_filtered` audit rows, same as location_filter's.
    """

    criterion: str
    model: str = "claude-sonnet-4-6"
    batch_size: int = 40
    # Free regex applied to Posting.title first; matches are dropped with no
    # LLM call (e.g. re.compile(r"intern|new grad", re.IGNORECASE)).
    title_precut: re.Pattern[str] | None = None


@dataclass
class Scan:
    source: BoardSource
    extraction: type[BaseModel]
    comparison: type[BaseModel] | None = None
    system_context_files: list[Path] = field(default_factory=list)
    model: str = "claude-haiku-4-5"
    # Optional regex applied to Posting.location BEFORE the LLM call.
    # Postings that don't match are skipped (no extraction cost). Use re.compile(...).
    location_filter: re.Pattern[str] | None = None
    # Optional cheap-model triage between location_filter and extraction.
    # None skips the stage entirely.
    prefilter: Prefilter | None = None


@dataclass(frozen=True)
class Ladder:
    """One pairwise-ranking ladder over a role family (see ranking.py).

    A ladder is the case-specific selection of which scan rows compete: the
    `roles`/`tiers` to include and an optional `exclude_title` regex to drop
    e.g. new-grad/internship postings. `label` is the only case-supplied prompt
    content — a short role framing slotted into the otherwise-generic judge
    prompt (empty string omits the framing clause).
    """

    roles: tuple[str, ...]
    tiers: tuple[str, ...] = ("strong", "stretch")
    label: str = ""
    exclude_title: re.Pattern[str] | None = None


@dataclass
class RankConfig:
    """Per-scan ranking config, co-located with `scan = Scan(...)` in a scan
    module. One `Ladder` per role family you want ranked; the ranking engine
    stays generic and reads this."""

    ladders: list[Ladder]
