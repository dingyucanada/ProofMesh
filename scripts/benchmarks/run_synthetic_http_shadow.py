#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from proofmesh.synthetic_shadow import write_artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the independent-process HTTP synthetic shadow workload")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/synthetic-http-shadow"),
    )
    parser.add_argument("--cases", type=int, default=240)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--payment-port", type=int, default=18765)
    parser.add_argument("--crm-port", type=int, default=18766)
    args = parser.parse_args()
    report = write_artifacts(
        args.output,
        case_count=args.cases,
        seed=args.seed,
        payment_port=args.payment_port,
        crm_port=args.crm_port,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "cases": report["sample"]["cases"],
                "terminal_counts": report["terminal_counts"],
                "provenance": report["provenance"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
