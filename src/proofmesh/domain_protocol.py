from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .capabilities import ActionPassportClaims, JwsSigner, sha256_digest
from .gateway import ActionGateway


class CompositeToolBackend:
    """Expose multiple domain adapters through one policy-enforcing Action Gateway.

    Tool names must be globally unique.  This deliberately keeps routing outside
    model output: a verified tool name selects the adapter after Passport checks.
    """

    def __init__(self, *backends: Any):
        self._routes: dict[str, Any] = {}
        self.specs: dict[str, Any] = {}
        for backend in backends:
            for name, spec in backend.specs.items():
                if name in self._routes:
                    raise ValueError(f"duplicate composite tool: {name}")
                self._routes[name] = backend
                self.specs[name] = spec

    def list_tools(self) -> list[dict[str, Any]]:
        return [item for backend in dict.fromkeys(self._routes.values()) for item in backend.list_tools()]

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._routes[tool].call(tool, arguments)

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> Any:
        return self._routes[tool].reconcile(
            tool,
            arguments,
            tenant_id=tenant_id,
            operation_id=operation_id,
        )


class AuthorizedToolCaller:
    """Domain-neutral Action Passport mint/call/receipt binding.

    Both the refund and operations-change workflows use this exact path.  Domain
    code supplies policy, plan and business semantics; it cannot bypass Gateway
    verification or write a synthetic receipt itself.
    """

    def __init__(self, *, gateway: ActionGateway, passport_signer: JwsSigner):
        self.gateway = gateway
        self.passport_signer = passport_signer

    def call(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        subject: str,
        context_digest: str,
        policy_digest: str,
        plan_digest: str | None,
        state: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        resource: str,
        scopes: list[str],
        mode: str,
        approval_digest: str,
        approval_assertion: str | None = None,
        max_amount_minor: int = 0,
        budget_limit_minor: int = 0,
        currency: str = "XTS",
        role: str = "worker",
        event_callback: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        claims = ActionPassportClaims.issue(
            issuer=self.passport_signer.issuer,
            audience=self.gateway.audience,
            tenant_id=tenant_id,
            subject=subject,
            workflow_id=workflow_id,
            tool=tool,
            resource=str(resource),
            scopes=scopes,
            arguments=arguments,
            context_digest=context_digest,
            policy_digest=policy_digest,
            plan_digest=plan_digest,
            approval_digest=approval_digest,
            mode=mode,
            max_calls=1,
            max_amount_minor=max_amount_minor,
            budget_limit_minor=budget_limit_minor,
            currency=currency,
            ttl_seconds=45,
        )
        output = self.gateway.call_tool(
            tool=tool,
            arguments=arguments,
            passport=self.passport_signer.sign_passport(claims),
            workflow_id=workflow_id,
            context_digest=context_digest,
            idempotency_key=f"{tenant_id}:{workflow_id}:{tool}:{sha256_digest(arguments)[:20]}",
            approval_assertion=approval_assertion,
        )
        state.setdefault("receipts", []).append(output["receipt"])
        state.setdefault("tool_results", {})[tool] = output["result"]
        state.setdefault("tool_executions", []).append(
            {
                "tool": tool,
                "arguments": arguments,
                "result": output["result"],
                "receipt_call_id": output["receipt"]["call_id"],
                "decision": output["receipt"]["decision"],
                "actor": subject,
                "role": role,
            }
        )
        if event_callback is not None:
            event_callback(
                "tool.call.completed",
                subject,
                {
                    "tool": tool,
                    "call_id": output["receipt"]["call_id"],
                    "receipt_digest": sha256_digest(output["receipt"]),
                    "idempotent_replay": output["idempotent_replay"],
                    "role": role,
                },
            )
        return output
