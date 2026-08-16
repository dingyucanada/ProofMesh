from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from proofmesh.auth import (
    AuthenticationError,
    AuthenticationUnavailable,
    OidcAuthenticator,
    build_authenticator_from_environment,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _fixture(tmp_path, **overrides):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    jwks = tmp_path / "jwks.json"
    jwks.write_text(
        json.dumps({"keys": [{"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "kid": "idp-1", "x": _b64(public)}]}),
        encoding="utf-8",
    )
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.test",
        "aud": "proofmesh-api",
        "sub": "worker-42",
        "iat": now - 1,
        "nbf": now - 1,
        "exp": now + 299,
        "roles": ["executor", "read"],
        "tenant_ids": ["acme-cn"],
        **overrides,
    }
    header = {"alg": "EdDSA", "kid": "idp-1", "typ": "at+jwt"}
    encoded_header = _b64(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(private.sign(f"{encoded_header}.{encoded_payload}".encode("ascii")))
    token = f"{encoded_header}.{encoded_payload}.{signature}"
    auth = OidcAuthenticator(
        issuer="https://idp.example.test",
        audience="proofmesh-api",
        jwks_uri=jwks.as_uri(),
    )
    return auth, token


def test_oidc_verifies_signature_issuer_audience_role_and_tenant(tmp_path):
    auth, token = _fixture(tmp_path)
    principal = auth.authenticate(f"Bearer {token}", required_role="executor")
    assert principal.subject == "worker-42"
    assert principal.roles == frozenset({"executor", "read"})
    assert principal.tenant_ids == frozenset({"acme-cn"})


@pytest.mark.parametrize(
    "claims,reason",
    [
        ({"aud": "another-api"}, "audience mismatch"),
        ({"roles": ["read"]}, "required role"),
        ({"tenant_ids": []}, "tenant scope is empty"),
        ({"exp": int(time.time()) - 60}, "expired"),
    ],
)
def test_oidc_fails_closed_on_claim_drift(tmp_path, claims, reason):
    auth, token = _fixture(tmp_path, **claims)
    with pytest.raises(AuthenticationError, match=reason):
        auth.authenticate(f"Bearer {token}", required_role="executor")


def test_oidc_rejects_tampered_payload(tmp_path):
    auth, token = _fixture(tmp_path)
    header, payload, signature = token.split(".")
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(AuthenticationError):
        auth.authenticate(f"Bearer {header}.{tampered}.{signature}", required_role="executor")


def test_production_auth_forbids_static_tokens(monkeypatch):
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.setenv("PROOFMESH_AUTH_MODE", "static")
    with pytest.raises(AuthenticationUnavailable, match="forbidden"):
        build_authenticator_from_environment()


def test_production_oidc_requires_complete_configuration(monkeypatch):
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.delenv("PROOFMESH_AUTH_MODE", raising=False)
    with pytest.raises(AuthenticationUnavailable, match="missing OIDC configuration"):
        build_authenticator_from_environment()
