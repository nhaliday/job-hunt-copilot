from typing import Iterable

import httpx

from job_description_scan.boards import Posting, fetch_by_walk, strip_html


def _content(job: dict) -> str:
    parts = [strip_html(job.get("description") or "")]
    if job.get("is_salary_visible") and (
        job.get("salary_min") or job.get("salary_max")
    ):
        lo = job.get("salary_min") or "?"
        hi = job.get("salary_max") or "?"
        cur = job.get("currency_code") or ""
        # No frequency field in the payload — pass the numbers through verbatim
        # (tenants mix hourly and annual figures) and let the LLM read them.
        parts.append(f"Salary: {lo} - {hi} {cur}".strip())
    return "\n\n".join(p for p in parts if p)


def _location(job: dict) -> str:
    # location_display is country-qualified ("City, State, United States");
    # is_remote (nullable) gets the same bracket-tag treatment as pinpoint's
    # workplace type so filters can anchor on remoteness.
    loc = job.get("location_display") or ""
    return f"{loc} [Remote]" if job.get("is_remote") else loc


class ManatalClient:
    """Paginated one-shot: the open career-page API serves full descriptions
    inline at 10 jobs per page with a verbatim `next` URL (it hops to
    core.api.manatal.com — follow it as given, don't rebuild it). A wrong slug
    404s, which raise_for_status turns into a loud failure. The public posting
    URL is careers-page.com/<slug>/job/<hash>."""

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def iter_postings(self) -> Iterable[Posting]:
        url = f"https://api.manatal.com/open/v3/career-page/{self.slug}/jobs/"
        while url:
            r = httpx.get(url, timeout=30)
            r.raise_for_status()
            page = r.json()
            for job in page["results"]:
                yield Posting(
                    id=str(job["id"]),
                    title=job.get("position_name", ""),
                    location=_location(job),
                    content_text=_content(job),
                    url=f"https://www.careers-page.com/{self.slug}/job/{job['hash']}",
                    raw=job,
                )
            url = page.get("next")

    def fetch_postings(self, ids: Iterable[str]) -> Iterable[Posting]:
        return fetch_by_walk(self, ids)
