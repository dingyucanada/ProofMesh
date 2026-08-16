from __future__ import annotations

import json
import os
import sqlite3
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .business import ReconciliationResult, ToolExecutionError, ToolSpec
from .capabilities import (
    BUSINESS_ATTESTATION_TYPE,
    PROOF_TYPE,
    JwsSigner,
    ExternalTrustVerifier,
    PassportError,
    canonical_json,
    sha256_digest,
)
from .domain_protocol import AuthorizedToolCaller
from .gateway import ActionGateway, GatewayDenied
from .ledger import EvidenceLedger
from .timeutil import utc_now


class ChangeStatus(str, Enum):
    RECEIVED = "RECEIVED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    EXECUTED = "EXECUTED"
    COMPLETED = "COMPLETED"
    COMPENSATED = "COMPENSATED"
    UNKNOWN_MANUAL = "UNKNOWN_MANUAL"
    BLOCKED = "BLOCKED"


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(pattern=r"^CHG-[A-Z0-9-]{3,60}$")
    tenant_id: str = Field(pattern=r"^[a-z0-9-]{2,64}$")
    requester: str = Field(min_length=3, max_length=128)


class ChangeSummary(BaseModel):
    workflow_id: str
    project_id: str
    domain: str = "production-operations-change"
    change_id: str
    tenant_id: str
    requester: str
    status: ChangeStatus
    revision: int = 0
    created_at: str
    updated_at: str
    context_digest: str
    policy_digest: str
    plan_digest: str | None = None
    approval_required: bool = False
    approval_scope: list[str] = Field(default_factory=list)
    approval_digest: str | None = None
    risk_units: int = 0
    final_message: str = ""
    proof_bundle: str | None = None
    gateway_receipt_count: int = 0


class OperationsSandbox:
    """Deterministic operations target with real SQLite state and reversible writes.

    It is a reference adapter, not a production Kubernetes/cloud account.  The
    uncertainty fixture emulates an upstream whose write cannot be reconciled;
    the adapter therefore returns UNKNOWN and stops automatic retries.
    """

    def __init__(self, db_path: str | Path, *, attestation_signer: JwsSigner):
        self.db_path = Path(db_path)
        self.attestation_signer = attestation_signer
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ops.get_change": self.get_change,
            "ops.get_service_config": self.get_service_config,
            "ops.apply_config": self.apply_config,
            "ops.run_health_check": self.run_health_check,
            "ops.restore_config": self.restore_config,
        }
        self.specs = self._build_specs()

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
                CREATE TABLE IF NOT EXISTS services (
                    service_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    health TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS changes (
                    change_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    patch_json TEXT NOT NULL,
                    risk_units INTEGER NOT NULL,
                    blast_radius TEXT NOT NULL,
                    force_health_failure INTEGER NOT NULL,
                    force_unknown_apply INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    workflow_id TEXT,
                    FOREIGN KEY(service_id) REFERENCES services(service_id)
                );
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL UNIQUE,
                    change_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    before_config_json TEXT NOT NULL,
                    before_version INTEGER NOT NULL,
                    after_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    restored_at TEXT
                );
                """
            )
            if conn.execute("SELECT COUNT(*) AS n FROM services").fetchone()["n"] == 0:
                conn.execute(
                    "INSERT INTO services VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        "svc-checkout",
                        "acme-cn",
                        "production",
                        canonical_json({"checkout_timeout_ms": 1500, "max_inflight": 80}),
                        7,
                        "HEALTHY",
                    ),
                )
                conn.executemany(
                    "INSERT INTO changes VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL)",
                    [
                        (
                            "CHG-LOW-001",
                            "acme-cn",
                            "svc-checkout",
                            canonical_json({"checkout_timeout_ms": 1700}),
                            20,
                            "single-service",
                            0,
                            0,
                        ),
                        (
                            "CHG-HIGH-001",
                            "acme-cn",
                            "svc-checkout",
                            canonical_json({"max_inflight": 160}),
                            80,
                            "customer-path",
                            0,
                            0,
                        ),
                        (
                            "CHG-ROLLBACK-001",
                            "acme-cn",
                            "svc-checkout",
                            canonical_json({"checkout_timeout_ms": 300}),
                            35,
                            "single-service",
                            1,
                            0,
                        ),
                        (
                            "CHG-UNKNOWN-001",
                            "acme-cn",
                            "svc-checkout",
                            canonical_json({"max_inflight": 96}),
                            25,
                            "single-service",
                            0,
                            1,
                        ),
                    ],
                )

    @staticmethod
    def _tenant(arguments: dict[str, Any]) -> str:
        tenant_id = arguments.get("_proofmesh_tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ToolExecutionError("tenant_context_missing", "verified tenant is required")
        return tenant_id

    @staticmethod
    def _build_specs() -> dict[str, ToolSpec]:
        obj = {"type": "object", "additionalProperties": False}
        return {
            "ops.get_change": ToolSpec(
                "ops.get_change",
                "Read a sanitized production change request.",
                ("change:read",),
                "read",
                "change_id",
                None,
                {**obj, "required": ["change_id"], "properties": {"change_id": {"type": "string"}}},
            ),
            "ops.get_service_config": ToolSpec(
                "ops.get_service_config",
                "Read current service configuration and optimistic version.",
                ("config:read",),
                "read",
                "service_id",
                None,
                {**obj, "required": ["service_id"], "properties": {"service_id": {"type": "string"}}},
            ),
            "ops.apply_config": ToolSpec(
                "ops.apply_config",
                "Apply one frozen configuration patch with optimistic locking.",
                ("config:write",),
                "execute",
                "service_id",
                "risk_units",
                {
                    **obj,
                    "required": ["change_id", "service_id", "patch", "expected_version", "workflow_id", "risk_units"],
                    "properties": {
                        "change_id": {"type": "string"},
                        "service_id": {"type": "string"},
                        "patch": {"type": "object"},
                        "expected_version": {"type": "integer", "minimum": 0},
                        "workflow_id": {"type": "string"},
                        "risk_units": {"type": "integer", "minimum": 0},
                    },
                },
            ),
            "ops.run_health_check": ToolSpec(
                "ops.run_health_check",
                "Evaluate post-change synthetic health gates.",
                ("health:read",),
                "read",
                "service_id",
                None,
                {
                    **obj,
                    "required": ["change_id", "service_id", "workflow_id"],
                    "properties": {
                        "change_id": {"type": "string"},
                        "service_id": {"type": "string"},
                        "workflow_id": {"type": "string"},
                    },
                },
            ),
            "ops.restore_config": ToolSpec(
                "ops.restore_config",
                "Restore the exact pre-change configuration after failed health gates.",
                ("config:restore",),
                "compensate",
                "service_id",
                None,
                {
                    **obj,
                    "required": ["change_id", "service_id", "workflow_id", "reason"],
                    "properties": {
                        "change_id": {"type": "string"},
                        "service_id": {"type": "string"},
                        "workflow_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            ),
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": {"readOnlyHint": spec.mode == "read", "destructiveHint": spec.mode != "read"},
                "_meta": {"proofmesh/requiredScopes": list(spec.required_scopes), "proofmesh/mode": spec.mode},
            }
            for spec in self.specs.values()
        ]

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(tool)
        if handler is None:
            raise ToolExecutionError("tool_not_found", f"unknown tool {tool}")
        return handler(arguments)

    def get_change(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant = self._tenant(arguments)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM changes WHERE change_id = ? AND tenant_id = ?",
                (arguments["change_id"], tenant),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("change_not_found", "change request does not exist")
        value = dict(row)
        value["patch"] = json.loads(value.pop("patch_json"))
        return value

    def get_service_config(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant = self._tenant(arguments)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM services WHERE service_id = ? AND tenant_id = ?",
                (arguments["service_id"], tenant),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("service_not_found", "service does not exist")
        value = dict(row)
        value["config"] = json.loads(value.pop("config_json"))
        return value

    def apply_config(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant = self._tenant(arguments)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            change = conn.execute(
                "SELECT * FROM changes WHERE change_id = ? AND tenant_id = ?",
                (arguments["change_id"], tenant),
            ).fetchone()
            service = conn.execute(
                "SELECT * FROM services WHERE service_id = ? AND tenant_id = ?",
                (arguments["service_id"], tenant),
            ).fetchone()
            if change is None or service is None or change["service_id"] != service["service_id"]:
                raise ToolExecutionError("change_target_mismatch", "change and target do not match")
            if change["force_unknown_apply"]:
                # A transport loss is intentionally not a typed upstream rejection:
                # Gateway must retain the logical operation and reconcile before retry.
                raise ConnectionError("reference upstream response lost after uncertain dispatch")
            existing = conn.execute(
                "SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?",
                (arguments["workflow_id"], tenant),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {
                    "execution_id": existing["execution_id"],
                    "service_id": existing["service_id"],
                    "version": existing["after_version"],
                    "status": existing["status"],
                    "idempotent_replay": True,
                }
            if service["version"] != arguments["expected_version"]:
                raise ToolExecutionError("concurrent_config_update", "service version changed")
            if json.loads(change["patch_json"]) != arguments["patch"]:
                raise ToolExecutionError("frozen_patch_mismatch", "patch differs from approved request")
            new_config = {**json.loads(service["config_json"]), **arguments["patch"]}
            updated = conn.execute(
                "UPDATE services SET config_json = ?, version = version + 1 WHERE service_id = ? AND tenant_id = ? AND version = ?",
                (canonical_json(new_config), service["service_id"], tenant, service["version"]),
            )
            if updated.rowcount != 1:
                raise ToolExecutionError("concurrent_config_update", "optimistic update failed")
            execution_id = f"EXEC-{uuid.uuid4().hex[:16].upper()}"
            conn.execute(
                "INSERT INTO executions VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'APPLIED', ?, ?, NULL)",
                (
                    execution_id,
                    tenant,
                    arguments["workflow_id"],
                    change["change_id"],
                    service["service_id"],
                    service["config_json"],
                    service["version"],
                    service["version"] + 1,
                    arguments["_proofmesh_operation_id"],
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE changes SET status = 'APPLIED', workflow_id = ? WHERE change_id = ? AND tenant_id = ?",
                (arguments["workflow_id"], change["change_id"], tenant),
            )
            conn.commit()
            return {
                "execution_id": execution_id,
                "service_id": service["service_id"],
                "version": service["version"] + 1,
                "status": "APPLIED",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_health_check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant = self._tenant(arguments)
        with self._connect() as conn:
            execution = conn.execute(
                "SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?",
                (arguments["workflow_id"], tenant),
            ).fetchone()
            change = conn.execute(
                "SELECT * FROM changes WHERE change_id = ? AND tenant_id = ?",
                (arguments["change_id"], tenant),
            ).fetchone()
        if execution is None or change is None:
            raise ToolExecutionError("execution_not_found", "health check requires applied execution")
        if change["force_health_failure"]:
            raise ToolExecutionError("health_gate_failed", "reference error-rate gate failed")
        return {"service_id": arguments["service_id"], "healthy": True, "gates": {"error_rate": "PASS"}}

    def restore_config(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant = self._tenant(arguments)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            execution = conn.execute(
                "SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?",
                (arguments["workflow_id"], tenant),
            ).fetchone()
            if execution is None or execution["change_id"] != arguments["change_id"]:
                raise ToolExecutionError("execution_not_found", "restorable execution not found")
            if execution["status"] == "RESTORED":
                conn.commit()
                return {"execution_id": execution["execution_id"], "status": "RESTORED", "idempotent_replay": True}
            service = conn.execute(
                "SELECT * FROM services WHERE service_id = ? AND tenant_id = ?",
                (arguments["service_id"], tenant),
            ).fetchone()
            if service is None or service["version"] != execution["after_version"]:
                raise ToolExecutionError("unsafe_restore_after_drift", "service changed after this workflow")
            conn.execute(
                "UPDATE services SET config_json = ?, version = version + 1 WHERE service_id = ? AND tenant_id = ?",
                (execution["before_config_json"], service["service_id"], tenant),
            )
            conn.execute(
                "UPDATE executions SET status = 'RESTORED', restored_at = ? WHERE execution_id = ?",
                (utc_now(), execution["execution_id"]),
            )
            conn.execute(
                "UPDATE changes SET status = 'RESTORED' WHERE change_id = ? AND tenant_id = ?",
                (execution["change_id"], tenant),
            )
            conn.commit()
            return {"execution_id": execution["execution_id"], "status": "RESTORED", "config_restored": True}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        del operation_id
        if tool in {"ops.get_change", "ops.get_service_config", "ops.run_health_check"}:
            return ReconciliationResult("SAFE_TO_RETRY")
        if tool == "ops.apply_config":
            with self._connect() as conn:
                change = conn.execute(
                    "SELECT * FROM changes WHERE change_id = ? AND tenant_id = ?",
                    (arguments["change_id"], tenant_id),
                ).fetchone()
                row = conn.execute(
                    "SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?",
                    (arguments["workflow_id"], tenant_id),
                ).fetchone()
            if change is not None and change["force_unknown_apply"]:
                return ReconciliationResult("UNKNOWN", reason="upstream_commit_state_unavailable")
            if row is None:
                return ReconciliationResult("SAFE_TO_RETRY")
            return ReconciliationResult(
                "SUCCEEDED",
                {"execution_id": row["execution_id"], "service_id": row["service_id"], "version": row["after_version"], "status": row["status"], "idempotent_replay": True},
            )
        if tool == "ops.restore_config":
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?",
                    (arguments["workflow_id"], tenant_id),
                ).fetchone()
            if row is None:
                return ReconciliationResult("UNKNOWN", reason="execution_missing_during_restore")
            if row["status"] == "RESTORED":
                return ReconciliationResult("SUCCEEDED", {"execution_id": row["execution_id"], "status": "RESTORED", "idempotent_replay": True})
            return ReconciliationResult("SAFE_TO_RETRY")
        return ReconciliationResult("UNKNOWN", reason="unsupported_reconciliation")

    def workflow_snapshot(self, workflow_id: str, *, tenant_id: str, change_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            change = conn.execute("SELECT * FROM changes WHERE change_id = ? AND tenant_id = ?", (change_id, tenant_id)).fetchone()
            execution = conn.execute("SELECT * FROM executions WHERE workflow_id = ? AND tenant_id = ?", (workflow_id, tenant_id)).fetchone()
            service = conn.execute("SELECT * FROM services WHERE service_id = ? AND tenant_id = ?", (change["service_id"], tenant_id)).fetchone() if change else None
        return {
            "workflow_id": workflow_id,
            "change": dict(change) if change else None,
            "execution": dict(execution) if execution else None,
            "service": {**dict(service), "config": json.loads(service["config_json"])} if service else None,
        }

    def attest_snapshot(self, workflow_id: str, *, tenant_id: str, change_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = self.workflow_snapshot(workflow_id, tenant_id=tenant_id, change_id=change_id)
        statement = {
            "schema_version": "proofmesh.business-snapshot-attestation/v1",
            "issuer": self.attestation_signer.issuer,
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "domain": "production-operations-change",
            "snapshot_digest": sha256_digest(snapshot),
            "captured_at": utc_now(),
        }
        return snapshot, {
            "statement": statement,
            "signature": self.attestation_signer.sign_payload(statement, token_type=BUSINESS_ATTESTATION_TYPE),
        }


class OperationsChangeControlPlane:
    APPROVAL_SCOPE = ["ops.apply_config"]

    def __init__(
        self,
        *,
        home: str | Path,
        gateway: ActionGateway,
        passport_signer: JwsSigner,
        proof_signer: JwsSigner,
        sandbox: OperationsSandbox,
        approval_verifier: ExternalTrustVerifier,
        reference_fault_injection: bool = False,
    ):
        self.home = Path(home)
        self.gateway = gateway
        self.sandbox = sandbox
        self.proof_signer = proof_signer
        self.approval_verifier = approval_verifier
        self.reference_fault_injection = reference_fault_injection
        self.authorized_tools = AuthorizedToolCaller(gateway=gateway, passport_signer=passport_signer)
        self.policy = json.loads((self.home / "data/policies/operations_change_policy.json").read_text(encoding="utf-8"))
        self.policy_digest = sha256_digest(self.policy)
        self.db_path = self.home / "var/operations-control-plane.db"
        self.artifact_dir = self.home / "artifacts/operations-workflows"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = EvidenceLedger(self.home / "var/operations-evidence.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS operations_workflows (
                    workflow_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _store(self, summary: ChangeSummary, state: dict[str, Any], *, expected_revision: int | None) -> ChangeSummary:
        now = utc_now()
        summary.updated_at = now
        if expected_revision is None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO operations_workflows VALUES(?, ?, ?, ?, ?)",
                    (summary.workflow_id, summary.revision, canonical_json(summary.model_dump(mode="json")), canonical_json(state), now),
                )
            return summary
        next_revision = expected_revision + 1
        summary.revision = next_revision
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE operations_workflows SET revision = ?, summary_json = ?, state_json = ?, updated_at = ? WHERE workflow_id = ? AND revision = ?",
                (next_revision, canonical_json(summary.model_dump(mode="json")), canonical_json(state), now, summary.workflow_id, expected_revision),
            )
            if result.rowcount != 1:
                raise RuntimeError("operations workflow revision conflict")
        return summary

    def _load(self, workflow_id: str) -> tuple[ChangeSummary, dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM operations_workflows WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(workflow_id)
        return ChangeSummary.model_validate_json(row["summary_json"]), json.loads(row["state_json"])

    def _event(self, summary: ChangeSummary, actor: str, event_type: str, payload: dict[str, Any]) -> None:
        self.ledger.append(summary.workflow_id, actor, event_type, {"domain": summary.domain, **payload})

    def _call(
        self,
        summary: ChangeSummary,
        state: dict[str, Any],
        *,
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
    ) -> dict[str, Any]:
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
            resource=resource,
            scopes=scopes,
            mode=mode,
            approval_digest=approval_digest,
            approval_assertion=approval_assertion,
            max_amount_minor=max_amount_minor,
            budget_limit_minor=int(self.policy["limits"]["max_workflow_risk_units"]),
            currency="XTS",
            role=role,
            event_callback=lambda event_type, subject, payload: self._event(summary, subject, event_type, payload),
        )

    def create_and_plan(self, request: ChangeRequest, *, actor: str = "ops-orchestrator") -> ChangeSummary:
        workflow_id = f"ops-{uuid.uuid4().hex}"
        now = utc_now()
        context_digest = sha256_digest({"workflow_id": workflow_id, "change_id": request.change_id, "tenant_id": request.tenant_id})
        summary = ChangeSummary(
            workflow_id=workflow_id,
            project_id=workflow_id,
            change_id=request.change_id,
            tenant_id=request.tenant_id,
            requester=request.requester,
            status=ChangeStatus.RECEIVED,
            created_at=now,
            updated_at=now,
            context_digest=context_digest,
            policy_digest=self.policy_digest,
            final_message="运维变更已创建，冻结事实和计划。",
        )
        state: dict[str, Any] = {"receipts": [], "tool_results": {}, "tool_executions": [], "approval": None, "policy": self.policy}
        self._store(summary, state, expected_revision=None)
        change = self._call(
            summary,
            state,
            actor="ops-context-investigator",
            role="investigator",
            tool="ops.get_change",
            arguments={"change_id": request.change_id},
            resource=request.change_id,
            scopes=["change:read"],
            mode="read",
            approval_digest="AUTOMATIC",
        )["result"]
        service = self._call(
            summary,
            state,
            actor="ops-context-investigator",
            role="investigator",
            tool="ops.get_service_config",
            arguments={"service_id": change["service_id"]},
            resource=change["service_id"],
            scopes=["config:read"],
            mode="read",
            approval_digest="AUTOMATIC",
        )["result"]
        plan = {
            "change_id": change["change_id"],
            "service_id": change["service_id"],
            "environment": service["environment"],
            "patch": change["patch"],
            "expected_version": service["version"],
            "before_config_digest": sha256_digest(service["config"]),
            "risk_units": int(change["risk_units"]),
            "blast_radius": change["blast_radius"],
            "actions": list(self.APPROVAL_SCOPE),
            "compensation": "ops.restore_config",
        }
        state["change"] = change
        state["service_before"] = service
        state["plan"] = plan
        summary.plan_digest = sha256_digest(plan)
        summary.risk_units = int(change["risk_units"])
        summary.approval_scope = list(self.APPROVAL_SCOPE)
        summary.approval_required = summary.risk_units > int(self.policy["limits"]["auto_approve_risk_units"])
        if summary.risk_units > int(self.policy["limits"]["max_workflow_risk_units"]):
            summary.status = ChangeStatus.BLOCKED
            summary.final_message = "风险单位超过策略上限，已阻断。"
        elif summary.approval_required:
            summary.status = ChangeStatus.WAITING_APPROVAL
            summary.final_message = "高风险生产变更已冻结，等待职责分离审批。"
        else:
            summary.status = ChangeStatus.AUTHORIZED
            summary.approval_digest = "AUTOMATIC"
            summary.final_message = "低风险冻结计划满足自动策略。"
        summary.gateway_receipt_count = len(state["receipts"])
        self._event(summary, actor, "operations.plan.frozen", {"plan_digest": summary.plan_digest, "approval_required": summary.approval_required})
        return self._store(summary, state, expected_revision=0)

    def approval_challenge(self, workflow_id: str) -> dict[str, Any]:
        summary, state = self._load(workflow_id)
        if summary.status != ChangeStatus.WAITING_APPROVAL:
            raise RuntimeError("operations workflow is not waiting for approval")
        plan = state["plan"]
        apply_arguments = {
            "change_id": plan["change_id"],
            "service_id": plan["service_id"],
            "patch": plan["patch"],
            "expected_version": plan["expected_version"],
            "workflow_id": summary.workflow_id,
            "risk_units": plan["risk_units"],
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
                    "tool": "ops.apply_config",
                    "resource": plan["service_id"],
                    "args_digest": sha256_digest(apply_arguments),
                    "amount_minor": plan["risk_units"],
                    "currency": "XTS",
                }
            ],
            "amount_minor": plan["risk_units"],
            "currency": "XTS",
        }
        return {**body, "challenge_digest": sha256_digest(body)}

    def approve(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        approver: str,
        reason: str,
        approval_assertion: str,
    ) -> ChangeSummary:
        summary, state = self._load(workflow_id)
        if summary.status != ChangeStatus.WAITING_APPROVAL or summary.revision != expected_revision:
            raise RuntimeError("operations workflow approval state or revision mismatch")
        if len(reason.strip()) < 12:
            raise RuntimeError("approval reason is too short")
        try:
            claims = self.approval_verifier.verify_human_approval_assertion(approval_assertion)
        except PassportError as exc:
            raise RuntimeError(f"external approval rejected: {exc.reason}") from exc
        challenge = self.approval_challenge(workflow_id)
        challenge_body = {key: value for key, value in challenge.items() if key != "challenge_digest"}
        if (
            claims.subject != approver
            or claims.requester != summary.requester
            or claims.workflow_id != summary.workflow_id
            or claims.project_id != summary.project_id
            or claims.tenant_id != summary.tenant_id
            or claims.expected_revision != expected_revision
            or claims.context_digest != summary.context_digest
            or claims.policy_digest != summary.policy_digest
            or claims.plan_digest != summary.plan_digest
            or claims.challenge_digest != sha256_digest(challenge_body)
            or claims.reason_digest != sha256_digest(reason.strip())
            or claims.scope != challenge["scope"]
            or [item.model_dump(mode="json") for item in claims.actions] != challenge["actions"]
            or claims.amount_minor != summary.risk_units
            or claims.currency != "XTS"
        ):
            raise RuntimeError("external approval does not match frozen operations challenge")
        approval = {
            "schema_version": "proofmesh.human-approval/v2",
            "approver": approver,
            "reason": reason.strip(),
            "approved_at": utc_now(),
            "scope": claims.scope,
            "assertion": approval_assertion,
            "assertion_digest": sha256_digest(approval_assertion),
            "assertion_issuer": claims.issuer,
            "origin_assurance": "EXTERNAL_SIGNED_ASSERTION",
        }
        state["approval"] = approval
        summary.approval_digest = sha256_digest({"assertion": approval_assertion, "reason_digest": claims.reason_digest})
        summary.status = ChangeStatus.AUTHORIZED
        summary.final_message = "外部签名审批已绑定冻结计划；尚未执行。"
        self._event(summary, approver, "operations.approval.recorded", {"approval_digest": summary.approval_digest})
        return self._store(summary, state, expected_revision=expected_revision)

    def execute_and_verify(self, workflow_id: str, *, expected_revision: int, actor: str = "ops-action-executor") -> ChangeSummary:
        summary, state = self._load(workflow_id)
        if summary.status != ChangeStatus.AUTHORIZED or summary.revision != expected_revision or not summary.approval_digest:
            raise RuntimeError("operations workflow is not authorized")
        plan = state["plan"]
        apply_arguments = {
            "change_id": plan["change_id"],
            "service_id": plan["service_id"],
            "patch": plan["patch"],
            "expected_version": plan["expected_version"],
            "workflow_id": summary.workflow_id,
            "risk_units": plan["risk_units"],
        }
        if state["change"].get("force_unknown_apply") and self.reference_fault_injection:
            self._expire_pending_operation = True
        try:
            applied = self._call(
                summary,
                state,
                actor=actor,
                role="executor",
                tool="ops.apply_config",
                arguments=apply_arguments,
                resource=plan["service_id"],
                scopes=["config:write"],
                mode="execute",
                approval_digest=summary.approval_digest,
                approval_assertion=(state.get("approval") or {}).get("assertion"),
                max_amount_minor=plan["risk_units"],
            )["result"]
            state["apply_result"] = applied
        except ConnectionError:
            # This immediate second attempt is exercised only by the reference
            # fixture with an expired lease.  It enters the normal Gateway
            # reconcile-before-retry path; no adapter result is fabricated.
            if not self.reference_fault_injection:
                summary.status = ChangeStatus.UNKNOWN_MANUAL
                state["execution_outcome"] = "UNKNOWN_MANUAL"
                state["unknown_reason"] = "transport_state_unknown_reference_hook_disabled"
                summary.final_message = "上游提交状态不可判定；需要真实上游查询或人工对账。"
                self._event(summary, actor, "operations.execution.unknown", {"reason": state["unknown_reason"]})
                return self._store(summary, state, expected_revision=expected_revision)
            if getattr(self, "_expire_pending_operation", False):
                with self.gateway.store._connect() as conn:  # noqa: SLF001 - isolated reference fault hook
                    conn.execute(
                        "UPDATE gateway_operations SET lease_until = 0 WHERE tenant_id = ? AND workflow_id = ? AND tool = ?",
                        (summary.tenant_id, summary.workflow_id, "ops.apply_config"),
                    )
                self._expire_pending_operation = False
            try:
                self._call(
                    summary,
                    state,
                    actor=actor,
                    role="executor",
                    tool="ops.apply_config",
                    arguments=apply_arguments,
                    resource=plan["service_id"],
                    scopes=["config:write"],
                    mode="execute",
                    approval_digest=summary.approval_digest,
                    approval_assertion=(state.get("approval") or {}).get("assertion"),
                    max_amount_minor=plan["risk_units"],
                )
            except GatewayDenied as exc:
                if exc.reason == "operation_unknown_manual_reconciliation_required":
                    summary.status = ChangeStatus.UNKNOWN_MANUAL
                    state["execution_outcome"] = "UNKNOWN_MANUAL"
                    state["unknown_reason"] = exc.reason
                    summary.final_message = "上游提交状态不可判定；Gateway 已冻结逻辑操作并停止自动重试。"
                    self._event(summary, actor, "operations.execution.unknown", {"reason": exc.reason})
                    summary.gateway_receipt_count = len(state["receipts"])
                    return self._store(summary, state, expected_revision=expected_revision)
                raise
            raise RuntimeError("uncertain operation unexpectedly became redispatchable")
        except GatewayDenied as exc:
            if exc.reason == "operation_unknown_manual_reconciliation_required":
                summary.status = ChangeStatus.UNKNOWN_MANUAL
                state["execution_outcome"] = "UNKNOWN_MANUAL"
                state["unknown_reason"] = exc.reason
                summary.final_message = "上游提交状态不可判定；Gateway 已冻结逻辑操作并停止自动重试。"
                self._event(summary, actor, "operations.execution.unknown", {"reason": exc.reason})
                summary.gateway_receipt_count = len(state["receipts"])
                return self._store(summary, state, expected_revision=expected_revision)
            raise
        try:
            health = self._call(
                summary,
                state,
                actor="ops-outcome-verifier",
                role="verifier",
                tool="ops.run_health_check",
                arguments={"change_id": plan["change_id"], "service_id": plan["service_id"], "workflow_id": summary.workflow_id},
                resource=plan["service_id"],
                scopes=["health:read"],
                mode="read",
                approval_digest="AUTOMATIC",
            )["result"]
            state["health_result"] = health
            state["execution_outcome"] = "SUCCEEDED"
            summary.status = ChangeStatus.EXECUTED
        except ToolExecutionError as exc:
            failed_receipt = exc.gateway_receipt
            if isinstance(failed_receipt, dict) and not any(item.get("call_id") == failed_receipt.get("call_id") for item in state["receipts"]):
                state["receipts"].append(failed_receipt)
            emergency = "EMERGENCY:" + sha256_digest({"workflow_id": summary.workflow_id, "change_id": plan["change_id"], "failed_gate": exc.code})
            restored = self._call(
                summary,
                state,
                actor=actor,
                role="executor",
                tool="ops.restore_config",
                arguments={"change_id": plan["change_id"], "service_id": plan["service_id"], "workflow_id": summary.workflow_id, "reason": f"health gate failed: {exc.code}"},
                resource=plan["service_id"],
                scopes=["config:restore"],
                mode="compensate",
                approval_digest=emergency,
            )["result"]
            state["restore_result"] = restored
            state["execution_outcome"] = "COMPENSATED"
            summary.status = ChangeStatus.EXECUTED
        snapshot = self.sandbox.workflow_snapshot(summary.workflow_id, tenant_id=summary.tenant_id, change_id=summary.change_id)
        if state["execution_outcome"] == "SUCCEEDED":
            expected = {**state["service_before"]["config"], **plan["patch"]}
            verified = (
                snapshot["execution"]["status"] == "APPLIED"
                and snapshot["service"]["config"] == expected
                and snapshot["service"]["version"] == plan["expected_version"] + 1
            )
            summary.status = ChangeStatus.COMPLETED if verified else ChangeStatus.BLOCKED
        else:
            verified = (
                snapshot["execution"]["status"] == "RESTORED"
                and snapshot["service"]["config"] == state["service_before"]["config"]
                and snapshot["service"]["version"] == plan["expected_version"] + 2
            )
            summary.status = ChangeStatus.COMPENSATED if verified else ChangeStatus.BLOCKED
        state["verification"] = {"passed": verified, "outcome": state["execution_outcome"], "verified_at": utc_now()}
        summary.gateway_receipt_count = len(state["receipts"])
        summary.final_message = "配置与健康终态已独立回读并封存。" if verified else "业务终态与冻结计划不一致，已阻断。"
        self._event(summary, "ops-outcome-verifier", "operations.postconditions.verified", {"passed": verified, "outcome": state["execution_outcome"]})
        if summary.status in {ChangeStatus.COMPLETED, ChangeStatus.COMPENSATED}:
            summary.proof_bundle = f"artifacts/operations-workflows/{summary.workflow_id}/workflow-proof.json"
        terminal = self._store(summary, state, expected_revision=expected_revision)
        if terminal.status in {ChangeStatus.COMPLETED, ChangeStatus.COMPENSATED}:
            terminal.proof_bundle = self._write_proof(terminal, state)
        return terminal

    def _write_proof(self, summary: ChangeSummary, state: dict[str, Any]) -> str:
        target = self.artifact_dir / summary.workflow_id
        target.mkdir(parents=True, exist_ok=True)
        snapshot, attestation = self.sandbox.attest_snapshot(summary.workflow_id, tenant_id=summary.tenant_id, change_id=summary.change_id)
        chain_valid, chain_head = self.ledger.verify(summary.workflow_id)
        body = {
            "schema_version": "proofmesh.operations-workflow-proof/v1",
            "issuer": self.proof_signer.issuer,
            "claim_boundary": "deterministic local operations reference adapter; not a production cluster",
            "summary": summary.model_dump(mode="json"),
            "policy": state["policy"],
            "policy_digest": summary.policy_digest,
            "plan": state["plan"],
            "approval": state.get("approval"),
            "gateway_receipts": self.gateway.store.receipts(summary.workflow_id, tenant_id=summary.tenant_id),
            "tool_executions": state["tool_executions"],
            "workflow_events": self.ledger.events(summary.workflow_id),
            "event_chain": {"valid_at_seal": chain_valid, "head": chain_head},
            "verified_business_snapshot": snapshot,
            "business_snapshot_attestation": attestation,
            "verification": state["verification"],
        }
        body["bundle_signature"] = self.proof_signer.sign_payload(body, token_type=PROOF_TYPE)
        path = target / "workflow-proof.json"
        temp = target / ".workflow-proof.json.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return str(path.relative_to(self.home))

    def get_state(self, workflow_id: str) -> dict[str, Any]:
        summary, state = self._load(workflow_id)
        return {"summary": summary.model_dump(mode="json"), **state}
