from __future__ import annotations

import json
import os
import sqlite3
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .business import CommerceSandbox, ToolExecutionError
from .capabilities import (
    PROOF_TYPE,
    TASK_RECEIPT_TYPE,
    JwsSigner,
    ExternalTrustVerifier,
    PassportError,
    canonical_json,
    sha256_digest,
)
from .gateway import ActionGateway
from .domain_protocol import AuthorizedToolCaller
from .ledger import EvidenceLedger
from .timeutil import utc_now


class WorkflowStatus(str, Enum):
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    CONTEXT_READY = "CONTEXT_READY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    COMPENSATED = "COMPENSATED"


class RefundWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(pattern=r"^[A-Z0-9-]{3,64}$")
    tenant_id: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    requester: str = Field(min_length=3, max_length=128)


class RefundWorkflowSummary(BaseModel):
    workflow_id: str
    project_id: str
    ticket_id: str
    tenant_id: str
    requester: str
    status: WorkflowStatus
    revision: int = 0
    created_at: str
    updated_at: str
    context_digest: str
    policy_digest: str
    plan_digest: str | None = None
    approval_required: bool = False
    approval_scope: list[str] = Field(default_factory=list)
    approval_digest: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    risk_score: float | None = None
    last_step: str = "create_case"
    next_step: str | None = "normalize_case"
    final_message: str = ""
    proof_bundle: str | None = None
    gateway_receipt_count: int = 0
    task_receipt_count: int = 0
    agent_count: int = 0


class WorkflowConflict(RuntimeError):
    pass


class WorkerRoleError(PermissionError):
    pass


class RefundControlPlane:
    """A recoverable, role-separated refund saga designed for real AgentTeams tasks.

    Public step methods never trust an actor supplied by model output. The API authenticates
    the caller and passes its subject and role here. Each transition uses revision/status CAS,
    persists a signed step receipt, and writes an outbox record in the same SQLite transaction.
    Action Passports are minted and consumed internally and are never returned to a Worker.
    """

    ROLES = {
        "orchestrator": "case-orchestrator",
        "intake": "ticket-intake",
        "investigator": "context-investigator",
        "policy": "risk-policy-sentinel",
        "executor": "action-executor",
        "verifier": "outcome-verifier",
        "memory": "case-memory-curator",
    }
    APPROVAL_SCOPE = ["payments.issue_refund", "crm.close_ticket"]

    def __init__(
        self,
        *,
        home: str | Path,
        gateway: ActionGateway,
        passport_signer: JwsSigner,
        task_signer: JwsSigner,
        proof_signer: JwsSigner,
        sandbox: CommerceSandbox,
        approval_verifier: ExternalTrustVerifier,
    ):
        self.home = Path(home)
        self.gateway = gateway
        self.passport_signer = passport_signer
        self.task_signer = task_signer
        self.proof_signer = proof_signer
        self.sandbox = sandbox
        self.approval_verifier = approval_verifier
        self.authorized_tools = AuthorizedToolCaller(
            gateway=gateway,
            passport_signer=passport_signer,
        )
        self.var_dir = self.home / "var"
        self.artifact_dir = self.home / "artifacts" / "workflows"
        self.var_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.var_dir / "control-plane.db"
        self.ledger = EvidenceLedger(self.var_dir / "workflow-evidence.db")
        self.policy = json.loads((self.home / "data/policies/refund_policy.json").read_text(encoding="utf-8"))
        self.policy_digest = sha256_digest(self.policy)
        self._init_db()
        self._flush_outbox()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_status ON workflows(status, updated_at);
                CREATE TABLE IF NOT EXISTS workflow_outbox (
                    event_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_outbox_pending
                    ON workflow_outbox(delivered, created_at);
                """
            )

    @staticmethod
    def _require_role(role: str, expected: str) -> None:
        if role != expected:
            raise WorkerRoleError(f"step requires {expected} role")

    @staticmethod
    def _event_payload(actor: str, role: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "authenticated_actor": actor,
            "authenticated_role": role,
            "otel": {
                "gen_ai.operation.name": event_type,
                "gen_ai.agent.name": actor,
            },
            **payload,
        }

    def _direct_event(
        self,
        workflow_id: str,
        actor: str,
        role: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.ledger.append(
            workflow_id,
            actor,
            event_type,
            self._event_payload(actor, role, event_type, payload),
        )

    def _queue_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        workflow_id: str,
        actor: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO workflow_outbox(
                   event_id, workflow_id, actor, event_type, payload_json, created_at
               ) VALUES(?, ?, ?, ?, ?, ?)""",
            (event_id, workflow_id, actor, event_type, canonical_json(payload), utc_now()),
        )

    def _flush_outbox(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_outbox WHERE delivered = 0 ORDER BY created_at, event_id"
            ).fetchall()
        for row in rows:
            self.ledger.append(
                row["workflow_id"],
                row["actor"],
                row["event_type"],
                json.loads(row["payload_json"]),
                event_id=row["event_id"],
            )
            with self._connect() as conn:
                conn.execute(
                    "UPDATE workflow_outbox SET delivered = 1, delivered_at = ? WHERE event_id = ?",
                    (utc_now(), row["event_id"]),
                )

    @staticmethod
    def _sync_counts(summary: RefundWorkflowSummary, state: dict[str, Any]) -> None:
        summary.gateway_receipt_count = len(state.get("receipts", []))
        summary.task_receipt_count = len(state.get("task_receipts", []))
        summary.agent_count = len({entry["actor"] for entry in state.get("actors", [])})

    def _make_step_receipt(
        self,
        *,
        summary: RefundWorkflowSummary,
        actor: str,
        role: str,
        task_id: str,
        step: str,
        from_status: WorkflowStatus,
        to_status: WorkflowStatus,
        revision_from: int,
        input_material: dict[str, Any],
        output_material: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "schema_version": "proofmesh.task-step-receipt/v1",
            "issuer": self.task_signer.issuer,
            "workflow_id": summary.workflow_id,
            "project_id": summary.project_id,
            "task_id": task_id,
            "step": step,
            "actor": actor,
            "role": role,
            "tenant_id": summary.tenant_id,
            "from_status": from_status.value,
            "to_status": to_status.value,
            "revision_from": revision_from,
            "revision_to": revision_from + 1,
            "context_digest": summary.context_digest,
            "policy_digest": summary.policy_digest,
            "plan_digest": summary.plan_digest,
            "approval_digest": summary.approval_digest,
            "approval_assertion_digest": (
                output_material.get("assertion_digest")
                if step == "record_human_approval" and role == "human-approver"
                else None
            ),
            "input_digest": sha256_digest(input_material),
            "output_digest": sha256_digest(output_material),
            "timestamp": utc_now(),
        }
        body["signature"] = self.task_signer.sign_payload(body, token_type=TASK_RECEIPT_TYPE)
        return body

    def _commit_transition(
        self,
        *,
        summary: RefundWorkflowSummary,
        state: dict[str, Any],
        expected_revision: int,
        from_status: WorkflowStatus,
        actor: str,
        role: str,
        task_id: str,
        step: str,
        event_type: str,
        event_payload: dict[str, Any],
        input_material: dict[str, Any],
        output_material: dict[str, Any],
    ) -> RefundWorkflowSummary:
        if summary.revision != expected_revision or from_status == summary.status:
            if summary.revision != expected_revision:
                raise WorkflowConflict("expected revision does not match loaded workflow")
        receipt = self._make_step_receipt(
            summary=summary,
            actor=actor,
            role=role,
            task_id=task_id,
            step=step,
            from_status=from_status,
            to_status=summary.status,
            revision_from=expected_revision,
            input_material=input_material,
            output_material=output_material,
        )
        state.setdefault("task_receipts", []).append(receipt)
        state.setdefault("actors", []).append({"actor": actor, "role": role, "task_id": task_id, "step": step})
        summary.revision = expected_revision + 1
        summary.updated_at = utc_now()
        summary.last_step = step
        self._sync_counts(summary, state)
        outbox_event_id = f"evt-{uuid.uuid4().hex}"
        durable_payload = self._event_payload(
            actor,
            role,
            event_type,
            {
                "task_id": task_id,
                "project_id": summary.project_id,
                "revision": summary.revision,
                "step_receipt_digest": sha256_digest(receipt),
                **event_payload,
            },
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                """UPDATE workflows SET
                     status = ?, revision = ?, summary_json = ?, state_json = ?, updated_at = ?
                   WHERE workflow_id = ? AND status = ? AND revision = ?""",
                (
                    summary.status.value,
                    summary.revision,
                    canonical_json(summary.model_dump(mode="json")),
                    canonical_json(state),
                    summary.updated_at,
                    summary.workflow_id,
                    from_status.value,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise WorkflowConflict("workflow transition lost compare-and-swap")
            self._queue_outbox(
                conn,
                event_id=outbox_event_id,
                workflow_id=summary.workflow_id,
                actor=actor,
                event_type=event_type,
                payload=durable_payload,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._flush_outbox()
        return summary

    def _load(self, workflow_id: str) -> tuple[RefundWorkflowSummary, dict[str, Any]]:
        self._flush_outbox()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(workflow_id)
        return (
            RefundWorkflowSummary.model_validate(json.loads(row["summary_json"])),
            json.loads(row["state_json"]),
        )

    def get(self, workflow_id: str) -> RefundWorkflowSummary:
        return self._load(workflow_id)[0]

    def get_state(self, workflow_id: str) -> dict[str, Any]:
        summary, state = self._load(workflow_id)
        return {
            "summary": summary.model_dump(mode="json"),
            "step_receipts": state.get("task_receipts", []),
            "execution_outcome": state.get("execution_outcome"),
        }

    def events(self, workflow_id: str) -> list[dict[str, Any]]:
        self.get(workflow_id)
        return self.ledger.events(workflow_id)

    def create_case(
        self,
        request: RefundWorkflowRequest,
        *,
        actor: str,
        role: str = "orchestrator",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "orchestrator")
        workflow_id = f"wf-{uuid.uuid4().hex}"
        task_id = task_id or f"{workflow_id}:00-create"
        created_at = utc_now()
        context_digest = sha256_digest(
            {"workflow_id": workflow_id, "ticket_id": request.ticket_id, "tenant_id": request.tenant_id}
        )
        summary = RefundWorkflowSummary(
            workflow_id=workflow_id,
            project_id=workflow_id,
            ticket_id=request.ticket_id,
            tenant_id=request.tenant_id,
            requester=request.requester,
            status=WorkflowStatus.RECEIVED,
            created_at=created_at,
            updated_at=created_at,
            context_digest=context_digest,
            policy_digest=self.policy_digest,
            final_message="退款案例已创建；等待 intake Worker 认领规范化任务。",
        )
        state: dict[str, Any] = {
            "receipts": [],
            "tool_results": {},
            "tool_executions": [],
            "task_receipts": [],
            "actors": [{"actor": actor, "role": role, "task_id": task_id, "step": "create_case"}],
            "approval": None,
            "policy_snapshot": self.policy,
            "policy_digest": self.policy_digest,
        }
        create_receipt = self._make_step_receipt(
            summary=summary,
            actor=actor,
            role=role,
            task_id=task_id,
            step="create_case",
            from_status=WorkflowStatus.RECEIVED,
            to_status=WorkflowStatus.RECEIVED,
            revision_from=-1,
            input_material=request.model_dump(mode="json"),
            output_material={"workflow_id": workflow_id, "context_digest": context_digest},
        )
        create_receipt["revision_from"] = -1
        create_receipt["revision_to"] = 0
        create_receipt["signature"] = self.task_signer.sign_payload(
            {key: value for key, value in create_receipt.items() if key != "signature"},
            token_type=TASK_RECEIPT_TYPE,
        )
        state["task_receipts"].append(create_receipt)
        self._sync_counts(summary, state)
        event_id = f"evt-{uuid.uuid4().hex}"
        payload = self._event_payload(
            actor,
            role,
            "workflow.received",
            {
                "task_id": task_id,
                "project_id": workflow_id,
                "ticket_id": request.ticket_id,
                "tenant_id": request.tenant_id,
                "context_digest": context_digest,
                "step_receipt_digest": sha256_digest(create_receipt),
            },
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO workflows(
                       workflow_id, status, revision, summary_json, state_json, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow_id,
                    summary.status.value,
                    summary.revision,
                    canonical_json(summary.model_dump(mode="json")),
                    canonical_json(state),
                    created_at,
                    created_at,
                ),
            )
            self._queue_outbox(
                conn,
                event_id=event_id,
                workflow_id=workflow_id,
                actor=actor,
                event_type="workflow.received",
                payload=payload,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._flush_outbox()
        return summary

    def normalize_case(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "intake",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "intake")
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.RECEIVED:
            raise WorkflowConflict("normalize_case requires RECEIVED state")
        task_id = task_id or f"{workflow_id}:01-intake"
        normalized = {
            "schema_version": "proofmesh.refund-case/v1",
            "workflow_id": workflow_id,
            "ticket_id": summary.ticket_id,
            "tenant_id": summary.tenant_id,
            "requester": summary.requester,
            "dedupe_key": sha256_digest(
                {"tenant_id": summary.tenant_id, "ticket_id": summary.ticket_id, "requester": summary.requester}
            ),
        }
        state["normalized_case"] = normalized
        summary.status = WorkflowStatus.NORMALIZED
        summary.next_step = "gather_context"
        summary.final_message = "输入已规范化；等待 investigator Worker 获取只读事实。"
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.RECEIVED,
            actor=actor,
            role=role,
            task_id=task_id,
            step="normalize_case",
            event_type="ticket.normalized",
            event_payload={"schema": normalized["schema_version"], "dedupe_key": normalized["dedupe_key"]},
            input_material={"ticket_id": summary.ticket_id, "tenant_id": summary.tenant_id},
            output_material=normalized,
        )

    def gather_context(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "investigator",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "investigator")
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.NORMALIZED:
            raise WorkflowConflict("gather_context requires NORMALIZED state")
        task_id = task_id or f"{workflow_id}:02-context"
        try:
            ticket = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="crm.get_ticket",
                arguments={"ticket_id": summary.ticket_id},
                resource=summary.ticket_id,
                scopes=["ticket:read"],
                mode="read",
                approval_digest="AUTOMATIC",
            )["result"]
            order = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="orders.get_refund_context",
                arguments={"order_id": ticket["order_id"]},
                resource=ticket["order_id"],
                scopes=["order:read"],
                mode="read",
                approval_digest="AUTOMATIC",
            )["result"]
        except (ToolExecutionError, PermissionError, ValueError) as exc:
            summary.status = WorkflowStatus.BLOCKED
            summary.next_step = None
            summary.final_message = f"上下文获取失败，已安全阻断：{getattr(exc, 'code', type(exc).__name__)}。"
            state["block_reason"] = getattr(exc, "code", type(exc).__name__)
            return self._commit_transition(
                summary=summary,
                state=state,
                expected_revision=expected_revision,
                from_status=WorkflowStatus.NORMALIZED,
                actor=actor,
                role=role,
                task_id=task_id,
                step="gather_context",
                event_type="workflow.blocked",
                event_payload={"stage": "context", "reason": state["block_reason"]},
                input_material=state["normalized_case"],
                output_material={"blocked": True, "reason": state["block_reason"]},
            )
        state["ticket"] = ticket
        state["order_before"] = order
        state["context_facts_digest"] = sha256_digest({"ticket": ticket, "order": order})
        summary.amount_minor = int(ticket["requested_amount_minor"])
        summary.currency = order["currency"]
        summary.status = WorkflowStatus.CONTEXT_READY
        summary.next_step = "evaluate_policy"
        summary.final_message = "脱敏上下文已固定；等待 policy Worker 评估。"
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.NORMALIZED,
            actor=actor,
            role=role,
            task_id=task_id,
            step="gather_context",
            event_type="context.gathered",
            event_payload={"context_facts_digest": state["context_facts_digest"], "order_version": order["version"]},
            input_material=state["normalized_case"],
            output_material={"ticket": ticket, "order": order},
        )

    def evaluate_policy(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "policy",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "policy")
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.CONTEXT_READY:
            raise WorkflowConflict("evaluate_policy requires CONTEXT_READY state")
        task_id = task_id or f"{workflow_id}:03-policy"
        ticket = state["ticket"]
        order = state["order_before"]
        try:
            risk = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="risk.score_refund",
                arguments={"ticket_id": summary.ticket_id, "amount_minor": ticket["requested_amount_minor"]},
                resource=summary.ticket_id,
                scopes=["risk:score"],
                mode="read",
                approval_digest="AUTOMATIC",
            )["result"]
        except (ToolExecutionError, PermissionError, ValueError) as exc:
            summary.status = WorkflowStatus.BLOCKED
            summary.next_step = None
            summary.final_message = "风险服务不可用，策略按 fail-closed 阻断。"
            state["block_reason"] = getattr(exc, "code", type(exc).__name__)
            return self._commit_transition(
                summary=summary,
                state=state,
                expected_revision=expected_revision,
                from_status=WorkflowStatus.CONTEXT_READY,
                actor=actor,
                role=role,
                task_id=task_id,
                step="evaluate_policy",
                event_type="policy.blocked",
                event_payload={"reason": state["block_reason"]},
                input_material={"ticket": ticket, "order": order},
                output_material={"decision": "DENY", "reason": state["block_reason"]},
            )
        policy = state["policy_snapshot"]
        amount = int(ticket["requested_amount_minor"])
        summary.risk_score = float(risk["score"])
        plan = {
            "ticket_id": ticket["ticket_id"],
            "order_id": order["order_id"],
            "amount_minor": amount,
            "currency": order["currency"],
            "expected_order_version": order["version"],
            "refundable_before_minor": order["refundable_minor"],
            "context_facts_digest": state["context_facts_digest"],
            "risk": risk,
            "actions": list(self.APPROVAL_SCOPE),
            "compensation": "payments.compensate_refund",
        }
        state["risk"] = risk
        state["plan"] = plan
        summary.plan_digest = sha256_digest(plan)
        summary.approval_scope = list(self.APPROVAL_SCOPE)
        block_reason = self._block_reason(ticket=ticket, order=order, risk=risk, policy=policy)
        if block_reason:
            decision = "DENY"
            summary.status = WorkflowStatus.BLOCKED
            summary.next_step = None
            summary.final_message = f"策略拒绝执行：{block_reason}。"
            state["block_reason"] = block_reason
        else:
            summary.approval_required = self._requires_approval(amount=amount, risk=float(risk["score"]), policy=policy)
            if summary.approval_required:
                decision = "REQUIRE_APPROVAL"
                summary.status = WorkflowStatus.WAITING_APPROVAL
                summary.next_step = "record_human_approval"
                summary.final_message = "冻结计划已生成；项目必须暂停并等待职责分离的 Human 审批。"
            else:
                decision = "ALLOW"
                summary.approval_digest = "AUTOMATIC"
                summary.status = WorkflowStatus.AUTHORIZED
                summary.next_step = "execute_authorized"
                summary.final_message = "冻结计划满足自动策略；等待 executor Worker。"
        state["policy_decision"] = decision
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.CONTEXT_READY,
            actor=actor,
            role=role,
            task_id=task_id,
            step="evaluate_policy",
            event_type="policy.evaluated",
            event_payload={
                "decision": decision,
                "policy_digest": summary.policy_digest,
                "plan_digest": summary.plan_digest,
                "risk_score": summary.risk_score,
                "approval_required": summary.approval_required,
            },
            input_material={"ticket": ticket, "order": order, "policy_digest": summary.policy_digest},
            output_material={"decision": decision, "plan": plan},
        )

    def approve(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        approver: str,
        reason: str,
        approval_assertion: str,
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.WAITING_APPROVAL:
            raise WorkflowConflict("workflow is not waiting for approval")
        if len(reason.strip()) < 12:
            raise WorkflowConflict("approval reason is too short")
        if state.get("policy_digest") != summary.policy_digest:
            raise WorkflowConflict("frozen policy digest is inconsistent")
        try:
            assertion = self.approval_verifier.verify_human_approval_assertion(approval_assertion)
        except PassportError as exc:
            raise WorkflowConflict(f"external approval assertion rejected: {exc.reason}") from exc
        challenge_with_digest = self.approval_challenge(workflow_id)
        expected_actions = challenge_with_digest["actions"]
        challenge = {key: value for key, value in challenge_with_digest.items() if key != "challenge_digest"}
        if (
            assertion.subject != approver
            or assertion.workflow_id != summary.workflow_id
            or assertion.project_id != summary.project_id
            or assertion.tenant_id != summary.tenant_id
            or assertion.requester != summary.requester
            or assertion.expected_revision != expected_revision
            or assertion.context_digest != summary.context_digest
            or assertion.policy_digest != summary.policy_digest
            or assertion.plan_digest != summary.plan_digest
            or assertion.challenge_digest != sha256_digest(challenge)
            or assertion.reason_digest != sha256_digest(reason.strip())
            or assertion.scope != sorted(summary.approval_scope)
            or [item.model_dump(mode="json") for item in assertion.actions] != expected_actions
            or assertion.amount_minor != summary.amount_minor
            or assertion.currency != summary.currency
        ):
            raise WorkflowConflict("external approval assertion does not match the frozen challenge")
        task_id = task_id or f"{workflow_id}:approval"
        approval = {
            "schema_version": "proofmesh.human-approval/v2",
            "workflow_id": workflow_id,
            "tenant_id": summary.tenant_id,
            "requester": summary.requester,
            "approver": approver,
            "reason": reason.strip(),
            "scope": assertion.scope,
            "plan_digest": assertion.plan_digest,
            "policy_digest": summary.policy_digest,
            "approved_at": utc_now(),
            "origin_assurance": "EXTERNAL_SIGNED_ASSERTION",
            "assertion": approval_assertion,
            "assertion_digest": sha256_digest(approval_assertion),
            "assertion_issuer": assertion.issuer,
            "assertion_jti": assertion.jti,
            "acr": assertion.acr,
            "amr": assertion.amr,
        }
        summary.approval_digest = sha256_digest(
            {"assertion": approval_assertion, "reason_digest": assertion.reason_digest}
        )
        state["approval"] = approval
        summary.status = WorkflowStatus.AUTHORIZED
        summary.next_step = "execute_authorized"
        summary.final_message = "职责分离的审批已原子记录；等待 executor Worker，尚未执行副作用。"
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.WAITING_APPROVAL,
            actor=approver,
            role="human-approver",
            task_id=task_id,
            step="record_human_approval",
            event_type="approval.recorded",
            event_payload={
                "approver": approver,
                "approval_digest": summary.approval_digest,
                "scope_digest": sha256_digest(assertion.scope),
                "assertion_digest": sha256_digest(approval_assertion),
                "project_resume_allowed": True,
            },
            input_material={"assertion_digest": sha256_digest(approval_assertion), "reason": reason.strip()},
            output_material=approval,
        )

    def approval_challenge(self, workflow_id: str) -> dict[str, Any]:
        """Return the exact content an external approval service must independently sign."""

        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.WAITING_APPROVAL:
            raise WorkflowConflict("workflow is not waiting for approval")
        plan = state["plan"]
        refund_arguments = {
            "ticket_id": plan["ticket_id"],
            "order_id": plan["order_id"],
            "amount_minor": plan["amount_minor"],
            "currency": plan["currency"],
            "expected_order_version": plan["expected_order_version"],
            "workflow_id": summary.workflow_id,
        }
        close_arguments = {
            "ticket_id": plan["ticket_id"],
            "workflow_id": summary.workflow_id,
            "resolution": f"退款 {plan['amount_minor']} {plan['currency']} 已签发",
        }
        body = {
            "workflow_id": summary.workflow_id,
            "project_id": summary.project_id,
            "tenant_id": summary.tenant_id,
            "requester": summary.requester,
            "expected_revision": summary.revision,
            "context_digest": summary.context_digest,
            "policy_digest": summary.policy_digest,
            "plan_digest": summary.plan_digest,
            "scope": sorted(summary.approval_scope),
            "actions": [
                {
                    "tool": "crm.close_ticket",
                    "resource": plan["ticket_id"],
                    "args_digest": sha256_digest(close_arguments),
                    "amount_minor": 0,
                    "currency": plan["currency"],
                },
                {
                    "tool": "payments.issue_refund",
                    "resource": plan["order_id"],
                    "args_digest": sha256_digest(refund_arguments),
                    "amount_minor": plan["amount_minor"],
                    "currency": plan["currency"],
                },
            ],
            "amount_minor": summary.amount_minor,
            "currency": summary.currency,
        }
        return {**body, "challenge_digest": sha256_digest(body)}

    def execute_authorized(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "executor",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "executor")
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.AUTHORIZED or not summary.approval_digest:
            raise WorkflowConflict("execute_authorized requires digest-bound AUTHORIZED state")
        if state.get("policy_digest") != summary.policy_digest or sha256_digest(state["plan"]) != summary.plan_digest:
            raise WorkflowConflict("frozen policy or plan was modified")
        task_id = task_id or f"{workflow_id}:04-execute"
        ticket = state["ticket"]
        plan = state["plan"]
        refund_id: str | None = None
        execution_error: str | None = None
        try:
            refund_arguments = {
                "ticket_id": ticket["ticket_id"],
                "order_id": plan["order_id"],
                "amount_minor": plan["amount_minor"],
                "currency": plan["currency"],
                "expected_order_version": plan["expected_order_version"],
                "workflow_id": summary.workflow_id,
            }
            refund = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="payments.issue_refund",
                arguments=refund_arguments,
                resource=plan["order_id"],
                scopes=["refund:write"],
                mode="execute",
                approval_digest=summary.approval_digest,
                max_amount_minor=plan["amount_minor"],
                budget_limit_minor=int(state["policy_snapshot"]["limits"]["max_workflow_refund_minor"]),
                approval_assertion=(state.get("approval") or {}).get("assertion"),
            )["result"]
            refund_id = refund["refund_id"]
            close = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="crm.close_ticket",
                arguments={
                    "ticket_id": ticket["ticket_id"],
                    "workflow_id": summary.workflow_id,
                    "resolution": f"退款 {plan['amount_minor']} {plan['currency']} 已签发",
                },
                resource=ticket["ticket_id"],
                scopes=["ticket:write"],
                mode="execute",
                approval_digest=summary.approval_digest,
                approval_assertion=(state.get("approval") or {}).get("assertion"),
            )["result"]
            state["execution_outcome"] = "SUCCEEDED"
            state["refund_result"] = refund
            state["close_result"] = close
            summary.status = WorkflowStatus.EXECUTED
            summary.next_step = "verify_outcome"
            summary.final_message = "退款与关单动作已执行；尚待独立 verifier 回查。"
        except Exception as exc:
            execution_error = getattr(exc, "code", type(exc).__name__)
            failed_receipt = getattr(exc, "gateway_receipt", None)
            if isinstance(failed_receipt, dict) and not any(
                item.get("call_id") == failed_receipt.get("call_id") for item in state["receipts"]
            ):
                state["receipts"].append(failed_receipt)
            if refund_id is None:
                reconciled = self.sandbox.workflow_snapshot(summary.workflow_id, tenant_id=summary.tenant_id)
                reconciled_refund = reconciled.get("refund") or {}
                if reconciled_refund.get("status") == "ISSUED":
                    refund_id = reconciled_refund["refund_id"]
            if refund_id is None:
                state["execution_outcome"] = "FAILED_BEFORE_SIDE_EFFECT"
                state["execution_error"] = execution_error
                summary.status = WorkflowStatus.BLOCKED
                summary.next_step = None
                summary.final_message = "执行未产生退款副作用，工作流已阻断。"
            else:
                emergency_digest = "EMERGENCY:" + sha256_digest(
                    {
                        "workflow_id": summary.workflow_id,
                        "refund_id": refund_id,
                        "prior_approval": summary.approval_digest,
                        "failed_step": execution_error,
                    }
                )
                try:
                    compensation = self._authorized_call(
                        summary=summary,
                        state=state,
                        actor=actor,
                        role=role,
                        tool="payments.compensate_refund",
                        arguments={
                            "refund_id": refund_id,
                            "workflow_id": summary.workflow_id,
                            "reason": f"downstream step failed: {execution_error}",
                        },
                        resource=refund_id,
                        scopes=["refund:compensate"],
                        mode="compensate",
                        approval_digest=emergency_digest,
                    )["result"]
                    state["execution_outcome"] = "COMPENSATED"
                    state["compensation_result"] = compensation
                    state["execution_error"] = execution_error
                    summary.status = WorkflowStatus.EXECUTED
                    summary.next_step = "verify_outcome"
                    summary.final_message = "下游步骤失败且补偿已执行；等待 verifier 独立确认余额与工单状态。"
                except Exception as compensation_error:
                    state["execution_outcome"] = "COMPENSATION_FAILED"
                    state["execution_error"] = execution_error
                    state["compensation_error"] = getattr(
                        compensation_error, "code", type(compensation_error).__name__
                    )
                    summary.status = WorkflowStatus.BLOCKED
                    summary.next_step = None
                    summary.final_message = "补偿失败，状态已冻结并升级人工处置。"
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.AUTHORIZED,
            actor=actor,
            role=role,
            task_id=task_id,
            step="execute_authorized",
            event_type="saga.executed",
            event_payload={
                "outcome": state.get("execution_outcome"),
                "error": execution_error,
                "approval_digest": summary.approval_digest,
            },
            input_material={"plan": plan, "approval_digest": summary.approval_digest},
            output_material={
                "outcome": state.get("execution_outcome"),
                "refund_result": state.get("refund_result"),
                "close_result": state.get("close_result"),
                "compensation_result": state.get("compensation_result"),
            },
        )

    def verify_outcome(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "verifier",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "verifier")
        summary, state = self._load(workflow_id)
        if summary.status != WorkflowStatus.EXECUTED:
            raise WorkflowConflict("verify_outcome requires EXECUTED state")
        task_id = task_id or f"{workflow_id}:05-verify"
        snapshot = self.sandbox.workflow_snapshot(summary.workflow_id, tenant_id=summary.tenant_id)
        plan = state["plan"]
        outcome = state.get("execution_outcome")
        if outcome == "SUCCEEDED":
            expected_balance = state["order_before"]["refundable_minor"] - plan["amount_minor"]
            verified = (
                snapshot.get("refund", {}).get("status") == "ISSUED"
                and snapshot.get("refund", {}).get("ticket_id") == plan["ticket_id"]
                and snapshot.get("refund", {}).get("order_id") == plan["order_id"]
                and snapshot.get("refund", {}).get("amount_minor") == plan["amount_minor"]
                and snapshot.get("refund", {}).get("currency") == plan["currency"]
                and snapshot.get("ticket", {}).get("status") == "CLOSED"
                and snapshot.get("ticket", {}).get("closed_workflow_id") == summary.workflow_id
                and snapshot.get("order", {}).get("refundable_minor") == expected_balance
            )
            summary.status = WorkflowStatus.VERIFIED if verified else WorkflowStatus.BLOCKED
            summary.next_step = "curate_memory" if verified else None
            summary.final_message = (
                "独立业务回查通过；等待 memory Worker 封存脱敏经验与证明。"
                if verified
                else "执行结果与冻结计划不一致，已阻断并升级人工处置。"
            )
        elif outcome == "COMPENSATED":
            verified = (
                snapshot.get("refund", {}).get("status") == "COMPENSATED"
                and snapshot.get("ticket", {}).get("status") == "OPEN"
                and snapshot.get("ticket", {}).get("closed_workflow_id") is None
                and snapshot.get("order", {}).get("refundable_minor")
                == state["order_before"]["refundable_minor"]
            )
            summary.status = WorkflowStatus.COMPENSATED if verified else WorkflowStatus.BLOCKED
            summary.next_step = "curate_memory" if verified else None
            summary.final_message = (
                "补偿已独立验证：余额恢复、工单开放；等待封存证明。"
                if verified
                else "补偿后业务状态不一致，已冻结并升级人工处置。"
            )
        else:
            verified = False
            summary.status = WorkflowStatus.BLOCKED
            summary.next_step = None
            summary.final_message = "执行结果不是可验证终态，已安全阻断。"
        state["verified_business_snapshot"] = snapshot
        state["verification"] = {"passed": verified, "outcome": outcome, "verified_at": utc_now()}
        return self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=WorkflowStatus.EXECUTED,
            actor=actor,
            role=role,
            task_id=task_id,
            step="verify_outcome",
            event_type="postconditions.verified",
            event_payload={"passed": verified, "outcome": outcome, "snapshot_digest": sha256_digest(snapshot)},
            input_material={"plan": plan, "execution_outcome": outcome},
            output_material={"verified": verified, "snapshot": snapshot},
        )

    def curate_memory(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        actor: str,
        role: str = "memory",
        task_id: str | None = None,
    ) -> RefundWorkflowSummary:
        self._require_role(role, "memory")
        summary, state = self._load(workflow_id)
        from_status = summary.status
        if from_status not in {WorkflowStatus.VERIFIED, WorkflowStatus.COMPENSATED}:
            raise WorkflowConflict("curate_memory requires VERIFIED or COMPENSATED state")
        if not state.get("verification", {}).get("passed"):
            raise WorkflowConflict("memory curation requires an independently verified outcome")
        task_id = task_id or f"{workflow_id}:06-memory"
        ticket = state["ticket"]
        resolution = {
            "reason_category": ticket["reason"],
            "amount_band": "human" if summary.approval_required else "automatic",
            "outcome": "compensated" if from_status == WorkflowStatus.COMPENSATED else "refund_and_close",
        }
        memory_error: str | None = None
        try:
            memory_result = self._authorized_call(
                summary=summary,
                state=state,
                actor=actor,
                role=role,
                tool="memory.store_resolution",
                arguments={
                    "workflow_id": summary.workflow_id,
                    "fingerprint": sha256_digest({"reason": ticket["reason"], "risk": state["risk"]["reasons"]}),
                    "resolution": resolution,
                },
                resource=summary.workflow_id,
                scopes=["memory:write"],
                mode="execute",
                approval_digest=summary.approval_digest or "AUTOMATIC",
            )["result"]
            state["memory_result"] = memory_result
            state["memory_status"] = "STORED"
        except Exception as exc:
            memory_error = getattr(exc, "code", type(exc).__name__)
            state["memory_status"] = "FAILED"
            state["memory_error"] = memory_error
        if from_status == WorkflowStatus.VERIFIED:
            summary.status = WorkflowStatus.COMPLETED
        else:
            summary.status = WorkflowStatus.COMPENSATED
        summary.next_step = None
        summary.proof_bundle = f"artifacts/workflows/{workflow_id}/workflow-proof.json"
        summary.final_message = (
            "业务终态、签名回执、任务轨迹与脱敏经验均已封存。"
            if memory_error is None
            else "业务终态与证明已封存；经验写入失败已作为审计告警保留。"
        )
        committed = self._commit_transition(
            summary=summary,
            state=state,
            expected_revision=expected_revision,
            from_status=from_status,
            actor=actor,
            role=role,
            task_id=task_id,
            step="curate_memory",
            event_type="workflow.sealed",
            event_payload={"memory_status": state["memory_status"], "memory_error": memory_error},
            input_material={"verification": state["verification"], "resolution": resolution},
            output_material={"memory_status": state["memory_status"], "proof_bundle": summary.proof_bundle},
        )
        self._write_proof_bundle(committed, state)
        return committed

    def run_until_gate_or_terminal(self, request: RefundWorkflowRequest) -> RefundWorkflowSummary:
        """Deterministic local smoke runner; production AgentTeams calls each step separately."""

        summary = self.create_case(request, actor=self.ROLES["orchestrator"])
        summary = self.normalize_case(
            summary.workflow_id, expected_revision=summary.revision, actor=self.ROLES["intake"]
        )
        summary = self.gather_context(
            summary.workflow_id, expected_revision=summary.revision, actor=self.ROLES["investigator"]
        )
        if summary.status == WorkflowStatus.BLOCKED:
            return summary
        summary = self.evaluate_policy(
            summary.workflow_id, expected_revision=summary.revision, actor=self.ROLES["policy"]
        )
        if summary.status != WorkflowStatus.AUTHORIZED:
            return summary
        return self.resume_authorized_locally(summary.workflow_id, expected_revision=summary.revision)

    def resume_authorized_locally(self, workflow_id: str, *, expected_revision: int) -> RefundWorkflowSummary:
        summary = self.execute_authorized(
            workflow_id, expected_revision=expected_revision, actor=self.ROLES["executor"]
        )
        if summary.status != WorkflowStatus.EXECUTED:
            return summary
        summary = self.verify_outcome(
            workflow_id, expected_revision=summary.revision, actor=self.ROLES["verifier"]
        )
        if summary.status not in {WorkflowStatus.VERIFIED, WorkflowStatus.COMPENSATED}:
            return summary
        return self.curate_memory(
            workflow_id, expected_revision=summary.revision, actor=self.ROLES["memory"]
        )

    def start(self, request: RefundWorkflowRequest) -> RefundWorkflowSummary:
        """Backward-compatible local smoke entry point; not used by AgentTeams or the public API."""

        return self.run_until_gate_or_terminal(request)

    def _authorized_call(
        self,
        *,
        summary: RefundWorkflowSummary,
        state: dict[str, Any],
        actor: str,
        role: str,
        tool: str,
        arguments: dict[str, Any],
        resource: str,
        scopes: list[str],
        mode: str,
        approval_digest: str,
        approval_assertion: str | None = None,
        max_amount_minor: int = 0,
        budget_limit_minor: int | None = None,
    ) -> dict[str, Any]:
        if budget_limit_minor is None:
            budget_limit_minor = int(state["policy_snapshot"]["limits"]["max_workflow_refund_minor"])
        return self.authorized_tools.call(
            workflow_id=summary.workflow_id,
            tenant_id=summary.tenant_id,
            subject=actor,
            context_digest=summary.context_digest,
            policy_digest=summary.policy_digest,
            plan_digest=summary.plan_digest,
            state=state,
            tool=tool,
            arguments=arguments,
            resource=str(resource),
            scopes=scopes,
            mode=mode,
            approval_digest=approval_digest,
            approval_assertion=approval_assertion,
            max_amount_minor=max_amount_minor,
            budget_limit_minor=budget_limit_minor,
            currency=summary.currency or "CNY",
            role=role,
            event_callback=lambda event_type, subject, payload: self._direct_event(
                summary.workflow_id,
                subject,
                role,
                event_type,
                {key: value for key, value in payload.items() if key != "role"},
            ),
        )

    @staticmethod
    def _block_reason(
        *,
        ticket: dict[str, Any],
        order: dict[str, Any],
        risk: dict[str, Any],
        policy: dict[str, Any],
    ) -> str | None:
        amount = int(ticket["requested_amount_minor"])
        limits = policy["limits"]
        if ticket["status"] != "OPEN":
            return "ticket_not_open"
        if order["status"] not in policy["eligible_order_statuses"]:
            return "order_not_eligible"
        if amount <= 0 or amount > int(order["refundable_minor"]):
            return "amount_exceeds_refundable_balance"
        if amount > int(limits["max_workflow_refund_minor"]):
            return "amount_exceeds_policy_limit"
        if float(risk["score"]) >= float(limits["block_risk_score"]):
            return "risk_score_blocked"
        return None

    @staticmethod
    def _requires_approval(*, amount: int, risk: float, policy: dict[str, Any]) -> bool:
        limits = policy["limits"]
        return amount > int(limits["auto_approve_minor"]) or risk >= float(limits["human_review_risk_score"])

    def _write_proof_bundle(
        self,
        summary: RefundWorkflowSummary,
        state: dict[str, Any],
    ) -> str:
        target = self.artifact_dir / summary.workflow_id
        target.mkdir(parents=True, exist_ok=True)
        events = self.ledger.events(summary.workflow_id)
        chain_valid, chain_head = self.ledger.verify(summary.workflow_id)
        snapshot, business_attestation = self.sandbox.attest_workflow_snapshot(
            summary.workflow_id,
            tenant_id=summary.tenant_id,
        )
        body = {
            "schema_version": "proofmesh.workflow-proof/v2",
            "issuer": self.proof_signer.issuer,
            "summary": summary.model_dump(mode="json"),
            "policy": state["policy_snapshot"],
            "policy_digest": state["policy_digest"],
            "plan": state.get("plan"),
            "approval": state.get("approval"),
            "task_step_receipts": state.get("task_receipts", []),
            "gateway_receipts": self.gateway.store.receipts(
                summary.workflow_id, tenant_id=summary.tenant_id
            ),
            "tool_executions": state.get("tool_executions", []),
            "workflow_events": events,
            "event_chain": {"valid_at_seal": chain_valid, "head": chain_head},
            "verified_business_snapshot": snapshot,
            "business_snapshot_attestation": business_attestation,
            "verification": state.get("verification"),
            "memory_status": state.get("memory_status"),
            "trust_model": "Trust keys and pinned policy are external verifier inputs; no embedded root is trusted.",
        }
        body["bundle_signature"] = self.proof_signer.sign_payload(
            body, token_type=PROOF_TYPE
        )
        path = target / "workflow-proof.json"
        temp_path = target / ".workflow-proof.json.tmp"
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return str(path.relative_to(self.home))
