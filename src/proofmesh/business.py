from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .capabilities import BUSINESS_ATTESTATION_TYPE, JwsSigner, sha256_digest
from .timeutil import utc_now


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.gateway_receipt: dict[str, Any] | None = None
        super().__init__(message)


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    result: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_scopes: tuple[str, ...]
    mode: str
    resource_argument: str
    amount_argument: str | None
    input_schema: dict[str, Any]


class CommerceSandbox:
    """A transactionally consistent target system with observable, reversible side effects."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        attestation_signer: JwsSigner | None = None,
    ):
        self.db_path = Path(db_path)
        self.attestation_signer = attestation_signer
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "crm.get_ticket": self.get_ticket,
            "orders.get_refund_context": self.get_refund_context,
            "risk.score_refund": self.score_refund,
            "payments.issue_refund": self.issue_refund,
            "crm.close_ticket": self.close_ticket,
            "payments.compensate_refund": self.compensate_refund,
            "memory.store_resolution": self.store_resolution,
        }
        self.specs = self._build_specs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS customers (
                    customer_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'acme-cn',
                    display_name TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    email TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'acme-cn',
                    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                    total_minor INTEGER NOT NULL CHECK(total_minor >= 0),
                    refundable_minor INTEGER NOT NULL CHECK(refundable_minor >= 0),
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'acme-cn',
                    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    requested_amount_minor INTEGER NOT NULL CHECK(requested_amount_minor >= 0),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    force_close_failure INTEGER NOT NULL DEFAULT 0,
                    closed_workflow_id TEXT
                );
                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'acme-cn',
                    workflow_id TEXT NOT NULL UNIQUE,
                    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    compensated_at TEXT,
                    compensation_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS resolution_memory (
                    memory_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'acme-cn',
                    workflow_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    resolution_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            for table in ("customers", "orders", "tickets", "refunds", "resolution_memory"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "tenant_id" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'acme-cn'")
            count = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
            if count == 0:
                self._seed(conn)

    @staticmethod
    def _seed(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO customers(customer_id, tenant_id, display_name, segment, email) VALUES(?, ?, ?, ?, ?)",
            ("CUS-100", "acme-cn", "林晓", "gold", "linxiao@example.invalid"),
        )
        conn.execute(
            """INSERT INTO orders(order_id, tenant_id, customer_id, total_minor, refundable_minor, currency, status)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            ("ORD-1001", "acme-cn", "CUS-100", 29900, 29900, "CNY", "DELIVERED"),
        )
        conn.executemany(
            """INSERT INTO tickets(
                   ticket_id, tenant_id, customer_id, order_id, requested_amount_minor, reason, status, force_close_failure
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("TKT-LOW-001", "acme-cn", "CUS-100", "ORD-1001", 8000, "包装破损", "OPEN", 0),
                ("TKT-HIGH-001", "acme-cn", "CUS-100", "ORD-1001", 12900, "核心部件损坏", "OPEN", 0),
                ("TKT-SAGA-001", "acme-cn", "CUS-100", "ORD-1001", 9000, "配件缺失", "OPEN", 1),
            ],
        )

    @staticmethod
    def _tenant(arguments: dict[str, Any]) -> str:
        tenant_id = arguments.get("_proofmesh_tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ToolExecutionError("tenant_context_missing", "Verified gateway tenant context is required")
        return tenant_id

    @staticmethod
    def _build_specs() -> dict[str, ToolSpec]:
        object_schema = {"type": "object", "additionalProperties": False}
        return {
            "crm.get_ticket": ToolSpec(
                name="crm.get_ticket",
                description="Read a sanitized customer-service ticket.",
                required_scopes=("ticket:read",),
                mode="read",
                resource_argument="ticket_id",
                amount_argument=None,
                input_schema={**object_schema, "required": ["ticket_id"], "properties": {"ticket_id": {"type": "string"}}},
            ),
            "orders.get_refund_context": ToolSpec(
                name="orders.get_refund_context",
                description="Read the current refundable balance and optimistic-lock version.",
                required_scopes=("order:read",),
                mode="read",
                resource_argument="order_id",
                amount_argument=None,
                input_schema={**object_schema, "required": ["order_id"], "properties": {"order_id": {"type": "string"}}},
            ),
            "risk.score_refund": ToolSpec(
                name="risk.score_refund",
                description="Compute a deterministic refund-risk score from current business state.",
                required_scopes=("risk:score",),
                mode="read",
                resource_argument="ticket_id",
                amount_argument=None,
                input_schema={
                    **object_schema,
                    "required": ["ticket_id", "amount_minor"],
                    "properties": {"ticket_id": {"type": "string"}, "amount_minor": {"type": "integer", "minimum": 1}},
                },
            ),
            "payments.issue_refund": ToolSpec(
                name="payments.issue_refund",
                description="Create a refund and atomically reduce the order refundable balance.",
                required_scopes=("refund:write",),
                mode="execute",
                resource_argument="order_id",
                amount_argument="amount_minor",
                input_schema={
                    **object_schema,
                    "required": [
                        "ticket_id",
                        "order_id",
                        "amount_minor",
                        "currency",
                        "expected_order_version",
                        "workflow_id",
                    ],
                    "properties": {
                        "ticket_id": {"type": "string"},
                        "order_id": {"type": "string"},
                        "amount_minor": {"type": "integer", "minimum": 1},
                        "currency": {"type": "string"},
                        "expected_order_version": {"type": "integer", "minimum": 0},
                        "workflow_id": {"type": "string"},
                    },
                },
            ),
            "crm.close_ticket": ToolSpec(
                name="crm.close_ticket",
                description="Close the ticket only after a verified business action.",
                required_scopes=("ticket:write",),
                mode="execute",
                resource_argument="ticket_id",
                amount_argument=None,
                input_schema={
                    **object_schema,
                    "required": ["ticket_id", "workflow_id", "resolution"],
                    "properties": {
                        "ticket_id": {"type": "string"},
                        "workflow_id": {"type": "string"},
                        "resolution": {"type": "string"},
                    },
                },
            ),
            "payments.compensate_refund": ToolSpec(
                name="payments.compensate_refund",
                description="Compensate a partially completed refund saga and restore the balance.",
                required_scopes=("refund:compensate",),
                mode="compensate",
                resource_argument="refund_id",
                amount_argument=None,
                input_schema={
                    **object_schema,
                    "required": ["refund_id", "workflow_id", "reason"],
                    "properties": {
                        "refund_id": {"type": "string"},
                        "workflow_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            ),
            "memory.store_resolution": ToolSpec(
                name="memory.store_resolution",
                description="Persist a sanitized resolution pattern for future retrieval.",
                required_scopes=("memory:write",),
                mode="execute",
                resource_argument="workflow_id",
                amount_argument=None,
                input_schema={
                    **object_schema,
                    "required": ["workflow_id", "fingerprint", "resolution"],
                    "properties": {
                        "workflow_id": {"type": "string"},
                        "fingerprint": {"type": "string"},
                        "resolution": {"type": "object"},
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
            raise ToolExecutionError("tool_not_found", f"Unknown tool: {tool}")
        return handler(arguments)

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        """Resolve an expired gateway lease before any retry.

        The sandbox's write tools commit their idempotency record and business mutation in one
        SQLite transaction, so absence is a safe retry signal. A real adapter must provide an
        equivalent upstream idempotency/read-after-write contract or return UNKNOWN.
        """

        del operation_id
        if tool in {"crm.get_ticket", "orders.get_refund_context", "risk.score_refund"}:
            return ReconciliationResult("SAFE_TO_RETRY")
        with self._connect() as conn:
            if tool == "payments.issue_refund":
                row = conn.execute(
                    "SELECT * FROM refunds WHERE workflow_id = ? AND tenant_id = ?",
                    (arguments["workflow_id"], tenant_id),
                ).fetchone()
                if row is None:
                    return ReconciliationResult("SAFE_TO_RETRY")
                if any(
                    row[field] != arguments[field]
                    for field in ("ticket_id", "order_id", "amount_minor", "currency")
                ):
                    return ReconciliationResult("UNKNOWN", reason="upstream_idempotency_conflict")
                return ReconciliationResult(
                    "SUCCEEDED",
                    {
                        "refund_id": row["refund_id"],
                        "workflow_id": row["workflow_id"],
                        "amount_minor": row["amount_minor"],
                        "currency": row["currency"],
                        "status": row["status"],
                        "idempotent_replay": True,
                    },
                )
            if tool == "crm.close_ticket":
                ticket = conn.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                    (arguments["ticket_id"], tenant_id),
                ).fetchone()
                if ticket is None:
                    return ReconciliationResult("UNKNOWN", reason="ticket_missing_during_reconcile")
                if ticket["status"] == "OPEN":
                    return ReconciliationResult("SAFE_TO_RETRY")
                if ticket["status"] == "CLOSED" and ticket["closed_workflow_id"] == arguments["workflow_id"]:
                    refund = conn.execute(
                        """SELECT refund_id FROM refunds
                           WHERE workflow_id = ? AND tenant_id = ? AND ticket_id = ?""",
                        (arguments["workflow_id"], tenant_id, arguments["ticket_id"]),
                    ).fetchone()
                    return ReconciliationResult(
                        "SUCCEEDED",
                        {
                            "ticket_id": ticket["ticket_id"],
                            "status": "CLOSED",
                            "refund_id": refund["refund_id"] if refund else None,
                            "idempotent_replay": True,
                        },
                    )
                return ReconciliationResult("UNKNOWN", reason="ticket_closed_by_other_workflow")
            if tool == "payments.compensate_refund":
                refund = conn.execute(
                    "SELECT * FROM refunds WHERE refund_id = ? AND tenant_id = ?",
                    (arguments["refund_id"], tenant_id),
                ).fetchone()
                if refund is None or refund["workflow_id"] != arguments["workflow_id"]:
                    return ReconciliationResult("UNKNOWN", reason="refund_missing_during_reconcile")
                if refund["status"] == "ISSUED":
                    return ReconciliationResult("SAFE_TO_RETRY")
                if refund["status"] == "COMPENSATED":
                    return ReconciliationResult(
                        "SUCCEEDED",
                        {
                            "refund_id": refund["refund_id"],
                            "status": "COMPENSATED",
                            "balance_restored": True,
                            "idempotent_replay": True,
                        },
                    )
                return ReconciliationResult("UNKNOWN", reason="refund_state_unknown")
            if tool == "memory.store_resolution":
                memory = conn.execute(
                    "SELECT * FROM resolution_memory WHERE workflow_id = ? AND tenant_id = ?",
                    (arguments["workflow_id"], tenant_id),
                ).fetchone()
                if memory is None:
                    return ReconciliationResult("SAFE_TO_RETRY")
                if (
                    memory["fingerprint"] == arguments["fingerprint"]
                    and json.loads(memory["resolution_json"]) == arguments["resolution"]
                ):
                    return ReconciliationResult(
                        "SUCCEEDED",
                        {"workflow_id": arguments["workflow_id"], "stored": True, "idempotent_replay": True},
                    )
                return ReconciliationResult("UNKNOWN", reason="memory_idempotency_conflict")
        return ReconciliationResult("UNKNOWN", reason="tool_has_no_reconciliation_contract")

    def get_ticket(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT ticket_id, customer_id, order_id, requested_amount_minor, reason, status
                   FROM tickets WHERE ticket_id = ? AND tenant_id = ?""",
                (arguments["ticket_id"], tenant_id),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("ticket_not_found", "Ticket does not exist")
        return dict(row)

    def get_refund_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT order_id, customer_id, total_minor, refundable_minor, currency, status, version
                   FROM orders WHERE order_id = ? AND tenant_id = ?""",
                (arguments["order_id"], tenant_id),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("order_not_found", "Order does not exist")
        return dict(row)

    def score_refund(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ticket = self.get_ticket(
            {"ticket_id": arguments["ticket_id"], "_proofmesh_tenant_id": self._tenant(arguments)}
        )
        amount = int(arguments["amount_minor"])
        score = 0.08
        reasons: list[str] = []
        if amount > 10000:
            score += 0.09
            reasons.append("amount_requires_human_review")
        if "损坏" in ticket["reason"]:
            score += 0.04
            reasons.append("physical_damage_claim")
        return {"score": round(score, 4), "reasons": reasons, "model": "deterministic-risk-v1"}

    def issue_refund(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        amount = int(arguments["amount_minor"])
        if amount <= 0:
            raise ToolExecutionError("invalid_amount", "Refund amount must be positive")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM refunds WHERE workflow_id = ? AND tenant_id = ?",
                (arguments["workflow_id"], tenant_id),
            ).fetchone()
            if existing is not None:
                immutable_request = {
                    "ticket_id": arguments["ticket_id"],
                    "order_id": arguments["order_id"],
                    "amount_minor": amount,
                    "currency": arguments["currency"],
                }
                immutable_existing = {
                    "ticket_id": existing["ticket_id"],
                    "order_id": existing["order_id"],
                    "amount_minor": existing["amount_minor"],
                    "currency": existing["currency"],
                }
                if immutable_request != immutable_existing:
                    raise ToolExecutionError(
                        "workflow_idempotency_conflict",
                        "Workflow already has a refund with different immutable arguments",
                    )
                conn.commit()
                return {**dict(existing), "idempotent_replay": True}
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (arguments["ticket_id"], tenant_id),
            ).fetchone()
            order = conn.execute(
                "SELECT * FROM orders WHERE order_id = ? AND tenant_id = ?",
                (arguments["order_id"], tenant_id),
            ).fetchone()
            if ticket is None or order is None or ticket["order_id"] != order["order_id"]:
                raise ToolExecutionError("business_context_mismatch", "Ticket and order are not aligned")
            if ticket["status"] != "OPEN":
                raise ToolExecutionError("ticket_not_open", "Ticket is not open")
            if order["status"] != "DELIVERED":
                raise ToolExecutionError("order_not_eligible", "Order is not eligible for refund")
            if amount != int(ticket["requested_amount_minor"]):
                raise ToolExecutionError(
                    "amount_not_requested",
                    "Refund amount must equal the reviewed ticket request",
                )
            if order["currency"] != arguments["currency"]:
                raise ToolExecutionError("currency_mismatch", "Currency does not match the order")
            expected_version = int(arguments["expected_order_version"])
            if expected_version != int(order["version"]):
                raise ToolExecutionError(
                    "frozen_plan_stale",
                    "Order changed after the plan was frozen; re-planning and re-approval are required",
                )
            if amount > order["refundable_minor"]:
                raise ToolExecutionError("refund_exceeds_balance", "Refund exceeds the current refundable balance")
            refund_id = f"RFD-{uuid.uuid4().hex[:12].upper()}"
            updated = conn.execute(
                """UPDATE orders
                   SET refundable_minor = refundable_minor - ?, version = version + 1
                   WHERE order_id = ? AND tenant_id = ? AND refundable_minor >= ? AND version = ?""",
                (amount, order["order_id"], tenant_id, amount, expected_version),
            )
            if updated.rowcount != 1:
                raise ToolExecutionError("concurrent_order_update", "Order changed concurrently")
            conn.execute(
                """INSERT INTO refunds(
                       refund_id, tenant_id, workflow_id, ticket_id, order_id, amount_minor, currency, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'ISSUED', ?)""",
                (
                    refund_id,
                    tenant_id,
                    arguments["workflow_id"],
                    ticket["ticket_id"],
                    order["order_id"],
                    amount,
                    order["currency"],
                    utc_now(),
                ),
            )
            conn.commit()
            return {
                "refund_id": refund_id,
                "workflow_id": arguments["workflow_id"],
                "amount_minor": amount,
                "currency": order["currency"],
                "status": "ISSUED",
                "order_version": order["version"] + 1,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close_ticket(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (arguments["ticket_id"], tenant_id),
            ).fetchone()
            if ticket is None:
                raise ToolExecutionError("ticket_not_found", "Ticket does not exist")
            if ticket["force_close_failure"]:
                raise ToolExecutionError("crm_dependency_unavailable", "Injected CRM dependency failure")
            if ticket["status"] == "CLOSED" and ticket["closed_workflow_id"] == arguments["workflow_id"]:
                conn.commit()
                return {"ticket_id": ticket["ticket_id"], "status": "CLOSED", "idempotent_replay": True}
            if ticket["status"] != "OPEN":
                raise ToolExecutionError("ticket_not_open", "Ticket is not open")
            refund = conn.execute(
                """SELECT refund_id FROM refunds
                   WHERE workflow_id = ? AND tenant_id = ? AND ticket_id = ? AND status = 'ISSUED'""",
                (arguments["workflow_id"], tenant_id, ticket["ticket_id"]),
            ).fetchone()
            if refund is None:
                raise ToolExecutionError("refund_not_verified", "An issued refund is required before closing")
            conn.execute(
                """UPDATE tickets SET status = 'CLOSED', closed_workflow_id = ?
                   WHERE ticket_id = ? AND tenant_id = ?""",
                (arguments["workflow_id"], ticket["ticket_id"], tenant_id),
            )
            conn.commit()
            return {"ticket_id": ticket["ticket_id"], "status": "CLOSED", "refund_id": refund["refund_id"]}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def compensate_refund(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            refund = conn.execute(
                "SELECT * FROM refunds WHERE refund_id = ? AND tenant_id = ?",
                (arguments["refund_id"], tenant_id),
            ).fetchone()
            if refund is None or refund["workflow_id"] != arguments["workflow_id"]:
                raise ToolExecutionError("refund_not_found", "Refund does not belong to this workflow")
            if refund["status"] == "COMPENSATED":
                conn.commit()
                return {"refund_id": refund["refund_id"], "status": "COMPENSATED", "idempotent_replay": True}
            if refund["status"] != "ISSUED":
                raise ToolExecutionError("refund_not_compensatable", "Refund cannot be compensated")
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (refund["ticket_id"], tenant_id),
            ).fetchone()
            if ticket is None:
                raise ToolExecutionError("ticket_not_found", "Refund ticket no longer exists")
            if ticket["status"] == "CLOSED" and ticket["closed_workflow_id"] != refund["workflow_id"]:
                raise ToolExecutionError(
                    "ticket_owned_by_other_workflow",
                    "A different workflow closed the ticket; automatic compensation is unsafe",
                )
            conn.execute(
                """UPDATE orders SET refundable_minor = refundable_minor + ?, version = version + 1
                   WHERE order_id = ? AND tenant_id = ?""",
                (refund["amount_minor"], refund["order_id"], tenant_id),
            )
            conn.execute(
                """UPDATE refunds
                   SET status = 'COMPENSATED', compensated_at = ?, compensation_reason = ?
                   WHERE refund_id = ? AND tenant_id = ?""",
                (utc_now(), arguments["reason"], refund["refund_id"], tenant_id),
            )
            if ticket["status"] == "CLOSED":
                conn.execute(
                    """UPDATE tickets SET status = 'OPEN', closed_workflow_id = NULL
                       WHERE ticket_id = ? AND tenant_id = ? AND closed_workflow_id = ?""",
                    (ticket["ticket_id"], tenant_id, refund["workflow_id"]),
                )
            conn.commit()
            return {"refund_id": refund["refund_id"], "status": "COMPENSATED", "balance_restored": True}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def store_resolution(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = self._tenant(arguments)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO resolution_memory(
                       memory_id, tenant_id, workflow_id, fingerprint, resolution_json, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    f"MEM-{uuid.uuid4().hex[:12].upper()}",
                    tenant_id,
                    arguments["workflow_id"],
                    arguments["fingerprint"],
                    json.dumps(arguments["resolution"], ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
        return {"workflow_id": arguments["workflow_id"], "stored": True}

    def attest_workflow_snapshot(
        self,
        workflow_id: str,
        *,
        tenant_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read and sign a fresh upstream snapshot with a business-system key."""

        if self.attestation_signer is None:
            raise RuntimeError("business snapshot attestation signer is unavailable")
        snapshot = self.workflow_snapshot(workflow_id, tenant_id=tenant_id)
        statement = {
            "schema_version": "proofmesh.business-snapshot-attestation/v1",
            "issuer": self.attestation_signer.issuer,
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "snapshot_digest": sha256_digest(snapshot),
            "captured_at": utc_now(),
        }
        attestation = {
            "statement": statement,
            "signature": self.attestation_signer.sign_payload(
                statement,
                token_type=BUSINESS_ATTESTATION_TYPE,
            ),
        }
        return snapshot, attestation

    def workflow_snapshot(self, workflow_id: str, *, tenant_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            refund = conn.execute(
                "SELECT * FROM refunds WHERE workflow_id = ? AND tenant_id = ?", (workflow_id, tenant_id)
            ).fetchone()
            if refund is None:
                return {"workflow_id": workflow_id, "refund": None}
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?", (refund["ticket_id"], tenant_id)
            ).fetchone()
            order = conn.execute(
                "SELECT * FROM orders WHERE order_id = ? AND tenant_id = ?", (refund["order_id"], tenant_id)
            ).fetchone()
        return {
            "workflow_id": workflow_id,
            "refund": dict(refund),
            "ticket": dict(ticket),
            "order": dict(order),
        }
