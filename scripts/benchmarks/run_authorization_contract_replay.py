#!/usr/bin/env python3
"""Run the AgentDojo authorization-contract consistency replay."""

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

from proofmesh.benchmarks.authorization_replay import (  # noqa: E402
    canonical_report_json,
    load_benchmark,
    render_markdown,
    run_authorization_contract_replay,
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay normalized AgentDojo user tool contracts through the real ProofMesh "
            "ActionGateway. This is not a model ASR or utility evaluation."
        )
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "artifacts/public-benchmark/cases.jsonl",
        help="normalized public benchmark JSONL",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts/public-benchmark/authorization-contract-replay.json",
        help="machine-readable result",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts/public-benchmark/authorization-contract-replay.md",
        help="human-readable result",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run only the first N cases (intended for development smoke tests)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    benchmark = load_benchmark(args.cases, limit=args.limit)
    report = run_authorization_contract_replay(benchmark)
    _atomic_write(args.json_output, canonical_report_json(report))
    _atomic_write(args.markdown_output, render_markdown(report))
    print(
        canonical_report_json(
            {
                "passed": report["passed"],
                "case_count": report["dataset"]["case_count"],
                "user_ground_truth_call_count": report["dataset"][
                    "user_ground_truth_call_count"
                ],
                "json_output": str(args.json_output),
                "markdown_output": str(args.markdown_output),
            }
        ),
        end="",
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
