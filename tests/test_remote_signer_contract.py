import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from proofmesh.capabilities import CompactEd25519Signer, PassportError, RemoteEd25519Signer, TOKEN_TYPE


def test_remote_signer_requires_https_before_loading_certificates(tmp_path: Path):
    with pytest.raises(PassportError) as rejected:
        RemoteEd25519Signer(
            endpoint="http://kms.internal/v1/sign/key",
            trust_bundle_path=tmp_path / "trust.json",
            key_id="key-1",
            issuer="issuer-1",
            allowed_token_types=frozenset({TOKEN_TYPE}),
            ca_bundle_path=tmp_path / "ca.pem",
            client_certificate_path=tmp_path / "client.pem",
            client_key_path=tmp_path / "client-key.pem",
        )
    assert rejected.value.reason == "remote_signer_endpoint_invalid"


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def _adapter(private_key, monkeypatch, returned_payload):
    signer = CompactEd25519Signer(private_key, key_id="key-1", issuer="issuer-1")
    adapter = RemoteEd25519Signer.__new__(RemoteEd25519Signer)
    adapter.endpoint = "https://signer.example.test/v1/sign/key-1"
    adapter.key_id = "key-1"
    adapter.issuer = "issuer-1"
    adapter.allowed_token_types = frozenset({TOKEN_TYPE})
    adapter.timeout_seconds = 3.0
    adapter._ssl_context = object()
    adapter._public_key = private_key.public_key()
    token = signer.sign_payload(returned_payload, token_type=TOKEN_TYPE)
    monkeypatch.setattr("proofmesh.capabilities.urlopen", lambda *_args, **_kwargs: _Response({"jws": token}))
    return adapter


def test_remote_signer_accepts_only_exact_pinned_payload(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    payload = {"issuer": "issuer-1", "workflow_id": "wf-1"}
    adapter = _adapter(private_key, monkeypatch, payload)
    assert adapter.sign_payload(payload, token_type=TOKEN_TYPE).count(".") == 2


def test_remote_signer_rejects_validly_signed_payload_substitution(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    adapter = _adapter(private_key, monkeypatch, {"issuer": "issuer-1", "workflow_id": "wf-other"})
    with pytest.raises(PassportError) as rejected:
        adapter.sign_payload({"issuer": "issuer-1", "workflow_id": "wf-1"}, token_type=TOKEN_TYPE)
    assert rejected.value.reason == "remote_signer_payload_mismatch"
