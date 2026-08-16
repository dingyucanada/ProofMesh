from __future__ import annotations

import hashlib
import json
from pathlib import Path

from proofmesh.operations_verifier import verify_operations_proof


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "artifacts/operations-reference"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_operations_reference_manifest_and_semantics():
    manifest = json.loads((REFERENCE / "manifest.json").read_text(encoding="utf-8"))
    declared = {entry["path"]: entry for entry in manifest["files"]}
    actual = {
        str(path.relative_to(REFERENCE))
        for path in REFERENCE.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert set(declared) == actual
    for relative, entry in declared.items():
        path = REFERENCE / relative
        assert path.stat().st_size == entry["bytes"]
        assert _sha256(path) == entry["sha256"]

    report = json.loads((REFERENCE / "report.json").read_text(encoding="utf-8"))
    assert report["case_count"] == 4
    assert report["claim_boundaries"] == {
        "production_cluster": False,
        "customer_data": False,
        "external_cloud_account": False,
        "unknown_lease_expiry_hook_is_production_api": False,
        "purpose": "deterministic local cross-domain protocol-reuse evidence",
    }
    assert {case["terminal_status"] for case in report["cases"]} == {
        "COMPLETED",
        "COMPENSATED",
        "UNKNOWN_MANUAL",
    }
    for case in report["cases"]:
        if case["proof"]:
            verification = verify_operations_proof(
                REFERENCE / case["proof"],
                trust_bundle_path=REFERENCE / "trust-bundle.json",
                pinned_policy_path=REFERENCE / "pinned-operations-policy.json",
            )
            assert verification.valid is True, verification.errors


def test_unknown_reference_proves_durable_stop_without_claiming_commit_state():
    evidence = json.loads(
        (REFERENCE / "unknown-manual/unknown-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["summary"]["status"] == "UNKNOWN_MANUAL"
    assert evidence["operation"]["status"] == "UNKNOWN"
    assert evidence["business_snapshot"]["execution"] is None
    assert evidence["assertions"] == {
        "gateway_status_unknown": True,
        "automatic_redispatch_stopped": True,
        "no_claim_of_known_business_commit": True,
    }
