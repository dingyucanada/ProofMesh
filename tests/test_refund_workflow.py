import json
import threading

import pytest

from proofmesh.workflow import (
    RefundWorkflowRequest,
    WorkerRoleError,
    WorkflowConflict,
    WorkflowStatus,
)
from proofmesh.workflow_verifier import verify_workflow_proof
from approval_helpers import issue_approval


def test_low_value_refund_completes_with_real_side_effects(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    assert summary.status == WorkflowStatus.COMPLETED
    snapshot = action_runtime.sandbox.workflow_snapshot(summary.workflow_id, tenant_id="acme-cn")
    assert snapshot["refund"]["status"] == "ISSUED"
    assert snapshot["ticket"]["status"] == "CLOSED"
    assert snapshot["order"]["refundable_minor"] == 21900
    report = verify_workflow_proof(
        action_runtime.home / summary.proof_bundle,
        trust_bundle_path=action_runtime.trust_bundle_path,
        pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
    )
    assert report.valid is True


def test_high_value_refund_waits_for_distinct_digest_bound_approver(action_runtime):
    waiting = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    assert waiting.status == WorkflowStatus.WAITING_APPROVAL
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None
    self_reason = "requester must not approve their own high risk action"
    with pytest.raises(ValueError, match="requester cannot approve"):
        issue_approval(action_runtime, waiting, subject="operator-a", reason=self_reason)
    good_reason = "reviewed order balance, reason, policy and exact action scope"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=good_reason)
    with pytest.raises(WorkflowConflict, match="does not match"):
        action_runtime.control_plane.approve(
            waiting.workflow_id,
            expected_revision=waiting.revision,
            approver="operator-a",
            reason=good_reason,
            approval_assertion=assertion,
        )
    with pytest.raises(WorkflowConflict, match="does not match"):
        action_runtime.control_plane.approve(
            waiting.workflow_id,
            expected_revision=waiting.revision,
            approver="approver-b",
            reason="reviewed business evidence and frozen action scope",
            approval_assertion=assertion,
        )
    authorized = action_runtime.control_plane.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=good_reason,
        approval_assertion=assertion,
    )
    assert authorized.status == WorkflowStatus.AUTHORIZED
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None
    completed = action_runtime.control_plane.resume_authorized_locally(
        waiting.workflow_id, expected_revision=authorized.revision
    )
    assert completed.status == WorkflowStatus.COMPLETED
    with pytest.raises(WorkflowConflict, match="not waiting"):
        action_runtime.control_plane.approve(
            waiting.workflow_id,
            expected_revision=completed.revision,
            approver="approver-c",
            reason="second approval must never execute the workflow again",
            approval_assertion=assertion,
        )


def test_downstream_failure_triggers_verified_compensation(action_runtime):
    before = action_runtime.sandbox.get_refund_context(
        {"order_id": "ORD-1001", "_proofmesh_tenant_id": "acme-cn"}
    )["refundable_minor"]
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-SAGA-001", tenant_id="acme-cn", requester="operator-a")
    )
    assert summary.status == WorkflowStatus.COMPENSATED
    snapshot = action_runtime.sandbox.workflow_snapshot(summary.workflow_id, tenant_id="acme-cn")
    assert snapshot["refund"]["status"] == "COMPENSATED"
    assert snapshot["ticket"]["status"] == "OPEN"
    assert snapshot["order"]["refundable_minor"] == before
    report = verify_workflow_proof(
        action_runtime.home / summary.proof_bundle,
        trust_bundle_path=action_runtime.trust_bundle_path,
        pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
    )
    assert report.valid is True


def test_workflow_proof_tampering_fails_external_verification(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a")
    )
    proof_path = action_runtime.home / summary.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["verified_business_snapshot"]["ticket"]["status"] = "OPEN"
    proof_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = verify_workflow_proof(
        proof_path,
        trust_bundle_path=action_runtime.trust_bundle_path,
        pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
    )
    assert report.valid is False
    assert report.checks["bundle_signature"] is False
    assert report.checks["terminal_business_state"] is False


def test_tenant_claim_is_enforced_inside_business_queries(action_runtime):
    summary = action_runtime.control_plane.start(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="other-tenant", requester="operator-a")
    )
    assert summary.status == WorkflowStatus.BLOCKED
    assert summary.amount_minor is None


def test_role_separated_steps_use_cas_and_signed_task_receipts(action_runtime):
    control = action_runtime.control_plane
    created = control.create_case(
        RefundWorkflowRequest(ticket_id="TKT-LOW-001", tenant_id="acme-cn", requester="operator-a"),
        actor="leader-1",
    )
    assert created.status == WorkflowStatus.RECEIVED
    with pytest.raises(WorkerRoleError, match="intake"):
        control.normalize_case(
            created.workflow_id,
            expected_revision=created.revision,
            actor="intruder",
            role="executor",
        )
    normalized = control.normalize_case(
        created.workflow_id,
        expected_revision=created.revision,
        actor="intake-1",
        task_id="task-intake-001",
    )
    with pytest.raises(WorkflowConflict, match="revision"):
        control.gather_context(
            created.workflow_id,
            expected_revision=created.revision,
            actor="investigator-1",
        )
    context = control.gather_context(
        created.workflow_id,
        expected_revision=normalized.revision,
        actor="investigator-1",
    )
    authorized = control.evaluate_policy(
        created.workflow_id,
        expected_revision=context.revision,
        actor="policy-1",
    )
    executed = control.execute_authorized(
        created.workflow_id,
        expected_revision=authorized.revision,
        actor="executor-1",
    )
    verified = control.verify_outcome(
        created.workflow_id,
        expected_revision=executed.revision,
        actor="verifier-1",
    )
    completed = control.curate_memory(
        created.workflow_id,
        expected_revision=verified.revision,
        actor="memory-1",
    )
    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.agent_count == 7
    assert completed.task_receipt_count == 7
    state = control.get_state(created.workflow_id)
    assert [receipt["revision_to"] for receipt in state["step_receipts"]] == list(range(7))
    assert all("signature" in receipt for receipt in state["step_receipts"])


def test_waiting_plan_uses_frozen_policy_snapshot_after_runtime_policy_change(action_runtime):
    control = action_runtime.control_plane
    waiting = control.start(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    frozen_digest = waiting.policy_digest
    control.policy["limits"]["max_workflow_refund_minor"] = 1
    control.policy_digest = "f" * 64
    reason = "reviewed the frozen evidence, policy and exact action scope"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=reason,
        approval_assertion=assertion,
    )
    assert authorized.policy_digest == frozen_digest
    completed = control.resume_authorized_locally(
        waiting.workflow_id, expected_revision=authorized.revision
    )
    assert completed.status == WorkflowStatus.COMPLETED


def test_concurrent_approvers_produce_one_atomic_authorization_and_no_execution(action_runtime):
    control = action_runtime.control_plane
    waiting = control.start(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def approve_as(identity: str) -> None:
        reason = f"{identity} reviewed the frozen policy evidence and exact scope"
        assertion = issue_approval(action_runtime, waiting, subject=identity, reason=reason)
        barrier.wait(timeout=3)
        try:
            control.approve(
                waiting.workflow_id,
                expected_revision=waiting.revision,
                approver=identity,
                reason=reason,
                approval_assertion=assertion,
            )
            outcomes.append("authorized")
        except WorkflowConflict:
            outcomes.append("conflict")

    threads = [
        threading.Thread(target=approve_as, args=("approver-b",)),
        threading.Thread(target=approve_as, args=("approver-c",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(outcomes) == ["authorized", "conflict"]
    current = control.get(waiting.workflow_id)
    assert current.status == WorkflowStatus.AUTHORIZED
    assert current.revision == waiting.revision + 1
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None
    assert [event["event_type"] for event in control.events(waiting.workflow_id)].count(
        "approval.recorded"
    ) == 1
