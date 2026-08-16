from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import ssl
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator


TOKEN_TYPE = "proofmesh-action-passport+jws"
RECEIPT_TYPE = "proofmesh-execution-receipt+jws"
TASK_RECEIPT_TYPE = "proofmesh-task-step+jws"
PROOF_TYPE = "proofmesh-workflow-proof+jws"
BUSINESS_ATTESTATION_TYPE = "proofmesh-business-snapshot+jws"
APPROVAL_ASSERTION_TYPE = "proofmesh-human-approval+jws"
SCHEMA_VERSION = "proofmesh.action-passport/v1"
APPROVAL_ASSERTION_SCHEMA_VERSION = "proofmesh.human-approval-assertion/v1"
TRUST_SCHEMA_VERSION = "proofmesh.trust-bundle/v1"
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")


class TrustKeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    algorithm: Literal["ed25519"]
    public_key: StrictStr = Field(min_length=44, max_length=44)
    issuers: list[StrictStr] = Field(min_length=1, max_length=16)
    token_types: list[StrictStr] = Field(min_length=1, max_length=16)
    revoked: StrictBool = False
    not_before: StrictInt | None = None
    not_after: StrictInt | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> "TrustKeyEntry":
        if any(not value or len(value) > 256 for value in self.issuers + self.token_types):
            raise ValueError("issuer and token type values must be non-empty")
        if len(set(self.issuers)) != len(self.issuers) or len(set(self.token_types)) != len(self.token_types):
            raise ValueError("issuer and token type values must be unique")
        if self.not_before is not None and self.not_after is not None and self.not_before >= self.not_after:
            raise ValueError("invalid trust-key validity window")
        try:
            decoded = base64.b64decode(self.public_key, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("public key is not canonical base64") from exc
        if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != self.public_key:
            raise ValueError("public key must encode exactly 32 bytes")
        return self


class TrustBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[TRUST_SCHEMA_VERSION]
    keys: dict[StrictStr, TrustKeyEntry] = Field(min_length=1, max_length=128)
    approval_issuers: list[StrictStr] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_key_ids(self) -> "TrustBundle":
        if any(KEY_ID_RE.fullmatch(key_id) is None for key_id in self.keys):
            raise ValueError("invalid trust key id")
        if (
            any(not issuer or len(issuer) > 256 for issuer in self.approval_issuers)
            or len(set(self.approval_issuers)) != len(self.approval_issuers)
            or self.approval_issuers != sorted(self.approval_issuers)
        ):
            raise ValueError("approval issuer allowlist must be canonical and unique")
        return self


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_digest(value: Any) -> str:
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class PassportError(ValueError):
    """A fail-closed capability validation error with a stable reason code."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        super().__init__(detail or reason)


class ActionPassportClaims(BaseModel):
    """A short-lived, least-privilege authorization for one concrete tool call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    jti: str = Field(min_length=16, max_length=128)
    issuer: str = Field(min_length=3, max_length=128)
    audience: str = Field(min_length=3, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=256)
    workflow_id: str = Field(min_length=8, max_length=128)
    tool: str = Field(min_length=3, max_length=256)
    resource: str = Field(min_length=1, max_length=512)
    scopes: list[str] = Field(min_length=1, max_length=16)
    args_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approval_digest: str = Field(pattern=r"^(AUTOMATIC|EMERGENCY:[a-f0-9]{64}|[a-f0-9]{64})$")
    mode: Literal["read", "execute", "compensate"]
    max_calls: int = Field(ge=1, le=8)
    max_amount_minor: int = Field(ge=0, le=10**12)
    budget_limit_minor: int = Field(ge=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    issued_at: int
    not_before: int
    expires_at: int

    @classmethod
    def issue(
        cls,
        *,
        issuer: str,
        audience: str,
        tenant_id: str,
        subject: str,
        workflow_id: str,
        tool: str,
        resource: str,
        scopes: list[str],
        arguments: dict[str, Any],
        context_digest: str,
        policy_digest: str,
        plan_digest: str | None = None,
        approval_digest: str,
        mode: Literal["read", "execute", "compensate"],
        max_calls: int = 1,
        max_amount_minor: int = 0,
        budget_limit_minor: int = 0,
        currency: str = "CNY",
        ttl_seconds: int = 60,
        now: int | None = None,
    ) -> "ActionPassportClaims":
        now = int(time.time()) if now is None else int(now)
        if not 5 <= ttl_seconds <= 300:
            raise PassportError("invalid_ttl", "passport TTL must be between 5 and 300 seconds")
        return cls(
            jti=f"apt-{uuid.uuid4().hex}",
            issuer=issuer,
            audience=audience,
            tenant_id=tenant_id,
            subject=subject,
            workflow_id=workflow_id,
            tool=tool,
            resource=resource,
            scopes=sorted(set(scopes)),
            args_digest=sha256_digest(arguments),
            context_digest=context_digest,
            policy_digest=policy_digest,
            plan_digest=plan_digest,
            approval_digest=approval_digest,
            mode=mode,
            max_calls=max_calls,
            max_amount_minor=max_amount_minor,
            budget_limit_minor=budget_limit_minor,
            currency=currency,
            issued_at=now,
            not_before=now - 1,
            expires_at=now + ttl_seconds,
        )


class ApprovalActionContract(BaseModel):
    """One exact side effect covered by an external Human approval assertion."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool: StrictStr = Field(min_length=3, max_length=256)
    resource: StrictStr = Field(min_length=1, max_length=512)
    args_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    amount_minor: StrictInt = Field(ge=0, le=10**12)
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")


class HumanApprovalAssertionClaims(BaseModel):
    """Short-lived assertion minted outside the ProofMesh control-plane trust boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[APPROVAL_ASSERTION_SCHEMA_VERSION] = APPROVAL_ASSERTION_SCHEMA_VERSION
    issuer: StrictStr = Field(min_length=3, max_length=256)
    audience: StrictStr = Field(min_length=3, max_length=256)
    jti: StrictStr = Field(pattern=r"^approval-[a-f0-9]{32}$")
    workflow_id: StrictStr = Field(min_length=8, max_length=128)
    project_id: StrictStr = Field(min_length=8, max_length=128)
    tenant_id: StrictStr = Field(min_length=1, max_length=128)
    requester: StrictStr = Field(min_length=3, max_length=128)
    subject: StrictStr = Field(min_length=3, max_length=256)
    decision: Literal["APPROVE"]
    expected_revision: StrictInt = Field(ge=0)
    context_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    policy_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    plan_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    challenge_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    reason_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    scope: list[StrictStr] = Field(min_length=1, max_length=16)
    actions: list[ApprovalActionContract] = Field(min_length=1, max_length=16)
    amount_minor: StrictInt = Field(ge=0, le=10**12)
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")
    auth_time: StrictInt
    acr: StrictStr = Field(min_length=3, max_length=256)
    amr: list[StrictStr] = Field(min_length=1, max_length=16)
    issued_at: StrictInt
    not_before: StrictInt
    expires_at: StrictInt

    @model_validator(mode="after")
    def validate_semantics(self) -> "HumanApprovalAssertionClaims":
        tools = [action.tool for action in self.actions]
        if self.subject == self.requester:
            raise ValueError("requester cannot approve their own action")
        if self.scope != sorted(set(self.scope)) or tools != sorted(set(tools)):
            raise ValueError("approval scope and action tools must be canonical and unique")
        if self.scope != tools:
            raise ValueError("approval scope must exactly match action contracts")
        if self.amr != sorted(set(self.amr)):
            raise ValueError("authentication methods must be canonical and unique")
        if not self.auth_time <= self.issued_at:
            raise ValueError("approval authentication happened after issuance")
        if not self.not_before <= self.issued_at < self.expires_at:
            raise ValueError("invalid approval assertion time window")
        if self.expires_at - self.issued_at > 900:
            raise ValueError("approval assertion TTL exceeds 15 minutes")
        return self


@dataclass(frozen=True)
class VerifiedToken:
    header: dict[str, Any]
    payload: dict[str, Any]
    compact: str


class CompactEd25519Signer:
    """Minimal deterministic compact JWS signer; no algorithm negotiation is allowed."""

    def __init__(self, private_key: Ed25519PrivateKey, *, key_id: str, issuer: str):
        self.private_key = private_key
        self.key_id = key_id
        self.issuer = issuer

    @property
    def public_key_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign_payload(self, payload: dict[str, Any], *, token_type: str) -> str:
        header = {"alg": "EdDSA", "kid": self.key_id, "typ": token_type}
        encoded_header = _b64url_encode(canonical_json(header).encode("utf-8"))
        encoded_payload = _b64url_encode(canonical_json(payload).encode("utf-8"))
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = _b64url_encode(self.private_key.sign(signing_input))
        return f"{encoded_header}.{encoded_payload}.{signature}"

    def sign_passport(self, claims: ActionPassportClaims) -> str:
        if claims.issuer != self.issuer:
            raise PassportError("issuer_mismatch", "claims issuer does not match signer")
        return self.sign_payload(claims.model_dump(mode="json"), token_type=TOKEN_TYPE)


class JwsSigner(Protocol):
    key_id: str
    issuer: str

    def sign_payload(self, payload: dict[str, Any], *, token_type: str) -> str: ...

    def sign_passport(self, claims: ActionPassportClaims) -> str: ...


class RemoteEd25519Signer:
    """mTLS signing-proxy adapter that never loads private key material.

    The returned JWS must contain the exact canonical payload and verify against
    the externally mounted trust bundle. Proxy outages or any substitution fail closed.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        trust_bundle_path: str | Path,
        key_id: str,
        issuer: str,
        allowed_token_types: frozenset[str],
        ca_bundle_path: str | Path,
        client_certificate_path: str | Path,
        client_key_path: str | Path,
        timeout_seconds: float = 3.0,
    ):
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise PassportError("remote_signer_endpoint_invalid", "remote signer endpoint must be HTTPS")
        if not allowed_token_types:
            raise PassportError("remote_signer_usage_empty")
        self.endpoint = endpoint
        self.key_id = key_id
        self.issuer = issuer
        self.allowed_token_types = allowed_token_types
        self.timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context(cafile=str(ca_bundle_path))
        self._ssl_context.load_cert_chain(
            certfile=str(client_certificate_path),
            keyfile=str(client_key_path),
        )
        verifier = ExternalTrustVerifier(trust_bundle_path)
        key = verifier.keys.get(key_id)
        if (
            not isinstance(key, dict)
            or key.get("algorithm") != "ed25519"
            or issuer not in key.get("issuers", [])
            or not allowed_token_types.issubset(key.get("token_types", []))
            or key.get("revoked") is True
        ):
            raise PassportError("remote_signer_key_not_pinned", f"key {key_id} is not usable")
        try:
            self._public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(key["public_key"], validate=True)
            )
        except (KeyError, ValueError, binascii.Error) as exc:
            raise PassportError("remote_signer_key_invalid") from exc

    def sign_payload(self, payload: dict[str, Any], *, token_type: str) -> str:
        if token_type not in self.allowed_token_types:
            raise PassportError("remote_signer_usage_not_allowed")
        body = canonical_json(
            {
                "key_id": self.key_id,
                "issuer": self.issuer,
                "token_type": token_type,
                "payload": payload,
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(  # noqa: S310 - endpoint is constrained to HTTPS above
                request,
                timeout=self.timeout_seconds,
                context=self._ssl_context,
            ) as response:
                if response.status != 200:
                    raise PassportError("remote_signer_unavailable")
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    raise PassportError("remote_signer_response_too_large")
                response_payload = json.loads(raw)
        except PassportError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PassportError("remote_signer_unavailable") from exc
        token = response_payload.get("jws") if isinstance(response_payload, dict) else None
        if not isinstance(token, str):
            raise PassportError("remote_signer_response_invalid")
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = json.loads(_b64url_decode(encoded_header))
            returned_payload = json.loads(_b64url_decode(encoded_payload))
            signature = _b64url_decode(encoded_signature)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
            raise PassportError("remote_signer_response_invalid") from exc
        if header != {"alg": "EdDSA", "kid": self.key_id, "typ": token_type}:
            raise PassportError("remote_signer_header_mismatch")
        if canonical_json(returned_payload) != canonical_json(payload):
            raise PassportError("remote_signer_payload_mismatch")
        try:
            self._public_key.verify(
                signature,
                f"{encoded_header}.{encoded_payload}".encode("ascii"),
            )
        except InvalidSignature as exc:
            raise PassportError("remote_signer_signature_invalid") from exc
        return token

    def sign_passport(self, claims: ActionPassportClaims) -> str:
        if claims.issuer != self.issuer:
            raise PassportError("issuer_mismatch", "claims issuer does not match signer")
        return self.sign_payload(claims.model_dump(mode="json"), token_type=TOKEN_TYPE)


class ExternalTrustVerifier:
    """Verifier anchored in a trust bundle supplied outside any proof or receipt pack."""

    def __init__(self, trust_bundle_path: str | Path):
        self.trust_bundle_path = Path(trust_bundle_path)
        if not self.trust_bundle_path.is_file():
            raise PassportError("trust_bundle_missing")
        try:
            payload = json.loads(self.trust_bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise PassportError("trust_bundle_schema") from exc
        if isinstance(payload, dict) and payload.get("keys") == {}:
            raise PassportError("trust_bundle_empty")
        try:
            bundle = TrustBundle.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise PassportError("trust_bundle_schema") from exc
        self.keys = {
            key_id: entry.model_dump(mode="json") for key_id, entry in bundle.keys.items()
        }
        self.approval_issuers = frozenset(bundle.approval_issuers)

    def verify_compact(
        self,
        token: str,
        *,
        expected_type: str,
        allowed_issuers: set[str] | None = None,
    ) -> VerifiedToken:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header_bytes = _b64url_decode(encoded_header)
            payload_bytes = _b64url_decode(encoded_payload)
            signature_bytes = _b64url_decode(encoded_signature)
            if (
                _b64url_encode(header_bytes) != encoded_header
                or _b64url_encode(payload_bytes) != encoded_payload
                or _b64url_encode(signature_bytes) != encoded_signature
            ):
                raise ValueError("non-canonical base64url")
            header = json.loads(header_bytes)
            payload = json.loads(payload_bytes)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
            raise PassportError("malformed_token") from exc
        if header != {"alg": "EdDSA", "kid": header.get("kid"), "typ": expected_type}:
            raise PassportError("invalid_header")
        key_id = header.get("kid")
        key_entry = self.keys.get(key_id)
        if not isinstance(key_entry, dict) or key_entry.get("algorithm") != "ed25519":
            raise PassportError("untrusted_key")
        now_epoch = int(time.time())
        if key_entry.get("revoked") is True:
            raise PassportError("key_revoked")
        if isinstance(key_entry.get("not_before"), int) and now_epoch < key_entry["not_before"]:
            raise PassportError("key_not_active")
        if isinstance(key_entry.get("not_after"), int) and now_epoch >= key_entry["not_after"]:
            raise PassportError("key_expired")
        if expected_type not in key_entry.get("token_types", []):
            raise PassportError("key_usage_not_allowed")
        issuer = payload.get("issuer")
        if issuer not in key_entry.get("issuers", []):
            raise PassportError("issuer_not_authorized")
        if allowed_issuers is not None and issuer not in allowed_issuers:
            raise PassportError("issuer_not_allowed")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_entry["public_key"], validate=True))
            public_key.verify(
                signature_bytes,
                f"{encoded_header}.{encoded_payload}".encode("ascii"),
            )
        except (KeyError, ValueError, InvalidSignature, binascii.Error) as exc:
            raise PassportError("invalid_signature") from exc
        return VerifiedToken(header=header, payload=payload, compact=token)

    def verify_passport(
        self,
        token: str,
        *,
        audience: str,
        tool: str,
        workflow_id: str,
        arguments: dict[str, Any],
        context_digest: str,
        now: int | None = None,
        leeway_seconds: int = 2,
    ) -> ActionPassportClaims:
        verified = self.verify_compact(
            token,
            expected_type=TOKEN_TYPE,
            allowed_issuers={"proofmesh-policy-control-plane"},
        )
        try:
            claims = ActionPassportClaims.model_validate(verified.payload)
        except ValueError as exc:
            raise PassportError("invalid_claims") from exc
        now = int(time.time()) if now is None else int(now)
        if not claims.not_before <= claims.issued_at < claims.expires_at:
            raise PassportError("invalid_time_window")
        if claims.issued_at > now + leeway_seconds:
            raise PassportError("passport_issued_in_future")
        if claims.audience != audience:
            raise PassportError("audience_mismatch")
        if claims.tool != tool:
            raise PassportError("tool_mismatch")
        if claims.workflow_id != workflow_id:
            raise PassportError("workflow_mismatch")
        if claims.args_digest != sha256_digest(arguments):
            raise PassportError("arguments_mismatch")
        if claims.context_digest != context_digest:
            raise PassportError("context_mismatch")
        if now + leeway_seconds < claims.not_before:
            raise PassportError("passport_not_active")
        if now - leeway_seconds >= claims.expires_at:
            raise PassportError("passport_expired")
        if claims.expires_at - claims.issued_at > 300:
            raise PassportError("passport_ttl_exceeded")
        return claims

    def verify_human_approval_assertion(
        self,
        token: str,
        *,
        audience: str = "proofmesh-approval-gate",
        allowed_issuers: set[str] | None = None,
        now: int | None = None,
        leeway_seconds: int = 2,
        enforce_current_time: bool = True,
    ) -> HumanApprovalAssertionClaims:
        issuer_allowlist = set(allowed_issuers) if allowed_issuers is not None else set(self.approval_issuers)
        if not issuer_allowlist:
            raise PassportError("approval_issuer_allowlist_missing")
        verified = self.verify_compact(
            token,
            expected_type=APPROVAL_ASSERTION_TYPE,
            allowed_issuers=issuer_allowlist,
        )
        try:
            claims = HumanApprovalAssertionClaims.model_validate(verified.payload)
        except ValueError as exc:
            raise PassportError("approval_assertion_invalid_claims") from exc
        if claims.audience != audience:
            raise PassportError("approval_assertion_audience_mismatch")
        if not enforce_current_time:
            return claims
        now = int(time.time()) if now is None else int(now)
        if claims.issued_at > now + leeway_seconds:
            raise PassportError("approval_assertion_issued_in_future")
        if now + leeway_seconds < claims.not_before:
            raise PassportError("approval_assertion_not_active")
        if now - leeway_seconds >= claims.expires_at:
            raise PassportError("approval_assertion_expired")
        return claims


def load_or_create_signer(
    *,
    private_key_path: str | Path,
    trust_bundle_path: str | Path,
    key_id: str,
    issuer: str,
    allow_bootstrap: bool = True,
    token_types: list[str] | None = None,
) -> CompactEd25519Signer:
    """Bootstrap a local key and an external trust anchor with strict file permissions."""

    private_key_path = Path(private_key_path)
    trust_bundle_path = Path(trust_bundle_path)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    if private_key_path.parent.is_symlink():
        raise PassportError("signing_key_parent_symlink")
    trust_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if private_key_path.exists():
        if private_key_path.is_symlink() or not private_key_path.is_file():
            raise PassportError("signing_key_not_regular")
        if stat.S_IMODE(private_key_path.stat().st_mode) != 0o600:
            raise PassportError("signing_key_permissions", f"key must be mode 0600: {private_key_path}")
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(private_key_path.read_bytes())
        except ValueError as exc:
            raise PassportError("signing_key_invalid") from exc
    else:
        if not allow_bootstrap:
            raise PassportError("signing_key_missing", f"missing key: {private_key_path}")
        private_key = Ed25519PrivateKey.generate()
        private_key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        os.chmod(private_key_path, 0o600)
    signer = CompactEd25519Signer(private_key, key_id=key_id, issuer=issuer)
    if trust_bundle_path.exists():
        trust_bundle = json.loads(trust_bundle_path.read_text(encoding="utf-8"))
    else:
        if not allow_bootstrap:
            raise PassportError("trust_bundle_missing", f"missing trust bundle: {trust_bundle_path}")
        trust_bundle = {"schema_version": TRUST_SCHEMA_VERSION, "keys": {}}
    keys = trust_bundle.setdefault("keys", {})
    expected_entry = {
        "algorithm": "ed25519",
        "public_key": base64.b64encode(signer.public_key_bytes).decode("ascii"),
        "issuers": sorted(set(keys.get(key_id, {}).get("issuers", [])) | {issuer}),
        "token_types": sorted(set(token_types or [])),
    }
    if not allow_bootstrap:
        pinned = keys.get(key_id)
        pinned_ok = (
            isinstance(pinned, dict)
            and pinned.get("algorithm") == expected_entry["algorithm"]
            and pinned.get("public_key") == expected_entry["public_key"]
            and issuer in pinned.get("issuers", [])
            and set(token_types or []).issubset(pinned.get("token_types", []))
            and pinned.get("revoked") is not True
        )
        if not pinned_ok:
            raise PassportError("signing_key_not_pinned", f"key {key_id} is not pinned in the external trust bundle")
    else:
        keys[key_id] = expected_entry
        if APPROVAL_ASSERTION_TYPE in (token_types or []):
            trust_bundle["approval_issuers"] = sorted(
                set(trust_bundle.get("approval_issuers", [])) | {issuer}
            )
        trust_bundle_path.write_text(json.dumps(trust_bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(trust_bundle_path, 0o644)
    return signer
