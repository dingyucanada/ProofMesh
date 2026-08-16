from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from .business import CommerceSandbox
from .capabilities import (
    BUSINESS_ATTESTATION_TYPE,
    JwsSigner,
    PROOF_TYPE,
    RECEIPT_TYPE,
    TASK_RECEIPT_TYPE,
    TOKEN_TYPE,
    ExternalTrustVerifier,
    PassportError,
    RemoteEd25519Signer,
    load_or_create_signer,
    sha256_digest,
)
from .gateway import ActionGateway, GatewayStore
from .postgres_store import PostgresGatewayStore
from .workflow import RefundControlPlane


@dataclass(frozen=True)
class ProofMeshRuntime:
    home: Path
    trust_bundle_path: Path
    passport_signer: JwsSigner
    receipt_signer: JwsSigner
    task_signer: JwsSigner
    proof_signer: JwsSigner
    business_attestation_signer: JwsSigner
    verifier: ExternalTrustVerifier
    sandbox: CommerceSandbox
    gateway_store: GatewayStore | PostgresGatewayStore
    gateway: ActionGateway
    control_plane: RefundControlPlane


def build_runtime(home: str | Path) -> ProofMeshRuntime:
    home = Path(home).resolve()
    key_dir = home / "var" / "keys"
    trust_bundle_path = home / "config" / "trust" / "action-issuers.json"
    production_mode = os.getenv("PROOFMESH_ENV", "development").lower() == "production"

    def signer(*, filename: str, key_id: str, issuer: str, token_type: str) -> JwsSigner:
        signer_mode = os.getenv("PROOFMESH_SIGNER_MODE", "remote" if production_mode else "file").lower()
        if signer_mode == "file":
            if production_mode:
                raise PassportError("file_signer_forbidden_in_production")
            return load_or_create_signer(
                private_key_path=key_dir / filename,
                trust_bundle_path=trust_bundle_path,
                key_id=key_id,
                issuer=issuer,
                allow_bootstrap=True,
                token_types=[token_type],
            )
        if signer_mode != "remote":
            raise PassportError("signer_mode_invalid")
        base_url = os.getenv("PROOFMESH_SIGNER_BASE_URL", "").rstrip("/")
        required_paths = {
            "ca_bundle_path": os.getenv("PROOFMESH_SIGNER_CA_BUNDLE", ""),
            "client_certificate_path": os.getenv("PROOFMESH_SIGNER_CLIENT_CERT", ""),
            "client_key_path": os.getenv("PROOFMESH_SIGNER_CLIENT_KEY", ""),
        }
        if not base_url or any(not value for value in required_paths.values()):
            raise PassportError("remote_signer_configuration_missing")
        return RemoteEd25519Signer(
            endpoint=f"{base_url}/v1/sign/{key_id}",
            trust_bundle_path=trust_bundle_path,
            key_id=key_id,
            issuer=issuer,
            allowed_token_types=frozenset({token_type}),
            **required_paths,
        )

    passport_signer = signer(
        filename="policy-issuer.ed25519",
        key_id="proofmesh-policy-local-v1",
        issuer="proofmesh-policy-control-plane",
        token_type=TOKEN_TYPE,
    )
    receipt_signer = signer(
        filename="gateway-receipt.ed25519",
        key_id="proofmesh-gateway-local-v1",
        issuer="proofmesh-action-gateway",
        token_type=RECEIPT_TYPE,
    )
    task_signer = signer(
        filename="workflow-task-receipt.ed25519",
        key_id="proofmesh-workflow-task-local-v1",
        issuer="proofmesh-workflow-control-plane",
        token_type=TASK_RECEIPT_TYPE,
    )
    proof_signer = signer(
        filename="workflow-proof-seal.ed25519",
        key_id="proofmesh-proof-seal-local-v1",
        issuer="proofmesh-proof-sealer",
        token_type=PROOF_TYPE,
    )
    business_attestation_signer = signer(
        filename="commerce-snapshot-attestation.ed25519",
        key_id="proofmesh-commerce-snapshot-local-v1",
        issuer="proofmesh-commerce-sandbox",
        token_type=BUSINESS_ATTESTATION_TYPE,
    )
    verifier = ExternalTrustVerifier(trust_bundle_path)
    sandbox = CommerceSandbox(
        home / "var" / "commerce-sandbox.db",
        attestation_signer=business_attestation_signer,
    )
    store_mode = os.getenv("PROOFMESH_GATEWAY_STORE", "postgres" if production_mode else "sqlite").lower()
    if store_mode == "postgres":
        database_url = os.getenv("PROOFMESH_DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("PROOFMESH_DATABASE_URL is required for the PostgreSQL gateway store")
        gateway_store = PostgresGatewayStore(database_url)
    elif store_mode == "sqlite" and not production_mode:
        gateway_store = GatewayStore(home / "var" / "action-gateway.db")
    elif store_mode == "sqlite":
        raise RuntimeError("SQLite gateway store is forbidden in production")
    else:
        raise RuntimeError(f"unsupported gateway store: {store_mode}")
    pinned_policy = json.loads((home / "data/policies/refund_policy.json").read_text(encoding="utf-8"))
    gateway = ActionGateway(
        verifier=verifier,
        receipt_signer=receipt_signer,
        store=gateway_store,
        upstream=sandbox,
        auto_approve_minor=int(pinned_policy["limits"]["auto_approve_minor"]),
        pinned_policy_digest=sha256_digest(pinned_policy),
    )
    control_plane = RefundControlPlane(
        home=home,
        gateway=gateway,
        passport_signer=passport_signer,
        task_signer=task_signer,
        proof_signer=proof_signer,
        sandbox=sandbox,
        approval_verifier=verifier,
    )
    return ProofMeshRuntime(
        home=home,
        trust_bundle_path=trust_bundle_path,
        passport_signer=passport_signer,
        receipt_signer=receipt_signer,
        task_signer=task_signer,
        proof_signer=proof_signer,
        business_attestation_signer=business_attestation_signer,
        verifier=verifier,
        sandbox=sandbox,
        gateway_store=gateway_store,
        gateway=gateway,
        control_plane=control_plane,
    )
