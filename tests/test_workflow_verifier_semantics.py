import json
from datetime import datetime, timezone

import pytest

from proofmesh.capabilities import PROOF_TYPE, RECEIPT_TYPE, PassportError, sha256_digest
import proofmesh.capabilities as capabilities_module
from proofmesh.workflow import RefundWorkflowRequest
from proofmesh.workflow_verifier import verify_workflow_proof
from approval_helpers import issue_approval


def resign_bundle(action_runtime, payload: dict) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "bundle_signature"}
    payload["bundle_signature"] = action_runtime.proof_signer.sign_payload(
        unsigned, token_type=PROOF_TYPE
    )


def verify(action_runtime, proof_path):
    return verify_workflow_proof(
        proof_path,
        trust_bundle_path=action_runtime.trust_bundle_path,
        pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
    )


def test_human_approval_receipt_is_cross_bound_and_origin_is_explicit(action_runtime):
    control = action_runtime.control_plane
    waiting = control.run_until_gate_or_terminal(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    reason = "Reviewed the exact frozen plan and action scope"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=reason,
        approval_assertion=assertion,
    )
    terminal = control.resume_authorized_locally(
        authorized.workflow_id, expected_revision=authorized.revision
    )
    proof_path = action_runtime.home / terminal.proof_bundle
    valid = verify(action_runtime, proof_path)
    assert valid.valid is True
    assert valid.approval_origin_assurance == "EXTERNAL_SIGNED_ASSERTION"

    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    human = next(
        receipt
        for receipt in payload["task_step_receipts"]
        if receipt["step"] == "record_human_approval"
    )
    human["actor"] = "forged-approver"
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["task_step_receipts"] is False
    assert report.valid is False


def test_archived_proof_uses_execution_time_not_audit_wall_clock(action_runtime, monkeypatch):
    control = action_runtime.control_plane
    waiting = control.run_until_gate_or_terminal(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    reason = "Reviewed the exact frozen plan and action scope"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=reason,
        approval_assertion=assertion,
    )
    terminal = control.resume_authorized_locally(
        authorized.workflow_id, expected_revision=authorized.revision
    )
    proof_path = action_runtime.home / terminal.proof_bundle

    monkeypatch.setattr(capabilities_module.time, "time", lambda: 4_102_444_800)
    report = verify(action_runtime, proof_path)
    assert report.valid is True
    assert report.approval_origin_assurance == "EXTERNAL_SIGNED_ASSERTION"

    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["approval"]["approved_at"] = datetime.fromtimestamp(
        4_102_444_800, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")
    tampered = verify(action_runtime, proof_path)
    assert tampered.checks["approval_semantics"] is False
    assert tampered.valid is False


@pytest.mark.parametrize("field", ["approval_digest", "approval_assertion_digest"])
def test_human_receipt_rejects_wrapper_or_raw_assertion_digest_tampering(action_runtime, field):
    control = action_runtime.control_plane
    waiting = control.run_until_gate_or_terminal(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    reason = "Reviewed the exact frozen plan, policy, amount and action contracts"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=reason,
        approval_assertion=assertion,
    )
    terminal = control.resume_authorized_locally(
        authorized.workflow_id, expected_revision=authorized.revision
    )
    proof_path = action_runtime.home / terminal.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    human = next(item for item in payload["task_step_receipts"] if item["step"] == "record_human_approval")
    human[field] = "0" * 64
    unsigned_human = {key: value for key, value in human.items() if key != "signature"}
    human["signature"] = action_runtime.task_signer.sign_payload(
        unsigned_human, token_type="proofmesh-task-step+jws"
    )
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["task_step_receipts"] is False
    assert report.valid is False


def test_gateway_receipt_key_cannot_be_reused_as_proof_sealer(action_runtime):
    forged = action_runtime.receipt_signer.sign_payload(
        {"issuer": action_runtime.receipt_signer.issuer},
        token_type=PROOF_TYPE,
    )
    with pytest.raises(PassportError) as rejected:
        action_runtime.verifier.verify_compact(
            forged,
            expected_type=PROOF_TYPE,
            allowed_issuers={"proofmesh-proof-sealer"},
        )
    assert rejected.value.reason == "key_usage_not_allowed"


def test_proof_sealer_cannot_forge_the_business_system_snapshot(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    proof_path = action_runtime.home / summary.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["verified_business_snapshot"]["order"]["refundable_minor"] += 1
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["business_snapshot_attestation"] is False
    assert report.checks["terminal_business_state"] is False
    assert report.valid is False


def test_trusted_signature_cannot_hide_plan_amount_semantic_mismatch(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    proof_path = action_runtime.home / summary.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["plan"]["amount_minor"] = 7000
    payload["summary"]["amount_minor"] = 7000
    payload["summary"]["plan_digest"] = sha256_digest(payload["plan"])
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["plan_binding"] is True
    assert report.checks["tool_execution_semantics"] is False
    assert report.checks["terminal_business_state"] is False
    assert report.valid is False


def test_trusted_signature_cannot_turn_allowed_receipt_into_failed_decision(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    proof_path = action_runtime.home / summary.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    target = next(
        receipt for receipt in payload["gateway_receipts"] if receipt["tool"] == "payments.issue_refund"
    )
    target["decision"] = "upstream_error"
    target["reason"] = "forged_business_failure"
    unsigned_target = {
        key: value for key, value in target.items() if key not in {"signature", "_chain"}
    }
    target["signature"] = action_runtime.receipt_signer.sign_payload(
        unsigned_target, token_type=RECEIPT_TYPE
    )
    previous = "GENESIS"
    for receipt in payload["gateway_receipts"]:
        signed_receipt = {key: value for key, value in receipt.items() if key != "_chain"}
        entry_hash = sha256_digest({"previous_hash": previous, "receipt": signed_receipt})
        receipt["_chain"] = {"previous_hash": previous, "entry_hash": entry_hash}
        previous = entry_hash
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["gateway_receipts"] is True
    assert report.checks["tool_execution_semantics"] is False
    assert report.valid is False


def test_validly_signed_unpinned_policy_is_rejected(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    proof_path = action_runtime.home / summary.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["policy"]["limits"]["auto_approve_minor"] = 999999
    payload["policy_digest"] = sha256_digest(payload["policy"])
    payload["summary"]["policy_digest"] = payload["policy_digest"]
    resign_bundle(action_runtime, payload)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify(action_runtime, proof_path)
    assert report.checks["bundle_signature"] is True
    assert report.checks["embedded_policy_digest"] is True
    assert report.checks["pinned_policy"] is False
    assert report.valid is False
