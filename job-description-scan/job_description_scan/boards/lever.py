from typing import Iterable

from job_description_scan.boards import Posting


class LeverClient:
    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        raise NotImplementedError(
            "Lever client pending validation. Implementation shape will follow "
            "https://api.lever.co/v0/postings/<slug>?mode=json once tested."
        )
