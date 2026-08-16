#!/usr/bin/env python3
"""Run the official Banking77 held-out routing evaluation."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proofmesh.benchmarks.banking77_routing import (  # noqa: E402
    canonical_report_json,
    case_results_jsonl,
    evaluate_banking77,
    render_markdown,
)
from proofmesh.capabilities import canonical_json  # noqa: E402


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Train on official Banking77 train and evaluate official test without raw-text redistribution."
    )
    result.add_argument(
        "--dataset-root",
        type=Path,
        default=PROJECT_ROOT.parents[1] / "work/public-datasets/task-specific-datasets",
        help="pinned PolyAI task-specific-datasets checkout",
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/public-domain-evaluation/banking77",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report, records, source_lock = evaluate_banking77(args.dataset_root)
    output = args.output_dir
    _atomic_write(output / "report.json", canonical_report_json(report))
    _atomic_write(output / "report.md", render_markdown(report))
    _atomic_write(output / "case-results.jsonl", case_results_jsonl(records))
    _atomic_write(output / "source-lock.json", canonical_report_json(source_lock))
    print(
        canonical_json(
            {
                "benchmark_execution_passed": report["benchmark_execution_passed"],
                "readiness_verdict": report["readiness_verdict"],
                "test_rows": report["dataset"]["test"]["rows"],
                "intent_accuracy": report["metrics"]["intent_77_way"]["accuracy"]["rate"],
                "refund_recall": report["metrics"]["refund_focus_detection"]["recall"]["rate"],
                "output_dir": str(output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
