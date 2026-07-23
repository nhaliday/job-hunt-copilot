import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, TypeVar

import anthropic
from pydantic import BaseModel, create_model

from job_description_scan.boards import BoardClient, Posting
from job_description_scan.config import Prefilter, Scan

_T = TypeVar("_T")


class _TriageVerdict(BaseModel):
    id: int
    keep: bool
    reason: str


class _TriageResult(BaseModel):
    verdicts: list[_TriageVerdict]


def cached_system(texts: list[str]) -> list[dict]:
    """Wrap text strings as system blocks, caching the whole prefix.

    Puts a `cache_control` breakpoint on the last non-empty block so the entire
    prefix (identical across calls in one run) is written once and read
    thereafter. Empty strings are dropped.
    """
    blocks: list[dict] = [{"type": "text", "text": t} for t in texts if t]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


async def lead_then_fanout(
    items: list[_T],
    call: Callable[[_T], Awaitable[dict]],
    concurrency: int,
) -> AsyncIterator[dict]:
    """Run item[0] sequentially so it writes the prompt cache, then fan the rest
    out under a `Semaphore` reading that cache — without the lead call every
    concurrent call pays `cache_creation` and cost balloons. Yields in
    completion order.
    """
    if not items:
        return
    yield await call(items[0])
    if len(items) == 1:
        return
    sem = asyncio.Semaphore(concurrency)

    async def bounded(x: _T) -> dict:
        async with sem:
            return await call(x)

    tasks = [asyncio.create_task(bounded(x)) for x in items[1:]]
    for fut in asyncio.as_completed(tasks):
        yield await fut


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

    texts = [
        instructions,
        "## Output schema\n\n" + json.dumps(Result.model_json_schema(), indent=2),
    ]
    for path in scan.system_context_files:
        p = Path(path)
        texts.append(f"## Reference: {p.name}\n\n{p.read_text(encoding='utf-8')}")
    if resume_text:
        texts.append(f"## Candidate resume\n\n{resume_text}")

    system_blocks = cached_system(texts)

    # Collect + filter postings synchronously. Yield filter markers immediately
    # so the CLI can count and (if you wanted) log them early.
    matched: list[Posting] = []
    for posting in client.iter_postings():
        if scan.location_filter is not None and not scan.location_filter.search(
            posting.location
        ):
            yield {
                "posting": _posting_dict(posting),
                "_filtered": True,
                "_filter_stage": "location",
            }
        else:
            matched.append(posting)

    # --limit caps everything downstream, so smoke runs cap ALL LLM spend
    # (triage included), not just extraction calls.
    if limit is not None:
        matched = matched[:limit]
    if not matched:
        return

    # max_retries above the SDK's default (2) — at high concurrency 429s become
    # common; the SDK already retries with exponential backoff.
    anth = anthropic.AsyncAnthropic(max_retries=8)

    if scan.prefilter is not None:
        matched, audit_rows = await _run_prefilter(
            anth, scan.prefilter, matched, concurrency
        )
        for row in audit_rows:
            yield row
        if not matched:
            return

    async def call(posting: Posting) -> dict:
        return await _call(anth, model, system_blocks, Result, posting)

    async for row in lead_then_fanout(matched, call, concurrency):
        yield row


async def _run_prefilter(
    anth: anthropic.AsyncAnthropic,
    pf: Prefilter,
    postings: list[Posting],
    concurrency: int,
) -> tuple[list[Posting], list[dict]]:
    """Cheap-model triage on title+location, batched. Returns (survivors,
    audit rows) — one `_filtered` row per drop plus a `_prefilter_stats` row.

    Fail-open throughout: a failed batch call, or an id the model didn't echo
    back, keeps its postings. A dropped extraction candidate is unrecoverable;
    a kept junk posting just costs one extraction call.
    """
    audit: list[dict] = []
    kept: list[Posting] = []

    if pf.title_precut is not None:
        for p in postings:
            if pf.title_precut.search(p.title):
                audit.append(
                    {
                        "posting": _posting_dict(p),
                        "_filtered": True,
                        "_filter_stage": "title_precut",
                    }
                )
            else:
                kept.append(p)
        postings = kept

    if not postings:
        return [], audit

    instructions = (
        "You triage job postings by title and location only. For each "
        "numbered posting in the list, decide whether it plausibly fits the "
        "criteria below. Echo every posting's id exactly once with "
        "keep=true/false and a terse reason. Titles under-specify roles: when "
        "a title is ambiguous or you are unsure, keep it — a false drop is "
        "unrecoverable, a false keep costs one downstream extraction call.\n\n"
        "## Criteria\n\n" + pf.criterion
    )
    system_blocks = cached_system([instructions])
    indexed = list(enumerate(postings))
    batches = [
        indexed[i : i + pf.batch_size]
        for i in range(0, len(indexed), pf.batch_size)
    ]

    async def triage(batch: list[tuple[int, Posting]]) -> dict:
        lines = "\n".join(f"{i}. {p.title} — {p.location}" for i, p in batch)
        try:
            response = await anth.messages.parse(
                model=pf.model,
                max_tokens=4000,
                system=system_blocks,
                messages=[{"role": "user", "content": f"# Postings\n\n{lines}"}],
                output_format=_TriageResult,
            )
        except Exception as e:
            return {"batch": batch, "error": f"{type(e).__name__}: {e}"}
        usage = response.usage
        return {
            "batch": batch,
            "verdicts": {v.id: v for v in response.parsed_output.verdicts},
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(
                    usage, "cache_read_input_tokens", 0
                )
                or 0,
            },
        }

    survivors: list[Posting] = []
    stats = {
        "model": pf.model,
        "batches": len(batches),
        "kept": 0,
        "dropped": 0,
        "batch_errors": 0,
        "unechoed_ids": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    async for res in lead_then_fanout(batches, triage, concurrency):
        verdicts = res.get("verdicts")
        if verdicts is None:
            stats["batch_errors"] += 1
        for k, v in (res.get("usage") or {}).items():
            stats[k] += v
        for i, p in res["batch"]:
            verdict = verdicts.get(i) if verdicts is not None else None
            if verdict is None and verdicts is not None:
                stats["unechoed_ids"] += 1
            if verdict is None or verdict.keep:
                survivors.append(p)
            else:
                audit.append(
                    {
                        "posting": _posting_dict(p),
                        "_filtered": True,
                        "_filter_stage": "prefilter",
                        "_prefilter_reason": verdict.reason,
                    }
                )
    stats["kept"] = len(survivors)
    stats["dropped"] = sum(
        1 for r in audit if r.get("_filter_stage") == "prefilter"
    )
    audit.append({"_prefilter_stats": stats})
    return survivors, audit


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
            # 12000 headroom (not 2048): models with always-on thinking (e.g.
            # Fable 5) spend output tokens on reasoning that counts against
            # max_tokens, and a tight cap truncates the structured result into a
            # parse failure. You only pay for tokens actually generated, so the
            # larger ceiling is free insurance and stays under the ~16K
            # non-streaming timeout threshold.
            model=model,
            max_tokens=12000,
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
