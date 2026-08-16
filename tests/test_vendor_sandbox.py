from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib import parse

import pytest

from proofmesh.business import ToolExecutionError
from proofmesh.capabilities import ActionPassportClaims, sha256_digest
from proofmesh.gateway import ActionGateway, GatewayDenied, GatewayStore
from proofmesh.vendor_sandbox import (
    StripeHubSpotSandboxBackend,
    VendorConfigurationError,
    VendorSandboxConfig,
    VendorTransportUncertain,
    vendor_readiness_report,
)


class _ProviderState:
    def __init__(self):
        self.lock = threading.Lock()
        self.refund_mode = "normal"
        self.refund_list_mode = "normal"
        self.crm_patch_mode = "normal"
        self.refund_posts = 0
        self.crm_patches = 0
        self.refunds_by_key: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
        self.ticket = {
            "id": "12345",
            "properties": {
                "hs_pipeline_stage": "1",
                "proofmesh_order_id": "pi_test_123",
                "proofmesh_customer_id": "customer-test-1",
                "proofmesh_requested_amount_minor": "8000",
                "proofmesh_currency": "CNY",
                "proofmesh_reason_code": "damaged_package",
                "proofmesh_operation_id": "",
                "proofmesh_workflow_id": "",
                "proofmesh_refund_id": "",
                "proofmesh_tenant_id": "acme-cn",
                "proofmesh_resolution_digest": "",
            },
        }


def _provider_handler(state: _ProviderState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: dict[str, Any], *, delay: float = 0.0) -> None:
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _authorized(self) -> bool:
            value = self.headers.get("Authorization", "")
            if value not in {"Bearer sk_test_fixture", "Bearer pat-test-fixture"}:
                self._json(401, {"error": {"type": "authentication_error"}})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            parsed = parse.urlsplit(self.path)
            if parsed.path == "/v1/account":
                self._json(200, {"id": "acct_test_contract", "livemode": False})
                return
            if parsed.path == "/account-info/v3/details":
                self._json(200, {"portalId": 987654})
                return
            if parsed.path == "/v1/payment_intents/pi_test_123":
                refunded = sum(int(item[1]["amount"]) for item in state.refunds_by_key.values())
                self._json(
                    200,
                    {
                        "id": "pi_test_123",
                        "status": "succeeded",
                        "currency": "cny",
                        "customer": "cus_test_1",
                        "latest_charge": {
                            "id": "ch_test_123",
                            "amount_captured": 29900,
                            "amount_refunded": refunded,
                        },
                    },
                )
                return
            if parsed.path == "/v1/refunds":
                if state.refund_list_mode == "unknown":
                    self._json(503, {"error": {"type": "api_error"}})
                    return
                query = parse.parse_qs(parsed.query)
                target = query.get("payment_intent", [""])[0]
                with state.lock:
                    data = [
                        dict(refund)
                        for _, refund in state.refunds_by_key.values()
                        if refund["payment_intent"] == target
                    ]
                self._json(200, {"object": "list", "data": data, "has_more": False})
                return
            if parsed.path == "/crm/v3/objects/tickets/12345":
                with state.lock:
                    payload = json.loads(json.dumps(state.ticket))
                self._json(200, payload)
                return
            self._json(404, {"status": "error"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if self.path != "/v1/refunds":
                self._json(404, {"status": "error"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            fields = {key: values[0] for key, values in parse.parse_qs(self.rfile.read(length).decode()).items()}
            key = self.headers.get("Idempotency-Key", "")
            with state.lock:
                state.refund_posts += 1
                if state.refund_mode == "reject":
                    self._json(402, {"error": {"type": "card_error", "code": "refund_failed"}})
                    return
                existing = state.refunds_by_key.get(key)
                if existing is not None and existing[0] != fields:
                    self._json(400, {"error": {"type": "idempotency_error"}})
                    return
                if existing is None:
                    metadata = {
                        name.removeprefix("metadata[").removesuffix("]"): value
                        for name, value in fields.items()
                        if name.startswith("metadata[")
                    }
                    refund = {
                        "id": f"re_test_{len(state.refunds_by_key) + 1}",
                        "payment_intent": fields["payment_intent"],
                        "amount": int(fields["amount"]),
                        "currency": "cny",
                        "status": "succeeded",
                        "metadata": metadata,
                    }
                    state.refunds_by_key[key] = (fields, refund)
                else:
                    refund = existing[1]
                mode = state.refund_mode
            self._json(200, refund, delay=0.18 if mode == "commit_timeout" else 0.0)

        def do_PATCH(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if self.path != "/crm/v3/objects/tickets/12345":
                self._json(404, {"status": "error"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            with state.lock:
                state.crm_patches += 1
                if state.crm_patch_mode == "reject":
                    self._json(403, {"status": "error"})
                    return
                state.ticket["properties"].update(payload["properties"])
                response = json.loads(json.dumps(state.ticket))
                mode = state.crm_patch_mode
            self._json(200, response, delay=0.18 if mode == "commit_timeout" else 0.0)

    return Handler


@contextmanager
def provider_server() -> Iterator[tuple[str, _ProviderState]]:
    state = _ProviderState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _provider_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _config(base_url: str, *, timeout: float = 0.5) -> VendorSandboxConfig:
    return VendorSandboxConfig(
        tenant_id="acme-cn",
        stripe_secret_key="sk_test_fixture",
        hubspot_access_token="pat-test-fixture",
        hubspot_open_stage_id="1",
        hubspot_closed_stage_id="4",
        stripe_base_url=base_url,
        hubspot_base_url=base_url,
        timeout_seconds=timeout,
    )


def _refund_arguments(*, workflow_id: str = "wf-vendor-success") -> dict[str, Any]:
    return {
        "ticket_id": "12345",
        "order_id": "pi_test_123",
        "amount_minor": 8000,
        "currency": "CNY",
        "expected_order_version": 0,
        "workflow_id": workflow_id,
    }


def _gateway(action_runtime, tmp_path: Path, backend: StripeHubSpotSandboxBackend) -> ActionGateway:
    return ActionGateway(
        verifier=action_runtime.verifier,
        receipt_signer=action_runtime.receipt_signer,
        store=GatewayStore(tmp_path / "vendor-gateway.db"),
        upstream=backend,
    )


def _passport(action_runtime, gateway: ActionGateway, arguments: dict[str, Any]) -> tuple[str, str]:
    context_digest = sha256_digest({"vendor": arguments["workflow_id"]})
    claims = ActionPassportClaims.issue(
        issuer=action_runtime.passport_signer.issuer,
        audience=gateway.audience,
        tenant_id="acme-cn",
        subject="action-executor",
        workflow_id=arguments["workflow_id"],
        tool="payments.issue_refund",
        resource=arguments["order_id"],
        scopes=["refund:write"],
        arguments=arguments,
        context_digest=context_digest,
        policy_digest="b" * 64,
        approval_digest="AUTOMATIC",
        mode="execute",
        max_calls=1,
        max_amount_minor=8000,
        budget_limit_minor=8000,
        currency="CNY",
        ttl_seconds=30,
    )
    return action_runtime.passport_signer.sign_passport(claims), context_digest


def _gateway_refund(
    gateway: ActionGateway,
    arguments: dict[str, Any],
    token: str,
    context_digest: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    return gateway.call_tool(
        tool="payments.issue_refund",
        arguments=arguments,
        passport=token,
        workflow_id=arguments["workflow_id"],
        context_digest=context_digest,
        idempotency_key=idempotency_key,
    )


def _expire(gateway: ActionGateway, idempotency_key: str) -> None:
    with sqlite3.connect(gateway.store.db_path) as conn:
        conn.execute("UPDATE gateway_operations SET lease_until = 0 WHERE idempotency_key = ?", (idempotency_key,))


def test_configuration_rejects_live_key_and_never_represents_secrets():
    with pytest.raises(VendorConfigurationError, match="stripe_test_key_required"):
        VendorSandboxConfig(
            tenant_id="acme-cn",
            stripe_secret_key="sk_live_forbidden",
            hubspot_access_token="pat-sensitive",
            hubspot_open_stage_id="1",
            hubspot_closed_stage_id="4",
        )
    config = VendorSandboxConfig(
        tenant_id="acme-cn",
        stripe_secret_key="sk_test_sensitive",
        hubspot_access_token="pat-sensitive",
        hubspot_open_stage_id="1",
        hubspot_closed_stage_id="4",
    )
    assert "sk_test_sensitive" not in repr(config)
    assert "pat-sensitive" not in repr(config)


def test_gateway_success_and_duplicate_have_one_stripe_side_effect(action_runtime, tmp_path: Path):
    with provider_server() as (base_url, state):
        backend = StripeHubSpotSandboxBackend(_config(base_url))
        gateway = _gateway(action_runtime, tmp_path, backend)
        arguments = _refund_arguments()
        token, context_digest = _passport(action_runtime, gateway, arguments)
        call = dict(
            gateway=gateway,
            arguments=arguments,
            token=token,
            context_digest=context_digest,
            idempotency_key="vendor-success-key",
        )
        first = _gateway_refund(**call)
        duplicate = _gateway_refund(**call)
        assert first["result"]["status"] == "ISSUED"
        assert duplicate["idempotent_replay"] is True
        assert duplicate["receipt"] == first["receipt"]
        assert state.refund_posts == 1


def test_timeout_after_commit_reconciles_before_retry(action_runtime, tmp_path: Path):
    with provider_server() as (base_url, state):
        backend = StripeHubSpotSandboxBackend(_config(base_url, timeout=0.05))
        gateway = _gateway(action_runtime, tmp_path, backend)
        arguments = _refund_arguments(workflow_id="wf-vendor-timeout")
        token, context_digest = _passport(action_runtime, gateway, arguments)
        key = "vendor-timeout-key"
        state.refund_mode = "commit_timeout"
        with pytest.raises(VendorTransportUncertain, match="stripe_transport_uncertain"):
            _gateway_refund(gateway, arguments, token, context_digest, idempotency_key=key)
        _expire(gateway, key)
        state.refund_mode = "normal"
        replacement, _ = _passport(action_runtime, gateway, arguments)
        recovered = _gateway_refund(gateway, arguments, replacement, context_digest, idempotency_key=key)
        assert recovered["result"]["idempotent_replay"] is True
        assert recovered["receipt"]["recovered"] is True
        assert state.refund_posts == 1


def test_uncertain_reconcile_is_frozen_unknown_without_second_post(action_runtime, tmp_path: Path):
    with provider_server() as (base_url, state):
        backend = StripeHubSpotSandboxBackend(_config(base_url, timeout=0.05))
        gateway = _gateway(action_runtime, tmp_path, backend)
        arguments = _refund_arguments(workflow_id="wf-vendor-unknown")
        token, context_digest = _passport(action_runtime, gateway, arguments)
        key = "vendor-unknown-key"
        state.refund_mode = "commit_timeout"
        with pytest.raises(VendorTransportUncertain):
            _gateway_refund(gateway, arguments, token, context_digest, idempotency_key=key)
        _expire(gateway, key)
        state.refund_list_mode = "unknown"
        replacement, _ = _passport(action_runtime, gateway, arguments)
        with pytest.raises(GatewayDenied, match="operation_unknown_manual_reconciliation_required"):
            _gateway_refund(gateway, arguments, replacement, context_digest, idempotency_key=key)
        assert state.refund_posts == 1
        with sqlite3.connect(gateway.store.db_path) as conn:
            assert conn.execute("SELECT status FROM gateway_operations").fetchone()[0] == "UNKNOWN"


def test_stripe_rejection_and_idempotency_conflict_are_nonretryable():
    with provider_server() as (base_url, state):
        backend = StripeHubSpotSandboxBackend(_config(base_url))
        arguments = _refund_arguments(workflow_id="wf-vendor-reject")
        call_arguments = {
            **arguments,
            "_proofmesh_tenant_id": "acme-cn",
            "_proofmesh_operation_id": "op-reject-contract",
        }
        state.refund_mode = "reject"
        with pytest.raises(ToolExecutionError) as rejected:
            backend.call("payments.issue_refund", call_arguments)
        assert rejected.value.code == "stripe_refund_rejected"

        state.refund_mode = "normal"
        backend.call("payments.issue_refund", call_arguments)
        changed = {**call_arguments, "amount_minor": 7000, "expected_order_version": 8000}
        with pytest.raises(ToolExecutionError) as conflict:
            backend.stripe.issue_refund(
                changed,
                tenant_id="acme-cn",
                operation_id="op-reject-contract",
            )
        assert conflict.value.code == "stripe_idempotency_conflict"


def test_hubspot_timeout_reconcile_conflict_and_rejection():
    with provider_server() as (base_url, state):
        backend = StripeHubSpotSandboxBackend(_config(base_url, timeout=0.05))
        refund_args = _refund_arguments(workflow_id="wf-crm-contract")
        backend.call(
            "payments.issue_refund",
            {
                **refund_args,
                "_proofmesh_tenant_id": "acme-cn",
                "_proofmesh_operation_id": "op-refund-for-crm",
            },
        )
        close_args = {
            "ticket_id": "12345",
            "workflow_id": "wf-crm-contract",
            "resolution": "sanitized resolution",
        }
        state.crm_patch_mode = "commit_timeout"
        with pytest.raises(VendorTransportUncertain):
            backend.call(
                "crm.close_ticket",
                {
                    **close_args,
                    "_proofmesh_tenant_id": "acme-cn",
                    "_proofmesh_operation_id": "op-crm-close",
                },
            )
        reconciled = backend.reconcile(
            "crm.close_ticket", close_args, tenant_id="acme-cn", operation_id="op-crm-close"
        )
        assert reconciled.status == "SUCCEEDED"
        assert reconciled.result and reconciled.result["idempotent_replay"] is True
        assert state.crm_patches == 1

        conflict = backend.reconcile(
            "crm.close_ticket", close_args, tenant_id="acme-cn", operation_id="op-other"
        )
        assert conflict.status == "UNKNOWN"

        state.ticket["properties"].update(
            {
                "hs_pipeline_stage": "1",
                "proofmesh_operation_id": "",
                "proofmesh_workflow_id": "",
                "proofmesh_refund_id": "",
            }
        )
        state.crm_patch_mode = "reject"
        with pytest.raises(ToolExecutionError) as denied:
            backend.call(
                "crm.close_ticket",
                {
                    **close_args,
                    "_proofmesh_tenant_id": "acme-cn",
                    "_proofmesh_operation_id": "op-crm-denied",
                },
            )
        assert denied.value.code == "hubspot_permission_denied"


def test_readiness_probe_returns_only_boolean_and_account_ids(monkeypatch):
    with provider_server() as (base_url, _):
        values = {
            "PROOFMESH_VENDOR_TENANT_ID": "acme-cn",
            "PROOFMESH_STRIPE_SECRET_KEY": "sk_test_fixture",
            "PROOFMESH_HUBSPOT_ACCESS_TOKEN": "pat-test-fixture",
            "PROOFMESH_HUBSPOT_OPEN_STAGE_ID": "1",
            "PROOFMESH_HUBSPOT_CLOSED_STAGE_ID": "4",
            "PROOFMESH_STRIPE_API_BASE": base_url,
            "PROOFMESH_HUBSPOT_API_BASE": base_url,
        }
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        report = vendor_readiness_report(probe_network=True)
        rendered = json.dumps(report, sort_keys=True)
        assert report["ready"] is True
        assert report["providers"] == {
            "stripe": {"ok": True, "account_id": "acct_test_contract"},
            "hubspot": {"ok": True, "account_id": "987654"},
        }
        assert "sk_test_fixture" not in rendered
        assert "pat-test-fixture" not in rendered
