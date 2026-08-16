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
    TASK_RECEIPT_TYPE,
    ExternalTrustVerifier,
    canonical_json,
    sha256_digest,
)


MAX_PROOF_BYTES = 10 * 1024 * 1024


def _iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


@dataclass
class WorkflowVerificationReport:
    valid: bool = False
    workflow_id: str = ""
    terminal_status: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    approval_origin_assurance: str = "NOT_APPLICABLE"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_workflow_proof(
    proof_path: str | Path,
    *,
    trust_bundle_path: str | Path,
    pinned_policy_path: str | Path | None = None,
) -> WorkflowVerificationReport:
    """Fail-closed verification using external trust keys and an external pinned policy."""

    report = WorkflowVerificationReport()

    def check(name: str, condition: bool, error: str) -> None:
        report.checks[name] = bool(condition)
        if not condition:
            report.errors.append(error)

    try:
        proof_file = Path(proof_path)
        if proof_file.stat().st_size > MAX_PROOF_BYTES:
            raise ValueError("proof exceeds size limit")
        payload = json.loads(proof_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("proof root must be an object")
    except Exception as exc:
        check("proof_input", False, f"proof cannot be parsed safely: {type(exc).__name__}")
        return report

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    report.workflow_id = str(summary.get("workflow_id", ""))
    report.terminal_status = str(summary.get("status", ""))
    check(
        "schema",
        payload.get("schema_version") == "proofmesh.workflow-proof/v2",
        "unsupported proof schema",
    )

    try:
        verifier = ExternalTrustVerifier(trust_bundle_path)
        trust_ok = True
    except Exception:
        verifier = None
        trust_ok = False
    check("external_trust_bundle", trust_ok, "external trust bundle is unavailable or invalid")

    bundle_signature_ok = False
    if verifier is not None:
        try:
            verified_bundle = verifier.verify_compact(
                str(payload.get("bundle_signature", "")),
                expected_type=PROOF_TYPE,
                allowed_issuers={"proofmesh-proof-sealer"},
            )
            unsigned_bundle = {key: value for key, value in payload.items() if key != "bundle_signature"}
            bundle_signature_ok = verified_bundle.payload == unsigned_bundle
        except Exception:
            bundle_signature_ok = False
    check("bundle_signature", bundle_signature_ok, "workflow proof signature is invalid")

    embedded_policy = payload.get("policy")
    embedded_policy_digest = payload.get("policy_digest")
    embedded_policy_ok = isinstance(embedded_policy, dict) and sha256_digest(embedded_policy) == embedded_policy_digest
    check("embedded_policy_digest", embedded_policy_ok, "embedded policy content does not match its digest")
    pinned_policy_ok = False
    if pinned_policy_path is not None:
        try:
            pinned_policy = json.loads(Path(pinned_policy_path).read_text(encoding="utf-8"))
            pinned_policy_ok = sha256_digest(pinned_policy) == embedded_policy_digest
        except Exception:
            pinned_policy_ok = False
    check("pinned_policy", pinned_policy_ok, "proof policy is not the verifier's externally pinned policy")
    check(
        "summary_policy_binding",
        summary.get("policy_digest") == embedded_policy_digest,
        "summary policy digest differs from the sealed policy",
    )

    plan = payload.get("plan")
    plan_ok = isinstance(plan, dict) and sha256_digest(plan) == summary.get("plan_digest")
    check("plan_binding", plan_ok, "plan digest does not match the frozen plan")

    approval = payload.get("approval")
    approval_digest = summary.get("approval_digest")
    approval_ok = False
    assertion_claims = None
    if approval_digest == "AUTOMATIC" and approval is None and isinstance(embedded_policy, dict):
        report.approval_origin_assurance = "AUTOMATIC_POLICY"
        try:
            limits = embedded_policy["limits"]
            approval_ok = (
                int(summary["amount_minor"]) <= int(limits["auto_approve_minor"])
                and float(summary["risk_score"]) < float(limits["human_review_risk_score"])
            )
        except Exception:
            approval_ok = False
    elif isinstance(approval_digest, str) and len(approval_digest) == 64 and isinstance(approval, dict):
        assertion = approval.get("assertion")
        try:
            # Runtime admission checks the assertion against the current clock.  A
            # sealed historical Proof instead verifies that the approval and every
            # authorized side effect happened inside the signed assertion window.
            assertion_claims = verifier.verify_human_approval_assertion(  # type: ignore[union-attr]
                str(assertion), enforce_current_time=False
            )
        except Exception:
            assertion_claims = None
        report.approval_origin_assurance = (
            "EXTERNAL_SIGNED_ASSERTION" if assertion_claims is not None else "UNVERIFIED"
        )
        plan_actions: list[dict[str, Any]] = []
        if isinstance(plan, dict):
            refund_arguments = {
                "ticket_id": plan.get("ticket_id"),
                "order_id": plan.get("order_id"),
                "amount_minor": plan.get("amount_minor"),
                "currency": plan.get("currency"),
                "expected_order_version": plan.get("expected_order_version"),
                "workflow_id": report.workflow_id,
            }
            close_arguments = {
                "ticket_id": plan.get("ticket_id"),
                "workflow_id": report.workflow_id,
                "resolution": f"退款 {plan.get('amount_minor')} {plan.get('currency')} 已签发",
            }
            plan_actions = [
                {
                    "tool": "crm.close_ticket",
                    "resource": plan.get("ticket_id"),
                    "args_digest": sha256_digest(close_arguments),
                    "amount_minor": 0,
                    "currency": plan.get("currency"),
                },
                {
                    "tool": "payments.issue_refund",
                    "resource": plan.get("order_id"),
                    "args_digest": sha256_digest(refund_arguments),
                    "amount_minor": plan.get("amount_minor"),
                    "currency": plan.get("currency"),
                },
            ]
        challenge = {
            "workflow_id": report.workflow_id,
            "project_id": summary.get("project_id"),
            "tenant_id": summary.get("tenant_id"),
            "requester": summary.get("requester"),
            "expected_revision": assertion_claims.expected_revision if assertion_claims else None,
            "context_digest": summary.get("context_digest"),
            "policy_digest": embedded_policy_digest,
            "plan_digest": summary.get("plan_digest"),
            "scope": sorted(plan.get("actions", [])) if isinstance(plan, dict) else [],
            "actions": plan_actions,
            "amount_minor": summary.get("amount_minor"),
            "currency": summary.get("currency"),
        }
        approval_ok = (
            assertion_claims is not None
            and (_iso_epoch(approval.get("approved_at")) is not None)
            and assertion_claims.not_before
            <= int(_iso_epoch(approval.get("approved_at")))
            < assertion_claims.expires_at
            and sha256_digest({"assertion": assertion, "reason_digest": assertion_claims.reason_digest})
            == approval_digest
            and approval.get("schema_version") == "proofmesh.human-approval/v2"
            and approval.get("workflow_id") == report.workflow_id
            and approval.get("tenant_id") == summary.get("tenant_id")
            and approval.get("requester") == summary.get("requester")
            and approval.get("approver") != summary.get("requester")
            and isinstance(approval.get("approver"), str)
            and bool(approval.get("approver", "").strip())
            and isinstance(approval.get("reason"), str)
            and len(approval.get("reason", "").strip()) >= 12
            and isinstance(approval.get("approved_at"), str)
            and bool(approval.get("approved_at", "").strip())
            and approval.get("plan_digest") == summary.get("plan_digest")
            and approval.get("policy_digest") == embedded_policy_digest
            and approval.get("origin_assurance") == "EXTERNAL_SIGNED_ASSERTION"
            and approval.get("assertion_digest") == sha256_digest(str(assertion))
            and approval.get("assertion_issuer") == assertion_claims.issuer
            and approval.get("assertion_jti") == assertion_claims.jti
            and approval.get("approver") == assertion_claims.subject
            and sha256_digest(approval.get("reason", "")) == assertion_claims.reason_digest
            and assertion_claims.workflow_id == report.workflow_id
            and assertion_claims.project_id == summary.get("project_id")
            and assertion_claims.tenant_id == summary.get("tenant_id")
            and assertion_claims.requester == summary.get("requester")
            and assertion_claims.context_digest == summary.get("context_digest")
            and assertion_claims.policy_digest == embedded_policy_digest
            and assertion_claims.plan_digest == summary.get("plan_digest")
            and assertion_claims.amount_minor == summary.get("amount_minor")
            and assertion_claims.currency == summary.get("currency")
            and assertion_claims.challenge_digest == sha256_digest(challenge)
            and [item.model_dump(mode="json") for item in assertion_claims.actions] == plan_actions
            and isinstance(plan, dict)
            and sorted(approval.get("scope", [])) == sorted(plan.get("actions", []))
            and assertion_claims.scope == sorted(plan.get("actions", []))
            and len(approval.get("scope", [])) == len(set(approval.get("scope", [])))
        )
    check("approval_semantics", approval_ok, "approval is missing, self-issued, out of scope, or not plan-bound")

    task_receipts = payload.get("task_step_receipts")
    task_receipts_ok = isinstance(task_receipts, list) and bool(task_receipts)
    revision = -1
    task_steps: list[str] = []
    task_actors: set[str] = set()
    human_receipts: list[dict[str, Any]] = []
    expected_role = {
        "create_case": "orchestrator",
        "normalize_case": "intake",
        "gather_context": "investigator",
        "evaluate_policy": "policy",
        "record_human_approval": "human-approver",
        "execute_authorized": "executor",
        "verify_outcome": "verifier",
        "curate_memory": "memory",
    }
    if isinstance(task_receipts, list):
        for receipt in task_receipts:
            if not isinstance(receipt, dict):
                task_receipts_ok = False
                continue
            signature = receipt.get("signature")
            unsigned = {key: value for key, value in receipt.items() if key != "signature"}
            try:
                verified = verifier.verify_compact(  # type: ignore[union-attr]
                    str(signature),
                    expected_type=TASK_RECEIPT_TYPE,
                    allowed_issuers={"proofmesh-workflow-control-plane"},
                )
                if verified.payload != unsigned:
                    task_receipts_ok = False
            except Exception:
                task_receipts_ok = False
            step = receipt.get("step")
            if (
                receipt.get("schema_version") != "proofmesh.task-step-receipt/v1"
                or receipt.get("workflow_id") != report.workflow_id
                or receipt.get("project_id") != summary.get("project_id")
                or receipt.get("tenant_id") != summary.get("tenant_id")
                or receipt.get("policy_digest") != embedded_policy_digest
                or receipt.get("revision_from") != revision
                or receipt.get("revision_to") != revision + 1
                or expected_role.get(str(step)) != receipt.get("role")
            ):
                task_receipts_ok = False
            revision += 1
            task_steps.append(str(step))
            task_actors.add(str(receipt.get("actor", "")))
            if step == "record_human_approval":
                human_receipts.append(receipt)
    required_task_steps = {
        "create_case",
        "normalize_case",
        "gather_context",
        "evaluate_policy",
        "execute_authorized",
        "verify_outcome",
        "curate_memory",
    }
    if approval is not None:
        required_task_steps.add("record_human_approval")
    task_receipts_ok = (
        task_receipts_ok
        and revision == summary.get("revision")
        and required_task_steps.issubset(set(task_steps))
        and len(task_receipts) == summary.get("task_receipt_count")
        and len(task_actors) == summary.get("agent_count")
    )
    if approval is None:
        task_receipts_ok = task_receipts_ok and not human_receipts
    else:
        task_receipts_ok = task_receipts_ok and len(human_receipts) == 1
        if human_receipts:
            human = human_receipts[0]
            task_receipts_ok = task_receipts_ok and (
                human.get("actor") == approval.get("approver")
                and human.get("role") == "human-approver"
                and human.get("from_status") == "WAITING_APPROVAL"
                and human.get("to_status") == "AUTHORIZED"
                and human.get("approval_digest") == approval_digest
                and human.get("approval_assertion_digest") == approval.get("assertion_digest")
                and assertion_claims is not None
                and (_iso_epoch(human.get("timestamp")) is not None)
                and assertion_claims.not_before
                <= int(_iso_epoch(human.get("timestamp")))
                < assertion_claims.expires_at
                and human.get("revision_from") == assertion_claims.expected_revision
                and human.get("plan_digest") == approval.get("plan_digest")
                and human.get("policy_digest") == approval.get("policy_digest")
                and human.get("output_digest") == sha256_digest(approval)
            )
    check("task_step_receipts", task_receipts_ok, "task receipts do not prove the complete role-separated DAG")

    receipts = payload.get("gateway_receipts")
    receipts_ok = isinstance(receipts, list) and bool(receipts)
    assertion_action_map = (
        {item.tool: item for item in assertion_claims.actions} if assertion_claims is not None else {}
    )
    receipts_by_call: dict[str, dict[str, Any]] = {}
    previous_hash = "GENESIS"
    if isinstance(receipts, list):
        for receipt in receipts:
            if not isinstance(receipt, dict):
                receipts_ok = False
                continue
            unsigned_receipt = {
                key: value for key, value in receipt.items() if key not in {"signature", "_chain"}
            }
            signed_receipt = {key: value for key, value in receipt.items() if key != "_chain"}
            try:
                verified = verifier.verify_compact(  # type: ignore[union-attr]
                    str(receipt.get("signature", "")),
                    expected_type=RECEIPT_TYPE,
                    allowed_issuers={"proofmesh-action-gateway"},
                )
                if verified.payload != unsigned_receipt:
                    receipts_ok = False
            except Exception:
                receipts_ok = False
            decision = receipt.get("decision")
            if (
                receipt.get("workflow_id") != report.workflow_id
                or receipt.get("tenant_id") != summary.get("tenant_id")
                or receipt.get("policy_digest") != embedded_policy_digest
                or decision not in {"allowed", "upstream_error"}
            ):
                receipts_ok = False
            tool = receipt.get("tool")
            action_approval = receipt.get("approval_digest")
            bound_execution = next(
                (
                    execution
                    for execution in payload.get("tool_executions", [])
                    if isinstance(execution, dict)
                    and execution.get("receipt_call_id") == receipt.get("call_id")
                ),
                {},
            )
            bound_arguments = bound_execution.get("arguments") if isinstance(bound_execution, dict) else None
            if tool in {"crm.get_ticket", "orders.get_refund_context", "risk.score_refund"}:
                receipts_ok = receipts_ok and action_approval == "AUTOMATIC"
            elif tool == "payments.compensate_refund":
                receipts_ok = receipts_ok and str(action_approval).startswith("EMERGENCY:")
            else:
                receipts_ok = receipts_ok and action_approval == approval_digest
                if approval is not None:
                    receipts_ok = receipts_ok and receipt.get("approval_assertion_digest") == approval.get(
                        "assertion_digest"
                    )
                    receipts_ok = receipts_ok and assertion_claims is not None and (
                        _iso_epoch(receipt.get("timestamp")) is not None
                        and assertion_claims.not_before
                        <= int(_iso_epoch(receipt.get("timestamp")))
                        < assertion_claims.expires_at
                    )
                    action_contract = assertion_action_map.get(str(tool))
                    receipts_ok = receipts_ok and action_contract is not None and isinstance(
                        bound_arguments, dict
                    ) and (
                        receipt.get("resource") == action_contract.resource
                        and sha256_digest(bound_arguments) == action_contract.args_digest
                        and int(bound_arguments.get("amount_minor", 0)) == action_contract.amount_minor
                        and bound_arguments.get("currency", summary.get("currency"))
                        == action_contract.currency
                        and receipt.get("request_digest")
                        == sha256_digest(
                            {
                                "workflow_id": report.workflow_id,
                                "tool": tool,
                                "arguments": bound_arguments,
                                "context_digest": summary.get("context_digest"),
                            }
                        )
                    )
            chain = receipt.get("_chain") if isinstance(receipt.get("_chain"), dict) else {}
            expected_hash = sha256_digest({"previous_hash": previous_hash, "receipt": signed_receipt})
            if chain.get("previous_hash") != previous_hash or chain.get("entry_hash") != expected_hash:
                receipts_ok = False
            previous_hash = str(chain.get("entry_hash", ""))
            call_id = str(receipt.get("call_id", ""))
            if not call_id or call_id in receipts_by_call:
                receipts_ok = False
            receipts_by_call[call_id] = receipt
    check("gateway_receipts", receipts_ok, "gateway receipt signature, semantics, or chain is invalid")

    executions = payload.get("tool_executions")
    executions_ok = isinstance(executions, list) and bool(executions)
    allowed_executions: dict[str, list[dict[str, Any]]] = {}
    if isinstance(executions, list):
        for execution in executions:
            if not isinstance(execution, dict):
                executions_ok = False
                continue
            receipt = receipts_by_call.get(str(execution.get("receipt_call_id", "")))
            arguments = execution.get("arguments")
            result = execution.get("result")
            if (
                receipt is None
                or receipt.get("tool") != execution.get("tool")
                or receipt.get("decision") != "allowed"
                or execution.get("decision") != "allowed"
                or receipt.get("subject") != execution.get("actor")
                or not isinstance(arguments, dict)
                or not isinstance(result, dict)
            ):
                executions_ok = False
                continue
            expected_request_digest = sha256_digest(
                {
                    "workflow_id": report.workflow_id,
                    "tool": execution["tool"],
                    "arguments": arguments,
                    "context_digest": summary.get("context_digest"),
                }
            )
            if (
                receipt.get("request_digest") != expected_request_digest
                or receipt.get("result_digest") != sha256_digest(result)
            ):
                executions_ok = False
            allowed_executions.setdefault(str(execution["tool"]), []).append(execution)

    issue = (allowed_executions.get("payments.issue_refund") or [{}])[-1]
    issue_args = issue.get("arguments") if isinstance(issue, dict) else {}
    issue_receipt = receipts_by_call.get(str(issue.get("receipt_call_id", "")), {}) if isinstance(issue, dict) else {}
    plan_execution_ok = isinstance(plan, dict) and isinstance(issue_args, dict) and (
        issue_args.get("ticket_id") == plan.get("ticket_id")
        and issue_args.get("order_id") == plan.get("order_id")
        and issue_args.get("amount_minor") == plan.get("amount_minor")
        and issue_args.get("currency") == plan.get("currency")
        and issue_args.get("expected_order_version") == plan.get("expected_order_version")
        and issue_args.get("workflow_id") == report.workflow_id
        and issue_receipt.get("resource") == plan.get("order_id")
    )
    close = (allowed_executions.get("crm.close_ticket") or [{}])[-1]
    close_args = close.get("arguments") if isinstance(close, dict) else {}
    if report.terminal_status == "COMPLETED":
        plan_execution_ok = plan_execution_ok and isinstance(close_args, dict) and (
            close_args.get("ticket_id") == plan.get("ticket_id")
            and close_args.get("workflow_id") == report.workflow_id
        )
    elif report.terminal_status == "COMPENSATED":
        plan_execution_ok = plan_execution_ok and bool(allowed_executions.get("payments.compensate_refund"))
    else:
        plan_execution_ok = False
    executions_ok = executions_ok and plan_execution_ok
    check("tool_execution_semantics", executions_ok, "tool calls are not fully bound to the frozen plan and receipts")

    events = payload.get("workflow_events")
    events_ok = isinstance(events, list) and bool(events)
    event_previous = "GENESIS"
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                events_ok = False
                continue
            envelope = {
                "run_id": event.get("run_id"),
                "ts": event.get("ts"),
                "agent": event.get("agent"),
                "event_type": event.get("event_type"),
                "payload": event.get("payload"),
                "prev_hash": event.get("prev_hash"),
            }
            if (
                event.get("run_id") != report.workflow_id
                or event.get("prev_hash") != event_previous
                or event.get("event_hash") != sha256_digest(canonical_json(envelope))
            ):
                events_ok = False
            event_previous = str(event.get("event_hash", ""))
    event_chain = payload.get("event_chain") if isinstance(payload.get("event_chain"), dict) else {}
    events_ok = (
        events_ok
        and event_chain.get("head") == event_previous
        and event_chain.get("valid_at_seal") is True
    )
    check("event_chain", events_ok, "workflow event chain is invalid")

    snapshot = payload.get("verified_business_snapshot")
    business_attestation = payload.get("business_snapshot_attestation")
    business_attestation_ok = (
        isinstance(snapshot, dict)
        and isinstance(business_attestation, dict)
        and isinstance(business_attestation.get("statement"), dict)
    )
    if business_attestation_ok:
        statement = business_attestation["statement"]
        try:
            verified_attestation = verifier.verify_compact(  # type: ignore[union-attr]
                str(business_attestation.get("signature", "")),
                expected_type=BUSINESS_ATTESTATION_TYPE,
                allowed_issuers={"proofmesh-commerce-sandbox"},
            )
            business_attestation_ok = (
                verified_attestation.payload == statement
                and statement.get("schema_version")
                == "proofmesh.business-snapshot-attestation/v1"
                and statement.get("workflow_id") == report.workflow_id
                and statement.get("tenant_id") == summary.get("tenant_id")
                and statement.get("snapshot_digest") == sha256_digest(snapshot)
            )
        except Exception:
            business_attestation_ok = False
    check(
        "business_snapshot_attestation",
        business_attestation_ok,
        "business snapshot is not signed by the independently pinned upstream identity",
    )
    snapshot_ok = isinstance(snapshot, dict) and isinstance(plan, dict)
    refund = snapshot.get("refund", {}) if isinstance(snapshot, dict) else {}
    ticket = snapshot.get("ticket", {}) if isinstance(snapshot, dict) else {}
    order = snapshot.get("order", {}) if isinstance(snapshot, dict) else {}
    common_state_ok = snapshot_ok and (
        snapshot.get("workflow_id") == report.workflow_id
        and refund.get("workflow_id") == report.workflow_id
        and refund.get("tenant_id") == summary.get("tenant_id")
        and ticket.get("tenant_id") == summary.get("tenant_id")
        and order.get("tenant_id") == summary.get("tenant_id")
        and refund.get("ticket_id") == plan.get("ticket_id")
        and refund.get("order_id") == plan.get("order_id")
        and refund.get("amount_minor") == plan.get("amount_minor")
        and refund.get("currency") == plan.get("currency")
    )
    if report.terminal_status == "COMPLETED":
        terminal_ok = common_state_ok and (
            refund.get("status") == "ISSUED"
            and ticket.get("status") == "CLOSED"
            and ticket.get("closed_workflow_id") == report.workflow_id
            and order.get("refundable_minor")
            == plan.get("refundable_before_minor", -1) - plan.get("amount_minor", 0)
        )
    elif report.terminal_status == "COMPENSATED":
        terminal_ok = common_state_ok and (
            refund.get("status") == "COMPENSATED"
            and ticket.get("status") == "OPEN"
            and ticket.get("closed_workflow_id") is None
            and order.get("refundable_minor") == plan.get("refundable_before_minor")
        )
    else:
        terminal_ok = False
    verification = payload.get("verification")
    terminal_ok = terminal_ok and isinstance(verification, dict) and verification.get("passed") is True
    check("terminal_business_state", terminal_ok, "business snapshot conflicts with plan or terminal decision")

    memory_status = payload.get("memory_status")
    check(
        "memory_audit_status",
        memory_status in {"STORED", "FAILED"},
        "memory outcome is missing from the proof",
    )
    report.valid = bool(report.checks) and all(report.checks.values())
    return report
