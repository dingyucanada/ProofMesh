from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .capabilities import sha256_digest
from .domain_protocol import CompositeToolBackend
from .gateway import ActionGateway
from .operations import OperationsChangeControlPlane, OperationsSandbox
from .runtime import ProofMeshRuntime, build_runtime


@dataclass(frozen=True)
class MultiDomainRuntime:
    """Development/reference composition of refund and operations adapters."""

    base: ProofMeshRuntime
    gateway: ActionGateway
    operations_sandbox: OperationsSandbox
    operations_control_plane: OperationsChangeControlPlane


def build_multidomain_runtime(
    home: str | Path,
    *,
    enable_reference_fault_injection: bool = False,
) -> MultiDomainRuntime:
    home = Path(home).resolve()
    base = build_runtime(home)
    operations_sandbox = OperationsSandbox(
        home / "var/operations-sandbox.db",
        attestation_signer=base.business_attestation_signer,
    )
    backend = CompositeToolBackend(base.sandbox, operations_sandbox)
    refund_policy = json.loads((home / "data/policies/refund_policy.json").read_text(encoding="utf-8"))
    operations_policy = json.loads(
        (home / "data/policies/operations_change_policy.json").read_text(encoding="utf-8")
    )
    gateway = ActionGateway(
        verifier=base.verifier,
        receipt_signer=base.receipt_signer,
        store=base.gateway_store,
        upstream=backend,
        pinned_policy_limits={
            sha256_digest(refund_policy): int(refund_policy["limits"]["auto_approve_minor"]),
            sha256_digest(operations_policy): int(
                operations_policy["limits"]["auto_approve_risk_units"]
            ),
        },
    )
    control = OperationsChangeControlPlane(
        home=home,
        gateway=gateway,
        passport_signer=base.passport_signer,
        proof_signer=base.proof_signer,
        sandbox=operations_sandbox,
        approval_verifier=base.verifier,
        reference_fault_injection=enable_reference_fault_injection,
    )
    return MultiDomainRuntime(
        base=base,
        gateway=gateway,
        operations_sandbox=operations_sandbox,
        operations_control_plane=control,
    )
