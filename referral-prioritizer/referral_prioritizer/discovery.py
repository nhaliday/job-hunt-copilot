"""Enrich a companies CSV with each company's job board (kind + slug).

Two stages per un-enriched row:

- Stage A (free): probe the four slug-guessable board APIs (greenhouse, lever,
  ashby, smartrecruiters) with name-derived slug variants. A hit whose board
  identity is corroborated by a display name is accepted outright; a hit
  without corroboration is only a candidate — generic-word slugs are often a
  different company's board — and goes to Stage B for verification.
- Stage B (Anthropic API): a cheap no-tools call verifies uncorroborated
  probe hits against sample job titles; misses and impostors get a discovery
  call with the server-side web_search tool that finds the careers page and
  extracts the provider + slug.

Adds columns in place (never touching input columns): board_kind, board_slug,
board_url, board_confidence, board_source, board_note. The CSV is rewritten
atomically after every resolved row, and rows with a non-empty board_source
are skipped on re-runs — so interrupted or credit-starved runs resume, and
hand-prefilled rows (board_source=manual) are never touched.
"""

import argparse
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import polars as pl
from pydantic import BaseModel, Field

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

# Words too generic to corroborate a board's identity on their own.
_STOPWORDS = {"inc", "llc", "ltd", "lp", "co", "corp", "company", "group", "the", "of", "and"}

_BOARD_URL = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
}


class Verification(BaseModel):
    belongs_to_company: bool = Field(
        description="True iff the sampled job board clearly belongs to the"
        " company described (not a different company with a similar name)."
    )
    confidence: Literal["high", "medium", "low"]


class Discovery(BaseModel):
    kind: Literal[
        "greenhouse", "ashby", "lever", "workday", "smartrecruiters",
        "custom", "none", "unknown",
    ]
    slug: str = Field(
        description="Board slug for the five scannable kinds (workday:"
        " 'hostprefix/site'); empty for custom/none/unknown."
    )
    careers_url: str
    confidence: Literal["high", "medium", "low"]
    note: str = Field(description="Max 15 words of evidence/context.")


VERIFY_SYSTEM = """\
You verify job-board identities. A slug guessed from a company's name returned
a live board on a job-board API, but generic slugs often belong to a DIFFERENT
company with a similar name. Decide from the sample job titles (and display
name, when given) whether the board belongs to the described company. Job
titles inconsistent with the company's business, size, or the connection's
positions mean it does not."""

DISCOVER_SYSTEM = """\
You find a company's job/careers posting site and identify the ATS provider.
Web-search for the company's careers or jobs page (use the positions context to
disambiguate same-name companies), then classify:

- greenhouse: boards.greenhouse.io/SLUG or job-boards[.eu].greenhouse.io/SLUG,
  or a custom page embedding greenhouse (gh_jid params). slug = SLUG.
- lever: jobs.lever.co/SLUG. slug = SLUG.
- ashby: jobs.ashbyhq.com/SLUG. slug = SLUG exactly as cased in the URL.
- workday: TENANT.wdN.myworkdayjobs.com/SITE or TENANT.wdN.myworkdaysite.com/
  recruiting/TENANT/SITE. slug = "TENANT.wdN/SITE" (e.g. "acme.wd5/Acme_Careers").
- smartrecruiters: jobs.smartrecruiters.com/SLUG or careers.smartrecruiters.com/
  SLUG. slug = SLUG (the API company identifier sometimes differs in case).
- custom: an in-house careers system, or any other ATS vendor (icims,
  successfactors, oracle/taleo, avature, workable, rippling, bamboohr, ...).
  Name the vendor in the note. slug stays empty.
- none: no careers/jobs page exists (tiny firm, defunct, hires via email only).
- unknown: cannot determine after searching.

Report the underlying ATS when a custom-domain page embeds or redirects to one
of the five named kinds. careers_url = the postings page you found."""


def _sig_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", name.lower())} - _STOPWORDS


def _corroborated(company: str, display: str | None) -> bool:
    # The display name must cover every significant word of the company name;
    # a bare-subset display ("Apollo" for "Apollo Global Management") is
    # exactly the impostor signature and needs verification instead.
    return bool(display) and _sig_words(company) <= _sig_words(display)


def _slug_variants(name: str) -> list[str]:
    base = re.sub(r"\s*\(.*?\)", "", name)
    base = re.sub(r",?\s+(Inc\.?|LLC\.?|Ltd\.?|L\.P\.|Corp\.?|Co\.)$", "", base, flags=re.I)
    base = base.replace("&", "and").strip().rstrip(".,")
    words = re.findall(r"[A-Za-z0-9]+", base)
    if not words:
        return []
    camel = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+|\d+", base)
    out = [base]  # as-is, for case-sensitive ashby slugs
    for v in ("".join(words).lower(), "-".join(w.lower() for w in words),
              "-".join(w.lower() for w in camel), words[0].lower()):
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class ProbeHit:
    kind: str
    slug: str
    titles: list[str]
    display_name: str | None


def _get_json(http: httpx.Client, url: str, **params) -> dict | list | None:
    try:
        r = http.get(url, params=params or None)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def probe(http: httpx.Client, company: str) -> ProbeHit | None:
    for slug in _slug_variants(company):
        data = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if isinstance(data, dict) and "jobs" in data:
            root = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            display = root.get("name") if isinstance(root, dict) else None
            titles = [j.get("title", "") for j in data["jobs"][:5]]
            return ProbeHit("greenhouse", slug, titles, display)

        data = _get_json(http, f"https://api.lever.co/v0/postings/{slug}", mode="json", limit=5)
        if isinstance(data, list):
            return ProbeHit("lever", slug, [j.get("text", "") for j in data[:5]], None)

        data = _get_json(
            http, "https://api.ashbyhq.com/posting-api/job-board/" + urllib.parse.quote(slug)
        )
        if isinstance(data, dict) and "jobs" in data:
            return ProbeHit("ashby", slug, [j.get("title", "") for j in data["jobs"][:5]], None)

        data = _get_json(
            http, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", limit=5
        )
        if isinstance(data, dict) and data.get("totalFound", 0) > 0:
            rows = data["content"]
            display = (rows[0].get("company") or {}).get("name")
            return ProbeHit("smartrecruiters", slug, [r.get("name", "") for r in rows], display)
    return None


def _parse(anth: anthropic.Anthropic, model: str, system: str, prompt: str,
           output_format: type[BaseModel], tools: list[dict] | None = None):
    messages: list[dict] = [{"role": "user", "content": prompt}]
    extra = {"tools": tools} if tools else {}
    for _ in range(4):
        response = anth.messages.parse(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
            messages=messages,
            output_format=output_format,
            **extra,
        )
        if response.stop_reason != "pause_turn":
            return response.parsed_output
        # Server-tool loop paused mid-turn; re-send to let it resume.
        messages = messages[:1] + [{"role": "assistant", "content": response.content}]
    raise RuntimeError(f"still pause_turn after retries for {prompt[:80]!r}")


def _context(row: dict) -> str:
    return (
        f"Company: {row['company']}\n"
        f"Connections' positions there: {row['positions'] or '(none listed)'}"
    )


def verify(anth: anthropic.Anthropic, model: str, row: dict, hit: ProbeHit) -> Verification:
    titles = "\n".join(f"- {t}" for t in hit.titles if t) or "(no titles returned)"
    display = f"\nBoard display name: {hit.display_name}" if hit.display_name else ""
    prompt = (
        f"{_context(row)}\n\nProbed board: {hit.kind}, slug {hit.slug!r}{display}\n"
        f"Sample job titles on that board:\n{titles}\n\nDoes this board belong to"
        " this company?"
    )
    return _parse(anth, model, VERIFY_SYSTEM, prompt, Verification)


def discover(anth: anthropic.Anthropic, model: str, row: dict) -> Discovery:
    prompt = f"{_context(row)}\n\nFind this company's job board."
    return _parse(anth, model, DISCOVER_SYSTEM, prompt, Discovery, tools=[WEB_SEARCH])


def _write(df_rows: list[dict], out: Path) -> None:
    tmp = out.with_suffix(".tmp")
    pl.DataFrame(df_rows).write_csv(tmp)
    tmp.replace(out)


BOARD_COLS = ("board_kind", "board_slug", "board_url", "board_confidence",
              "board_source", "board_note")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", type=Path, required=True)
    ap.add_argument("--probe-only", action="store_true",
                    help="no LLM calls; accept only corroborated probe hits")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and print per-stage counts + cost estimate, no writes")
    ap.add_argument("--limit", type=int, help="resolve at most N rows")
    ap.add_argument("--model", default="claude-opus-4-8")
    args = ap.parse_args()

    df = pl.read_csv(args.companies, infer_schema_length=0)
    for col in BOARD_COLS:
        if col not in df.columns:
            df = df.with_columns(pl.lit("").alias(col))
    rows = df.to_dicts()
    for row in rows:
        for col in BOARD_COLS:
            row[col] = row[col] or ""

    pending = [r for r in rows if not r["board_source"]]
    if args.limit is not None:
        pending = pending[: args.limit]
    print(f"{len(rows)} rows, {len(pending)} pending")

    anth = None if (args.probe_only or args.dry_run) else anthropic.Anthropic(max_retries=8)
    accepted = to_verify = to_discover = 0
    with httpx.Client(timeout=10, follow_redirects=True) as http:
        for row in pending:
            hit = probe(http, row["company"])
            outcome = None
            # Auto-accept needs BOTH a covering display name and actual
            # postings: an empty board matches any same-name impostor and is
            # unscannable even when genuine.
            if hit and any(hit.titles) and _corroborated(row["company"], hit.display_name):
                outcome = (hit.kind, hit.slug, "high", "probe",
                           f"display name: {hit.display_name}")
                accepted += 1
            elif hit and anth:
                v = verify(anth, args.model, row, hit)
                if v.belongs_to_company:
                    outcome = (hit.kind, hit.slug, v.confidence, "probe+verify",
                               "titles match company")
                    accepted += 1
                else:
                    hit = None  # impostor board; fall through to discovery
            elif hit:
                to_verify += 1

            if hit is None:
                if anth:
                    d = discover(anth, args.model, row)
                    outcome = (d.kind, d.slug, d.confidence, "llm", d.note)
                    accepted += 1
                    row["board_url"] = d.careers_url
                else:
                    to_discover += 1

            if outcome and not args.dry_run:
                kind, slug, confidence, source, note = outcome
                row["board_kind"], row["board_slug"] = kind, slug
                row["board_confidence"], row["board_source"] = confidence, source
                row["board_note"] = note
                if not row["board_url"] and kind in _BOARD_URL:
                    row["board_url"] = _BOARD_URL[kind].format(slug=slug)
                _write(rows, args.companies)
                print(f"  {row['company']}: {kind} {slug} [{source}, {confidence}]")

    print(f"\nresolved: {accepted} | needs verify: {to_verify} | needs discovery: {to_discover}")
    if to_verify or to_discover:
        est = to_verify * 0.01 + to_discover * 0.06
        print(f"est. LLM cost for the remainder on {args.model}: ~${est:.2f}"
              " (verify ~1c, discovery ~6c incl. searches)")


if __name__ == "__main__":
    main()
