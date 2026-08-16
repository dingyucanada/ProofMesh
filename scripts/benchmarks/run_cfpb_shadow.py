#!/usr/bin/env python3
"""Run or validate the privacy-minimised CFPB public-data shadow evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from proofmesh.benchmarks.cfpb_shadow import API_URL, run_shadow, validate_artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/public-domain-evaluation/cfpb"),
    )
    parser.add_argument("--base-url", default=API_URL)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    report = validate_artifacts(args.output_dir) if args.validate_only else run_shadow(args.output_dir, base_url=args.base_url)
    print(json.dumps({"status": report["status"], "metrics": report["metrics"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
