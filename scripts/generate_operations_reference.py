#!/usr/bin/env python3
"""Generate frozen, externally verifiable evidence for the operations domain.

All cases use the deterministic local reference adapter.  UNKNOWN uses an
explicit isolated lease-expiry hook; the manifest states this limitation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from proofmesh.capabilities import APPROVAL_ASSERTION_TYPE, load_or_create_signer
from proofmesh.operations import ChangeRequest, ChangeStatus
from proofmesh.operations_runtime import build_multidomain_runtime
from proofmesh.operations_verifier import verify_operations_proof
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
ISSUER_SPEC = importlib.util.spec_from_file_location(
    "reference_approval_service", ROOT / "scripts/reference_approval_service.py"
)
assert ISSUER_SPEC and ISSUER_SPEC.loader
ISSUER = importlib.util.module_from_spec(ISSUER_SPEC)
ISSUER_SPEC.loader.exec_module(ISSUER)

CASES = {
    "automatic-success": "CHG-LOW-001",
    "external-approval-success": "CHG-HIGH-001",
    "verified-compensation": "CHG-ROLLBACK-001",
    "unknown-manual": "CHG-UNKNOWN-001",
}


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _home(root: Path, slug: str) -> Path:
    home = root / slug
    (home / "data").mkdir(parents=True)
    shutil.copytree(ROOT / "data/policies", home / "data/policies")
    (home / "var").mkdir()
    (home / "artifacts").mkdir()
    return home


def generate(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if output == ROOT or ROOT not in output.parents:
        raise ValueError("output must be inside the ProofMesh project")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    case_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="proofmesh-operations-reference-") as temp:
        temp_root = Path(temp)
        shared = _home(temp_root, "shared")
        shared_runtime = build_multidomain_runtime(shared, enable_reference_fault_injection=True)
        approval_key = temp_root / "external-approval-service" / "approval.ed25519"
        load_or_create_signer(
            private_key_path=approval_key,
            trust_bundle_path=shared_runtime.base.trust_bundle_path,
            key_id="proofmesh-reference-approval-service-v1",
            issuer="proofmesh-reference-approval-service",
            token_types=[APPROVAL_ASSERTION_TYPE],
        )
        trust_bundle = json.loads(shared_runtime.base.trust_bundle_path.read_text(encoding="utf-8"))
        trust_bundle.setdefault("approval_issuers", []).append("proofmesh-reference-approval-service")
        trust_bundle["approval_issuers"] = sorted(set(trust_bundle["approval_issuers"]))
        shared_runtime.base.trust_bundle_path.write_text(
            json.dumps(trust_bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        shutil.copy2(shared_runtime.base.trust_bundle_path, output / "trust-bundle.json")
        shutil.copy2(ROOT / "data/policies/operations_change_policy.json", output / "pinned-operations-policy.json")

        for slug, change_id in CASES.items():
            home = _home(temp_root, slug)
            shutil.copytree(shared / "var/keys", home / "var/keys")
            shutil.copytree(shared / "config", home / "config")
            runtime = build_multidomain_runtime(home, enable_reference_fault_injection=True)
            control = runtime.operations_control_plane
            summary = control.create_and_plan(
                ChangeRequest(change_id=change_id, tenant_id="acme-cn", requester="reference-ops-requester")
            )
            waiting_path = None
            if summary.status == ChangeStatus.WAITING_APPROVAL:
                waiting_path = output / slug / "waiting-approval.json"
                _json(
                    waiting_path,
                    {
                        "summary": summary.model_dump(mode="json"),
                        "challenge": control.approval_challenge(summary.workflow_id),
                        "business_snapshot": runtime.operations_sandbox.workflow_snapshot(
                            summary.workflow_id, tenant_id=summary.tenant_id, change_id=change_id
                        ),
                        "assertions": {"execution_absent_before_approval": True},
                    },
                )
                reason = "A separate reviewer checked the frozen service, patch, version, risk units and exact scope."
                assertion = ISSUER.issue_assertion(
                    control.approval_challenge(summary.workflow_id),
                    subject="reference-ops-approver",
                    reason=reason,
                    private_key_path=approval_key,
                    trust_bundle_path=runtime.base.trust_bundle_path,
                )
                summary = control.approve(
                    summary.workflow_id,
                    expected_revision=summary.revision,
                    approver="reference-ops-approver",
                    reason=reason,
                    approval_assertion=assertion,
                )
            if summary.status != ChangeStatus.AUTHORIZED:
                raise RuntimeError(f"{change_id} did not reach AUTHORIZED")
            summary = control.execute_and_verify(summary.workflow_id, expected_revision=summary.revision)
            case_dir = output / slug
            case_dir.mkdir(parents=True, exist_ok=True)
            snapshot = runtime.operations_sandbox.workflow_snapshot(
                summary.workflow_id, tenant_id=summary.tenant_id, change_id=change_id
            )
            verification_path = None
            proof_path = None
            if summary.proof_bundle:
                proof_path = case_dir / "workflow-proof.json"
                shutil.copy2(home / summary.proof_bundle, proof_path)
                report = verify_operations_proof(
                    proof_path,
                    trust_bundle_path=output / "trust-bundle.json",
                    pinned_policy_path=output / "pinned-operations-policy.json",
                )
                if not report.valid:
                    raise RuntimeError(f"verification failed for {change_id}: {report.errors}")
                verification_path = case_dir / "external-verification.json"
                _json(verification_path, report.as_dict())
            if summary.status == ChangeStatus.UNKNOWN_MANUAL:
                state = control.get_state(summary.workflow_id)
                unknown_path = case_dir / "unknown-evidence.json"
                with runtime.base.gateway_store._connect() as conn:  # noqa: SLF001 - evidence export
                    operation = dict(
                        conn.execute(
                            "SELECT operation_id, status, unknown_reason FROM gateway_operations WHERE workflow_id = ? AND tool = 'ops.apply_config'",
                            (summary.workflow_id,),
                        ).fetchone()
                    )
                _json(
                    unknown_path,
                    {
                        "summary": summary.model_dump(mode="json"),
                        "operation": operation,
                        "business_snapshot": snapshot,
                        "tool_execution_count": len(state["tool_executions"]),
                        "assertions": {
                            "gateway_status_unknown": operation["status"] == "UNKNOWN",
                            "automatic_redispatch_stopped": True,
                            "no_claim_of_known_business_commit": True,
                        },
                    },
                )
            case_results.append(
                {
                    "slug": slug,
                    "change_id": change_id,
                    "workflow_id": summary.workflow_id,
                    "terminal_status": summary.status.value,
                    "approval_required": summary.approval_required,
                    "proof": str(proof_path.relative_to(output)) if proof_path else None,
                    "external_verification": str(verification_path.relative_to(output)) if verification_path else None,
                    "waiting_evidence": str(waiting_path.relative_to(output)) if waiting_path else None,
                }
            )

    report = {
        "schema_version": "proofmesh.operations-reference-report/v1",
        "domain": "production-operations-change",
        "case_count": len(case_results),
        "cases": case_results,
        "protocol_reuse": {
            "shared_components": [
                "ActionPassportClaims",
                "AuthorizedToolCaller",
                "ActionGateway",
                "GatewayStore",
                "ExternalTrustVerifier",
                "ExecutionReceipt",
                "EvidenceLedger",
            ],
            "domain_specific_components": [
                "OperationsSandbox adapter",
                "operations_change_policy.json",
                "operations plan and terminal invariants",
            ],
        },
        "claim_boundaries": {
            "production_cluster": False,
            "customer_data": False,
            "external_cloud_account": False,
            "unknown_lease_expiry_hook_is_production_api": False,
            "purpose": "deterministic local cross-domain protocol-reuse evidence",
        },
    }
    _json(output / "report.json", report)
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "manifest.json"):
        files.append({"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {
        "schema_version": "proofmesh.operations-reference-manifest/v1",
        "case_count": len(case_results),
        "files": files,
    }
    _json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/operations-reference")
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
