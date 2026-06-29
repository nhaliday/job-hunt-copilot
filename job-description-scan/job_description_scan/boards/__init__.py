from dataclasses import dataclass
from typing import Iterable, Protocol

from job_description_scan.config import BoardSource


@dataclass
class Posting:
    id: str
    title: str
    location: str
    content_text: str
    url: str
    raw: dict


class BoardClient(Protocol):
    def iter_postings(self) -> Iterable[Posting]: ...


def make_client(source: BoardSource) -> BoardClient:
    from .ashby import AshbyClient
    from .greenhouse import GreenhouseClient
    from .lever import LeverClient

    if source.kind == "greenhouse":
        return GreenhouseClient(source.slug)
    if source.kind == "ashby":
        return AshbyClient(source.slug)
    if source.kind == "lever":
        return LeverClient(source.slug)
    raise ValueError(f"Unknown board kind: {source.kind!r}")
