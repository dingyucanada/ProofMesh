#!/usr/bin/env python3
"""Validate a private pilot dataset and emit a safe aggregate report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from proofmesh.pilot_dataset import validate_pilot_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--origin", choices=("SYNTHETIC", "AUTHORIZED_HISTORICAL"), required=True)
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--authorization-ref")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = validate_pilot_dataset(
        args.cases,
        args.labels,
        origin=args.origin,
        reviewed_by=args.reviewed_by,
        authorization_ref=args.authorization_ref,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(dataset.report(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"output": str(output), "case_count": len(dataset.cases)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

