#!/usr/bin/env python3
"""Development/reference approval issuer outside the ProofMesh runtime.

This process owns the approval private key.  The ProofMesh control plane receives
only the compact assertion and a trust bundle containing its public key.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from proofmesh.capabilities import (
    APPROVAL_ASSERTION_TYPE,
    HumanApprovalAssertionClaims,
    load_or_create_signer,
    sha256_digest,
)


ISSUER = "proofmesh-reference-approval-service"
KEY_ID = "proofmesh-reference-approval-service-v1"


def issue_assertion(
    challenge: dict,
    *,
    subject: str,
    reason: str,
    private_key_path: Path,
    trust_bundle_path: Path,
    now: int | None = None,
) -> str:
    now = int(time.time()) if now is None else int(now)
    challenge_body = {key: value for key, value in challenge.items() if key != "challenge_digest"}
    if challenge.get("challenge_digest") != sha256_digest(challenge_body):
        raise ValueError("challenge digest is invalid")
    signer = load_or_create_signer(
        private_key_path=private_key_path,
        trust_bundle_path=trust_bundle_path,
        key_id=KEY_ID,
        issuer=ISSUER,
        allow_bootstrap=not (private_key_path.exists() and trust_bundle_path.exists()),
        token_types=[APPROVAL_ASSERTION_TYPE],
    )
    claims = HumanApprovalAssertionClaims(
        issuer=ISSUER,
        audience="proofmesh-approval-gate",
        jti=f"approval-{uuid.uuid4().hex}",
        subject=subject,
        decision="APPROVE",
        challenge_digest=challenge["challenge_digest"],
        reason_digest=sha256_digest(reason.strip()),
        auth_time=now - 5,
        acr="urn:proofmesh:reference:synthetic-approval",
        amr=["reference-script"],
        issued_at=now,
        not_before=now - 1,
        expires_at=now + 600,
        **challenge_body,
    )
    return signer.sign_payload(claims.model_dump(mode="json"), token_type=APPROVAL_ASSERTION_TYPE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reference-only external approval assertion issuer")
    parser.add_argument("--challenge", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--trust-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = issue_assertion(
        json.loads(args.challenge.read_text(encoding="utf-8")),
        subject=args.subject,
        reason=args.reason,
        private_key_path=args.private_key,
        trust_bundle_path=args.trust_bundle,
    )
    args.output.write_text(token + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
