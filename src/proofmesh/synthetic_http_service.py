from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .timeutil import utc_now


SYNTHETIC_PROVENANCE = {
    "synthetic": True,
    "customer_data": False,
    "enterprise_historical_data": False,
    "third_party_provider": False,
}


class ServiceError(RuntimeError):
    def __init__(self, status: int, code: str, detail: str):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)


class SyntheticStore:
    service: str

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
        raise NotImplementedError

    def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], float]:
        raise NotImplementedError

    @staticmethod
    def _require(body: dict[str, Any], *names: str) -> None:
        missing = [name for name in names if name not in body]
        if missing:
            raise ServiceError(400, "invalid_request", f"Missing fields: {', '.join(missing)}")


class PaymentStore(SyntheticStore):
    service = "synthetic-payment"

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    total_minor INTEGER NOT NULL,
                    refundable_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_faults (
                    workflow_id TEXT PRIMARY KEY,
                    fault_profile TEXT NOT NULL,
                    response_delay_seconds REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL UNIQUE,
                    ticket_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue_effect_count INTEGER NOT NULL,
                    compensation_effect_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    compensated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    visible_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise ServiceError(400, "invalid_seed", "cases must be a list")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for case in cases:
                if not isinstance(case, dict):
                    raise ServiceError(400, "invalid_seed", "each case must be an object")
                self._require(
                    case,
                    "workflow_id",
                    "tenant_id",
                    "order_id",
                    "amount_minor",
                    "currency",
                    "fault_profile",
                )
                amount = int(case["amount_minor"])
                conn.execute(
                    """INSERT OR REPLACE INTO orders(
                           order_id, tenant_id, total_minor, refundable_minor, currency, status, version
                       ) VALUES(?, ?, ?, ?, ?, 'DELIVERED', 0)""",
                    (
                        case["order_id"],
                        case["tenant_id"],
                        amount + 20000,
                        amount + 10000,
                        case["currency"],
                    ),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO case_faults(
                           workflow_id, fault_profile, response_delay_seconds
                       ) VALUES(?, ?, ?)""",
                    (
                        case["workflow_id"],
                        case["fault_profile"],
                        float(case.get("response_delay_seconds", 0.25)),
                    ),
                )
            conn.commit()
        return {"seeded": len(cases), "provenance": SYNTHETIC_PROVENANCE}

    @staticmethod
    def _refund_id(workflow_id: str) -> str:
        suffix = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:16].upper()
        return f"SYN-RFD-{suffix}"

    def _issue_refund(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        self._require(
            body,
            "operation_id",
            "tenant_id",
            "workflow_id",
            "ticket_id",
            "order_id",
            "amount_minor",
            "currency",
            "expected_order_version",
        )
        conn = self._connect()
        delay = 0.0
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT result_json FROM operations WHERE operation_id = ?",
                (body["operation_id"],),
            ).fetchone()
            if operation is not None:
                conn.commit()
                result = json.loads(operation["result_json"])
                return {**result, "idempotent_replay": True}, 0.0

            existing = conn.execute(
                "SELECT * FROM refunds WHERE tenant_id = ? AND workflow_id = ?",
                (body["tenant_id"], body["workflow_id"]),
            ).fetchone()
            if existing is not None:
                immutable = (
                    existing["ticket_id"],
                    existing["order_id"],
                    int(existing["amount_minor"]),
                    existing["currency"],
                )
                requested = (
                    body["ticket_id"],
                    body["order_id"],
                    int(body["amount_minor"]),
                    body["currency"],
                )
                if immutable != requested:
                    raise ServiceError(
                        409,
                        "workflow_idempotency_conflict",
                        "Synthetic workflow already has a different refund",
                    )
                result = {
                    "refund_id": existing["refund_id"],
                    "workflow_id": existing["workflow_id"],
                    "amount_minor": existing["amount_minor"],
                    "currency": existing["currency"],
                    "status": existing["status"],
                    "idempotent_replay": True,
                }
                conn.execute(
                    """INSERT INTO operations(
                           operation_id, workflow_id, tool, visible_status, result_json, created_at
                       ) VALUES(?, ?, 'payments.issue_refund', 'SUCCEEDED', ?, ?)""",
                    (
                        body["operation_id"],
                        body["workflow_id"],
                        json.dumps(result, sort_keys=True),
                        utc_now(),
                    ),
                )
                conn.commit()
                return result, 0.0

            order = conn.execute(
                "SELECT * FROM orders WHERE tenant_id = ? AND order_id = ?",
                (body["tenant_id"], body["order_id"]),
            ).fetchone()
            if order is None:
                raise ServiceError(404, "order_not_found", "Synthetic order does not exist")
            amount = int(body["amount_minor"])
            if body["currency"] != order["currency"]:
                raise ServiceError(409, "currency_mismatch", "Synthetic order currency differs")
            if int(body["expected_order_version"]) != int(order["version"]):
                raise ServiceError(409, "frozen_plan_stale", "Synthetic order version changed")
            if amount <= 0 or amount > int(order["refundable_minor"]):
                raise ServiceError(409, "refund_exceeds_balance", "Synthetic refund exceeds balance")
            updated = conn.execute(
                """UPDATE orders SET refundable_minor = refundable_minor - ?, version = version + 1
                   WHERE order_id = ? AND tenant_id = ? AND version = ?""",
                (amount, body["order_id"], body["tenant_id"], order["version"]),
            )
            if updated.rowcount != 1:
                raise ServiceError(409, "concurrent_order_update", "Synthetic order changed")
            refund_id = self._refund_id(body["workflow_id"])
            result = {
                "refund_id": refund_id,
                "workflow_id": body["workflow_id"],
                "amount_minor": amount,
                "currency": body["currency"],
                "status": "ISSUED",
                "order_version": int(order["version"]) + 1,
            }
            conn.execute(
                """INSERT INTO refunds(
                       refund_id, tenant_id, workflow_id, ticket_id, order_id, amount_minor,
                       currency, status, issue_effect_count, compensation_effect_count, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'ISSUED', 1, 0, ?)""",
                (
                    refund_id,
                    body["tenant_id"],
                    body["workflow_id"],
                    body["ticket_id"],
                    body["order_id"],
                    amount,
                    body["currency"],
                    utc_now(),
                ),
            )
            fault = conn.execute(
                "SELECT * FROM case_faults WHERE workflow_id = ?",
                (body["workflow_id"],),
            ).fetchone()
            fault_profile = fault["fault_profile"] if fault else "normal"
            visible_status = "UNKNOWN" if fault_profile == "reconcile_unknown_after_commit" else "SUCCEEDED"
            conn.execute(
                """INSERT INTO operations(
                       operation_id, workflow_id, tool, visible_status, result_json, created_at
                   ) VALUES(?, ?, 'payments.issue_refund', ?, ?, ?)""",
                (
                    body["operation_id"],
                    body["workflow_id"],
                    visible_status,
                    json.dumps(result, sort_keys=True),
                    utc_now(),
                ),
            )
            conn.commit()
            if fault_profile in {"payment_timeout_after_commit", "reconcile_unknown_after_commit"}:
                delay = float(fault["response_delay_seconds"])
            return result, delay
        except ServiceError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _compensate(self, body: dict[str, Any]) -> dict[str, Any]:
        self._require(body, "operation_id", "tenant_id", "workflow_id", "refund_id", "reason")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT result_json FROM operations WHERE operation_id = ?",
                (body["operation_id"],),
            ).fetchone()
            if operation is not None:
                conn.commit()
                return {**json.loads(operation["result_json"]), "idempotent_replay": True}
            refund = conn.execute(
                "SELECT * FROM refunds WHERE tenant_id = ? AND refund_id = ?",
                (body["tenant_id"], body["refund_id"]),
            ).fetchone()
            if refund is None or refund["workflow_id"] != body["workflow_id"]:
                raise ServiceError(404, "refund_not_found", "Synthetic refund does not match workflow")
            if refund["status"] == "ISSUED":
                conn.execute(
                    """UPDATE orders SET refundable_minor = refundable_minor + ?, version = version + 1
                       WHERE tenant_id = ? AND order_id = ?""",
                    (refund["amount_minor"], body["tenant_id"], refund["order_id"]),
                )
                conn.execute(
                    """UPDATE refunds SET status = 'COMPENSATED', compensation_effect_count = 1,
                           compensated_at = ? WHERE refund_id = ?""",
                    (utc_now(), refund["refund_id"]),
                )
            elif refund["status"] != "COMPENSATED":
                raise ServiceError(409, "refund_not_compensatable", "Synthetic refund state is unsafe")
            result = {
                "refund_id": refund["refund_id"],
                "status": "COMPENSATED",
                "balance_restored": True,
                "idempotent_replay": refund["status"] == "COMPENSATED",
            }
            conn.execute(
                """INSERT INTO operations(
                       operation_id, workflow_id, tool, visible_status, result_json, created_at
                   ) VALUES(?, ?, 'payments.compensate_refund', 'SUCCEEDED', ?, ?)""",
                (body["operation_id"], body["workflow_id"], json.dumps(result, sort_keys=True), utc_now()),
            )
            conn.commit()
            return result
        except ServiceError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _operation(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT visible_status, result_json FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return {"status": "ABSENT"}
        if row["visible_status"] == "UNKNOWN":
            return {"status": "UNKNOWN", "reason": "synthetic_provider_visibility_gap"}
        return {"status": "SUCCEEDED", "result": json.loads(row["result_json"])}

    def _workflow(self, workflow_id: str, tenant_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            refund = conn.execute(
                "SELECT * FROM refunds WHERE workflow_id = ? AND tenant_id = ?",
                (workflow_id, tenant_id),
            ).fetchone()
            if refund is None:
                return {"workflow_id": workflow_id, "refund": None, "order": None}
            order = conn.execute(
                "SELECT * FROM orders WHERE order_id = ? AND tenant_id = ?",
                (refund["order_id"], tenant_id),
            ).fetchone()
        return {"workflow_id": workflow_id, "refund": dict(refund), "order": dict(order)}

    def _stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS refunds,
                          COALESCE(SUM(CASE WHEN issue_effect_count > 1 THEN issue_effect_count - 1 ELSE 0 END), 0)
                              AS duplicate_refund_effects,
                          COALESCE(SUM(compensation_effect_count), 0) AS compensations
                   FROM refunds"""
            ).fetchone()
            unknown = conn.execute(
                "SELECT COUNT(*) AS n FROM operations WHERE visible_status = 'UNKNOWN'"
            ).fetchone()["n"]
        return {**dict(row), "unknown_operations": unknown}

    def route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], float]:
        if method == "POST" and path == "/__synthetic__/seed":
            return 200, self.seed(body), 0.0
        if method == "GET" and path == "/__synthetic__/stats":
            return 200, {"stats": self._stats(), "provenance": SYNTHETIC_PROVENANCE}, 0.0
        if method == "POST" and path == "/v1/refunds":
            result, delay = self._issue_refund(body)
            return 200, {"result": result}, delay
        if method == "POST" and path.startswith("/v1/refunds/") and path.endswith("/compensations"):
            refund_id = path.split("/")[3]
            return 200, {"result": self._compensate({**body, "refund_id": refund_id})}, 0.0
        if method == "GET" and path.startswith("/v1/operations/"):
            return 200, self._operation(path.rsplit("/", 1)[-1]), 0.0
        if method == "GET" and path.startswith("/v1/workflows/"):
            tenant_id = query.get("tenant_id", [""])[0]
            return 200, self._workflow(path.rsplit("/", 1)[-1], tenant_id), 0.0
        if method == "GET" and path.startswith("/v1/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            tenant_id = query.get("tenant_id", [""])[0]
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM orders WHERE order_id = ? AND tenant_id = ?",
                    (order_id, tenant_id),
                ).fetchone()
            if row is None:
                raise ServiceError(404, "order_not_found", "Synthetic order does not exist")
            return 200, {"result": dict(row)}, 0.0
        raise ServiceError(404, "route_not_found", f"Unsupported payment route: {method} {path}")


class CrmStore(SyntheticStore):
    service = "synthetic-crm"

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    requested_amount_minor INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fault_profile TEXT NOT NULL,
                    closed_workflow_id TEXT,
                    close_effect_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    visible_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resolution_memory (
                    workflow_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    resolution_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise ServiceError(400, "invalid_seed", "cases must be a list")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for case in cases:
                if not isinstance(case, dict):
                    raise ServiceError(400, "invalid_seed", "each case must be an object")
                self._require(
                    case,
                    "ticket_id",
                    "tenant_id",
                    "order_id",
                    "amount_minor",
                    "reason",
                    "fault_profile",
                )
                conn.execute(
                    """INSERT OR REPLACE INTO tickets(
                           ticket_id, tenant_id, customer_id, order_id, requested_amount_minor,
                           reason, status, fault_profile, closed_workflow_id, close_effect_count
                       ) VALUES(?, ?, ?, ?, ?, ?, 'OPEN', ?, NULL, 0)""",
                    (
                        case["ticket_id"],
                        case["tenant_id"],
                        f"SYN-CUS-{case['ticket_id'][-6:]}",
                        case["order_id"],
                        int(case["amount_minor"]),
                        case["reason"],
                        case["fault_profile"],
                    ),
                )
            conn.commit()
        return {"seeded": len(cases), "provenance": SYNTHETIC_PROVENANCE}

    def _ticket(self, ticket_id: str, tenant_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT ticket_id, customer_id, order_id, requested_amount_minor,
                          reason, status, closed_workflow_id, close_effect_count
                   FROM tickets WHERE ticket_id = ? AND tenant_id = ?""",
                (ticket_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ServiceError(404, "ticket_not_found", "Synthetic ticket does not exist")
        return dict(row)

    def _close(self, ticket_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require(body, "operation_id", "tenant_id", "workflow_id", "refund_id", "resolution")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT result_json FROM operations WHERE operation_id = ?",
                (body["operation_id"],),
            ).fetchone()
            if operation is not None:
                conn.commit()
                return {**json.loads(operation["result_json"]), "idempotent_replay": True}
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (ticket_id, body["tenant_id"]),
            ).fetchone()
            if ticket is None:
                raise ServiceError(404, "ticket_not_found", "Synthetic ticket does not exist")
            if ticket["fault_profile"] == "crm_close_error":
                raise ServiceError(503, "crm_dependency_unavailable", "Injected synthetic CRM failure")
            if ticket["status"] == "CLOSED" and ticket["closed_workflow_id"] == body["workflow_id"]:
                result = {
                    "ticket_id": ticket_id,
                    "status": "CLOSED",
                    "refund_id": body["refund_id"],
                    "idempotent_replay": True,
                }
            elif ticket["status"] != "OPEN":
                raise ServiceError(409, "ticket_not_open", "Synthetic ticket is not open")
            else:
                conn.execute(
                    """UPDATE tickets SET status = 'CLOSED', closed_workflow_id = ?,
                           close_effect_count = close_effect_count + 1 WHERE ticket_id = ?""",
                    (body["workflow_id"], ticket_id),
                )
                result = {
                    "ticket_id": ticket_id,
                    "status": "CLOSED",
                    "refund_id": body["refund_id"],
                }
            conn.execute(
                """INSERT INTO operations(
                       operation_id, workflow_id, tool, visible_status, result_json, created_at
                   ) VALUES(?, ?, 'crm.close_ticket', 'SUCCEEDED', ?, ?)""",
                (body["operation_id"], body["workflow_id"], json.dumps(result, sort_keys=True), utc_now()),
            )
            conn.commit()
            return result
        except ServiceError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _store_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        self._require(body, "operation_id", "tenant_id", "workflow_id", "fingerprint", "resolution")
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO resolution_memory(
                       workflow_id, tenant_id, fingerprint, resolution_json, created_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    body["workflow_id"],
                    body["tenant_id"],
                    body["fingerprint"],
                    json.dumps(body["resolution"], sort_keys=True),
                    utc_now(),
                ),
            )
            result = {"workflow_id": body["workflow_id"], "stored": True}
            conn.execute(
                """INSERT OR REPLACE INTO operations(
                       operation_id, workflow_id, tool, visible_status, result_json, created_at
                   ) VALUES(?, ?, 'memory.store_resolution', 'SUCCEEDED', ?, ?)""",
                (body["operation_id"], body["workflow_id"], json.dumps(result, sort_keys=True), utc_now()),
            )
        return result

    def _operation(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT visible_status, result_json FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return {"status": "ABSENT"}
        return {"status": row["visible_status"], "result": json.loads(row["result_json"])}

    def _stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS tickets, COALESCE(SUM(close_effect_count), 0) AS close_effects,
                          COALESCE(SUM(CASE WHEN close_effect_count > 1 THEN close_effect_count - 1 ELSE 0 END), 0)
                              AS duplicate_close_effects
                   FROM tickets"""
            ).fetchone()
        return dict(row)

    def route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], float]:
        if method == "POST" and path == "/__synthetic__/seed":
            return 200, self.seed(body), 0.0
        if method == "GET" and path == "/__synthetic__/stats":
            return 200, {"stats": self._stats(), "provenance": SYNTHETIC_PROVENANCE}, 0.0
        if method == "GET" and path.startswith("/v1/tickets/"):
            ticket_id = path.rsplit("/", 1)[-1]
            tenant_id = query.get("tenant_id", [""])[0]
            return 200, {"result": self._ticket(ticket_id, tenant_id)}, 0.0
        if method == "POST" and path.startswith("/v1/tickets/") and path.endswith("/close"):
            ticket_id = path.split("/")[3]
            return 200, {"result": self._close(ticket_id, body)}, 0.0
        if method == "POST" and path == "/v1/memory":
            return 200, {"result": self._store_memory(body)}, 0.0
        if method == "GET" and path.startswith("/v1/operations/"):
            return 200, self._operation(path.rsplit("/", 1)[-1]), 0.0
        raise ServiceError(404, "route_not_found", f"Unsupported CRM route: {method} {path}")


class SyntheticHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: SyntheticStore):
        self.store = store
        super().__init__(address, SyntheticHandler)


class SyntheticHandler(BaseHTTPRequestHandler):
    server: SyntheticHttpServer

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ServiceError(413, "request_too_large", "Synthetic request exceeds 2 MB")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "invalid_json", "Request body must be a JSON object") from exc
        if not isinstance(payload, dict):
            raise ServiceError(400, "invalid_json", "Request body must be a JSON object")
        return payload

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/health":
                import os

                self._send(
                    200,
                    {
                        "ok": True,
                        "service": self.server.store.service,
                        "pid": os.getpid(),
                        "provenance": SYNTHETIC_PROVENANCE,
                    },
                )
                return
            status, payload, delay = self.server.store.route(
                method,
                parsed.path,
                parse_qs(parsed.query),
                self._read_body(),
            )
            if delay > 0:
                time.sleep(delay)
            self._send(status, payload)
        except ServiceError as exc:
            self._send(
                exc.status,
                {"error": {"code": exc.code, "detail": exc.detail}, "provenance": SYNTHETIC_PROVENANCE},
            )
        except Exception as exc:
            self._send(
                500,
                {
                    "error": {"code": "synthetic_internal_error", "detail": type(exc).__name__},
                    "provenance": SYNTHETIC_PROVENANCE,
                },
            )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("X-ProofMesh-Data-Class", "synthetic-no-customer-data")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent-process synthetic HTTP business sandbox")
    parser.add_argument("--service", required=True, choices=("payment", "crm"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    store: SyntheticStore = PaymentStore(args.db) if args.service == "payment" else CrmStore(args.db)
    server = SyntheticHttpServer((args.host, args.port), store)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return HTTPStatus.OK - HTTPStatus.OK


if __name__ == "__main__":
    raise SystemExit(main())
