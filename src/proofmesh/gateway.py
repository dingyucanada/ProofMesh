from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .business import ReconciliationResult, ToolExecutionError
from .capabilities import (
    RECEIPT_TYPE,
    ActionPassportClaims,
    JwsSigner,
    ExternalTrustVerifier,
    HumanApprovalAssertionClaims,
    PassportError,
    canonical_json,
    sha256_digest,
)
from .timeutil import utc_now


class GatewayDenied(PermissionError):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        super().__init__(detail or reason)


class ToolBackend(Protocol):
    specs: dict[str, Any]

    def list_tools(self) -> list[dict[str, Any]]: ...

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult: ...


@dataclass(frozen=True)
class Reservation:
    operation_id: str = ""
    owner_id: str = ""
    generation: int = 0
    cached_response: dict[str, Any] | None = None
    cached_receipt: dict[str, Any] | None = None
    needs_reconciliation: bool = False


class GatewayStore:
    """Durable logical operations with fencing, reconciliation, replay and total budgets."""

    LEASE_SECONDS = 15

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS capability_usage_v2 (
                    issuer TEXT NOT NULL,
                    jti TEXT NOT NULL,
                    call_count INTEGER NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(issuer, jti)
                );
                CREATE TABLE IF NOT EXISTS workflow_budget_usage_v2 (
                    tenant_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    PRIMARY KEY(tenant_id, workflow_id, currency)
                );
                CREATE TABLE IF NOT EXISTS gateway_operations (
                    operation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    reserved_amount_minor INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    lease_until INTEGER NOT NULL,
                    last_issuer TEXT NOT NULL,
                    last_jti TEXT NOT NULL,
                    response_json TEXT,
                    receipt_json TEXT,
                    unknown_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, workflow_id, tool, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_operations_status
                    ON gateway_operations(status, lease_until);
                CREATE TABLE IF NOT EXISTS gateway_receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    workflow_id TEXT NOT NULL,
                    jti TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT,
                    receipt_json TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL UNIQUE,
                    previous_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_denials (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT,
                    tool TEXT,
                    reason TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_workflow
                    ON gateway_receipts(tenant_id, workflow_id, seq);
                CREATE INDEX IF NOT EXISTS idx_gateway_denials ON gateway_denials(reason, created_at);
                """
            )
            receipt_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(gateway_receipts)").fetchall()
            }
            if "operation_id" not in receipt_columns:
                conn.execute("ALTER TABLE gateway_receipts ADD COLUMN operation_id TEXT NOT NULL DEFAULT ''")
            if "tenant_id" not in receipt_columns:
                conn.execute("ALTER TABLE gateway_receipts ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _contract_digest(
        claims: ActionPassportClaims,
        *,
        request_digest: str,
        amount_minor: int,
    ) -> str:
        authorization = claims.model_dump(
            mode="json",
            exclude={"jti", "issued_at", "not_before", "expires_at"},
        )
        return sha256_digest(
            {"request_digest": request_digest, "authorization": authorization, "amount_minor": amount_minor}
        )

    def reserve(
        self,
        *,
        claims: ActionPassportClaims,
        idempotency_key: str,
        request_digest: str,
        amount_minor: int,
    ) -> Reservation:
        contract_digest = self._contract_digest(
            claims,
            request_digest=request_digest,
            amount_minor=amount_minor,
        )
        owner_id = f"owner-{uuid.uuid4().hex}"
        now_epoch = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                """SELECT * FROM gateway_operations
                   WHERE tenant_id = ? AND workflow_id = ? AND tool = ? AND idempotency_key = ?""",
                (claims.tenant_id, claims.workflow_id, claims.tool, idempotency_key),
            ).fetchone()
            if operation is not None:
                if operation["contract_digest"] != contract_digest:
                    raise GatewayDenied("idempotency_conflict")
                if operation["status"] == "SUCCEEDED":
                    conn.commit()
                    return Reservation(
                        operation_id=operation["operation_id"],
                        cached_response=json.loads(operation["response_json"]),
                        cached_receipt=json.loads(operation["receipt_json"]),
                    )
                if operation["status"] == "UNKNOWN":
                    raise GatewayDenied("operation_unknown_manual_reconciliation_required")
                if operation["status"] == "FAILED":
                    raise GatewayDenied("previous_attempt_not_retriable")
                if int(operation["lease_until"]) >= now_epoch:
                    raise GatewayDenied("attempt_in_progress")
                generation = int(operation["generation"]) + 1
                updated = conn.execute(
                    """UPDATE gateway_operations SET
                         status = 'RECONCILING', owner_id = ?, generation = ?, lease_until = ?,
                         last_issuer = ?, last_jti = ?, updated_at = ?
                       WHERE operation_id = ? AND generation = ? AND lease_until < ?
                         AND status IN ('DISPATCHING', 'RECONCILING')""",
                    (
                        owner_id,
                        generation,
                        now_epoch + self.LEASE_SECONDS,
                        claims.issuer,
                        claims.jti,
                        utc_now(),
                        operation["operation_id"],
                        operation["generation"],
                        now_epoch,
                    ),
                )
                if updated.rowcount != 1:
                    raise GatewayDenied("attempt_in_progress")
                conn.commit()
                return Reservation(
                    operation_id=operation["operation_id"],
                    owner_id=owner_id,
                    generation=generation,
                    needs_reconciliation=True,
                )

            usage = conn.execute(
                "SELECT * FROM capability_usage_v2 WHERE issuer = ? AND jti = ?",
                (claims.issuer, claims.jti),
            ).fetchone()
            call_count = int(usage["call_count"]) if usage is not None else 0
            if call_count >= claims.max_calls:
                raise GatewayDenied("passport_exhausted")
            if amount_minor > claims.max_amount_minor:
                raise GatewayDenied("amount_exceeds_passport")

            budget = conn.execute(
                """SELECT amount_minor FROM workflow_budget_usage_v2
                   WHERE tenant_id = ? AND workflow_id = ? AND currency = ?""",
                (claims.tenant_id, claims.workflow_id, claims.currency),
            ).fetchone()
            budget_used = int(budget["amount_minor"]) if budget is not None else 0
            if budget_used + amount_minor > claims.budget_limit_minor:
                raise GatewayDenied("workflow_budget_exceeded")

            operation_id = f"op-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO gateway_operations(
                       operation_id, tenant_id, workflow_id, tool, idempotency_key,
                       request_digest, contract_digest, reserved_amount_minor, status,
                       owner_id, generation, lease_until, last_issuer, last_jti, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'DISPATCHING', ?, 1, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    claims.tenant_id,
                    claims.workflow_id,
                    claims.tool,
                    idempotency_key,
                    request_digest,
                    contract_digest,
                    amount_minor,
                    owner_id,
                    now_epoch + self.LEASE_SECONDS,
                    claims.issuer,
                    claims.jti,
                    utc_now(),
                    utc_now(),
                ),
            )
            conn.execute(
                """INSERT INTO capability_usage_v2(issuer, jti, call_count, amount_minor, expires_at, updated_at)
                   VALUES(?, ?, 1, ?, ?, ?)
                   ON CONFLICT(issuer, jti) DO UPDATE SET
                     call_count = capability_usage_v2.call_count + 1,
                     amount_minor = capability_usage_v2.amount_minor + excluded.amount_minor,
                     updated_at = excluded.updated_at""",
                (claims.issuer, claims.jti, amount_minor, claims.expires_at, utc_now()),
            )
            conn.execute(
                """INSERT INTO workflow_budget_usage_v2(tenant_id, workflow_id, currency, amount_minor)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(tenant_id, workflow_id, currency) DO UPDATE SET
                     amount_minor = workflow_budget_usage_v2.amount_minor + excluded.amount_minor""",
                (claims.tenant_id, claims.workflow_id, claims.currency, amount_minor),
            )
            conn.commit()
            return Reservation(operation_id=operation_id, owner_id=owner_id, generation=1)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_lease(self, reservation: Reservation) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE gateway_operations SET lease_until = ?, updated_at = ?
                   WHERE operation_id = ? AND owner_id = ? AND generation = ?
                     AND status IN ('DISPATCHING', 'RECONCILING')""",
                (
                    int(time.time()) + self.LEASE_SECONDS,
                    utc_now(),
                    reservation.operation_id,
                    reservation.owner_id,
                    reservation.generation,
                ),
            )
        return result.rowcount == 1

    def begin_redispatch(self, reservation: Reservation) -> None:
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE gateway_operations SET status = 'DISPATCHING', updated_at = ?
                   WHERE operation_id = ? AND owner_id = ? AND generation = ? AND status = 'RECONCILING'""",
                (utc_now(), reservation.operation_id, reservation.owner_id, reservation.generation),
            )
        if result.rowcount != 1:
            raise GatewayDenied("operation_fence_lost")

    def mark_unknown(self, reservation: Reservation, *, reason: str) -> None:
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE gateway_operations SET status = 'UNKNOWN', unknown_reason = ?, lease_until = 0,
                     updated_at = ?
                   WHERE operation_id = ? AND owner_id = ? AND generation = ? AND status = 'RECONCILING'""",
                (reason, utc_now(), reservation.operation_id, reservation.owner_id, reservation.generation),
            )
        if result.rowcount != 1:
            raise GatewayDenied("operation_fence_lost")

    def finalize(
        self,
        *,
        call_id: str,
        claims: ActionPassportClaims,
        tool: str,
        decision: str,
        reason: str | None,
        receipt: dict[str, Any],
        response: dict[str, Any] | None,
        request_digest: str,
        reservation: Reservation,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT * FROM gateway_operations WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone()
            if (
                operation is None
                or operation["owner_id"] != reservation.owner_id
                or int(operation["generation"]) != reservation.generation
                or operation["status"] not in {"DISPATCHING", "RECONCILING"}
                or operation["request_digest"] != request_digest
            ):
                raise GatewayDenied("operation_fence_lost")
            previous = conn.execute(
                """SELECT receipt_hash FROM gateway_receipts
                   WHERE tenant_id = ? AND workflow_id = ? ORDER BY seq DESC LIMIT 1""",
                (claims.tenant_id, claims.workflow_id),
            ).fetchone()
            previous_hash = previous["receipt_hash"] if previous else "GENESIS"
            receipt_hash = sha256_digest({"previous_hash": previous_hash, "receipt": receipt})
            stored_receipt = {
                **receipt,
                "_chain": {"previous_hash": previous_hash, "entry_hash": receipt_hash},
            }
            conn.execute(
                """INSERT INTO gateway_receipts(
                       call_id, operation_id, tenant_id, workflow_id, jti, tool, decision, reason,
                       receipt_json, receipt_hash, previous_hash, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    reservation.operation_id,
                    claims.tenant_id,
                    claims.workflow_id,
                    claims.jti,
                    tool,
                    decision,
                    reason,
                    canonical_json(stored_receipt),
                    receipt_hash,
                    previous_hash,
                    utc_now(),
                ),
            )
            updated = conn.execute(
                """UPDATE gateway_operations SET
                     status = ?, response_json = ?, receipt_json = ?, lease_until = 0,
                     last_issuer = ?, last_jti = ?, updated_at = ?
                   WHERE operation_id = ? AND owner_id = ? AND generation = ?
                     AND status IN ('DISPATCHING', 'RECONCILING')""",
                (
                    "SUCCEEDED" if response is not None else "FAILED",
                    canonical_json(response) if response is not None else None,
                    canonical_json(stored_receipt),
                    claims.issuer,
                    claims.jti,
                    utc_now(),
                    reservation.operation_id,
                    reservation.owner_id,
                    reservation.generation,
                ),
            )
            if updated.rowcount != 1:
                raise GatewayDenied("operation_fence_lost")
            conn.commit()
            return stored_receipt
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def receipts(self, workflow_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT receipt_json FROM gateway_receipts WHERE workflow_id = ? ORDER BY seq",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT receipt_json FROM gateway_receipts
                       WHERE tenant_id = ? AND workflow_id = ? ORDER BY seq""",
                    (tenant_id, workflow_id),
                ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]

    def record_denial(
        self,
        *,
        reason: str,
        tool: str | None,
        workflow_id: str | None,
        request_material: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO gateway_denials(workflow_id, tool, reason, request_digest, created_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (workflow_id, tool, reason, sha256_digest(request_material), utc_now()),
            )

    def metrics(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM gateway_receipts").fetchone()["n"]
            allowed = conn.execute(
                "SELECT COUNT(*) AS n FROM gateway_receipts WHERE decision = 'allowed'"
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM gateway_receipts WHERE decision = 'upstream_error'"
            ).fetchone()["n"]
            denied = conn.execute("SELECT COUNT(*) AS n FROM gateway_denials").fetchone()["n"]
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM gateway_operations WHERE status IN ('DISPATCHING', 'RECONCILING')"
            ).fetchone()["n"]
            unknown = conn.execute(
                "SELECT COUNT(*) AS n FROM gateway_operations WHERE status = 'UNKNOWN'"
            ).fetchone()["n"]
        return {
            "calls_total": total + denied,
            "calls_allowed": allowed,
            "calls_denied": denied,
            "upstream_errors": failed,
            "operations_pending": pending,
            "operations_unknown": unknown,
        }


class _LeaseHeartbeat:
    def __init__(self, store: GatewayStore, reservation: Reservation):
        self.store = store
        self.reservation = reservation
        self.stop_event = threading.Event()
        self.lost = False
        self.thread = threading.Thread(target=self._run, name=f"proofmesh-{reservation.operation_id}", daemon=True)

    def _run(self) -> None:
        interval = max(1.0, self.store.LEASE_SECONDS / 3)
        while not self.stop_event.wait(interval):
            if not self.store.renew_lease(self.reservation):
                self.lost = True
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)


class ActionGateway:
    """MCP action gateway: exact authorization, fenced dispatch, reconcile-before-retry."""

    def __init__(
        self,
        *,
        verifier: ExternalTrustVerifier,
        receipt_signer: JwsSigner,
        store: GatewayStore,
        upstream: ToolBackend,
        audience: str = "proofmesh-mcp-gateway",
        auto_approve_minor: int | None = None,
        pinned_policy_digest: str | None = None,
        pinned_policy_limits: dict[str, int] | None = None,
    ):
        self.verifier = verifier
        self.receipt_signer = receipt_signer
        self.store = store
        self.upstream = upstream
        self.audience = audience
        self.auto_approve_minor = auto_approve_minor
        self.pinned_policy_digest = pinned_policy_digest
        self.pinned_policy_limits = dict(pinned_policy_limits or {})
        if pinned_policy_digest is not None and auto_approve_minor is not None:
            self.pinned_policy_limits.setdefault(pinned_policy_digest, auto_approve_minor)

    def _receipt(
        self,
        *,
        call_id: str,
        claims: ActionPassportClaims,
        reservation: Reservation,
        request_digest: str,
        result_digest: str,
        decision: str,
        reason: str | None,
        duration_ms: float,
        recovered: bool,
        approval_assertion_digest: str | None,
    ) -> dict[str, Any]:
        body = {
            "schema_version": "proofmesh.execution-receipt/v1",
            "issuer": self.receipt_signer.issuer,
            "call_id": call_id,
            "operation_id": reservation.operation_id,
            "operation_generation": reservation.generation,
            "recovered": recovered,
            "workflow_id": claims.workflow_id,
            "tenant_id": claims.tenant_id,
            "subject": claims.subject,
            "tool": claims.tool,
            "resource": claims.resource,
            "passport_jti": claims.jti,
            "policy_digest": claims.policy_digest,
            "approval_digest": claims.approval_digest,
            "approval_assertion_digest": approval_assertion_digest,
            "request_digest": request_digest,
            "result_digest": result_digest,
            "decision": decision,
            "reason": reason,
            "duration_ms": round(duration_ms, 3),
            "timestamp": utc_now(),
        }
        body["signature"] = self.receipt_signer.sign_payload(body, token_type=RECEIPT_TYPE)
        return body

    def call_tool(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        passport: str,
        workflow_id: str,
        context_digest: str,
        idempotency_key: str,
        approval_assertion: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._call_tool(
                tool=tool,
                arguments=arguments,
                passport=passport,
                workflow_id=workflow_id,
                context_digest=context_digest,
                idempotency_key=idempotency_key,
                approval_assertion=approval_assertion,
            )
        except GatewayDenied as exc:
            self.store.record_denial(
                reason=exc.reason,
                tool=tool,
                workflow_id=workflow_id,
                request_material={
                    "tool": tool,
                    "arguments": arguments,
                    "workflow_id": workflow_id,
                    "context_digest": context_digest,
                    "idempotency_key": idempotency_key,
                },
            )
            raise

    def _call_tool(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        passport: str,
        workflow_id: str,
        context_digest: str,
        idempotency_key: str,
        approval_assertion: str | None,
    ) -> dict[str, Any]:
        if not 8 <= len(idempotency_key) <= 256:
            raise GatewayDenied("invalid_idempotency_key")
        if tool not in self.upstream.specs:
            raise GatewayDenied("tool_not_registered")
        spec = self.upstream.specs[tool]
        try:
            claims = self.verifier.verify_passport(
                passport,
                audience=self.audience,
                tool=tool,
                workflow_id=workflow_id,
                arguments=arguments,
                context_digest=context_digest,
            )
        except PassportError as exc:
            raise GatewayDenied(exc.reason) from exc
        if claims.mode != spec.mode:
            raise GatewayDenied("mode_mismatch")
        amount_minor = int(arguments.get(spec.amount_argument, 0)) if spec.amount_argument else 0
        assertion_required = claims.mode == "execute" and claims.approval_digest != "AUTOMATIC"
        if claims.mode == "execute" and self.pinned_policy_limits:
            if claims.policy_digest not in self.pinned_policy_limits:
                raise GatewayDenied("gateway_pinned_policy_mismatch")
            automatic_limit = self.pinned_policy_limits[claims.policy_digest]
            if amount_minor > automatic_limit and claims.approval_digest == "AUTOMATIC":
                raise GatewayDenied("approval_assertion_required_by_gateway_policy")
        approval_claims: HumanApprovalAssertionClaims | None = None
        approval_assertion_digest: str | None = None
        if assertion_required and claims.approval_digest != "AUTOMATIC":
            if not approval_assertion:
                raise GatewayDenied("approval_assertion_missing")
            try:
                approval_claims = self.verifier.verify_human_approval_assertion(approval_assertion)
            except PassportError as exc:
                raise GatewayDenied(exc.reason) from exc
            approval_assertion_digest = sha256_digest(approval_assertion)
            action = next((item for item in approval_claims.actions if item.tool == tool), None)
            if (
                sha256_digest({"assertion": approval_assertion, "reason_digest": approval_claims.reason_digest})
                != claims.approval_digest
                or approval_claims.workflow_id != claims.workflow_id
                or approval_claims.tenant_id != claims.tenant_id
                or approval_claims.context_digest != claims.context_digest
                or approval_claims.policy_digest != claims.policy_digest
                or approval_claims.plan_digest != claims.plan_digest
                or action is None
                or action.resource != claims.resource
                or action.args_digest != claims.args_digest
                or action.amount_minor != amount_minor
                or action.currency != claims.currency
            ):
                raise GatewayDenied("approval_assertion_contract_mismatch")
        if not set(spec.required_scopes).issubset(claims.scopes):
            raise GatewayDenied("scope_missing")
        resource = str(arguments.get(spec.resource_argument, ""))
        if not resource or resource != claims.resource:
            raise GatewayDenied("resource_mismatch")
        if "currency" in arguments and arguments["currency"] != claims.currency:
            raise GatewayDenied("currency_mismatch")
        request_material = {
            "workflow_id": workflow_id,
            "tool": tool,
            "arguments": arguments,
            "context_digest": context_digest,
        }
        request_digest = sha256_digest(request_material)
        reservation = self.store.reserve(
            claims=claims,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            amount_minor=amount_minor,
        )
        if reservation.cached_response is not None:
            return {
                "result": reservation.cached_response,
                "receipt": reservation.cached_receipt,
                "idempotent_replay": True,
            }

        if reservation.needs_reconciliation:
            try:
                reconciliation = self.upstream.reconcile(
                    tool,
                    arguments,
                    tenant_id=claims.tenant_id,
                    operation_id=reservation.operation_id,
                )
            except Exception as exc:
                reconciliation = ReconciliationResult("UNKNOWN", reason=f"reconcile_error:{type(exc).__name__}")
            if reconciliation.status == "UNKNOWN":
                reason = reconciliation.reason or "upstream_state_unknown"
                self.store.mark_unknown(reservation, reason=reason)
                raise GatewayDenied("operation_unknown_manual_reconciliation_required")
            if reconciliation.status == "SUCCEEDED" and reconciliation.result is not None:
                call_id = f"call-{uuid.uuid4().hex}"
                receipt = self._receipt(
                    call_id=call_id,
                    claims=claims,
                    reservation=reservation,
                    request_digest=request_digest,
                    result_digest=sha256_digest(reconciliation.result),
                    decision="allowed",
                    reason="recovered_after_reconciliation",
                    duration_ms=0.0,
                    recovered=True,
                    approval_assertion_digest=approval_assertion_digest,
                )
                receipt = self.store.finalize(
                    call_id=call_id,
                    claims=claims,
                    tool=tool,
                    decision="allowed",
                    reason="recovered_after_reconciliation",
                    receipt=receipt,
                    response=reconciliation.result,
                    request_digest=request_digest,
                    reservation=reservation,
                )
                return {"result": reconciliation.result, "receipt": receipt, "idempotent_replay": True}
            if reconciliation.status != "SAFE_TO_RETRY":
                self.store.mark_unknown(reservation, reason="invalid_reconciliation_result")
                raise GatewayDenied("operation_unknown_manual_reconciliation_required")
            self.store.begin_redispatch(reservation)

        call_id = f"call-{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            with _LeaseHeartbeat(self.store, reservation) as heartbeat:
                result = self.upstream.call(
                    tool,
                    {
                        **arguments,
                        "_proofmesh_tenant_id": claims.tenant_id,
                        "_proofmesh_operation_id": reservation.operation_id,
                    },
                )
            if heartbeat.lost:
                raise GatewayDenied("operation_fence_lost")
            duration_ms = (time.perf_counter() - started) * 1000
            receipt = self._receipt(
                call_id=call_id,
                claims=claims,
                reservation=reservation,
                request_digest=request_digest,
                result_digest=sha256_digest(result),
                decision="allowed",
                reason=None,
                duration_ms=duration_ms,
                    recovered=False,
                    approval_assertion_digest=approval_assertion_digest,
            )
            receipt = self.store.finalize(
                call_id=call_id,
                claims=claims,
                tool=tool,
                decision="allowed",
                reason=None,
                receipt=receipt,
                response=result,
                request_digest=request_digest,
                reservation=reservation,
            )
            return {"result": result, "receipt": receipt, "idempotent_replay": False}
        except ToolExecutionError as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            receipt = self._receipt(
                call_id=call_id,
                claims=claims,
                reservation=reservation,
                request_digest=request_digest,
                result_digest=sha256_digest({"code": exc.code}),
                decision="upstream_error",
                reason=exc.code,
                duration_ms=duration_ms,
                recovered=False,
                approval_assertion_digest=approval_assertion_digest,
            )
            receipt = self.store.finalize(
                call_id=call_id,
                claims=claims,
                tool=tool,
                decision="upstream_error",
                reason=exc.code,
                receipt=receipt,
                response=None,
                request_digest=request_digest,
                reservation=reservation,
            )
            exc.gateway_receipt = receipt
            raise

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._rpc_error(request_id, -32600, "Invalid Request", "invalid_jsonrpc")
        method = request["method"]
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ProofMesh Action Gateway", "version": "1.0.0"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.upstream.list_tools()}}
        if method != "tools/call":
            return self._rpc_error(request_id, -32601, "Method not found", "method_not_supported")
        params = request.get("params", {})
        meta = params.get("_meta", {}) if isinstance(params, dict) else {}
        try:
            output = self.call_tool(
                tool=params["name"],
                arguments=params.get("arguments", {}),
                passport=meta["proofmesh/passport"],
                workflow_id=meta["proofmesh/workflowId"],
                context_digest=meta["proofmesh/contextDigest"],
                idempotency_key=meta["proofmesh/idempotencyKey"],
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(output["result"], ensure_ascii=False)}],
                    "structuredContent": output["result"],
                    "isError": False,
                    "_meta": {
                        "proofmesh/receipt": output["receipt"],
                        "proofmesh/idempotentReplay": output["idempotent_replay"],
                    },
                },
            }
        except KeyError:
            self.store.record_denial(
                reason="required_metadata_missing",
                tool=params.get("name") if isinstance(params, dict) else None,
                workflow_id=meta.get("proofmesh/workflowId") if isinstance(meta, dict) else None,
                request_material=request,
            )
            return self._rpc_error(request_id, -32602, "Invalid params", "required_metadata_missing")
        except GatewayDenied as exc:
            return self._rpc_error(request_id, -32003, "Action denied", exc.reason)
        except ToolExecutionError as exc:
            return self._rpc_error(request_id, -32020, "Upstream tool failed", exc.code)
        except (TypeError, ValueError):
            return self._rpc_error(request_id, -32602, "Invalid params", "invalid_arguments")

    @staticmethod
    def _rpc_error(request_id: Any, code: int, message: str, reason: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message, "data": {"proofmesh/reason": reason}},
        }

    def prometheus_metrics(self) -> str:
        values = self.store.metrics()
        return "\n".join(
            [
                "# HELP proofmesh_gateway_calls_total Tool calls observed by the action gateway.",
                "# TYPE proofmesh_gateway_calls_total counter",
                f"proofmesh_gateway_calls_total {values['calls_total']}",
                "# HELP proofmesh_gateway_allowed_total Tool calls allowed and completed.",
                "# TYPE proofmesh_gateway_allowed_total counter",
                f"proofmesh_gateway_allowed_total {values['calls_allowed']}",
                "# HELP proofmesh_gateway_denied_total Tool calls rejected before upstream dispatch.",
                "# TYPE proofmesh_gateway_denied_total counter",
                f"proofmesh_gateway_denied_total {values['calls_denied']}",
                "# HELP proofmesh_gateway_upstream_errors_total Authorized calls that failed upstream.",
                "# TYPE proofmesh_gateway_upstream_errors_total counter",
                f"proofmesh_gateway_upstream_errors_total {values['upstream_errors']}",
                "# HELP proofmesh_gateway_operations_pending Operations with an active or expired lease.",
                "# TYPE proofmesh_gateway_operations_pending gauge",
                f"proofmesh_gateway_operations_pending {values['operations_pending']}",
                "# HELP proofmesh_gateway_operations_unknown Operations stopped for manual reconciliation.",
                "# TYPE proofmesh_gateway_operations_unknown gauge",
                f"proofmesh_gateway_operations_unknown {values['operations_unknown']}",
                "",
            ]
        )
