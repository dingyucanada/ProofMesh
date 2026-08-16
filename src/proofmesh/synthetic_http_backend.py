from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from .business import CommerceSandbox, ReconciliationResult, ToolExecutionError, ToolSpec


class SyntheticHttpTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: dict[str, Any]


class SyntheticHttpBackend:
    """Gateway ToolBackend for two independent-process HTTP synthetic services.

    This adapter is evidence infrastructure, not a third-party payment integration and not
    customer data. It keeps the existing ToolSpec contract while moving payment and CRM state
    outside the ProofMesh process boundary.
    """

    PROVENANCE = {
        "synthetic": True,
        "customer_data": False,
        "enterprise_historical_data": False,
        "third_party_provider": False,
        "transport": "independent-process-http",
    }

    def __init__(self, *, payment_url: str, crm_url: str, timeout_seconds: float = 0.12):
        self.payment_url = payment_url.rstrip("/")
        self.crm_url = crm_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.specs = CommerceSandbox._build_specs()

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": {"readOnlyHint": spec.mode == "read", "destructiveHint": spec.mode != "read"},
                "_meta": {
                    "proofmesh/requiredScopes": list(spec.required_scopes),
                    "proofmesh/mode": spec.mode,
                    "proofmesh/evidenceBackend": "independent-process-http-synthetic",
                },
            }
            for spec in self.specs.values()
        ]

    def _http(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResult:
        encoded = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
        req = request.Request(
            url,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json", "User-Agent": "proofmesh-synthetic-evidence/1"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds if timeout is None else timeout) as response:
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise SyntheticHttpTransportError("synthetic HTTP response is not an object")
                return HttpResult(response.status, body)
        except error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            detail = body.get("error") if isinstance(body, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else "synthetic_http_error"
            message = detail.get("detail") if isinstance(detail, dict) else f"Synthetic service HTTP {exc.code}"
            raise ToolExecutionError(str(code), str(message)) from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            raise SyntheticHttpTransportError(type(exc).__name__) from exc

    @staticmethod
    def _business_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = arguments.get("_proofmesh_tenant_id")
        operation_id = arguments.get("_proofmesh_operation_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ToolExecutionError("tenant_context_missing", "Verified tenant context is required")
        if not isinstance(operation_id, str) or not operation_id:
            raise ToolExecutionError("operation_context_missing", "Gateway operation id is required")
        return {
            **{key: value for key, value in arguments.items() if not key.startswith("_proofmesh_")},
            "tenant_id": tenant_id,
            "operation_id": operation_id,
        }

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in self.specs:
            raise ToolExecutionError("tool_not_found", f"Unknown synthetic tool: {tool}")
        body = self._business_arguments(arguments)
        tenant_query = parse.urlencode({"tenant_id": body["tenant_id"]})
        if tool == "crm.get_ticket":
            response = self._http(
                "GET", f"{self.crm_url}/v1/tickets/{parse.quote(body['ticket_id'])}?{tenant_query}"
            )
        elif tool == "orders.get_refund_context":
            response = self._http(
                "GET", f"{self.payment_url}/v1/orders/{parse.quote(body['order_id'])}?{tenant_query}"
            )
        elif tool == "risk.score_refund":
            ticket = self._http(
                "GET", f"{self.crm_url}/v1/tickets/{parse.quote(body['ticket_id'])}?{tenant_query}"
            ).payload["result"]
            amount = int(body["amount_minor"])
            score = 0.08
            reasons: list[str] = []
            if amount > 10000:
                score += 0.09
                reasons.append("amount_requires_human_review")
            if "损坏" in str(ticket["reason"]):
                score += 0.04
                reasons.append("physical_damage_claim")
            return {"score": round(score, 4), "reasons": reasons, "model": "synthetic-deterministic-risk-v1"}
        elif tool == "payments.issue_refund":
            response = self._http("POST", f"{self.payment_url}/v1/refunds", payload=body)
        elif tool == "crm.close_ticket":
            payment_snapshot = self.workflow_snapshot(body["workflow_id"], tenant_id=body["tenant_id"])
            refund = payment_snapshot.get("refund")
            if not isinstance(refund, dict) or refund.get("status") != "ISSUED":
                raise ToolExecutionError("refund_not_verified", "Synthetic issued refund is required")
            response = self._http(
                "POST",
                f"{self.crm_url}/v1/tickets/{parse.quote(body['ticket_id'])}/close",
                payload={**body, "refund_id": refund["refund_id"]},
            )
        elif tool == "payments.compensate_refund":
            response = self._http(
                "POST",
                f"{self.payment_url}/v1/refunds/{parse.quote(body['refund_id'])}/compensations",
                payload=body,
            )
        elif tool == "memory.store_resolution":
            response = self._http("POST", f"{self.crm_url}/v1/memory", payload=body)
        else:
            raise ToolExecutionError("tool_not_found", f"Unknown synthetic tool: {tool}")
        result = response.payload.get("result")
        if not isinstance(result, dict):
            raise SyntheticHttpTransportError("synthetic HTTP response has no result object")
        return result

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        del arguments, tenant_id
        if tool in {"crm.get_ticket", "orders.get_refund_context", "risk.score_refund"}:
            return ReconciliationResult("SAFE_TO_RETRY")
        service = self.payment_url if tool.startswith("payments.") else self.crm_url
        try:
            payload = self._http("GET", f"{service}/v1/operations/{parse.quote(operation_id)}").payload
        except Exception as exc:
            return ReconciliationResult("UNKNOWN", reason=f"synthetic_reconcile_transport:{type(exc).__name__}")
        status = payload.get("status")
        if status == "ABSENT":
            return ReconciliationResult("SAFE_TO_RETRY")
        if status == "SUCCEEDED" and isinstance(payload.get("result"), dict):
            return ReconciliationResult("SUCCEEDED", result=payload["result"])
        return ReconciliationResult("UNKNOWN", reason=str(payload.get("reason") or "synthetic_upstream_unknown"))

    def seed(self, cases: list[dict[str, Any]]) -> None:
        payload = {"cases": cases, "provenance": self.PROVENANCE}
        self._http("POST", f"{self.payment_url}/__synthetic__/seed", payload=payload, timeout=5.0)
        self._http("POST", f"{self.crm_url}/__synthetic__/seed", payload=payload, timeout=5.0)

    def health(self) -> dict[str, Any]:
        payment = self._http("GET", f"{self.payment_url}/health", timeout=1.0).payload
        crm = self._http("GET", f"{self.crm_url}/health", timeout=1.0).payload
        return {"payment": payment, "crm": crm, "provenance": self.PROVENANCE}

    def stats(self) -> dict[str, Any]:
        payment = self._http("GET", f"{self.payment_url}/__synthetic__/stats", timeout=2.0).payload
        crm = self._http("GET", f"{self.crm_url}/__synthetic__/stats", timeout=2.0).payload
        return {"payment": payment["stats"], "crm": crm["stats"], "provenance": self.PROVENANCE}

    def workflow_snapshot(self, workflow_id: str, *, tenant_id: str) -> dict[str, Any]:
        tenant_query = parse.urlencode({"tenant_id": tenant_id})
        payment = self._http(
            "GET", f"{self.payment_url}/v1/workflows/{parse.quote(workflow_id)}?{tenant_query}", timeout=2.0
        ).payload
        refund = payment.get("refund")
        if not isinstance(refund, dict):
            return {"workflow_id": workflow_id, "refund": None, "ticket": None, "order": None}
        ticket = self._http(
            "GET", f"{self.crm_url}/v1/tickets/{parse.quote(refund['ticket_id'])}?{tenant_query}", timeout=2.0
        ).payload["result"]
        return {
            "workflow_id": workflow_id,
            "refund": refund,
            "ticket": ticket,
            "order": payment.get("order"),
        }


def synthetic_tool_spec(name: str) -> ToolSpec:
    return CommerceSandbox._build_specs()[name]
