import sqlite3

import pytest

from proofmesh.business import ToolExecutionError
from proofmesh.workflow import RefundWorkflowRequest, WorkflowStatus
from approval_helpers import issue_approval


def refund_arguments(workflow_id: str) -> dict:
    return {
        "ticket_id": "TKT-LOW-001",
        "order_id": "ORD-1001",
        "amount_minor": 8000,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": workflow_id,
        "_proofmesh_tenant_id": "acme-cn",
    }


def test_business_idempotency_rejects_same_workflow_with_different_arguments(action_runtime):
    sandbox = action_runtime.sandbox
    sandbox.issue_refund(refund_arguments("wf-business-idem"))
    changed = refund_arguments("wf-business-idem")
    changed["ticket_id"] = "TKT-SAGA-001"
    changed["amount_minor"] = 9000
    with pytest.raises(ToolExecutionError, match="different immutable arguments") as exc:
        sandbox.issue_refund(changed)
    assert exc.value.code == "workflow_idempotency_conflict"


def test_order_version_drift_after_approval_requires_replanning(action_runtime):
    control = action_runtime.control_plane
    waiting = control.start(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="operator-a")
    )
    with sqlite3.connect(action_runtime.sandbox.db_path) as conn:
        conn.execute("UPDATE orders SET version = version + 1 WHERE order_id = 'ORD-1001'")
    reason = "reviewed the exact frozen plan before the order changed"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    authorized = control.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver="approver-b",
        reason=reason,
        approval_assertion=assertion,
    )
    blocked = control.execute_authorized(
        waiting.workflow_id,
        expected_revision=authorized.revision,
        actor="executor-a",
    )
    assert blocked.status == WorkflowStatus.BLOCKED
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None


def test_refund_for_ticket_a_cannot_close_ticket_b(action_runtime):
    sandbox = action_runtime.sandbox
    sandbox.issue_refund(refund_arguments("wf-ticket-binding"))
    with pytest.raises(ToolExecutionError, match="issued refund is required") as exc:
        sandbox.close_ticket(
            {
                "ticket_id": "TKT-HIGH-001",
                "workflow_id": "wf-ticket-binding",
                "resolution": "must not close using another ticket's refund",
                "_proofmesh_tenant_id": "acme-cn",
            }
        )
    assert exc.value.code == "refund_not_verified"


def test_compensation_reopens_ticket_and_restores_consistent_state(action_runtime):
    sandbox = action_runtime.sandbox
    before = sandbox.get_refund_context(
        {"order_id": "ORD-1001", "_proofmesh_tenant_id": "acme-cn"}
    )["refundable_minor"]
    refund = sandbox.issue_refund(refund_arguments("wf-closed-compensation"))
    sandbox.close_ticket(
        {
            "ticket_id": "TKT-LOW-001",
            "workflow_id": "wf-closed-compensation",
            "resolution": "refund issued",
            "_proofmesh_tenant_id": "acme-cn",
        }
    )
    sandbox.compensate_refund(
        {
            "refund_id": refund["refund_id"],
            "workflow_id": "wf-closed-compensation",
            "reason": "verified downstream reversal",
            "_proofmesh_tenant_id": "acme-cn",
        }
    )
    snapshot = sandbox.workflow_snapshot("wf-closed-compensation", tenant_id="acme-cn")
    assert snapshot["refund"]["status"] == "COMPENSATED"
    assert snapshot["ticket"]["status"] == "OPEN"
    assert snapshot["ticket"]["closed_workflow_id"] is None
    assert snapshot["order"]["refundable_minor"] == before
