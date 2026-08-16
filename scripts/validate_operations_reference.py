#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from proofmesh.operations_verifier import verify_operations_proof


ROOT = Path(__file__).resolve().parents[1]


def validate(root: Path = ROOT) -> dict[str, object]:
    reference = root / "artifacts/operations-reference"
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    declared = {entry["path"]: entry for entry in manifest["files"]}
    actual = {
        str(path.relative_to(reference))
        for path in reference.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(declared) != actual:
        raise RuntimeError("operations reference manifest file set mismatch")
    for relative, entry in declared.items():
        path = reference / relative
        if path.stat().st_size != entry["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"operations reference hash mismatch: {relative}")
    report = json.loads((reference / "report.json").read_text(encoding="utf-8"))
    expected = {
        "CHG-LOW-001": "COMPLETED",
        "CHG-HIGH-001": "COMPLETED",
        "CHG-ROLLBACK-001": "COMPENSATED",
        "CHG-UNKNOWN-001": "UNKNOWN_MANUAL",
    }
    if report.get("case_count") != 4 or {
        case["change_id"]: case["terminal_status"] for case in report.get("cases", [])
    } != expected:
        raise RuntimeError("operations reference case matrix mismatch")
    verified = 0
    for case in report["cases"]:
        if case["proof"]:
            result = verify_operations_proof(
                reference / case["proof"],
                trust_bundle_path=reference / "trust-bundle.json",
                pinned_policy_path=reference / "pinned-operations-policy.json",
            )
            if not result.valid:
                raise RuntimeError(f"operations proof invalid: {case['change_id']}: {result.errors}")
            verified += 1
    unknown = json.loads((reference / "unknown-manual/unknown-evidence.json").read_text(encoding="utf-8"))
    if (
        unknown["operation"]["status"] != "UNKNOWN"
        or unknown["business_snapshot"]["execution"] is not None
        or unknown["assertions"]["automatic_redispatch_stopped"] is not True
    ):
        raise RuntimeError("operations UNKNOWN evidence is not fail-closed")
    return {"cases": 4, "externally_verified_proofs": verified, "unknown_fail_closed": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(validate(args.root.resolve()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
