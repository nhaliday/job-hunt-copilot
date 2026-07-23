import argparse
import asyncio
import dataclasses
import importlib
import sys
from collections import Counter
from pathlib import Path

from job_description_scan.boards import make_client
from job_description_scan.config import Scan
from job_description_scan.output import JsonlWriter
from job_description_scan.pipeline import run_scan


def main() -> None:
    ap = argparse.ArgumentParser(prog="job-description-scan")
    ap.add_argument(
        "--scan",
        required=True,
        help="Python module path of the scan (e.g. scans.acme)",
    )
    ap.add_argument(
        "--resume",
        type=Path,
        help="Path to resume markdown file for comparison pass",
    )
    ap.add_argument("--model", help="Override the scan's default model")
    ap.add_argument(
        "--out",
        type=Path,
        help="Output JSONL path (default: _output/<scan-tail>.jsonl)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        help="Max number of postings to process (for smoke tests)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Max concurrent LLM calls (default 20). Lead call runs sequentially "
        "to populate prompt cache; the rest fan out.",
    )
    ap.add_argument(
        "--no-prefilter",
        action="store_true",
        help="Bypass the scan's cheap-model prefilter stage (if it has one)",
    )
    args = ap.parse_args()
    asyncio.run(_amain(args))


async def _amain(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module(args.scan)
    scan: Scan = module.scan
    if args.no_prefilter and scan.prefilter is not None:
        scan = dataclasses.replace(scan, prefilter=None)

    scan_tail = args.scan.rsplit(".", 1)[-1]
    out_path = args.out or Path("_output") / f"{scan_tail}.jsonl"

    client = make_client(scan.source, scan.location_filter)

    total_in = total_out = total_cache_read = 0
    filtered: Counter[str] = Counter()
    prefilter_stats: dict | None = None
    i = 0
    with JsonlWriter(out_path) as writer:
        async for row in run_scan(
            scan,
            client,
            args.resume,
            args.model,
            args.limit,
            args.concurrency,
        ):
            if "_prefilter_stats" in row:
                prefilter_stats = row["_prefilter_stats"]
                continue
            if row.get("_filtered"):
                filtered[row.get("_filter_stage", "location")] += 1
                continue
            i += 1
            writer.write(row)
            p = row["posting"]
            print(f"[{i}] {p['title']} @ {p['location']}", flush=True)
            if "error" in row:
                print(f"    ERROR: {row['error']}", flush=True)
            meta = row.get("_meta") or {}
            total_in += meta.get("input_tokens", 0)
            total_out += meta.get("output_tokens", 0)
            total_cache_read += meta.get("cache_read_input_tokens", 0)

    print(f"\n→ {out_path} ({writer.count} rows)")
    for stage, n in filtered.items():
        print(f"filtered: {n} postings ({stage})")
    if prefilter_stats:
        s = prefilter_stats
        print(
            f"prefilter: kept {s['kept']} / dropped {s['dropped']} "
            f"in {s['batches']} batches ({s['model']}); "
            f"{s['input_tokens']} input + {s['output_tokens']} output "
            f"+ {s['cache_read_input_tokens']} cache_read tokens"
        )
        if s["batch_errors"] or s["unechoed_ids"]:
            print(
                f"prefilter WARN: {s['batch_errors']} failed batches, "
                f"{s['unechoed_ids']} unechoed ids — affected postings kept"
            )
    print(
        f"usage: {total_in} input + {total_out} output "
        f"+ {total_cache_read} cache_read tokens"
    )


if __name__ == "__main__":
    main()
