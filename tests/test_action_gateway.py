import json
import sqlite3
import threading
import time

import pytest

from proofmesh.business import ReconciliationResult
from proofmesh.capabilities import ActionPassportClaims, ExternalTrustVerifier, PassportError, sha256_digest
from proofmesh.gateway import GatewayDenied


def issue_read_passport(action_runtime, *, workflow_id="wf-security-test", arguments=None, ttl=30):
    arguments = arguments or {"ticket_id": "TKT-LOW-001"}
    context_digest = sha256_digest({"context": workflow_id})
    claims = ActionPassportClaims.issue(
        issuer=action_runtime.passport_signer.issuer,
        audience=action_runtime.gateway.audience,
        tenant_id="acme-cn",
        subject="context-investigator",
        workflow_id=workflow_id,
        tool="crm.get_ticket",
        resource=arguments["ticket_id"],
        scopes=["ticket:read"],
        arguments=arguments,
        context_digest=context_digest,
        policy_digest="a" * 64,
        approval_digest="AUTOMATIC",
        mode="read",
        max_calls=1,
        max_amount_minor=0,
        budget_limit_minor=200000,
        ttl_seconds=ttl,
    )
    return claims, action_runtime.passport_signer.sign_passport(claims), context_digest


def issue_refund_passport(
    action_runtime,
    *,
    workflow_id,
    arguments,
    context_digest,
    policy_digest=None,
    subject="action-executor",
    budget_limit_minor=200000,
):
    policy_digest = policy_digest or action_runtime.gateway.pinned_policy_digest
    claims = ActionPassportClaims.issue(
        issuer=action_runtime.passport_signer.issuer,
        audience=action_runtime.gateway.audience,
        tenant_id="acme-cn",
        subject=subject,
        workflow_id=workflow_id,
        tool="payments.issue_refund",
        resource="ORD-1001",
        scopes=["refund:write"],
        arguments=arguments,
        context_digest=context_digest,
        policy_digest=policy_digest,
        approval_digest="AUTOMATIC",
        mode="execute",
        max_amount_minor=8000,
        budget_limit_minor=budget_limit_minor,
        ttl_seconds=30,
    )
    return claims, action_runtime.passport_signer.sign_passport(claims)


def test_gateway_executes_real_tool_and_safe_retry_is_idempotent(action_runtime):
    claims, token, context_digest = issue_read_passport(action_runtime)
    first = action_runtime.gateway.call_tool(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-read-ticket-001",
    )
    retry = action_runtime.gateway.call_tool(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-read-ticket-001",
    )
    assert first["result"]["ticket_id"] == "TKT-LOW-001"
    assert retry["idempotent_replay"] is True
    assert retry["receipt"] == first["receipt"]


def test_new_idempotency_key_cannot_replay_single_use_passport(action_runtime):
    claims, token, context_digest = issue_read_passport(action_runtime, workflow_id="wf-replay-test")
    kwargs = dict(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
    )
    action_runtime.gateway.call_tool(**kwargs, idempotency_key="idem-replay-first")
    with pytest.raises(GatewayDenied, match="passport_exhausted"):
        action_runtime.gateway.call_tool(**kwargs, idempotency_key="idem-replay-second")


def test_argument_and_context_drift_are_rejected_before_dispatch(action_runtime):
    claims, token, context_digest = issue_read_passport(action_runtime, workflow_id="wf-drift-test")
    with pytest.raises(GatewayDenied, match="arguments_mismatch"):
        action_runtime.gateway.call_tool(
            tool="crm.get_ticket",
            arguments={"ticket_id": "TKT-HIGH-001"},
            passport=token,
            workflow_id=claims.workflow_id,
            context_digest=context_digest,
            idempotency_key="idem-argument-drift",
        )
    with pytest.raises(GatewayDenied, match="context_mismatch"):
        action_runtime.gateway.call_tool(
            tool="crm.get_ticket",
            arguments={"ticket_id": "TKT-LOW-001"},
            passport=token,
            workflow_id=claims.workflow_id,
            context_digest="b" * 64,
            idempotency_key="idem-context-drift",
        )


def test_tampered_token_and_embedded_replacement_key_are_not_trusted(action_runtime, tmp_path):
    claims, token, context_digest = issue_read_passport(action_runtime, workflow_id="wf-signature-test")
    parts = token.split(".")
    flipped = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    tampered = f"{parts[0]}.{parts[1]}.{flipped}"
    with pytest.raises(GatewayDenied, match="invalid_signature"):
        action_runtime.gateway.call_tool(
            tool="crm.get_ticket",
            arguments={"ticket_id": "TKT-LOW-001"},
            passport=tampered,
            workflow_id=claims.workflow_id,
            context_digest=context_digest,
            idempotency_key="idem-bad-signature",
        )

    attacker_bundle = tmp_path / "attacker-trust.json"
    attacker_bundle.write_text(
        json.dumps({"schema_version": "proofmesh.trust-bundle/v1", "keys": {}}), encoding="utf-8"
    )
    with pytest.raises(PassportError, match="trust_bundle_empty"):
        ExternalTrustVerifier(attacker_bundle)


def test_mcp_jsonrpc_requires_passport_metadata(action_runtime):
    response = action_runtime.gateway.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "crm.get_ticket", "arguments": {"ticket_id": "TKT-LOW-001"}},
        }
    )
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["proofmesh/reason"] == "required_metadata_missing"


def test_pending_attempt_recovers_after_post_side_effect_finalize_failure(action_runtime, monkeypatch):
    workflow_id = "wf-finalize-recovery"
    context_digest = sha256_digest({"context": workflow_id})
    arguments = {
        "ticket_id": "TKT-LOW-001",
        "order_id": "ORD-1001",
        "amount_minor": 8000,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": workflow_id,
    }
    claims, token = issue_refund_passport(
        action_runtime,
        workflow_id=workflow_id,
        arguments=arguments,
        context_digest=context_digest,
    )
    real_finalize = action_runtime.gateway_store.finalize
    attempts = {"count": 0}

    def fail_once(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated durable-receipt outage")
        return real_finalize(**kwargs)

    monkeypatch.setattr(action_runtime.gateway_store, "finalize", fail_once)
    call = dict(
        tool="payments.issue_refund",
        arguments=arguments,
        passport=token,
        workflow_id=workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-finalize-recovery",
    )
    with pytest.raises(RuntimeError, match="durable-receipt outage"):
        action_runtime.gateway.call_tool(**call)
    assert action_runtime.sandbox.workflow_snapshot(workflow_id, tenant_id="acme-cn")["refund"] is not None
    with sqlite3.connect(action_runtime.gateway_store.db_path) as conn:
        conn.execute(
            "UPDATE gateway_operations SET lease_until = 0 WHERE idempotency_key = ?",
            ("idem-finalize-recovery",),
        )
    replacement_claims, replacement_token = issue_refund_passport(
        action_runtime,
        workflow_id=workflow_id,
        arguments=arguments,
        context_digest=context_digest,
    )
    assert replacement_claims.jti != claims.jti
    call["passport"] = replacement_token
    recovered = action_runtime.gateway.call_tool(**call)
    assert recovered["result"]["idempotent_replay"] is True
    assert len(action_runtime.gateway_store.receipts(workflow_id)) == 1

    with sqlite3.connect(action_runtime.gateway_store.db_path) as conn:
        usage = conn.execute("SELECT SUM(call_count), SUM(amount_minor) FROM capability_usage_v2").fetchone()
        budget = conn.execute("SELECT amount_minor FROM workflow_budget_usage_v2").fetchone()
    assert usage == (1, 8000)
    assert budget == (8000,)


def test_recovery_passport_must_match_every_immutable_binding(action_runtime, monkeypatch):
    workflow_id = "wf-recovery-binding"
    context_digest = sha256_digest({"context": workflow_id})
    arguments = {
        "ticket_id": "TKT-LOW-001",
        "order_id": "ORD-1001",
        "amount_minor": 8000,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": workflow_id,
    }
    _, token = issue_refund_passport(
        action_runtime, workflow_id=workflow_id, arguments=arguments, context_digest=context_digest
    )
    monkeypatch.setattr(
        action_runtime.gateway_store,
        "finalize",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("receipt store offline")),
    )
    call = dict(
        tool="payments.issue_refund",
        arguments=arguments,
        passport=token,
        workflow_id=workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-recovery-binding",
    )
    with pytest.raises(RuntimeError, match="offline"):
        action_runtime.gateway.call_tool(**call)
    with sqlite3.connect(action_runtime.gateway_store.db_path) as conn:
        conn.execute("UPDATE gateway_operations SET lease_until = 0")
    _, drifted_token = issue_refund_passport(
        action_runtime,
        workflow_id=workflow_id,
        arguments=arguments,
        context_digest=context_digest,
        policy_digest="c" * 64,
    )
    call["passport"] = drifted_token
    with pytest.raises(GatewayDenied, match="gateway_pinned_policy_mismatch"):
        action_runtime.gateway.call_tool(**call)


def test_workflow_budget_is_total_across_multiple_agent_subjects(action_runtime):
    workflow_id = "wf-total-budget"
    context_digest = sha256_digest({"context": workflow_id})
    arguments = {
        "ticket_id": "TKT-LOW-001",
        "order_id": "ORD-1001",
        "amount_minor": 8000,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": workflow_id,
    }
    _, first_token = issue_refund_passport(
        action_runtime,
        workflow_id=workflow_id,
        arguments=arguments,
        context_digest=context_digest,
        subject="executor-a",
        budget_limit_minor=10000,
    )
    action_runtime.gateway.call_tool(
        tool="payments.issue_refund",
        arguments=arguments,
        passport=first_token,
        workflow_id=workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-budget-first",
    )
    _, second_token = issue_refund_passport(
        action_runtime,
        workflow_id=workflow_id,
        arguments=arguments,
        context_digest=context_digest,
        subject="executor-b",
        budget_limit_minor=10000,
    )
    with pytest.raises(GatewayDenied, match="workflow_budget_exceeded"):
        action_runtime.gateway.call_tool(
            tool="payments.issue_refund",
            arguments=arguments,
            passport=second_token,
            workflow_id=workflow_id,
            context_digest=context_digest,
            idempotency_key="idem-budget-second",
        )
def test_unknown_reconciliation_never_redispatches(action_runtime, monkeypatch):
    claims, token, context_digest = issue_read_passport(action_runtime, workflow_id="wf-unknown-reconcile")
    real_finalize = action_runtime.gateway_store.finalize
    monkeypatch.setattr(
        action_runtime.gateway_store,
        "finalize",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("receipt store offline")),
    )
    call = dict(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-unknown-reconcile",
    )
    with pytest.raises(RuntimeError, match="offline"):
        action_runtime.gateway.call_tool(**call)
    monkeypatch.setattr(action_runtime.gateway_store, "finalize", real_finalize)
    with sqlite3.connect(action_runtime.gateway_store.db_path) as conn:
        conn.execute("UPDATE gateway_operations SET lease_until = 0")
    replacement, replacement_token, _ = issue_read_passport(
        action_runtime, workflow_id=claims.workflow_id
    )
    assert replacement.jti != claims.jti
    monkeypatch.setattr(
        action_runtime.sandbox,
        "reconcile",
        lambda *args, **kwargs: ReconciliationResult("UNKNOWN", reason="provider_timeout"),
    )
    call["passport"] = replacement_token
    with pytest.raises(GatewayDenied, match="manual_reconciliation"):
        action_runtime.gateway.call_tool(**call)
    with sqlite3.connect(action_runtime.gateway_store.db_path) as conn:
        status = conn.execute("SELECT status FROM gateway_operations").fetchone()[0]
    assert status == "UNKNOWN"
    with pytest.raises(GatewayDenied, match="manual_reconciliation"):
        action_runtime.gateway.call_tool(**call)


def test_same_idempotency_key_cannot_dispatch_concurrently(action_runtime, monkeypatch):
    claims, token, context_digest = issue_read_passport(action_runtime, workflow_id="wf-concurrent-idem")
    entered = threading.Event()
    release = threading.Event()
    original_call = action_runtime.sandbox.call

    def slow_call(tool, arguments):
        entered.set()
        assert release.wait(timeout=5)
        return original_call(tool, arguments)

    monkeypatch.setattr(action_runtime.sandbox, "call", slow_call)
    call = dict(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-concurrent-read",
    )
    result: dict[str, object] = {}

    def first_dispatch():
        result["response"] = action_runtime.gateway.call_tool(**call)

    thread = threading.Thread(target=first_dispatch)
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(GatewayDenied, match="attempt_in_progress"):
        action_runtime.gateway.call_tool(**call)
    release.set()
    thread.join(timeout=5)
    assert "response" in result


def test_lease_heartbeat_prevents_takeover_during_long_upstream_call(action_runtime, monkeypatch):
    action_runtime.gateway_store.LEASE_SECONDS = 1
    claims, token, context_digest = issue_read_passport(
        action_runtime, workflow_id="wf-long-call-heartbeat"
    )
    entered = threading.Event()
    release = threading.Event()
    original_call = action_runtime.sandbox.call

    def long_call(tool, arguments):
        entered.set()
        assert release.wait(timeout=5)
        return original_call(tool, arguments)

    monkeypatch.setattr(action_runtime.sandbox, "call", long_call)
    call = dict(
        tool="crm.get_ticket",
        arguments={"ticket_id": "TKT-LOW-001"},
        passport=token,
        workflow_id=claims.workflow_id,
        context_digest=context_digest,
        idempotency_key="idem-long-call-heartbeat",
    )
    result: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("response", action_runtime.gateway.call_tool(**call))
    )
    thread.start()
    assert entered.wait(timeout=3)
    time.sleep(1.25)
    with pytest.raises(GatewayDenied, match="attempt_in_progress"):
        action_runtime.gateway.call_tool(**call)
    release.set()
    thread.join(timeout=5)
    assert "response" in result


def test_receipt_signing_key_cannot_mint_action_passports(action_runtime):
    arguments = {"ticket_id": "TKT-LOW-001"}
    context_digest = sha256_digest({"context": "key-confusion"})
    claims = ActionPassportClaims.issue(
        issuer=action_runtime.receipt_signer.issuer,
        audience=action_runtime.gateway.audience,
        tenant_id="acme-cn",
        subject="context-investigator",
        workflow_id="wf-key-confusion",
        tool="crm.get_ticket",
        resource="TKT-LOW-001",
        scopes=["ticket:read"],
        arguments=arguments,
        context_digest=context_digest,
        policy_digest="a" * 64,
        approval_digest="AUTOMATIC",
        mode="read",
        budget_limit_minor=200000,
        ttl_seconds=30,
    )
    forged = action_runtime.receipt_signer.sign_passport(claims)
    with pytest.raises(GatewayDenied, match="key_usage_not_allowed"):
        action_runtime.gateway.call_tool(
            tool="crm.get_ticket",
            arguments=arguments,
            passport=forged,
            workflow_id=claims.workflow_id,
            context_digest=context_digest,
            idempotency_key="idem-key-confusion",
        )


def test_invalid_passport_time_window_and_revoked_key_fail_closed(action_runtime, tmp_path):
    claims, _, context_digest = issue_read_passport(action_runtime, workflow_id="wf-time-window")
    invalid = claims.model_copy(update={"not_before": claims.issued_at + 5})
    invalid_token = action_runtime.passport_signer.sign_passport(invalid)
    with pytest.raises(GatewayDenied, match="invalid_time_window"):
        action_runtime.gateway.call_tool(
            tool="crm.get_ticket",
            arguments={"ticket_id": "TKT-LOW-001"},
            passport=invalid_token,
            workflow_id=claims.workflow_id,
            context_digest=context_digest,
            idempotency_key="idem-invalid-time-window",
        )

    bundle = json.loads(action_runtime.trust_bundle_path.read_text(encoding="utf-8"))
    bundle["keys"][action_runtime.passport_signer.key_id]["revoked"] = True
    revoked_path = tmp_path / "revoked-trust.json"
    revoked_path.write_text(json.dumps(bundle), encoding="utf-8")
    revoked_gateway = type(action_runtime.gateway)(
        verifier=ExternalTrustVerifier(revoked_path),
        receipt_signer=action_runtime.receipt_signer,
        store=action_runtime.gateway_store,
        upstream=action_runtime.sandbox,
    )
    valid_claims, valid_token, valid_context = issue_read_passport(
        action_runtime, workflow_id="wf-revoked-key"
    )
    with pytest.raises(GatewayDenied, match="key_revoked"):
        revoked_gateway.call_tool(
            tool="crm.get_ticket",
            arguments={"ticket_id": "TKT-LOW-001"},
            passport=valid_token,
            workflow_id=valid_claims.workflow_id,
            context_digest=valid_context,
            idempotency_key="idem-revoked-key",
        )
