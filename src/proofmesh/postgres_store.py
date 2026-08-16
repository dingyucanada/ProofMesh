from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .capabilities import ActionPassportClaims, canonical_json, sha256_digest
from .gateway import GatewayDenied, Reservation
from .timeutil import utc_now


class PostgresGatewayStore:
    """PostgreSQL implementation of GatewayStore's fenced operation protocol.

    It preserves the SQLite contract while using row locks, transactional budget
    accounting and generation fencing so multiple replicas cannot dispatch the
    same logical operation.  psycopg is imported lazily, keeping local review
    installs lightweight.
    """

    LEASE_SECONDS = 15

    def __init__(self, dsn: str, *, connect_factory=None):
        if not dsn.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("PostgreSQL DSN must use postgresql://")
        if connect_factory is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("install ProofMesh with the production extra to use PostgreSQL") from exc

            def connect_factory():
                return psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), row_factory=dict_row)

        self.dsn = dsn
        self._connect_factory = connect_factory
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        connection = self._connect_factory()
        try:
            yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS capability_usage_v2 (
                   issuer TEXT NOT NULL, jti TEXT NOT NULL, call_count BIGINT NOT NULL,
                   amount_minor BIGINT NOT NULL, expires_at BIGINT NOT NULL,
                   updated_at TEXT NOT NULL, PRIMARY KEY(issuer, jti))""",
            """CREATE TABLE IF NOT EXISTS workflow_budget_usage_v2 (
                   tenant_id TEXT NOT NULL, workflow_id TEXT NOT NULL, currency TEXT NOT NULL,
                   amount_minor BIGINT NOT NULL, PRIMARY KEY(tenant_id, workflow_id, currency))""",
            """CREATE TABLE IF NOT EXISTS gateway_operations (
                   operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                   tool TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
                   contract_digest TEXT NOT NULL, reserved_amount_minor BIGINT NOT NULL,
                   status TEXT NOT NULL, owner_id TEXT NOT NULL, generation BIGINT NOT NULL,
                   lease_until BIGINT NOT NULL, last_issuer TEXT NOT NULL, last_jti TEXT NOT NULL,
                   response_json TEXT, receipt_json TEXT, unknown_reason TEXT,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                   UNIQUE(tenant_id, workflow_id, tool, idempotency_key))""",
            """CREATE TABLE IF NOT EXISTS gateway_receipts (
                   seq BIGSERIAL PRIMARY KEY, call_id TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL,
                   tenant_id TEXT NOT NULL, workflow_id TEXT NOT NULL, jti TEXT NOT NULL, tool TEXT NOT NULL,
                   decision TEXT NOT NULL, reason TEXT, receipt_json TEXT NOT NULL,
                   receipt_hash TEXT NOT NULL UNIQUE, previous_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS gateway_denials (
                   seq BIGSERIAL PRIMARY KEY, workflow_id TEXT, tool TEXT, reason TEXT NOT NULL,
                   request_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_gateway_operations_status ON gateway_operations(status, lease_until)",
            "CREATE INDEX IF NOT EXISTS idx_gateway_workflow ON gateway_receipts(tenant_id, workflow_id, seq)",
            "CREATE INDEX IF NOT EXISTS idx_gateway_denials ON gateway_denials(reason, created_at)",
        ]
        with self._connect() as conn, conn.transaction():
            with conn.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

    @staticmethod
    def _contract_digest(
        claims: ActionPassportClaims, *, request_digest: str, amount_minor: int
    ) -> str:
        authorization = claims.model_dump(
            mode="json", exclude={"jti", "issued_at", "not_before", "expires_at"}
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
            claims, request_digest=request_digest, amount_minor=amount_minor
        )
        owner_id = f"owner-{uuid.uuid4().hex}"
        now_epoch = int(time.time())
        with self._connect() as conn, conn.transaction():
            with conn.cursor() as cursor:
                # Missing rows cannot be protected by SELECT ... FOR UPDATE. These
                # transaction-scoped locks serialize the first operation, first
                # capability use and first budget use just as SQLite BEGIN IMMEDIATE does.
                for lock_key in (
                    f"operation:{claims.tenant_id}:{claims.workflow_id}:{claims.tool}:{idempotency_key}",
                    f"capability:{claims.issuer}:{claims.jti}",
                    f"budget:{claims.tenant_id}:{claims.workflow_id}:{claims.currency}",
                ):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                cursor.execute(
                    """SELECT * FROM gateway_operations
                       WHERE tenant_id = %s AND workflow_id = %s AND tool = %s AND idempotency_key = %s
                       FOR UPDATE""",
                    (claims.tenant_id, claims.workflow_id, claims.tool, idempotency_key),
                )
                operation = cursor.fetchone()
                if operation is not None:
                    if operation["contract_digest"] != contract_digest:
                        raise GatewayDenied("idempotency_conflict")
                    if operation["status"] == "SUCCEEDED":
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
                    cursor.execute(
                        """UPDATE gateway_operations SET status = 'RECONCILING', owner_id = %s,
                             generation = %s, lease_until = %s, last_issuer = %s, last_jti = %s,
                             updated_at = %s WHERE operation_id = %s AND generation = %s
                             AND lease_until < %s AND status IN ('DISPATCHING', 'RECONCILING')""",
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
                    if cursor.rowcount != 1:
                        raise GatewayDenied("attempt_in_progress")
                    return Reservation(
                        operation_id=operation["operation_id"],
                        owner_id=owner_id,
                        generation=generation,
                        needs_reconciliation=True,
                    )

                cursor.execute(
                    "SELECT * FROM capability_usage_v2 WHERE issuer = %s AND jti = %s FOR UPDATE",
                    (claims.issuer, claims.jti),
                )
                usage = cursor.fetchone()
                if usage is not None and int(usage["call_count"]) >= claims.max_calls:
                    raise GatewayDenied("passport_exhausted")
                if amount_minor > claims.max_amount_minor:
                    raise GatewayDenied("amount_exceeds_passport")
                cursor.execute(
                    """SELECT amount_minor FROM workflow_budget_usage_v2
                       WHERE tenant_id = %s AND workflow_id = %s AND currency = %s FOR UPDATE""",
                    (claims.tenant_id, claims.workflow_id, claims.currency),
                )
                budget = cursor.fetchone()
                if (int(budget["amount_minor"]) if budget else 0) + amount_minor > claims.budget_limit_minor:
                    raise GatewayDenied("workflow_budget_exceeded")
                operation_id = f"op-{uuid.uuid4().hex}"
                now_text = utc_now()
                cursor.execute(
                    """INSERT INTO gateway_operations(
                           operation_id, tenant_id, workflow_id, tool, idempotency_key, request_digest,
                           contract_digest, reserved_amount_minor, status, owner_id, generation, lease_until,
                           last_issuer, last_jti, created_at, updated_at)
                       VALUES(%s, %s, %s, %s, %s, %s, %s, %s, 'DISPATCHING', %s, 1, %s, %s, %s, %s, %s)""",
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
                        now_text,
                        now_text,
                    ),
                )
                cursor.execute(
                    """INSERT INTO capability_usage_v2(issuer, jti, call_count, amount_minor, expires_at, updated_at)
                       VALUES(%s, %s, 1, %s, %s, %s)
                       ON CONFLICT(issuer, jti) DO UPDATE SET call_count = capability_usage_v2.call_count + 1,
                       amount_minor = capability_usage_v2.amount_minor + EXCLUDED.amount_minor,
                       updated_at = EXCLUDED.updated_at""",
                    (claims.issuer, claims.jti, amount_minor, claims.expires_at, now_text),
                )
                cursor.execute(
                    """INSERT INTO workflow_budget_usage_v2(tenant_id, workflow_id, currency, amount_minor)
                       VALUES(%s, %s, %s, %s) ON CONFLICT(tenant_id, workflow_id, currency)
                       DO UPDATE SET amount_minor = workflow_budget_usage_v2.amount_minor + EXCLUDED.amount_minor""",
                    (claims.tenant_id, claims.workflow_id, claims.currency, amount_minor),
                )
                return Reservation(operation_id=operation_id, owner_id=owner_id, generation=1)

    def renew_lease(self, reservation: Reservation) -> bool:
        with self._connect() as conn, conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """UPDATE gateway_operations SET lease_until = %s, updated_at = %s
                   WHERE operation_id = %s AND owner_id = %s AND generation = %s
                   AND status IN ('DISPATCHING', 'RECONCILING')""",
                (
                    int(time.time()) + self.LEASE_SECONDS,
                    utc_now(),
                    reservation.operation_id,
                    reservation.owner_id,
                    reservation.generation,
                ),
            )
            return cursor.rowcount == 1

    def begin_redispatch(self, reservation: Reservation) -> None:
        with self._connect() as conn, conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """UPDATE gateway_operations SET status = 'DISPATCHING', updated_at = %s
                   WHERE operation_id = %s AND owner_id = %s AND generation = %s AND status = 'RECONCILING'""",
                (utc_now(), reservation.operation_id, reservation.owner_id, reservation.generation),
            )
            if cursor.rowcount != 1:
                raise GatewayDenied("operation_fence_lost")

    def mark_unknown(self, reservation: Reservation, *, reason: str) -> None:
        with self._connect() as conn, conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """UPDATE gateway_operations SET status = 'UNKNOWN', unknown_reason = %s, lease_until = 0,
                   updated_at = %s WHERE operation_id = %s AND owner_id = %s AND generation = %s
                   AND status = 'RECONCILING'""",
                (reason, utc_now(), reservation.operation_id, reservation.owner_id, reservation.generation),
            )
            if cursor.rowcount != 1:
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
        with self._connect() as conn, conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM gateway_operations WHERE operation_id = %s FOR UPDATE",
                (reservation.operation_id,),
            )
            operation = cursor.fetchone()
            if (
                operation is None
                or operation["owner_id"] != reservation.owner_id
                or int(operation["generation"]) != reservation.generation
                or operation["status"] not in {"DISPATCHING", "RECONCILING"}
                or operation["request_digest"] != request_digest
            ):
                raise GatewayDenied("operation_fence_lost")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"receipt-chain:{claims.tenant_id}:{claims.workflow_id}",),
            )
            cursor.execute(
                """SELECT receipt_hash FROM gateway_receipts WHERE tenant_id = %s AND workflow_id = %s
                   ORDER BY seq DESC LIMIT 1 FOR UPDATE""",
                (claims.tenant_id, claims.workflow_id),
            )
            previous = cursor.fetchone()
            previous_hash = previous["receipt_hash"] if previous else "GENESIS"
            receipt_hash = sha256_digest({"previous_hash": previous_hash, "receipt": receipt})
            stored_receipt = {**receipt, "_chain": {"previous_hash": previous_hash, "entry_hash": receipt_hash}}
            cursor.execute(
                """INSERT INTO gateway_receipts(call_id, operation_id, tenant_id, workflow_id, jti, tool,
                   decision, reason, receipt_json, receipt_hash, previous_hash, created_at)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
            cursor.execute(
                """UPDATE gateway_operations SET status = %s, response_json = %s, receipt_json = %s,
                   lease_until = 0, last_issuer = %s, last_jti = %s, updated_at = %s
                   WHERE operation_id = %s AND owner_id = %s AND generation = %s
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
            if cursor.rowcount != 1:
                raise GatewayDenied("operation_fence_lost")
            return stored_receipt

    def receipts(self, workflow_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cursor:
            if tenant_id is None:
                cursor.execute(
                    "SELECT receipt_json FROM gateway_receipts WHERE workflow_id = %s ORDER BY seq",
                    (workflow_id,),
                )
            else:
                cursor.execute(
                    """SELECT receipt_json FROM gateway_receipts WHERE tenant_id = %s
                       AND workflow_id = %s ORDER BY seq""",
                    (tenant_id, workflow_id),
                )
            return [json.loads(row["receipt_json"]) for row in cursor.fetchall()]

    def record_denial(
        self,
        *,
        reason: str,
        tool: str | None,
        workflow_id: str | None,
        request_material: dict[str, Any],
    ) -> None:
        with self._connect() as conn, conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO gateway_denials(workflow_id, tool, reason, request_digest, created_at)
                   VALUES(%s, %s, %s, %s, %s)""",
                (workflow_id, tool, reason, sha256_digest(request_material), utc_now()),
            )

    def metrics(self) -> dict[str, int]:
        queries = {
            "total": "SELECT COUNT(*) AS n FROM gateway_receipts",
            "allowed": "SELECT COUNT(*) AS n FROM gateway_receipts WHERE decision = 'allowed'",
            "failed": "SELECT COUNT(*) AS n FROM gateway_receipts WHERE decision = 'upstream_error'",
            "denied": "SELECT COUNT(*) AS n FROM gateway_denials",
            "pending": "SELECT COUNT(*) AS n FROM gateway_operations WHERE status IN ('DISPATCHING', 'RECONCILING')",
            "unknown": "SELECT COUNT(*) AS n FROM gateway_operations WHERE status = 'UNKNOWN'",
        }
        counts: dict[str, int] = {}
        with self._connect() as conn, conn.cursor() as cursor:
            for name, query in queries.items():
                cursor.execute(query)
                counts[name] = int(cursor.fetchone()["n"])
        return {
            "calls_total": counts["total"] + counts["denied"],
            "calls_allowed": counts["allowed"],
            "calls_denied": counts["denied"],
            "upstream_errors": counts["failed"],
            "operations_pending": counts["pending"],
            "operations_unknown": counts["unknown"],
        }
