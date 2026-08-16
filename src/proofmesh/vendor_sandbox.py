from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, parse, request

from .business import CommerceSandbox, ReconciliationResult, ToolExecutionError, ToolSpec


class VendorConfigurationError(RuntimeError):
    """Configuration is unsafe or incomplete; messages never contain secret values."""


class VendorTransportUncertain(RuntimeError):
    """The provider may have committed a write; the gateway must reconcile before retrying."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _safe_base_url(value: str, *, official_host: str, variable: str) -> str:
    candidate = value.rstrip("/")
    parts = parse.urlsplit(candidate)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        not candidate
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or parts.hostname is None
        or (parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in local_hosts))
    ):
        raise VendorConfigurationError(f"{variable}_unsafe")
    if parts.hostname != official_host and parts.hostname not in local_hosts:
        raise VendorConfigurationError(f"{variable}_host_not_allowed")
    return candidate


def _required_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise VendorConfigurationError(f"{name}_missing")
    return value


@dataclass(frozen=True)
class VendorSandboxConfig:
    """Single-tenant Stripe test + HubSpot developer-test configuration.

    Secrets are excluded from repr/equality and are only populated by ``from_env`` in
    non-test use. Live Stripe keys are deliberately rejected by this sandbox adapter.
    """

    tenant_id: str
    stripe_secret_key: str = field(repr=False, compare=False)
    hubspot_access_token: str = field(repr=False, compare=False)
    hubspot_open_stage_id: str
    hubspot_closed_stage_id: str
    stripe_base_url: str = "https://api.stripe.com"
    hubspot_base_url: str = "https://api.hubapi.com"
    timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        if not self.tenant_id or len(self.tenant_id) > 128:
            raise VendorConfigurationError("vendor_tenant_id_invalid")
        if not self.stripe_secret_key.startswith(("sk_test_", "rk_test_")):
            raise VendorConfigurationError("stripe_test_key_required")
        if not self.hubspot_access_token:
            raise VendorConfigurationError("hubspot_access_token_missing")
        if (
            not self.hubspot_open_stage_id
            or not self.hubspot_closed_stage_id
            or self.hubspot_open_stage_id == self.hubspot_closed_stage_id
            or len(self.hubspot_open_stage_id) > 128
            or len(self.hubspot_closed_stage_id) > 128
        ):
            raise VendorConfigurationError("hubspot_stage_contract_invalid")
        if not 0.05 <= float(self.timeout_seconds) <= 10.0:
            raise VendorConfigurationError("vendor_timeout_out_of_range")
        object.__setattr__(
            self,
            "stripe_base_url",
            _safe_base_url(
                self.stripe_base_url,
                official_host="api.stripe.com",
                variable="PROOFMESH_STRIPE_API_BASE",
            ),
        )
        object.__setattr__(
            self,
            "hubspot_base_url",
            _safe_base_url(
                self.hubspot_base_url,
                official_host="api.hubapi.com",
                variable="PROOFMESH_HUBSPOT_API_BASE",
            ),
        )

    @classmethod
    def from_env(cls) -> "VendorSandboxConfig":
        timeout_text = os.getenv("PROOFMESH_VENDOR_TIMEOUT_SECONDS", "3.0")
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise VendorConfigurationError("PROOFMESH_VENDOR_TIMEOUT_SECONDS_invalid") from exc
        return cls(
            tenant_id=_required_env("PROOFMESH_VENDOR_TENANT_ID"),
            stripe_secret_key=_required_env("PROOFMESH_STRIPE_SECRET_KEY"),
            hubspot_access_token=_required_env("PROOFMESH_HUBSPOT_ACCESS_TOKEN"),
            hubspot_open_stage_id=_required_env("PROOFMESH_HUBSPOT_OPEN_STAGE_ID"),
            hubspot_closed_stage_id=_required_env("PROOFMESH_HUBSPOT_CLOSED_STAGE_ID"),
            stripe_base_url=os.getenv("PROOFMESH_STRIPE_API_BASE", "https://api.stripe.com"),
            hubspot_base_url=os.getenv("PROOFMESH_HUBSPOT_API_BASE", "https://api.hubapi.com"),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class _HttpResult:
    status: int
    payload: dict[str, Any]


class _VendorHttpClient:
    MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float, vendor: str):
        self.base_url = base_url
        self._token = token
        self.timeout_seconds = timeout_seconds
        self.vendor = vendor
        self._opener = request.build_opener(_NoRedirect())

    def call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str | int] | None = None,
        idempotency_key: str | None = None,
    ) -> _HttpResult:
        if json_body is not None and form_body is not None:
            raise ValueError("only one request body format is allowed")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        data: bytes | None = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "ProofMesh-vendor-sandbox/1.0",
        }
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = parse.urlencode(form_body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        req = request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) > self.MAX_RESPONSE_BYTES:
                    raise VendorTransportUncertain(f"{self.vendor}_response_too_large")
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    raise VendorTransportUncertain(f"{self.vendor}_response_not_object")
                return _HttpResult(int(response.status), payload)
        except error.HTTPError as exc:
            raw = exc.read(self.MAX_RESPONSE_BYTES + 1)
            try:
                payload = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            self._raise_http_error(int(exc.code), payload if isinstance(payload, dict) else {})
            raise AssertionError("unreachable")
        except VendorTransportUncertain:
            raise
        except (error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise VendorTransportUncertain(f"{self.vendor}_transport_uncertain") from exc

    def _raise_http_error(self, status: int, payload: dict[str, Any]) -> None:
        if status in {408, 425, 429} or 500 <= status <= 599:
            raise VendorTransportUncertain(f"{self.vendor}_retryable_http_{status}")
        if self.vendor == "stripe":
            details = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            provider_code = str(details.get("code") or details.get("type") or "")
            if status == 409 or provider_code == "idempotency_error":
                code = "stripe_idempotency_conflict"
            elif status == 401:
                code = "stripe_authentication_failed"
            elif status == 403:
                code = "stripe_permission_denied"
            elif status == 404:
                code = "stripe_resource_not_found"
            elif status in {400, 402}:
                code = "stripe_refund_rejected"
            else:
                code = "stripe_nonretryable_error"
        else:
            if status == 401:
                code = "hubspot_authentication_failed"
            elif status == 403:
                code = "hubspot_permission_denied"
            elif status == 404:
                code = "hubspot_ticket_not_found"
            elif status == 409:
                code = "hubspot_write_conflict"
            elif status == 400:
                code = "hubspot_validation_rejected"
            else:
                code = "hubspot_nonretryable_error"
        # Provider response bodies can contain request data and are intentionally not logged.
        raise ToolExecutionError(code, code)


class StripeTestRefundBackend:
    """Stripe test-mode refund boundary with read-after-write reconciliation."""

    def __init__(self, config: VendorSandboxConfig):
        self.config = config
        self.http = _VendorHttpClient(
            base_url=config.stripe_base_url,
            token=config.stripe_secret_key,
            timeout_seconds=config.timeout_seconds,
            vendor="stripe",
        )
        all_specs = CommerceSandbox._build_specs()
        names = {"orders.get_refund_context", "payments.issue_refund", "payments.compensate_refund"}
        self.specs = {name: spec for name, spec in all_specs.items() if name in names}

    def _payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        if not payment_intent_id.startswith("pi_"):
            raise ToolExecutionError("stripe_payment_intent_required", "stripe_payment_intent_required")
        payload = self.http.call(
            "GET",
            f"/v1/payment_intents/{parse.quote(payment_intent_id, safe='')}",
            query={"expand[]": "latest_charge"},
        ).payload
        if payload.get("id") != payment_intent_id:
            raise VendorTransportUncertain("stripe_payment_intent_identity_mismatch")
        return payload

    @staticmethod
    def _charge_snapshot(payment_intent: dict[str, Any]) -> tuple[int, int]:
        charge = payment_intent.get("latest_charge")
        if not isinstance(charge, dict):
            raise ToolExecutionError("stripe_charge_not_available", "stripe_charge_not_available")
        captured = int(charge.get("amount_captured", charge.get("amount", 0)))
        refunded = int(charge.get("amount_refunded", 0))
        if captured < 0 or refunded < 0 or refunded > captured:
            raise VendorTransportUncertain("stripe_charge_amounts_invalid")
        return captured, refunded

    def get_refund_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payment_intent = self._payment_intent(str(arguments["order_id"]))
        captured, refunded = self._charge_snapshot(payment_intent)
        return {
            "order_id": payment_intent["id"],
            "customer_id": str(payment_intent.get("customer") or "stripe-test-customer"),
            "total_minor": captured,
            "refundable_minor": captured - refunded,
            "currency": str(payment_intent.get("currency", "")).upper(),
            "status": "DELIVERED" if payment_intent.get("status") == "succeeded" else "NOT_ELIGIBLE",
            # Stripe has no order version. The monotonic refunded amount is the frozen-write guard.
            "version": refunded,
        }

    def issue_refund(self, arguments: dict[str, Any], *, tenant_id: str, operation_id: str) -> dict[str, Any]:
        amount = int(arguments["amount_minor"])
        if amount <= 0:
            raise ToolExecutionError("invalid_amount", "invalid_amount")
        context = self.get_refund_context({"order_id": arguments["order_id"]})
        if context["status"] != "DELIVERED":
            raise ToolExecutionError("stripe_payment_not_refundable", "stripe_payment_not_refundable")
        if context["currency"] != str(arguments["currency"]).upper():
            raise ToolExecutionError("currency_mismatch", "currency_mismatch")
        if int(arguments["expected_order_version"]) != int(context["version"]):
            raise ToolExecutionError("frozen_plan_stale", "frozen_plan_stale")
        if amount > int(context["refundable_minor"]):
            raise ToolExecutionError("refund_exceeds_provider_balance", "refund_exceeds_provider_balance")
        payload = self.http.call(
            "POST",
            "/v1/refunds",
            form_body={
                "payment_intent": str(arguments["order_id"]),
                "amount": amount,
                "metadata[proofmesh_operation_id]": operation_id,
                "metadata[proofmesh_workflow_id]": str(arguments["workflow_id"]),
                "metadata[proofmesh_ticket_id]": str(arguments["ticket_id"]),
                "metadata[proofmesh_tenant_id]": tenant_id,
            },
            idempotency_key=operation_id,
        ).payload
        return self._validated_refund_result(payload, arguments, tenant_id=tenant_id, operation_id=operation_id)

    @staticmethod
    def _validated_refund_result(
        payload: dict[str, Any],
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
        idempotent_replay: bool = False,
    ) -> dict[str, Any]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        expected = {
            "proofmesh_operation_id": operation_id,
            "proofmesh_workflow_id": str(arguments["workflow_id"]),
            "proofmesh_ticket_id": str(arguments["ticket_id"]),
            "proofmesh_tenant_id": tenant_id,
        }
        if (
            not str(payload.get("id", "")).startswith("re_")
            or payload.get("payment_intent") != arguments["order_id"]
            or int(payload.get("amount", -1)) != int(arguments["amount_minor"])
            or str(payload.get("currency", "")).upper() != str(arguments["currency"]).upper()
            or any(str(metadata.get(key, "")) != value for key, value in expected.items())
        ):
            raise VendorTransportUncertain("stripe_refund_contract_mismatch")
        provider_status = str(payload.get("status", ""))
        if provider_status == "succeeded":
            return {
                "refund_id": payload["id"],
                "workflow_id": arguments["workflow_id"],
                "ticket_id": arguments["ticket_id"],
                "order_id": arguments["order_id"],
                "amount_minor": int(payload["amount"]),
                "currency": str(payload["currency"]).upper(),
                "status": "ISSUED",
                "provider_status": provider_status,
                "idempotent_replay": idempotent_replay,
            }
        if provider_status in {"failed", "canceled"}:
            raise ToolExecutionError("stripe_refund_rejected", "stripe_refund_rejected")
        # Pending refund rails are not declared successful before provider settlement.
        raise VendorTransportUncertain("stripe_refund_pending")

    def find_refund(
        self,
        *,
        payment_intent_id: str,
        workflow_id: str,
        tenant_id: str,
        operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        payload = self.http.call(
            "GET",
            "/v1/refunds",
            query={"payment_intent": payment_intent_id, "limit": 100},
        ).payload
        data = payload.get("data")
        if not isinstance(data, list):
            raise VendorTransportUncertain("stripe_refund_list_contract_invalid")
        matches = []
        for item in data:
            metadata = item.get("metadata") if isinstance(item, dict) and isinstance(item.get("metadata"), dict) else {}
            if (
                metadata.get("proofmesh_workflow_id") == workflow_id
                and metadata.get("proofmesh_tenant_id") == tenant_id
                and (
                    operation_id is None
                    or metadata.get("proofmesh_operation_id") == operation_id
                )
            ):
                matches.append(item)
        if not matches:
            return None
        if len(matches) != 1:
            raise VendorTransportUncertain("stripe_multiple_workflow_refunds")
        return matches[0]

    def reconcile_issue(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        try:
            refund = self.find_refund(
                payment_intent_id=str(arguments["order_id"]),
                workflow_id=str(arguments["workflow_id"]),
                tenant_id=tenant_id,
                operation_id=operation_id,
            )
            if refund is None:
                return ReconciliationResult("SAFE_TO_RETRY")
            result = self._validated_refund_result(
                refund,
                arguments,
                tenant_id=tenant_id,
                operation_id=operation_id,
                idempotent_replay=True,
            )
            return ReconciliationResult("SUCCEEDED", result=result)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return ReconciliationResult("UNKNOWN", reason=f"stripe_reconcile:{code}")


class HubSpotDeveloperTicketBackend:
    """HubSpot developer test-account Ticket boundary using whitelisted custom properties."""

    PROPERTY_ORDER_ID = "proofmesh_order_id"
    PROPERTY_CUSTOMER_ID = "proofmesh_customer_id"
    PROPERTY_AMOUNT = "proofmesh_requested_amount_minor"
    PROPERTY_CURRENCY = "proofmesh_currency"
    PROPERTY_REASON_CODE = "proofmesh_reason_code"
    PROPERTY_OPERATION_ID = "proofmesh_operation_id"
    PROPERTY_WORKFLOW_ID = "proofmesh_workflow_id"
    PROPERTY_REFUND_ID = "proofmesh_refund_id"
    PROPERTY_TENANT_ID = "proofmesh_tenant_id"
    PROPERTY_RESOLUTION_DIGEST = "proofmesh_resolution_digest"

    def __init__(self, config: VendorSandboxConfig):
        self.config = config
        self.http = _VendorHttpClient(
            base_url=config.hubspot_base_url,
            token=config.hubspot_access_token,
            timeout_seconds=config.timeout_seconds,
            vendor="hubspot",
        )
        all_specs = CommerceSandbox._build_specs()
        names = {"crm.get_ticket", "crm.close_ticket"}
        self.specs = {name: spec for name, spec in all_specs.items() if name in names}

    @classmethod
    def property_names(cls) -> tuple[str, ...]:
        return (
            "hs_pipeline_stage",
            cls.PROPERTY_ORDER_ID,
            cls.PROPERTY_CUSTOMER_ID,
            cls.PROPERTY_AMOUNT,
            cls.PROPERTY_CURRENCY,
            cls.PROPERTY_REASON_CODE,
            cls.PROPERTY_OPERATION_ID,
            cls.PROPERTY_WORKFLOW_ID,
            cls.PROPERTY_REFUND_ID,
            cls.PROPERTY_TENANT_ID,
            cls.PROPERTY_RESOLUTION_DIGEST,
        )

    def raw_ticket(self, ticket_id: str) -> dict[str, Any]:
        if not ticket_id.isdigit():
            raise ToolExecutionError("hubspot_numeric_ticket_id_required", "hubspot_numeric_ticket_id_required")
        payload = self.http.call(
            "GET",
            f"/crm/v3/objects/tickets/{parse.quote(ticket_id, safe='')}",
            query={"properties": ",".join(self.property_names()), "archived": "false"},
        ).payload
        if str(payload.get("id")) != ticket_id or not isinstance(payload.get("properties"), dict):
            raise VendorTransportUncertain("hubspot_ticket_contract_mismatch")
        return payload

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        raw = self.raw_ticket(ticket_id)
        properties = raw["properties"]
        tenant = str(properties.get(self.PROPERTY_TENANT_ID) or "")
        if tenant != self.config.tenant_id:
            raise ToolExecutionError("hubspot_tenant_binding_mismatch", "hubspot_tenant_binding_mismatch")
        try:
            amount = int(properties[self.PROPERTY_AMOUNT])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolExecutionError("hubspot_ticket_contract_incomplete", "hubspot_ticket_contract_incomplete") from exc
        stage = str(properties.get("hs_pipeline_stage") or "")
        if stage not in {self.config.hubspot_open_stage_id, self.config.hubspot_closed_stage_id}:
            raise ToolExecutionError("hubspot_ticket_stage_not_actionable", "hubspot_ticket_stage_not_actionable")
        closed = stage == self.config.hubspot_closed_stage_id
        return {
            "ticket_id": str(raw["id"]),
            "customer_id": str(properties.get(self.PROPERTY_CUSTOMER_ID) or "unlinked"),
            "order_id": str(properties.get(self.PROPERTY_ORDER_ID) or ""),
            "requested_amount_minor": amount,
            # A controlled reason code is returned; HubSpot free text is never copied into agent context.
            "reason": str(properties.get(self.PROPERTY_REASON_CODE) or "unspecified"),
            "status": "CLOSED" if closed else "OPEN",
            "closed_workflow_id": properties.get(self.PROPERTY_WORKFLOW_ID),
        }

    def close_ticket(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
        refund_id: str,
    ) -> dict[str, Any]:
        current = self.raw_ticket(str(arguments["ticket_id"]))
        props = current["properties"]
        if str(props.get(self.PROPERTY_TENANT_ID) or "") != tenant_id:
            raise ToolExecutionError("hubspot_tenant_binding_mismatch", "hubspot_tenant_binding_mismatch")
        stage = str(props.get("hs_pipeline_stage") or "")
        prior_operation = str(props.get(self.PROPERTY_OPERATION_ID) or "")
        prior_workflow = str(props.get(self.PROPERTY_WORKFLOW_ID) or "")
        if stage == self.config.hubspot_closed_stage_id:
            if prior_operation == operation_id and prior_workflow == arguments["workflow_id"]:
                return {
                    "ticket_id": arguments["ticket_id"],
                    "status": "CLOSED",
                    "refund_id": str(props.get(self.PROPERTY_REFUND_ID) or refund_id),
                    "idempotent_replay": True,
                }
            raise ToolExecutionError("hubspot_ticket_state_conflict", "hubspot_ticket_state_conflict")
        if stage != self.config.hubspot_open_stage_id:
            raise ToolExecutionError("hubspot_ticket_stage_not_actionable", "hubspot_ticket_stage_not_actionable")
        if prior_operation and prior_operation != operation_id:
            raise ToolExecutionError("hubspot_ticket_state_conflict", "hubspot_ticket_state_conflict")
        resolution_digest = hashlib.sha256(str(arguments["resolution"]).encode("utf-8")).hexdigest()
        payload = self.http.call(
            "PATCH",
            f"/crm/v3/objects/tickets/{parse.quote(str(arguments['ticket_id']), safe='')}",
            json_body={
                "properties": {
                    "hs_pipeline_stage": self.config.hubspot_closed_stage_id,
                    self.PROPERTY_OPERATION_ID: operation_id,
                    self.PROPERTY_WORKFLOW_ID: str(arguments["workflow_id"]),
                    self.PROPERTY_REFUND_ID: refund_id,
                    self.PROPERTY_TENANT_ID: tenant_id,
                    # Store only a digest, not potentially sensitive resolution prose.
                    self.PROPERTY_RESOLUTION_DIGEST: resolution_digest,
                }
            },
        ).payload
        updated = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
        if (
            str(payload.get("id")) != str(arguments["ticket_id"])
            or str(updated.get("hs_pipeline_stage")) != self.config.hubspot_closed_stage_id
            or str(updated.get(self.PROPERTY_OPERATION_ID)) != operation_id
            or str(updated.get(self.PROPERTY_WORKFLOW_ID)) != str(arguments["workflow_id"])
        ):
            raise VendorTransportUncertain("hubspot_close_contract_mismatch")
        return {
            "ticket_id": str(arguments["ticket_id"]),
            "status": "CLOSED",
            "refund_id": refund_id,
            "idempotent_replay": False,
        }

    def reconcile_close(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        try:
            raw = self.raw_ticket(str(arguments["ticket_id"]))
            props = raw["properties"]
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return ReconciliationResult("UNKNOWN", reason=f"hubspot_reconcile:{code}")
        if str(props.get(self.PROPERTY_TENANT_ID) or "") != tenant_id:
            return ReconciliationResult("UNKNOWN", reason="hubspot_reconcile:tenant_mismatch")
        stage = str(props.get("hs_pipeline_stage") or "")
        prior_operation = str(props.get(self.PROPERTY_OPERATION_ID) or "")
        prior_workflow = str(props.get(self.PROPERTY_WORKFLOW_ID) or "")
        if (
            stage == self.config.hubspot_closed_stage_id
            and prior_operation == operation_id
            and prior_workflow == str(arguments["workflow_id"])
        ):
            return ReconciliationResult(
                "SUCCEEDED",
                result={
                    "ticket_id": str(arguments["ticket_id"]),
                    "status": "CLOSED",
                    "refund_id": str(props.get(self.PROPERTY_REFUND_ID) or ""),
                    "idempotent_replay": True,
                },
            )
        if stage == self.config.hubspot_open_stage_id and not prior_operation and not prior_workflow:
            return ReconciliationResult("SAFE_TO_RETRY")
        return ReconciliationResult("UNKNOWN", reason="hubspot_reconcile:state_conflict")


class StripeHubSpotSandboxBackend:
    """A real-protocol ToolBackend for Stripe test mode and a HubSpot developer test account.

    This class is provider-ready but does not itself prove that any external account has been
    exercised. It is intentionally single-tenant to prevent credential/tenant confusion.
    """

    PROVENANCE = {
        "provider_protocols": ["stripe-v1", "hubspot-crm-v3"],
        "requires_external_test_accounts": True,
        "real_provider_run_claimed": False,
        "customer_data_claimed": False,
        "production_money_allowed": False,
    }

    def __init__(self, config: VendorSandboxConfig):
        self.config = config
        self.stripe = StripeTestRefundBackend(config)
        self.hubspot = HubSpotDeveloperTicketBackend(config)
        self.specs: dict[str, ToolSpec] = {**self.stripe.specs, **self.hubspot.specs}

    @classmethod
    def from_env(cls) -> "StripeHubSpotSandboxBackend":
        return cls(VendorSandboxConfig.from_env())

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
                    "proofmesh/backend": "stripe-test+hubspot-developer-test",
                },
            }
            for spec in self.specs.values()
        ]

    def _verified_context(self, arguments: dict[str, Any]) -> tuple[str, str]:
        tenant_id = arguments.get("_proofmesh_tenant_id")
        operation_id = arguments.get("_proofmesh_operation_id")
        if tenant_id != self.config.tenant_id:
            raise ToolExecutionError("vendor_tenant_binding_mismatch", "vendor_tenant_binding_mismatch")
        if not isinstance(operation_id, str) or not operation_id.startswith("op-"):
            raise ToolExecutionError("operation_context_missing", "operation_context_missing")
        return tenant_id, operation_id

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in self.specs:
            raise ToolExecutionError("tool_not_found", "tool_not_found")
        tenant_id, operation_id = self._verified_context(arguments)
        clean = {key: value for key, value in arguments.items() if not key.startswith("_proofmesh_")}
        if tool == "orders.get_refund_context":
            return self.stripe.get_refund_context(clean)
        if tool == "payments.issue_refund":
            return self.stripe.issue_refund(clean, tenant_id=tenant_id, operation_id=operation_id)
        if tool == "payments.compensate_refund":
            # Stripe refunds are not a reversible transaction. Never fabricate compensation.
            raise ToolExecutionError("stripe_refund_irreversible_manual_remediation", "stripe_refund_irreversible_manual_remediation")
        if tool == "crm.get_ticket":
            return self.hubspot.get_ticket(str(clean["ticket_id"]))
        if tool == "crm.close_ticket":
            ticket = self.hubspot.get_ticket(str(clean["ticket_id"]))
            refund = self.stripe.find_refund(
                payment_intent_id=str(ticket["order_id"]),
                workflow_id=str(clean["workflow_id"]),
                tenant_id=tenant_id,
            )
            if refund is None or refund.get("status") != "succeeded":
                raise ToolExecutionError("refund_not_verified", "refund_not_verified")
            return self.hubspot.close_ticket(
                clean,
                tenant_id=tenant_id,
                operation_id=operation_id,
                refund_id=str(refund["id"]),
            )
        raise ToolExecutionError("tool_not_found", "tool_not_found")

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        if tenant_id != self.config.tenant_id:
            return ReconciliationResult("UNKNOWN", reason="vendor_tenant_binding_mismatch")
        if tool in {"orders.get_refund_context", "crm.get_ticket"}:
            return ReconciliationResult("SAFE_TO_RETRY")
        if tool == "payments.issue_refund":
            return self.stripe.reconcile_issue(arguments, tenant_id=tenant_id, operation_id=operation_id)
        if tool == "crm.close_ticket":
            return self.hubspot.reconcile_close(arguments, tenant_id=tenant_id, operation_id=operation_id)
        return ReconciliationResult("UNKNOWN", reason="provider_action_irreversible_or_unsupported")


def vendor_readiness_report(
    *,
    probe_network: bool = False,
    config_factory: Callable[[], VendorSandboxConfig] = VendorSandboxConfig.from_env,
) -> dict[str, Any]:
    """Return only booleans and non-sensitive provider account identifiers."""

    configured = {
        "tenant_id_set": bool(os.getenv("PROOFMESH_VENDOR_TENANT_ID")),
        "stripe_test_key_set": os.getenv("PROOFMESH_STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_")),
        "hubspot_access_token_set": bool(os.getenv("PROOFMESH_HUBSPOT_ACCESS_TOKEN")),
        "hubspot_open_stage_id_set": bool(os.getenv("PROOFMESH_HUBSPOT_OPEN_STAGE_ID")),
        "hubspot_closed_stage_id_set": bool(os.getenv("PROOFMESH_HUBSPOT_CLOSED_STAGE_ID")),
    }
    report: dict[str, Any] = {
        "schema_version": "proofmesh.vendor-readiness/v1",
        "configuration": configured,
        "network_probe_requested": probe_network,
        "providers": {
            "stripe": {"ok": False, "account_id": None},
            "hubspot": {"ok": False, "account_id": None},
        },
        "ready": False,
        "claim_boundary": "configuration/readiness only; not a production or customer-pilot claim",
    }
    if not probe_network or not all(configured.values()):
        return report
    try:
        config = config_factory()
    except VendorConfigurationError:
        return report
    stripe = _VendorHttpClient(
        base_url=config.stripe_base_url,
        token=config.stripe_secret_key,
        timeout_seconds=config.timeout_seconds,
        vendor="stripe",
    )
    hubspot = _VendorHttpClient(
        base_url=config.hubspot_base_url,
        token=config.hubspot_access_token,
        timeout_seconds=config.timeout_seconds,
        vendor="hubspot",
    )
    try:
        account = stripe.call("GET", "/v1/account").payload
        account_id = str(account.get("id") or "")
        if account_id.startswith("acct_"):
            report["providers"]["stripe"] = {"ok": True, "account_id": account_id}
    except Exception:
        pass
    try:
        account = hubspot.call("GET", "/account-info/v3/details").payload
        account_id = str(account.get("portalId") or account.get("hubId") or "")
        if account_id.isdigit():
            report["providers"]["hubspot"] = {"ok": True, "account_id": account_id}
    except Exception:
        pass
    report["ready"] = bool(report["providers"]["stripe"]["ok"] and report["providers"]["hubspot"]["ok"])
    return report
