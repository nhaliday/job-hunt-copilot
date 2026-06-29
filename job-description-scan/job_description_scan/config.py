from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

BoardKind = Literal["greenhouse", "ashby", "lever"]


@dataclass(frozen=True)
class BoardSource:
    kind: BoardKind
    slug: str


@dataclass
class Scan:
    source: BoardSource
    extraction: type[BaseModel]
    comparison: type[BaseModel] | None = None
    system_context_files: list[Path] = field(default_factory=list)
    model: str = "claude-haiku-4-5"
