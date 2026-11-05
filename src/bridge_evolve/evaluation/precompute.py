"""Explicit real-DDS dataset precomputation with durable failure status."""

import json
import sys
import time
from pathlib import Path

from bridge_evolve.evaluation.cache import DDSCache
from bridge_evolve.evaluation.data import load_dataset
from bridge_evolve.oracle import DDSAdapter


def precompute(manifest_path: Path, cache_path: Path) -> dict:
    manifest, records = load_dataset(manifest_path)
    result = {
        "status": "running", "dataset_hash": manifest["boards_sha256"],
        "count": len(records), "completed": 0, "solves": 0, "hits": 0,
    }
    target = manifest_path.parent / "oracle-status.json"
    start = time.monotonic()
    try:
        with DDSCache(cache_path, DDSAdapter()) as cache:
            result["oracle"] = cache.provenance
            for record in records:
                cache.solve(record.board.deal)
                result.update(completed=record.index + 1, solves=cache.solves, hits=cache.hits)
                if result["completed"] % 100 == 0:
                    target.write_text(json.dumps(result, indent=2) + "\n")
                    print(f"DDS {result['completed']}/{len(records)}", file=sys.stderr, flush=True)
            result["status"] = "completed"
    finally:
        if result["status"] == "running":
            result["status"] = "failed"
        result["elapsed_seconds"] = time.monotonic() - start
        target.write_text(json.dumps(result, indent=2) + "\n")
    return result
