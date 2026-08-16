from __future__ import annotations

import json
import shutil
from pathlib import Path

from proofmesh.operations import ChangeRequest, ChangeStatus
from proofmesh.operations_runtime import build_multidomain_runtime
from proofmesh.operations_verifier import verify_operations_proof
from approval_helpers import MODULE as APPROVAL_MODULE, approval_key, provision_approval_service


def build_operations_runtime(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "operations-home"
    (home / "data").mkdir(parents=True)
    shutil.copytree(root / "data/policies", home / "data/policies")
    (home / "var").mkdir()
    (home / "artifacts").mkdir()
    runtime = build_multidomain_runtime(home, enable_reference_fault_injection=True)
    provision_approval_service(runtime.base, home)
    runtime.base.verifier.__init__(runtime.base.trust_bundle_path)
    runtime.operations_control_plane.approval_verifier.__init__(runtime.base.trust_bundle_path)
    return runtime


def verify(runtime, summary):
    return verify_operations_proof(
        runtime.base.home / summary.proof_bundle,
        trust_bundle_path=runtime.base.trust_bundle_path,
        pinned_policy_path=runtime.base.home / "data/policies/operations_change_policy.json",
    )


def test_low_risk_change_reuses_shared_gateway_and_produces_verifiable_proof(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    authorized = control.create_and_plan(
        ChangeRequest(change_id="CHG-LOW-001", tenant_id="acme-cn", requester="ops-requester")
    )
    assert authorized.status == ChangeStatus.AUTHORIZED
    assert authorized.approval_digest == "AUTOMATIC"
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    assert terminal.status == ChangeStatus.COMPLETED
    state = control.get_state(terminal.workflow_id)
    assert {item["tool"] for item in state["tool_executions"]} == {
        "ops.get_change",
        "ops.get_service_config",
        "ops.apply_config",
        "ops.run_health_check",
    }
    report = verify(runtime, terminal)
    assert report.valid is True, report.errors


def test_high_risk_change_waits_for_external_digest_bound_approval(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    waiting = control.create_and_plan(
        ChangeRequest(change_id="CHG-HIGH-001", tenant_id="acme-cn", requester="ops-requester")
    )
    assert waiting.status == ChangeStatus.WAITING_APPROVAL
    snapshot = runtime.operations_sandbox.workflow_snapshot(
        waiting.workflow_id, tenant_id="acme-cn", change_id="CHG-HIGH-001"
    )
    assert snapshot["execution"] is None
    reason = "reviewed the frozen production patch, version, risk units and exact service scope"
    assertion = APPROVAL_MODULE.issue_assertion(
        control.approval_challenge(waiting.workflow_id),
        subject="ops-approver",
        reason=reason,
        private_key_path=approval_key(runtime.base),
        trust_bundle_path=runtime.base.trust_bundle_path,
    )
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="ops-approver",
        reason=reason,
        approval_assertion=assertion,
    )
    assert authorized.status == ChangeStatus.AUTHORIZED
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    assert terminal.status == ChangeStatus.COMPLETED
    state = control.get_state(terminal.workflow_id)
    apply_receipt = next(item for item in state["receipts"] if item["tool"] == "ops.apply_config")
    assert apply_receipt["approval_assertion_digest"] == state["approval"]["assertion_digest"]
    assert verify(runtime, terminal).valid is True


def test_failed_post_change_gate_restores_exact_pre_change_config(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    before = runtime.operations_sandbox.get_service_config(
        {"service_id": "svc-checkout", "_proofmesh_tenant_id": "acme-cn"}
    )
    authorized = control.create_and_plan(
        ChangeRequest(change_id="CHG-ROLLBACK-001", tenant_id="acme-cn", requester="ops-requester")
    )
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    assert terminal.status == ChangeStatus.COMPENSATED
    after = runtime.operations_sandbox.get_service_config(
        {"service_id": "svc-checkout", "_proofmesh_tenant_id": "acme-cn"}
    )
    assert after["config"] == before["config"]
    assert after["version"] == before["version"] + 2
    state = control.get_state(terminal.workflow_id)
    restore_receipt = next(item for item in state["receipts"] if item["tool"] == "ops.restore_config")
    assert restore_receipt["approval_digest"].startswith("EMERGENCY:")
    assert verify(runtime, terminal).valid is True


def test_unreconcilable_apply_enters_unknown_and_second_retry_is_fail_closed(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    authorized = control.create_and_plan(
        ChangeRequest(change_id="CHG-UNKNOWN-001", tenant_id="acme-cn", requester="ops-requester")
    )
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    assert terminal.status == ChangeStatus.UNKNOWN_MANUAL
    with runtime.base.gateway_store._connect() as conn:  # noqa: SLF001 - verify durable gate state
        row = conn.execute(
            "SELECT status, unknown_reason FROM gateway_operations WHERE workflow_id = ? AND tool = 'ops.apply_config'",
            (authorized.workflow_id,),
        ).fetchone()
    assert dict(row) == {"status": "UNKNOWN", "unknown_reason": "upstream_commit_state_unavailable"}
    snapshot = runtime.operations_sandbox.workflow_snapshot(
        authorized.workflow_id, tenant_id="acme-cn", change_id="CHG-UNKNOWN-001"
    )
    assert snapshot["execution"] is None


def test_operations_proof_tampering_fails_external_verifier(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    authorized = control.create_and_plan(
        ChangeRequest(change_id="CHG-LOW-001", tenant_id="acme-cn", requester="ops-requester")
    )
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    path = runtime.base.home / terminal.proof_bundle
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["verified_business_snapshot"]["service"]["config"]["checkout_timeout_ms"] = 9999
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(runtime, terminal)
    assert report.valid is False
    assert report.checks["bundle_signature"] is False
    assert report.checks["business_snapshot_attestation"] is False
    assert report.checks["terminal_business_state"] is False


def test_trusted_proof_sealer_cannot_forge_operations_approval_semantics(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    control = runtime.operations_control_plane
    waiting = control.create_and_plan(
        ChangeRequest(change_id="CHG-HIGH-001", tenant_id="acme-cn", requester="ops-requester")
    )
    reason = "reviewed the frozen production patch, version, risk units and exact service scope"
    assertion = APPROVAL_MODULE.issue_assertion(
        control.approval_challenge(waiting.workflow_id),
        subject="ops-approver",
        reason=reason,
        private_key_path=approval_key(runtime.base),
        trust_bundle_path=runtime.base.trust_bundle_path,
    )
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="ops-approver",
        reason=reason,
        approval_assertion=assertion,
    )
    terminal = control.execute_and_verify(authorized.workflow_id, expected_revision=authorized.revision)
    path = runtime.base.home / terminal.proof_bundle
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["approval"]["approver"] = "forged-approver"
    unsigned = {key: value for key, value in payload.items() if key != "bundle_signature"}
    payload["bundle_signature"] = runtime.base.proof_signer.sign_payload(
        unsigned,
        token_type="proofmesh-workflow-proof+jws",
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(runtime, terminal)
    assert report.checks["bundle_signature"] is True
    assert report.checks["approval_semantics"] is False
    assert report.valid is False


def test_refund_and_operations_share_one_gateway_store_and_distinct_pinned_policies(tmp_path):
    runtime = build_operations_runtime(tmp_path)
    assert runtime.base.gateway_store is runtime.gateway.store
    assert len(runtime.gateway.pinned_policy_limits) == 2
    assert set(runtime.base.sandbox.specs).isdisjoint(runtime.operations_sandbox.specs)
