#!/usr/bin/env python3
"""Validate the fail-closed register for claims that need external evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTER = Path("artifacts/external-evidence/claim-status.json")
EXPECTED_CLAIMS = {
    "model_driven_agentteams",
    "stripe_hubspot_sandbox",
    "authorized_historical_pilot",
    "independent_reproduction",
    "public_repository_and_identity",
    "production_readiness",
}
TOP_LEVEL_KEYS = {"schema_version", "policy", "score_target", "claims"}
CLAIM_KEYS = {
    "id",
    "status",
    "allowed_claim",
    "prohibited_claims",
    "evidence_requirements",
    "evidence_files",
    "verified_by",
    "verified_at",
}
EVIDENCE_KEYS = {"requirement", "path", "bytes", "sha256"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SECRET_SHAPES = re.compile(
    r"(?:sk_(?:live|test)_[A-Za-z0-9]+|pat-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})"
)
FORBIDDEN_EVIDENCE_SUFFIXES = {".db", ".sqlite", ".ed25519", ".key", ".pem", ".env"}


class ExternalEvidenceError(RuntimeError):
    """Raised when an external evidence claim is structurally unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ExternalEvidenceError(f"{location} keys differ: missing={missing}, extra={extra}")


def _nonempty_unique_strings(value: Any, location: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ExternalEvidenceError(f"{location} must be a non-empty string list")
    if len(value) != len(set(value)):
        raise ExternalEvidenceError(f"{location} must not contain duplicates")
    return value


def _parse_timestamp(value: Any, location: str) -> None:
    if not isinstance(value, str) or not value:
        raise ExternalEvidenceError(f"{location} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalEvidenceError(f"{location} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ExternalEvidenceError(f"{location} must include a timezone")


def _validate_verified_evidence(root: Path, claim: dict[str, Any]) -> None:
    claim_id = claim["id"]
    requirements = set(claim["evidence_requirements"])
    entries = claim["evidence_files"]
    if not isinstance(entries, list) or len(entries) != len(requirements):
        raise ExternalEvidenceError(f"{claim_id}: VERIFIED requires one evidence file per requirement")
    represented: set[str] = set()
    evidence_root = (root / "artifacts/external-evidence").resolve()
    for index, entry in enumerate(entries):
        location = f"{claim_id}.evidence_files[{index}]"
        if not isinstance(entry, dict):
            raise ExternalEvidenceError(f"{location} must be an object")
        _require_exact_keys(entry, EVIDENCE_KEYS, location)
        requirement = entry["requirement"]
        if requirement not in requirements or requirement in represented:
            raise ExternalEvidenceError(f"{location}.requirement is unknown or duplicated")
        represented.add(requirement)
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative == REGISTER:
            raise ExternalEvidenceError(f"{location}.path is unsafe")
        absolute = (root / relative).resolve()
        if evidence_root not in absolute.parents or absolute.suffix.lower() in FORBIDDEN_EVIDENCE_SUFFIXES:
            raise ExternalEvidenceError(f"{location}.path must be a safe external-evidence artifact")
        if not absolute.is_file() or absolute.is_symlink():
            raise ExternalEvidenceError(f"{location}.path does not name a regular file")
        if not isinstance(entry["bytes"], int) or entry["bytes"] <= 0 or absolute.stat().st_size != entry["bytes"]:
            raise ExternalEvidenceError(f"{location}.bytes mismatch")
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(entry["sha256"]):
            raise ExternalEvidenceError(f"{location}.sha256 is invalid")
        if _sha256(absolute) != entry["sha256"]:
            raise ExternalEvidenceError(f"{location}.sha256 mismatch")
        if absolute.stat().st_size <= 2_000_000:
            try:
                text = absolute.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = ""
            if SECRET_SHAPES.search(text):
                raise ExternalEvidenceError(f"{location} contains credential-shaped text")
    if represented != requirements:
        raise ExternalEvidenceError(f"{claim_id}: evidence does not cover every requirement")
    if not isinstance(claim["verified_by"], str) or not claim["verified_by"].strip():
        raise ExternalEvidenceError(f"{claim_id}: VERIFIED requires a named external verifier")
    _parse_timestamp(claim["verified_at"], f"{claim_id}.verified_at")


def validate(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    register_path = root / REGISTER
    try:
        register = json.loads(register_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalEvidenceError(f"cannot load {REGISTER}: {exc}") from exc
    if not isinstance(register, dict):
        raise ExternalEvidenceError("claim register must be an object")
    _require_exact_keys(register, TOP_LEVEL_KEYS, "register")
    if register["schema_version"] != "proofmesh.external-claim-register/v1":
        raise ExternalEvidenceError("unsupported external claim register schema")
    if not isinstance(register["policy"], str) or "fail-closed" not in register["policy"]:
        raise ExternalEvidenceError("claim register must declare a fail-closed policy")
    score_target = register["score_target"]
    if not isinstance(score_target, dict) or set(score_target) != {"engineering", "external_evidence"}:
        raise ExternalEvidenceError("score_target must separate engineering and external evidence")
    if any(not isinstance(value, str) or "not " not in value.lower() for value in score_target.values()):
        raise ExternalEvidenceError("score targets must explicitly disclaim official scoring")
    claims = register["claims"]
    if not isinstance(claims, list) or not all(isinstance(claim, dict) for claim in claims):
        raise ExternalEvidenceError("claims must be an object list")
    ids = [claim.get("id") for claim in claims]
    if len(ids) != len(set(ids)) or set(ids) != EXPECTED_CLAIMS:
        raise ExternalEvidenceError("external claim set is incomplete or duplicated")

    statuses: dict[str, str] = {}
    for claim in claims:
        claim_id = claim["id"]
        _require_exact_keys(claim, CLAIM_KEYS, claim_id)
        status = claim["status"]
        if status not in {"PENDING", "VERIFIED"}:
            raise ExternalEvidenceError(f"{claim_id}.status must be PENDING or VERIFIED")
        if not isinstance(claim["allowed_claim"], str) or not claim["allowed_claim"].strip():
            raise ExternalEvidenceError(f"{claim_id}.allowed_claim must be non-empty")
        _nonempty_unique_strings(claim["prohibited_claims"], f"{claim_id}.prohibited_claims")
        _nonempty_unique_strings(claim["evidence_requirements"], f"{claim_id}.evidence_requirements")
        if status == "PENDING":
            if claim["evidence_files"] != [] or claim["verified_by"] is not None or claim["verified_at"] is not None:
                raise ExternalEvidenceError(f"{claim_id}: PENDING must not carry partial verification state")
        else:
            _validate_verified_evidence(root, claim)
        statuses[claim_id] = status
    return {
        "schema_version": register["schema_version"],
        "claims": statuses,
        "verified": sum(status == "VERIFIED" for status in statuses.values()),
        "pending": sum(status == "PENDING" for status in statuses.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(validate(args.root), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

