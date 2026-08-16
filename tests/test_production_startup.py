import json
from pathlib import Path

import pytest

from proofmesh.capabilities import (
    BUSINESS_ATTESTATION_TYPE,
    PROOF_TYPE,
    RECEIPT_TYPE,
    TASK_RECEIPT_TYPE,
    TOKEN_TYPE,
    PassportError,
)
from proofmesh.runtime import build_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_production_startup_rejects_file_signers_and_sqlite(proofmesh_home, monkeypatch):
    build_runtime(proofmesh_home)
    bundle = json.loads((proofmesh_home / "config/trust/action-issuers.json").read_text(encoding="utf-8"))
    assert {tuple(entry["token_types"]) for entry in bundle["keys"].values()} == {
        (TOKEN_TYPE,),
        (RECEIPT_TYPE,),
        (TASK_RECEIPT_TYPE,),
        (PROOF_TYPE,),
        (BUSINESS_ATTESTATION_TYPE,),
    }

    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.setenv("PROOFMESH_SIGNER_MODE", "file")
    with pytest.raises(PassportError) as rejected:
        build_runtime(proofmesh_home)
    assert rejected.value.reason == "file_signer_forbidden_in_production"


def test_production_startup_requires_remote_signer_configuration(proofmesh_home, monkeypatch):
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    with pytest.raises(PassportError) as rejected:
        build_runtime(proofmesh_home)
    assert rejected.value.reason == "remote_signer_configuration_missing"


def test_production_startup_reaches_postgres_gate_only_after_remote_signer_gate(
    proofmesh_home, monkeypatch
):
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.setenv("PROOFMESH_SIGNER_MODE", "file")
    with pytest.raises(PassportError, match="file_signer_forbidden_in_production"):
        build_runtime(proofmesh_home)


def test_production_selects_five_remote_purpose_signers_and_postgres(
    proofmesh_home, monkeypatch
):
    build_runtime(proofmesh_home)
    signer_calls = []

    class RemoteSigner:
        def __init__(self, **kwargs):
            signer_calls.append(kwargs)
            self.key_id = kwargs["key_id"]
            self.issuer = kwargs["issuer"]

        def sign_payload(self, _payload, *, token_type):
            raise AssertionError(f"startup must not sign {token_type}")

        def sign_passport(self, _claims):
            raise AssertionError("startup must not sign passports")

    class PostgresStore:
        def __init__(self, dsn):
            self.dsn = dsn

    monkeypatch.setattr("proofmesh.runtime.RemoteEd25519Signer", RemoteSigner)
    monkeypatch.setattr("proofmesh.runtime.PostgresGatewayStore", PostgresStore)
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.setenv("PROOFMESH_SIGNER_BASE_URL", "https://signer.example.test")
    monkeypatch.setenv("PROOFMESH_SIGNER_CA_BUNDLE", "/run/ca.pem")
    monkeypatch.setenv("PROOFMESH_SIGNER_CLIENT_CERT", "/run/client.pem")
    monkeypatch.setenv("PROOFMESH_SIGNER_CLIENT_KEY", "/run/client-key.pem")
    monkeypatch.setenv("PROOFMESH_DATABASE_URL", "postgresql://db/proofmesh")

    runtime = build_runtime(proofmesh_home)

    assert len(signer_calls) == 5
    assert len({next(iter(call["allowed_token_types"])) for call in signer_calls}) == 5
    assert len({call["key_id"] for call in signer_calls}) == 5
    assert runtime.gateway_store.dsn == "postgresql://db/proofmesh"


def test_production_container_installs_postgres_driver_and_neutralizes_static_tokens():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    production_requirements = (ROOT / "requirements-production.txt").read_text(encoding="utf-8")
    production_lock = (ROOT / "requirements-production.lock").read_text(encoding="utf-8")
    production_compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert "PROOFMESH_INSTALL_PROFILE=development" in dockerfile
    assert "requirements-production.txt" in dockerfile
    assert "--require-hashes -r requirements-production.lock" in dockerfile
    assert "psycopg[binary]==3.2.9" in production_requirements
    assert "psycopg==3.2.9" in production_lock
    assert "psycopg-binary==3.2.9" in production_lock
    assert "PROOFMESH_INSTALL_PROFILE: production" in production_compose
    for role in (
        "ORCHESTRATOR",
        "APPROVER",
        "AUDITOR",
        "INTAKE",
        "INVESTIGATOR",
        "POLICY",
        "EXECUTOR",
        "VERIFIER",
        "MEMORY",
        "GATEWAY",
    ):
        assert f'PROOFMESH_{role}_TOKEN: ""' in production_compose
