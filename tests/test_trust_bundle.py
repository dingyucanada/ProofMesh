import json
import os

import pytest

from proofmesh.capabilities import ExternalTrustVerifier, PassportError, load_or_create_signer


def test_external_trust_bundle_rejects_coerced_or_malformed_rotation_fields(action_runtime, tmp_path):
    payload = json.loads(action_runtime.trust_bundle_path.read_text(encoding="utf-8"))
    key = next(iter(payload["keys"].values()))
    key["revoked"] = "false"
    key["not_before"] = "0"
    malformed = tmp_path / "malformed-trust.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PassportError, match="trust_bundle_schema"):
        ExternalTrustVerifier(malformed)


def test_existing_private_key_must_be_regular_and_mode_0600(tmp_path):
    trust = tmp_path / "trust.json"
    key = tmp_path / "keys" / "issuer.ed25519"
    load_or_create_signer(
        private_key_path=key,
        trust_bundle_path=trust,
        key_id="test-key-v1",
        issuer="test-issuer",
        token_types=["test+jws"],
    )
    os.chmod(key, 0o644)
    with pytest.raises(PassportError) as rejected:
        load_or_create_signer(
            private_key_path=key,
            trust_bundle_path=trust,
            key_id="test-key-v1",
            issuer="test-issuer",
            token_types=["test+jws"],
        )
    assert rejected.value.reason == "signing_key_permissions"


def test_private_key_symlink_is_rejected(tmp_path):
    real_key = tmp_path / "real.ed25519"
    real_key.write_bytes(b"x" * 32)
    os.chmod(real_key, 0o600)
    linked = tmp_path / "keys" / "linked.ed25519"
    linked.parent.mkdir()
    linked.symlink_to(real_key)
    with pytest.raises(PassportError, match="signing_key_not_regular"):
        load_or_create_signer(
            private_key_path=linked,
            trust_bundle_path=tmp_path / "trust.json",
            key_id="test-key-v1",
            issuer="test-issuer",
            token_types=["test+jws"],
        )
