#!/usr/bin/env python3
"""Generate reviewer-facing evidence by executing isolated ProofMesh workflows.

The files are derived from real state transitions in the deterministic commerce
sandbox.  They are not model-quality or production-availability measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from proofmesh.runtime import build_runtime
from proofmesh.capabilities import APPROVAL_ASSERTION_TYPE, load_or_create_signer
from proofmesh.workflow import RefundWorkflowRequest, WorkflowStatus
from proofmesh.workflow_verifier import verify_workflow_proof
import importlib.util


_ISSUER_SPEC = importlib.util.spec_from_file_location(
    "reference_approval_service", Path(__file__).with_name("reference_approval_service.py")
)
assert _ISSUER_SPEC is not None and _ISSUER_SPEC.loader is not None
_ISSUER_MODULE = importlib.util.module_from_spec(_ISSUER_SPEC)
_ISSUER_SPEC.loader.exec_module(_ISSUER_MODULE)
issue_assertion = _ISSUER_MODULE.issue_assertion


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "automatic": "TKT-LOW-001",
    "human-approval": "TKT-HIGH-001",
    "compensation": "TKT-SAGA-001",
}
ACTORS = {
    "orchestrator": "reference-case-orchestrator",
    "intake": "reference-ticket-intake",
    "investigator": "reference-context-investigator",
    "policy": "reference-risk-policy",
    "approver": "reference-separation-of-duties-approver",
    "executor": "reference-action-executor",
    "verifier": "reference-outcome-verifier",
    "memory": "reference-memory-curator",
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


def _prepare_home(parent: Path, slug: str, *, signing_home: Path | None = None) -> Path:
    home = parent / slug
    (home / "data").mkdir(parents=True)
    shutil.copytree(ROOT / "data" / "policies", home / "data" / "policies")
    (home / "var").mkdir()
    (home / "artifacts").mkdir()
    if signing_home is not None:
        shutil.copytree(signing_home / "var" / "keys", home / "var" / "keys")
        shutil.copytree(signing_home / "config" / "trust", home / "config" / "trust")
    return home


def _run_case(
    parent: Path,
    slug: str,
    ticket_id: str,
    output: Path,
    *,
    signing_home: Path,
    approval_key_path: Path,
) -> dict[str, Any]:
    home = _prepare_home(parent, slug, signing_home=signing_home)
    runtime = build_runtime(home)
    control = runtime.control_plane
    summary = control.create_case(
        RefundWorkflowRequest(
            ticket_id=ticket_id,
            tenant_id="acme-cn",
            requester=ACTORS["orchestrator"],
        ),
        actor=ACTORS["orchestrator"],
        task_id=f"reference:{slug}:create",
    )
    summary = control.normalize_case(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["intake"],
        task_id=f"reference:{slug}:normalize",
    )
    summary = control.gather_context(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["investigator"],
        task_id=f"reference:{slug}:context",
    )
    summary = control.evaluate_policy(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["policy"],
        task_id=f"reference:{slug}:policy",
    )

    waiting_path: Path | None = None
    if summary.status == WorkflowStatus.WAITING_APPROVAL:
        waiting_path = output / slug / "waiting-approval.json"
        waiting_state = control.get_state(summary.workflow_id)
        _json(
            waiting_path,
            {
                "schema_version": "proofmesh.reference-waiting-evidence/v1",
                "summary": summary.model_dump(mode="json"),
                "task_step_receipts": waiting_state["step_receipts"],
                "workflow_events": control.events(summary.workflow_id),
                "business_snapshot": runtime.sandbox.workflow_snapshot(
                    summary.workflow_id, tenant_id=summary.tenant_id
                ),
                "assertions": {
                    "status_is_waiting_approval": summary.status == WorkflowStatus.WAITING_APPROVAL,
                    "refund_not_executed": runtime.sandbox.workflow_snapshot(
                        summary.workflow_id, tenant_id=summary.tenant_id
                    )["refund"]
                    is None,
                    "frozen_plan_digest_present": bool(summary.plan_digest),
                    "approval_scope_present": bool(summary.approval_scope),
                },
            },
        )
        reason = "A separately authenticated reviewer checked the frozen evidence, policy, amount, and exact action scope."
        assertion = issue_assertion(
            control.approval_challenge(summary.workflow_id),
            subject=ACTORS["approver"],
            reason=reason,
            private_key_path=approval_key_path,
            trust_bundle_path=runtime.trust_bundle_path,
        )
        summary = control.approve(
            summary.workflow_id,
            expected_revision=summary.revision,
            approver=ACTORS["approver"],
            reason=reason,
            approval_assertion=assertion,
            task_id=f"reference:{slug}:approval",
        )

    if summary.status != WorkflowStatus.AUTHORIZED:
        raise RuntimeError(f"{ticket_id} did not reach AUTHORIZED: {summary.status.value}")
    summary = control.execute_authorized(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["executor"],
        task_id=f"reference:{slug}:execute",
    )
    if summary.status != WorkflowStatus.EXECUTED:
        raise RuntimeError(f"{ticket_id} did not reach EXECUTED: {summary.status.value}")
    summary = control.verify_outcome(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["verifier"],
        task_id=f"reference:{slug}:verify",
    )
    if summary.status not in {WorkflowStatus.VERIFIED, WorkflowStatus.COMPENSATED}:
        raise RuntimeError(f"{ticket_id} was not independently verified: {summary.status.value}")
    summary = control.curate_memory(
        summary.workflow_id,
        expected_revision=summary.revision,
        actor=ACTORS["memory"],
        task_id=f"reference:{slug}:memory",
    )

    case_dir = output / slug
    case_dir.mkdir(parents=True, exist_ok=True)
    source_proof = home / str(summary.proof_bundle)
    proof_path = case_dir / "workflow-proof.json"
    shutil.copy2(source_proof, proof_path)
    report = verify_workflow_proof(
        proof_path,
        trust_bundle_path=runtime.trust_bundle_path,
        pinned_policy_path=home / "data" / "policies" / "refund_policy.json",
    )
    if not report.valid:
        raise RuntimeError(f"external verification failed for {ticket_id}: {report.errors}")
    report_path = case_dir / "external-verification.json"
    _json(report_path, report.as_dict())
    snapshot = runtime.sandbox.workflow_snapshot(summary.workflow_id, tenant_id=summary.tenant_id)
    return {
        "slug": slug,
        "ticket_id": ticket_id,
        "workflow_id": summary.workflow_id,
        "terminal_status": summary.status.value,
        "revision": summary.revision,
        "agent_count": summary.agent_count,
        "task_receipt_count": summary.task_receipt_count,
        "gateway_receipt_count": summary.gateway_receipt_count,
        "approval_required": summary.approval_required,
        "approval_digest": summary.approval_digest,
        "external_verification_valid": report.valid,
        "proof": str(proof_path.relative_to(output)),
        "verification_report": str(report_path.relative_to(output)),
        "waiting_evidence": str(waiting_path.relative_to(output)) if waiting_path else None,
        "business_outcome": {
            "refund_status": (snapshot.get("refund") or {}).get("status"),
            "ticket_status": (snapshot.get("ticket") or {}).get("status"),
            "refundable_minor": (snapshot.get("order") or {}).get("refundable_minor"),
        },
    }


def generate(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if output == ROOT or ROOT not in output.parents:
        raise ValueError("output must be a child of the ProofMesh project root")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="proofmesh-reference-") as temp:
        temp_root = Path(temp)
        signing_home = _prepare_home(temp_root, "shared-signing-material")
        build_runtime(signing_home)
        approval_service_home = temp_root / "external-approval-service"
        approval_service_home.mkdir()
        approval_key_path = approval_service_home / "approval-service.ed25519"
        load_or_create_signer(
            private_key_path=approval_key_path,
            trust_bundle_path=signing_home / "config" / "trust" / "action-issuers.json",
            key_id="proofmesh-reference-approval-service-v1",
            issuer="proofmesh-reference-approval-service",
            token_types=[APPROVAL_ASSERTION_TYPE],
        )
        cases = [
            _run_case(
                temp_root,
                slug,
                ticket,
                output,
                signing_home=signing_home,
                approval_key_path=approval_key_path,
            )
            for slug, ticket in CASES.items()
        ]
        shutil.copy2(signing_home / "config" / "trust" / "action-issuers.json", output / "trust-bundle.json")
        shutil.copy2(ROOT / "data" / "policies" / "refund_policy.json", output / "pinned-refund-policy.json")

    # Release verification intentionally uses only the exported public material,
    # never the per-run in-memory verifier or temporary private keys.
    for case in cases:
        report = verify_workflow_proof(
            output / case["proof"],
            trust_bundle_path=output / "trust-bundle.json",
            pinned_policy_path=output / "pinned-refund-policy.json",
        )
        case["external_verification_valid"] = report.valid
        _json(output / case["verification_report"], report.as_dict())
        if not report.valid:
            raise RuntimeError(
                f"packaged external verification failed for {case['ticket_id']}: {report.errors}"
            )

    lines = [
        "# ProofMesh reference evidence",
        "",
        "> These artifacts were generated by executing the role-separated workflow against an isolated deterministic commerce sandbox. They are not model-quality or production-availability measurements.",
        "",
        "| Case | Gate / terminal result | Task receipts | Gateway receipts | External verification |",
        "|---|---:|---:|---:|---:|",
    ]
    for case in cases:
        gate = "WAITING_APPROVAL → " if case["waiting_evidence"] else ""
        lines.append(
            f"| {case['ticket_id']} | {gate}{case['terminal_status']} | "
            f"{case['task_receipt_count']} | {case['gateway_receipt_count']} | "
            f"{'PASS' if case['external_verification_valid'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "Verification uses the separately exported public trust bundle and pinned policy. No private signing key, token, SQLite database, or runtime cache is included.",
            "",
            "The Human assertion is synthetic reference evidence issued by a separate script/key boundary; it is not enterprise IdP authentication or MFA evidence. One assertion JTI covers the two exact bound actions and their idempotent retries only, and cannot cross workflow/revision/plan bindings.",
            "",
        ]
    )
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    files = sorted(path for path in output.rglob("*") if path.is_file())
    manifest = {
        "schema_version": "proofmesh.reference-evidence-manifest/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated deterministic commerce sandbox; not a model-quality or production-availability benchmark",
        "approval_origin": (
            "reference script signed a synthetic assertion with a private key outside the ProofMesh runtime; "
            "not enterprise IdP authentication or MFA evidence"
        ),
        "approval_assertion_replay_semantics": (
            "one JTI covers two exact bound actions and idempotent retries; workflow, revision, plan, policy, "
            "context, subject and action contracts prevent cross-case reuse"
        ),
        "cases": cases,
        "files": [
            {
                "path": str(path.relative_to(output)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
        "private_keys_included": False,
        "databases_included": False,
        "tokens_included": False,
    }
    _json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "reference")
    args = parser.parse_args()
    manifest = generate(args.output)
    print(json.dumps({"output": str(args.output.resolve()), "cases": manifest["cases"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
