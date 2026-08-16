from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .capabilities import (
    BUSINESS_ATTESTATION_TYPE,
    PROOF_TYPE,
    RECEIPT_TYPE,
    ExternalTrustVerifier,
    sha256_digest,
)


@dataclass
class OperationsVerificationReport:
    valid: bool = False
    workflow_id: str = ""
    terminal_status: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def verify_operations_proof(
    proof_path: str | Path,
    *,
    trust_bundle_path: str | Path,
    pinned_policy_path: str | Path,
) -> OperationsVerificationReport:
    """Package-independent semantic verifier for the second-domain proof."""

    report = OperationsVerificationReport()

    def check(name: str, condition: bool, error: str) -> None:
        report.checks[name] = bool(condition)
        if not condition:
            report.errors.append(error)

    try:
        payload = json.loads(Path(proof_path).read_text(encoding="utf-8"))
        verifier = ExternalTrustVerifier(trust_bundle_path)
    except Exception as exc:
        check("input_and_trust", False, f"proof or trust input invalid: {type(exc).__name__}")
        return report
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    report.workflow_id = str(summary.get("workflow_id", ""))
    report.terminal_status = str(summary.get("status", ""))
    check(
        "schema_and_claim_boundary",
        payload.get("schema_version") == "proofmesh.operations-workflow-proof/v1"
        and "not a production cluster" in str(payload.get("claim_boundary", "")),
        "operations proof schema or honest claim boundary is missing",
    )
    try:
        signed = verifier.verify_compact(
            str(payload.get("bundle_signature", "")),
            expected_type=PROOF_TYPE,
            allowed_issuers={"proofmesh-proof-sealer"},
        )
        bundle_ok = signed.payload == {key: value for key, value in payload.items() if key != "bundle_signature"}
    except Exception:
        bundle_ok = False
    check("bundle_signature", bundle_ok, "bundle signature is invalid")
    try:
        pinned = json.loads(Path(pinned_policy_path).read_text(encoding="utf-8"))
        policy_ok = (
            payload.get("policy") == pinned
            and payload.get("policy_digest") == sha256_digest(pinned)
            and summary.get("policy_digest") == sha256_digest(pinned)
        )
    except Exception:
        policy_ok = False
    check("pinned_policy", policy_ok, "proof policy differs from verifier-pinned operations policy")
    check("plan_binding", bool(plan) and summary.get("plan_digest") == sha256_digest(plan), "plan digest is not bound")

    approval = payload.get("approval")
    approval_digest = summary.get("approval_digest")
    assertion_claims = None
    apply_arguments = {
        "change_id": plan.get("change_id"),
        "service_id": plan.get("service_id"),
        "patch": plan.get("patch"),
        "expected_version": plan.get("expected_version"),
        "workflow_id": report.workflow_id,
        "risk_units": plan.get("risk_units"),
    }
    expected_actions = [
        {
            "tool": "ops.apply_config",
            "resource": plan.get("service_id"),
            "args_digest": sha256_digest(apply_arguments),
            "amount_minor": plan.get("risk_units"),
            "currency": "XTS",
        }
    ]
    if approval_digest == "AUTOMATIC" and approval is None:
        try:
            approval_ok = int(summary.get("risk_units")) <= int(
                pinned["limits"]["auto_approve_risk_units"]
            )
        except Exception:
            approval_ok = False
    elif isinstance(approval_digest, str) and len(approval_digest) == 64 and isinstance(approval, dict):
        assertion = str(approval.get("assertion", ""))
        try:
            assertion_claims = verifier.verify_human_approval_assertion(
                assertion,
                enforce_current_time=False,
            )
        except Exception:
            assertion_claims = None
        challenge = {
            "workflow_id": report.workflow_id,
            "project_id": summary.get("project_id"),
            "tenant_id": summary.get("tenant_id"),
            "requester": summary.get("requester"),
            "expected_revision": assertion_claims.expected_revision if assertion_claims else None,
            "context_digest": summary.get("context_digest"),
            "policy_digest": summary.get("policy_digest"),
            "plan_digest": summary.get("plan_digest"),
            "scope": ["ops.apply_config"],
            "actions": expected_actions,
            "amount_minor": summary.get("risk_units"),
            "currency": "XTS",
        }
        approved_at = _iso_epoch(approval.get("approved_at"))
        approval_ok = (
            assertion_claims is not None
            and approved_at is not None
            and assertion_claims.not_before <= approved_at < assertion_claims.expires_at
            and approval.get("schema_version") == "proofmesh.human-approval/v2"
            and approval.get("approver") == assertion_claims.subject
            and approval.get("approver") != summary.get("requester")
            and isinstance(approval.get("reason"), str)
            and len(approval.get("reason", "").strip()) >= 12
            and sha256_digest(approval.get("reason", "")) == assertion_claims.reason_digest
            and approval.get("scope") == ["ops.apply_config"]
            and approval.get("origin_assurance") == "EXTERNAL_SIGNED_ASSERTION"
            and approval.get("assertion_digest") == sha256_digest(assertion)
            and approval.get("assertion_issuer") == assertion_claims.issuer
            and sha256_digest(
                {"assertion": assertion, "reason_digest": assertion_claims.reason_digest}
            )
            == approval_digest
            and assertion_claims.workflow_id == report.workflow_id
            and assertion_claims.project_id == summary.get("project_id")
            and assertion_claims.tenant_id == summary.get("tenant_id")
            and assertion_claims.requester == summary.get("requester")
            and assertion_claims.context_digest == summary.get("context_digest")
            and assertion_claims.policy_digest == summary.get("policy_digest")
            and assertion_claims.plan_digest == summary.get("plan_digest")
            and assertion_claims.scope == ["ops.apply_config"]
            and [item.model_dump(mode="json") for item in assertion_claims.actions]
            == expected_actions
            and assertion_claims.amount_minor == summary.get("risk_units")
            and assertion_claims.currency == "XTS"
            and assertion_claims.challenge_digest == sha256_digest(challenge)
        )
    else:
        approval_ok = False
    check(
        "approval_semantics",
        approval_ok,
        "operations approval is missing, self-issued, out of scope, or not plan-bound",
    )

    receipts = payload.get("gateway_receipts") if isinstance(payload.get("gateway_receipts"), list) else []
    receipt_map: dict[str, dict[str, Any]] = {}
    previous = "GENESIS"
    receipts_ok = bool(receipts)
    for receipt in receipts:
        try:
            unsigned = {key: value for key, value in receipt.items() if key not in {"signature", "_chain"}}
            verified = verifier.verify_compact(
                str(receipt.get("signature", "")),
                expected_type=RECEIPT_TYPE,
                allowed_issuers={"proofmesh-action-gateway"},
            )
            chain = receipt.get("_chain", {})
            signed_receipt = {key: value for key, value in receipt.items() if key != "_chain"}
            expected_hash = sha256_digest({"previous_hash": previous, "receipt": signed_receipt})
            receipts_ok = receipts_ok and (
                verified.payload == unsigned
                and receipt.get("workflow_id") == report.workflow_id
                and receipt.get("tenant_id") == summary.get("tenant_id")
                and receipt.get("policy_digest") == summary.get("policy_digest")
                and chain.get("previous_hash") == previous
                and chain.get("entry_hash") == expected_hash
            )
            if receipt.get("tool") in {
                "ops.get_change",
                "ops.get_service_config",
                "ops.run_health_check",
            }:
                receipts_ok = receipts_ok and receipt.get("approval_digest") == "AUTOMATIC"
            elif receipt.get("tool") == "ops.restore_config":
                receipts_ok = receipts_ok and str(receipt.get("approval_digest", "")).startswith(
                    "EMERGENCY:"
                )
            elif receipt.get("tool") == "ops.apply_config":
                receipts_ok = receipts_ok and receipt.get("approval_digest") == approval_digest
                if approval is not None:
                    receipt_at = _iso_epoch(receipt.get("timestamp"))
                    receipts_ok = receipts_ok and (
                        assertion_claims is not None
                        and receipt.get("approval_assertion_digest")
                        == approval.get("assertion_digest")
                        and receipt_at is not None
                        and assertion_claims.not_before
                        <= receipt_at
                        < assertion_claims.expires_at
                    )
            previous = chain.get("entry_hash", "")
            receipt_map[str(receipt.get("call_id"))] = receipt
        except Exception:
            receipts_ok = False
    check("gateway_receipts", receipts_ok, "Gateway receipt signature or chain is invalid")

    executions = payload.get("tool_executions") if isinstance(payload.get("tool_executions"), list) else []
    execution_ok = bool(executions)
    allowed: dict[str, list[dict[str, Any]]] = {}
    for execution in executions:
        receipt = receipt_map.get(str(execution.get("receipt_call_id")), {})
        args = execution.get("arguments")
        result = execution.get("result")
        execution_ok = execution_ok and isinstance(args, dict) and isinstance(result, dict) and (
            receipt.get("tool") == execution.get("tool")
            and receipt.get("subject") == execution.get("actor")
            and receipt.get("decision") == "allowed"
            and receipt.get("request_digest")
            == sha256_digest(
                {
                    "workflow_id": report.workflow_id,
                    "tool": execution.get("tool"),
                    "arguments": args,
                    "context_digest": summary.get("context_digest"),
                }
            )
            and receipt.get("result_digest") == sha256_digest(result)
        )
        allowed.setdefault(str(execution.get("tool")), []).append(execution)
    apply = (allowed.get("ops.apply_config") or [{}])[-1]
    apply_args = apply.get("arguments") if isinstance(apply, dict) else {}
    execution_ok = execution_ok and isinstance(apply_args, dict) and (
        apply_args.get("change_id") == plan.get("change_id")
        and apply_args.get("service_id") == plan.get("service_id")
        and apply_args.get("patch") == plan.get("patch")
        and apply_args.get("expected_version") == plan.get("expected_version")
        and apply_args.get("risk_units") == plan.get("risk_units")
        and apply_args.get("workflow_id") == report.workflow_id
    )
    if report.terminal_status == "COMPLETED":
        execution_ok = execution_ok and bool(allowed.get("ops.run_health_check"))
    elif report.terminal_status == "COMPENSATED":
        execution_ok = execution_ok and bool(allowed.get("ops.restore_config"))
    else:
        execution_ok = False
    check("tool_execution_semantics", execution_ok, "tool executions differ from frozen plan")

    events = payload.get("workflow_events") if isinstance(payload.get("workflow_events"), list) else []
    event_ok = bool(events)
    event_previous = "GENESIS"
    for event in events:
        envelope = {
            "run_id": event.get("run_id"),
            "ts": event.get("ts"),
            "agent": event.get("agent"),
            "event_type": event.get("event_type"),
            "payload": event.get("payload"),
            "prev_hash": event.get("prev_hash"),
        }
        from .ledger import canonical

        event_ok = event_ok and event.get("prev_hash") == event_previous and event.get("event_hash") == sha256_digest(canonical(envelope))
        event_previous = str(event.get("event_hash", ""))
    event_chain = payload.get("event_chain") or {}
    event_ok = event_ok and event_chain.get("valid_at_seal") is True and event_chain.get("head") == event_previous
    check("event_chain", event_ok, "operations event chain is invalid")

    snapshot = payload.get("verified_business_snapshot") if isinstance(payload.get("verified_business_snapshot"), dict) else {}
    attestation = payload.get("business_snapshot_attestation") or {}
    statement = attestation.get("statement") if isinstance(attestation.get("statement"), dict) else {}
    try:
        verified = verifier.verify_compact(
            str(attestation.get("signature", "")),
            expected_type=BUSINESS_ATTESTATION_TYPE,
            allowed_issuers={"proofmesh-commerce-sandbox"},
        )
        attestation_ok = (
            verified.payload == statement
            and statement.get("workflow_id") == report.workflow_id
            and statement.get("tenant_id") == summary.get("tenant_id")
            and statement.get("domain") == "production-operations-change"
            and statement.get("snapshot_digest") == sha256_digest(snapshot)
        )
    except Exception:
        attestation_ok = False
    check("business_snapshot_attestation", attestation_ok, "fresh upstream snapshot is not attested")
    service = snapshot.get("service") or {}
    execution = snapshot.get("execution") or {}
    if report.terminal_status == "COMPLETED":
        expected_config = {**json.loads(execution.get("before_config_json", "{}")), **plan.get("patch", {})}
        terminal_ok = (
            execution.get("status") == "APPLIED"
            and service.get("config") == expected_config
            and service.get("version") == plan.get("expected_version", -2) + 1
        )
    elif report.terminal_status == "COMPENSATED":
        terminal_ok = (
            execution.get("status") == "RESTORED"
            and service.get("config") == json.loads(execution.get("before_config_json", "{}"))
            and service.get("version") == plan.get("expected_version", -3) + 2
        )
    else:
        terminal_ok = False
    terminal_ok = terminal_ok and (payload.get("verification") or {}).get("passed") is True
    check("terminal_business_state", terminal_ok, "operations terminal state violates frozen plan")
    report.valid = bool(report.checks) and all(report.checks.values())
    return report
