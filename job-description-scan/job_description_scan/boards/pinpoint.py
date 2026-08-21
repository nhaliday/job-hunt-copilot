from typing import Iterable

import httpx

from job_description_scan.boards import Posting, fetch_by_walk, strip_html

# Content arrives split into named HTML sections; headers ride in sibling
# *_header fields (tenant-customizable, so not hardcoded).
_SECTIONS = (
    ("description", None),
    ("key_responsibilities", "key_responsibilities_header"),
    ("skills_knowledge_expertise", "skills_knowledge_expertise_header"),
    ("benefits", "benefits_header"),
)


def _full_text(row: dict) -> str:
    parts = []
    for body_field, header_field in _SECTIONS:
        body = strip_html(row.get(body_field) or "")
        if not body:
            continue
        header = (row.get(header_field) or "") if header_field else ""
        parts.append(f"{header}\n{body}".strip())
    if row.get("compensation_visible") and row.get("compensation"):
        parts.append(f"Compensation: {row['compensation']}")
    return "\n\n".join(parts)


def _location(row: dict) -> str:
    # location.name is "City, ST"-shaped; the bracketed workplace type
    # ("Remote"/"Hybrid"/"Onsite") lets location_filters anchor on remoteness
    # the same way lever's country tag works.
    name = (row.get("location") or {}).get("name") or ""
    workplace = row.get("workplace_type_text") or ""
    return f"{name} [{workplace}]" if workplace else name


class PinpointClient:
    """One-shot: <tenant>.pinpointhq.com/postings.json returns every published
    posting with full content inline — no pagination marker in the payload.
    Multi-location jobs appear as one row per location, each with its own
    posting id (the shared req rides in raw["job"]["requisition_id"]); the
    ranker's dedup collapses them. A wrong tenant subdomain 404s, which
    raise_for_status turns into a loud failure."""

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        url = f"https://{self.slug}.pinpointhq.com/postings.json"
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        for row in r.json()["data"]:
            yield Posting(
                id=str(row["id"]),
                title=row.get("title", ""),
                location=_location(row),
                content_text=_full_text(row),
                url=row.get("url", ""),
                raw=row,
            )

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        return fetch_by_walk(self, ids)
