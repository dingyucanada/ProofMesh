from __future__ import annotations

import base64
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.hashes import SHA256


class AuthenticationError(PermissionError):
    pass


class AuthenticationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]
    tenant_ids: frozenset[str] = frozenset({"acme-cn"})

    def allows_tenant(self, tenant_id: str) -> bool:
        return "*" in self.tenant_ids or tenant_id in self.tenant_ids


class Authenticator(Protocol):
    mode: str

    def has_role(self, role: str) -> bool: ...

    def authenticate(self, authorization: str | None, *, required_role: str) -> Principal: ...


class TokenAuthenticator:
    """Fail-closed authentication with one principal per mutually exclusive duty."""

    def __init__(self, principals: list[tuple[str, Principal]]):
        self._principals = [(token, principal) for token, principal in principals if token]
        self.mode = "bearer-rbac-tenant-scoped"

    @classmethod
    def from_environment(cls) -> "TokenAuthenticator":
        def tenant_scope(name: str, default: str = "acme-cn") -> frozenset[str]:
            return frozenset(item.strip() for item in os.getenv(name, default).split(",") if item.strip())

        return cls(
            [
                (
                    os.getenv("PROOFMESH_OPERATOR_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_OPERATOR_SUBJECT", "operator-local"),
                        frozenset({"operator", "read"}),
                        tenant_scope("PROOFMESH_OPERATOR_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_ORCHESTRATOR_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_ORCHESTRATOR_SUBJECT", "case-orchestrator-local"),
                        frozenset({"orchestrator", "read"}),
                        tenant_scope("PROOFMESH_ORCHESTRATOR_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_APPROVER_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_APPROVER_SUBJECT", "approver-local"),
                        frozenset({"approver", "read"}),
                        tenant_scope("PROOFMESH_APPROVER_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_AUDITOR_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_AUDITOR_SUBJECT", "auditor-local"),
                        frozenset({"auditor", "read"}),
                        tenant_scope("PROOFMESH_AUDITOR_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_INTAKE_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_INTAKE_SUBJECT", "ticket-intake-local"),
                        frozenset({"intake", "read"}),
                        tenant_scope("PROOFMESH_INTAKE_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_INVESTIGATOR_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_INVESTIGATOR_SUBJECT", "context-investigator-local"),
                        frozenset({"investigator", "read"}),
                        tenant_scope("PROOFMESH_INVESTIGATOR_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_POLICY_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_POLICY_SUBJECT", "risk-policy-sentinel-local"),
                        frozenset({"policy", "read"}),
                        tenant_scope("PROOFMESH_POLICY_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_EXECUTOR_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_EXECUTOR_SUBJECT", "action-executor-local"),
                        frozenset({"executor", "read"}),
                        tenant_scope("PROOFMESH_EXECUTOR_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_VERIFIER_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_VERIFIER_SUBJECT", "outcome-verifier-local"),
                        frozenset({"verifier", "read"}),
                        tenant_scope("PROOFMESH_VERIFIER_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_MEMORY_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_MEMORY_SUBJECT", "case-memory-curator-local"),
                        frozenset({"memory", "read"}),
                        tenant_scope("PROOFMESH_MEMORY_TENANTS"),
                    ),
                ),
                (
                    os.getenv("PROOFMESH_GATEWAY_TOKEN", ""),
                    Principal(
                        os.getenv("PROOFMESH_GATEWAY_SUBJECT", "proofmesh-gateway-proxy"),
                        frozenset({"gateway"}),
                        tenant_scope("PROOFMESH_GATEWAY_TENANTS", "*"),
                    ),
                ),
            ]
        )

    @property
    def configured(self) -> bool:
        return bool(self._principals)

    def has_role(self, role: str) -> bool:
        return any(role in principal.roles for _, principal in self._principals)

    def authenticate(self, authorization: str | None, *, required_role: str) -> Principal:
        if not self._principals:
            raise AuthenticationUnavailable("state-changing API disabled until bearer tokens are configured")
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer token")
        supplied = authorization[7:]
        for token, principal in self._principals:
            if hmac.compare_digest(supplied, token):
                if required_role not in principal.roles:
                    raise AuthenticationError("principal lacks the required role")
                return principal
        raise AuthenticationError("invalid bearer token")


def _b64url_decode(value: str) -> bytes:
    if not value or "=" in value:
        raise AuthenticationError("OIDC token uses non-canonical base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("malformed OIDC token") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise AuthenticationError("OIDC token uses non-canonical base64url")
    return decoded


class OidcAuthenticator:
    """Strict offline-verifiable OIDC access-token authenticator.

    JWKS may be supplied as an immutable mounted file or an HTTPS endpoint.  Only
    RS256 and EdDSA are accepted; issuer, audience, lifetime, role and tenant
    claims are all checked before a Principal is returned.
    """

    mode = "oidc-jwks"
    DEFAULT_ROLES = frozenset(
        {
            "operator",
            "orchestrator",
            "approver",
            "auditor",
            "intake",
            "investigator",
            "policy",
            "executor",
            "verifier",
            "memory",
            "gateway",
            "read",
        }
    )

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str,
        allowed_roles: frozenset[str] | None = None,
        roles_claim: str = "roles",
        tenants_claim: str = "tenant_ids",
        max_token_age_seconds: int = 900,
        clock_skew_seconds: int = 30,
    ):
        if not issuer.startswith("https://") or not audience:
            raise AuthenticationUnavailable("OIDC issuer must be HTTPS and audience must be non-empty")
        parsed = urlparse(jwks_uri)
        if parsed.scheme not in {"https", "file"}:
            raise AuthenticationUnavailable("OIDC JWKS URI must be HTTPS or file://")
        if parsed.scheme == "https" and (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise AuthenticationUnavailable("OIDC HTTPS JWKS URI is invalid")
        if parsed.scheme == "file" and not Path(parsed.path).is_absolute():
            raise AuthenticationUnavailable("OIDC file JWKS path must be absolute")
        if not 60 <= max_token_age_seconds <= 3600:
            raise AuthenticationUnavailable("OIDC maximum token age must be between 60 and 3600 seconds")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_uri = jwks_uri
        self.allowed_roles = allowed_roles or self.DEFAULT_ROLES
        self.roles_claim = roles_claim
        self.tenants_claim = tenants_claim
        self.max_token_age_seconds = max_token_age_seconds
        self.clock_skew_seconds = clock_skew_seconds

    @classmethod
    def from_environment(cls) -> "OidcAuthenticator":
        required = {
            "issuer": os.getenv("PROOFMESH_OIDC_ISSUER", ""),
            "audience": os.getenv("PROOFMESH_OIDC_AUDIENCE", ""),
            "jwks_uri": os.getenv("PROOFMESH_OIDC_JWKS_URI", ""),
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise AuthenticationUnavailable(f"missing OIDC configuration: {', '.join(missing)}")
        configured_roles = frozenset(
            item.strip()
            for item in os.getenv("PROOFMESH_OIDC_ALLOWED_ROLES", ",".join(sorted(cls.DEFAULT_ROLES))).split(",")
            if item.strip()
        )
        return cls(
            **required,
            allowed_roles=configured_roles,
            roles_claim=os.getenv("PROOFMESH_OIDC_ROLES_CLAIM", "roles"),
            tenants_claim=os.getenv("PROOFMESH_OIDC_TENANTS_CLAIM", "tenant_ids"),
            max_token_age_seconds=int(os.getenv("PROOFMESH_OIDC_MAX_TOKEN_AGE_SECONDS", "900")),
        )

    def has_role(self, role: str) -> bool:
        return role in self.allowed_roles

    def _load_jwks(self) -> dict[str, Any]:
        parsed = urlparse(self.jwks_uri)
        try:
            if parsed.scheme == "file":
                raw = Path(parsed.path).read_bytes()
            else:
                request = Request(self.jwks_uri, headers={"Accept": "application/json"})
                with urlopen(request, timeout=3) as response:  # noqa: S310 - HTTPS validated above
                    if response.status != 200:
                        raise AuthenticationUnavailable("OIDC JWKS endpoint is unavailable")
                    raw = response.read(1_048_577)
                    if len(raw) > 1_048_576:
                        raise AuthenticationUnavailable("OIDC JWKS exceeds one MiB")
            payload = json.loads(raw)
        except AuthenticationUnavailable:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AuthenticationUnavailable("OIDC JWKS cannot be loaded") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise AuthenticationUnavailable("OIDC JWKS schema is invalid")
        return payload

    @staticmethod
    def _verify_signature(jwk: dict[str, Any], alg: str, signing_input: bytes, signature: bytes) -> None:
        try:
            if alg == "RS256" and jwk.get("kty") == "RSA" and jwk.get("alg") in {None, "RS256"}:
                modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
                exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
                RSAPublicNumbers(exponent, modulus).public_key().verify(
                    signature, signing_input, PKCS1v15(), SHA256()
                )
                return
            if (
                alg == "EdDSA"
                and jwk.get("kty") == "OKP"
                and jwk.get("crv") == "Ed25519"
                and jwk.get("alg") in {None, "EdDSA"}
            ):
                Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"])).verify(signature, signing_input)
                return
        except (KeyError, ValueError, InvalidSignature) as exc:
            raise AuthenticationError("invalid OIDC token signature") from exc
        raise AuthenticationError("OIDC key type or algorithm is not allowed")

    def authenticate(self, authorization: str | None, *, required_role: str) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer token")
        token = authorization[7:]
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = json.loads(_b64url_decode(encoded_header))
            claims = json.loads(_b64url_decode(encoded_payload))
            signature = _b64url_decode(encoded_signature)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthenticationError("malformed OIDC token") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthenticationError("malformed OIDC token")
        alg = header.get("alg")
        if alg not in {"RS256", "EdDSA"} or not isinstance(header.get("kid"), str):
            raise AuthenticationError("OIDC algorithm or key id is not allowed")
        if header.get("typ") not in {None, "JWT", "at+jwt"}:
            raise AuthenticationError("OIDC token type is not allowed")
        keys = [key for key in self._load_jwks()["keys"] if key.get("kid") == header["kid"]]
        if len(keys) != 1:
            raise AuthenticationError("OIDC key id is missing or ambiguous")
        self._verify_signature(
            keys[0],
            alg,
            f"{encoded_header}.{encoded_payload}".encode("ascii"),
            signature,
        )
        now = int(time.time())
        if claims.get("iss") != self.issuer:
            raise AuthenticationError("OIDC issuer mismatch")
        audiences = claims.get("aud")
        audiences = [audiences] if isinstance(audiences, str) else audiences
        if not isinstance(audiences, list) or self.audience not in audiences:
            raise AuthenticationError("OIDC audience mismatch")
        if not isinstance(claims.get("sub"), str) or not 3 <= len(claims["sub"]) <= 128:
            raise AuthenticationError("OIDC subject is invalid")
        for claim in ("iat", "exp"):
            if not isinstance(claims.get(claim), int):
                raise AuthenticationError(f"OIDC {claim} claim is required")
        not_before = claims.get("nbf", claims["iat"])
        if not isinstance(not_before, int):
            raise AuthenticationError("OIDC nbf claim is invalid")
        if claims["iat"] > now + self.clock_skew_seconds or not_before > now + self.clock_skew_seconds:
            raise AuthenticationError("OIDC token is not active")
        if claims["exp"] <= now - self.clock_skew_seconds:
            raise AuthenticationError("OIDC token has expired")
        if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > self.max_token_age_seconds:
            raise AuthenticationError("OIDC token lifetime exceeds policy")
        roles = claims.get(self.roles_claim)
        tenants = claims.get(self.tenants_claim)
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise AuthenticationError("OIDC roles claim is invalid")
        if not isinstance(tenants, list) or any(not isinstance(item, str) for item in tenants):
            raise AuthenticationError("OIDC tenant claim is invalid")
        role_set = frozenset(roles) & self.allowed_roles
        if required_role not in role_set:
            raise AuthenticationError("principal lacks the required role")
        if not tenants:
            raise AuthenticationError("OIDC tenant scope is empty")
        return Principal(subject=claims["sub"], roles=role_set, tenant_ids=frozenset(tenants))


def build_authenticator_from_environment() -> Authenticator:
    production = os.getenv("PROOFMESH_ENV", "development").lower() == "production"
    mode = os.getenv("PROOFMESH_AUTH_MODE", "oidc" if production else "static").lower()
    if mode == "oidc":
        return OidcAuthenticator.from_environment()
    if mode == "static" and not production:
        return TokenAuthenticator.from_environment()
    if mode == "static":
        raise AuthenticationUnavailable("static bearer authentication is forbidden in production")
    raise AuthenticationUnavailable(f"unsupported authentication mode: {mode}")
