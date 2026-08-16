from __future__ import annotations

import importlib.util
from pathlib import Path

from proofmesh.capabilities import APPROVAL_ASSERTION_TYPE, load_or_create_signer


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "reference_approval_service", ROOT / "scripts" / "reference_approval_service.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def provision_approval_service(action_runtime, tmp_path: Path) -> Path:
    key = tmp_path / "external-approval-service" / "approval.ed25519"
    load_or_create_signer(
        private_key_path=key,
        trust_bundle_path=action_runtime.trust_bundle_path,
        key_id="proofmesh-reference-approval-service-v1",
        issuer="proofmesh-reference-approval-service",
        token_types=[APPROVAL_ASSERTION_TYPE],
    )
    action_runtime.verifier.__init__(action_runtime.trust_bundle_path)
    return key


def approval_key(action_runtime) -> Path:
    return action_runtime.home / "external-approval-service" / "approval.ed25519"


def issue_approval(action_runtime, waiting, *, subject: str, reason: str, key: Path | None = None, now=None) -> str:
    return MODULE.issue_assertion(
        action_runtime.control_plane.approval_challenge(waiting.workflow_id),
        subject=subject,
        reason=reason,
        private_key_path=key or approval_key(action_runtime),
        trust_bundle_path=action_runtime.trust_bundle_path,
        now=now,
    )
