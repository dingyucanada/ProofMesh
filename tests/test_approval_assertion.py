from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from approval_helpers import MODULE, approval_key, issue_approval
from proofmesh.capabilities import APPROVAL_ASSERTION_TYPE, PROOF_TYPE, sha256_digest
from proofmesh.gateway import GatewayDenied
from proofmesh.workflow import RefundWorkflowRequest, WorkflowConflict
from proofmesh.workflow_verifier import verify_workflow_proof


REASON = "Reviewed the frozen policy, amount, exact action contracts, and recovery boundary."


def waiting_case(runtime, requester: str = "operator-a"):
    return runtime.control_plane.run_until_gate_or_terminal(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester=requester)
    )


def approve(runtime, waiting, token: str, *, subject: str = "approver-b", reason: str = REASON):
    return runtime.control_plane.approve(
        waiting.workflow_id,
        expected_revision=waiting.revision,
        approver=subject,
        reason=reason,
        approval_assertion=token,
    )


def mutate_signed_claims(runtime, token: str, **changes) -> str:
    verified = runtime.verifier.verify_human_approval_assertion(token)
    payload = {**verified.model_dump(mode="json"), **changes}
    signer = MODULE.load_or_create_signer(
        private_key_path=approval_key(runtime),
        trust_bundle_path=runtime.trust_bundle_path,
        key_id=MODULE.KEY_ID,
        issuer=MODULE.ISSUER,
        token_types=[APPROVAL_ASSERTION_TYPE],
    )
    return signer.sign_payload(payload, token_type=APPROVAL_ASSERTION_TYPE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "other-tenant"),
        ("workflow_id", "wf-00000000000000000000000000000000"),
        ("expected_revision", 999),
        ("plan_digest", "0" * 64),
        ("policy_digest", "1" * 64),
        ("context_digest", "2" * 64),
        ("reason_digest", "3" * 64),
        ("amount_minor", 1),
    ],
)
def test_workflow_rejects_validly_signed_but_mismatched_assertion(action_runtime, field, value):
    waiting = waiting_case(action_runtime)
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    forged = mutate_signed_claims(action_runtime, token, **{field: value})
    with pytest.raises(WorkflowConflict, match="does not match"):
        approve(action_runtime, waiting, forged)


def test_workflow_rejects_missing_malformed_and_wrong_bearer_subject_assertion(action_runtime):
    waiting = waiting_case(action_runtime)
    with pytest.raises(WorkflowConflict, match="assertion rejected"):
        approve(action_runtime, waiting, "not-a-jws")
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    with pytest.raises(WorkflowConflict, match="does not match"):
        approve(action_runtime, waiting, token, subject="different-approver")


def test_expired_and_future_assertions_fail_closed(action_runtime):
    waiting = waiting_case(action_runtime)
    old = issue_approval(
        action_runtime,
        waiting,
        subject="approver-b",
        reason=REASON,
        now=int(time.time()) - 1000,
    )
    with pytest.raises(WorkflowConflict, match="approval_assertion_expired"):
        approve(action_runtime, waiting, old)
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    future = mutate_signed_claims(
        action_runtime,
        token,
        auth_time=int(time.time()) + 95,
        issued_at=int(time.time()) + 100,
        not_before=int(time.time()) + 99,
        expires_at=int(time.time()) + 200,
    )
    with pytest.raises(WorkflowConflict, match="issued_in_future"):
        approve(action_runtime, waiting, future)


def test_gateway_rejects_control_plane_passport_without_external_assertion(action_runtime):
    waiting = waiting_case(action_runtime)
    challenge = action_runtime.control_plane.approval_challenge(waiting.workflow_id)
    refund = challenge["actions"][1]
    arguments = {
        "ticket_id": "TKT-HIGH-001",
        "order_id": "ORD-1001",
        "amount_minor": 12900,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": waiting.workflow_id,
    }
    from proofmesh.capabilities import ActionPassportClaims

    claims = ActionPassportClaims.issue(
        issuer=action_runtime.passport_signer.issuer,
        audience=action_runtime.gateway.audience,
        tenant_id=waiting.tenant_id,
        subject="compromised-control-plane",
        workflow_id=waiting.workflow_id,
        tool=refund["tool"],
        resource=refund["resource"],
        scopes=["refund:write"],
        arguments=arguments,
        context_digest=waiting.context_digest,
        policy_digest=waiting.policy_digest,
        plan_digest=waiting.plan_digest,
        approval_digest="f" * 64,
        mode="execute",
        max_amount_minor=12900,
        budget_limit_minor=200000,
        currency="CNY",
    )
    passport = action_runtime.passport_signer.sign_passport(claims)
    with pytest.raises(GatewayDenied, match="approval_assertion_missing"):
        action_runtime.gateway.call_tool(
            tool=refund["tool"],
            arguments=arguments,
            passport=passport,
            workflow_id=waiting.workflow_id,
            context_digest=waiting.context_digest,
            idempotency_key="attack-without-human-assertion",
        )
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None


def test_gateway_rejects_high_value_refund_falsely_labeled_automatic(action_runtime):
    waiting = waiting_case(action_runtime)
    challenge = action_runtime.control_plane.approval_challenge(waiting.workflow_id)
    refund = challenge["actions"][1]
    arguments = {
        "ticket_id": "TKT-HIGH-001",
        "order_id": "ORD-1001",
        "amount_minor": 12900,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": waiting.workflow_id,
    }
    from proofmesh.capabilities import ActionPassportClaims

    claims = ActionPassportClaims.issue(
        issuer=action_runtime.passport_signer.issuer,
        audience=action_runtime.gateway.audience,
        tenant_id=waiting.tenant_id,
        subject="compromised-control-plane",
        workflow_id=waiting.workflow_id,
        tool=refund["tool"],
        resource=refund["resource"],
        scopes=["refund:write"],
        arguments=arguments,
        context_digest=waiting.context_digest,
        policy_digest=waiting.policy_digest,
        plan_digest=waiting.plan_digest,
        approval_digest="AUTOMATIC",
        mode="execute",
        max_amount_minor=12900,
        budget_limit_minor=200000,
        currency="CNY",
    )
    with pytest.raises(GatewayDenied, match="approval_assertion_required_by_gateway_policy"):
        action_runtime.gateway.call_tool(
            tool=refund["tool"],
            arguments=arguments,
            passport=action_runtime.passport_signer.sign_passport(claims),
            workflow_id=waiting.workflow_id,
            context_digest=waiting.context_digest,
            idempotency_key="attack-false-automatic-12900",
        )
    assert action_runtime.sandbox.workflow_snapshot(waiting.workflow_id, tenant_id="acme-cn")["refund"] is None


def test_approval_verifier_requires_external_issuer_allowlist(action_runtime):
    waiting = waiting_case(action_runtime)
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    action_runtime.verifier.approval_issuers = frozenset()
    with pytest.raises(Exception, match="approval_issuer_allowlist_missing"):
        action_runtime.verifier.verify_human_approval_assertion(token)


def test_task_and_proof_keys_cannot_make_forged_human_origin_verify(action_runtime):
    waiting = waiting_case(action_runtime)
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    authorized = approve(action_runtime, waiting, token)
    terminal = action_runtime.control_plane.resume_authorized_locally(
        waiting.workflow_id, expected_revision=authorized.revision
    )
    proof_path = action_runtime.home / terminal.proof_bundle
    payload = json.loads(proof_path.read_text(encoding="utf-8"))
    payload["approval"]["assertion"] = payload["task_step_receipts"][-1]["signature"]
    payload["approval"]["assertion_digest"] = sha256_digest(payload["approval"]["assertion"])
    unsigned = {key: value for key, value in payload.items() if key != "bundle_signature"}
    payload["bundle_signature"] = action_runtime.proof_signer.sign_payload(unsigned, token_type=PROOF_TYPE)
    proof_path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_workflow_proof(
        proof_path,
        trust_bundle_path=action_runtime.trust_bundle_path,
        pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
    )
    assert report.checks["bundle_signature"] is True
    assert report.checks["approval_semantics"] is False
    assert report.valid is False


def test_offline_verifier_rejects_resigned_assertion_revision_challenge_and_action_drift(action_runtime):
    waiting = waiting_case(action_runtime)
    token = issue_approval(action_runtime, waiting, subject="approver-b", reason=REASON)
    authorized = approve(action_runtime, waiting, token)
    terminal = action_runtime.control_plane.resume_authorized_locally(
        waiting.workflow_id, expected_revision=authorized.revision
    )
    proof_path = action_runtime.home / terminal.proof_bundle
    original = json.loads(proof_path.read_text(encoding="utf-8"))
    for field, value in (
        ("expected_revision", 999),
        ("challenge_digest", "0" * 64),
        (
            "actions",
            [
                {
                    "tool": "payments.issue_refund",
                    "resource": "ORD-9999",
                    "args_digest": "9" * 64,
                    "amount_minor": 12900,
                    "currency": "CNY",
                }
            ],
        ),
    ):
        payload = json.loads(json.dumps(original))
        assertion = mutate_signed_claims(action_runtime, token, **{field: value})
        payload["approval"]["assertion"] = assertion
        payload["approval"]["assertion_digest"] = sha256_digest(assertion)
        unsigned = {key: item for key, item in payload.items() if key != "bundle_signature"}
        payload["bundle_signature"] = action_runtime.proof_signer.sign_payload(unsigned, token_type=PROOF_TYPE)
        proof_path.write_text(json.dumps(payload), encoding="utf-8")
        report = verify_workflow_proof(
            proof_path,
            trust_bundle_path=action_runtime.trust_bundle_path,
            pinned_policy_path=action_runtime.home / "data/policies/refund_policy.json",
        )
        assert report.valid is False
        assert report.checks["approval_semantics"] is False or report.checks["task_step_receipts"] is False


def test_control_plane_runtime_never_owns_external_approval_private_key(action_runtime):
    assert not hasattr(action_runtime, "approval_signer")
    assert not hasattr(action_runtime.control_plane, "approval_signer")
    assert "approval-service" not in {path.name for path in (action_runtime.home / "var" / "keys").glob("*")}
    source = (Path(__file__).resolve().parents[1] / "src/proofmesh/runtime.py").read_text(encoding="utf-8")
    assert "approval_signer" not in source
    assert "approval-service.ed25519" not in source
