import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import anthropic
from pydantic import BaseModel, create_model

from job_description_scan.boards import BoardClient, Posting
from job_description_scan.config import Scan


async def run_scan(
    scan: Scan,
    client: BoardClient,
    resume_path: Path | None = None,
    model_override: str | None = None,
    limit: int | None = None,
    concurrency: int = 20,
) -> AsyncIterator[dict]:
    model = model_override or scan.model

    resume_text = resume_path.read_text(encoding="utf-8") if resume_path else None
    has_comparison = scan.comparison is not None and resume_text is not None

    if has_comparison:
        Result = create_model(
            "Result",
            extraction=(scan.extraction, ...),
            comparison=(scan.comparison, ...),
        )
    else:
        Result = create_model("Result", extraction=(scan.extraction, ...))

    instructions = (
        "You extract structured facts from a job description per the JSON "
        "schema below. Be precise and conservative — set fields to null or "
        "empty lists when the JD does not state them."
    )
    if has_comparison:
        instructions += (
            "\n\nAfter extracting facts, also populate the comparison fields "
            "using the candidate resume below. Be honest about gaps: list "
            "required quals the resume does not satisfy, estimate the YoE gap "
            "(positive = candidate is short of the minimum), and rank overall "
            "fit."
        )

    system_blocks: list[dict] = [
        {"type": "text", "text": instructions},
        {
            "type": "text",
            "text": "## Output schema\n\n"
            + json.dumps(Result.model_json_schema(), indent=2),
        },
    ]

    for path in scan.system_context_files:
        p = Path(path)
        system_blocks.append(
            {
                "type": "text",
                "text": f"## Reference: {p.name}\n\n{p.read_text(encoding='utf-8')}",
            }
        )

    if resume_text:
        system_blocks.append(
            {"type": "text", "text": f"## Candidate resume\n\n{resume_text}"}
        )

    system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

    # Collect + filter postings synchronously. Yield filter markers immediately
    # so the CLI can count and (if you wanted) log them early.
    matched: list[Posting] = []
    for posting in client.iter_postings():
        if scan.location_filter is not None and not scan.location_filter.search(
            posting.location
        ):
            yield {"posting": _posting_dict(posting), "_filtered": True}
        else:
            matched.append(posting)

    if limit is not None:
        matched = matched[:limit]
    if not matched:
        return

    # max_retries above the SDK's default (2) — at high concurrency 429s become
    # common; the SDK already retries with exponential backoff.
    anth = anthropic.AsyncAnthropic(max_retries=8)

    # Lead-then-fan-out: send call #1 sequentially so it writes the cache
    # entry; subsequent fan-out reads from it (otherwise every concurrent call
    # pays cache_creation and the cost balloons).
    yield await _call(anth, model, system_blocks, Result, matched[0])

    if len(matched) == 1:
        return

    sem = asyncio.Semaphore(concurrency)

    async def bounded(p: Posting) -> dict:
        async with sem:
            return await _call(anth, model, system_blocks, Result, p)

    tasks = [asyncio.create_task(bounded(p)) for p in matched[1:]]
    for fut in asyncio.as_completed(tasks):
        yield await fut


async def _call(
    anth: anthropic.AsyncAnthropic,
    model: str,
    system_blocks: list[dict],
    Result: type[BaseModel],
    posting: Posting,
) -> dict:
    user_content = (
        f"# Job posting\n\nTitle: {posting.title}\n"
        f"Location: {posting.location}\n\n{posting.content_text}"
    )

    try:
        response = await anth.messages.parse(
            model=model,
            max_tokens=2048,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
            output_format=Result,
        )
    except Exception as e:
        return {
            "posting": _posting_dict(posting),
            "error": f"{type(e).__name__}: {e}",
            "_meta": {"model": model},
        }

    parsed = response.parsed_output.model_dump()
    usage = response.usage
    meta = {
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(
            usage, "cache_creation_input_tokens", 0
        )
        or 0,
        "cache_read_input_tokens": getattr(
            usage, "cache_read_input_tokens", 0
        )
        or 0,
    }
    return {
        "posting": _posting_dict(posting),
        "result": parsed,
        "_meta": meta,
    }


def _posting_dict(p: Posting) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "location": p.location,
        "url": p.url,
    }
