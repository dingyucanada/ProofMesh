from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate_external_evidence.py"


def _module():
    spec = importlib.util.spec_from_file_location("proofmesh_external_evidence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_register(tmp_path: Path) -> Path:
    target = tmp_path / "artifacts/external-evidence"
    target.mkdir(parents=True)
    shutil.copy2(ROOT / "artifacts/external-evidence/claim-status.json", target / "claim-status.json")
    return target


def test_current_external_claims_are_honest_and_pending():
    report = _module().validate(ROOT)
    assert report["verified"] == 0
    assert report["pending"] == 6
    assert set(report["claims"].values()) == {"PENDING"}


def test_pending_claim_cannot_carry_partial_evidence(tmp_path):
    module = _module()
    target = _copy_register(tmp_path)
    register_path = target / "claim-status.json"
    register = json.loads(register_path.read_text(encoding="utf-8"))
    register["claims"][0]["verified_by"] = "someone"
    register_path.write_text(json.dumps(register), encoding="utf-8")

    with pytest.raises(module.ExternalEvidenceError, match="PENDING must not carry"):
        module.validate(tmp_path)


def test_verified_claim_requires_hashed_evidence_for_every_requirement(tmp_path):
    module = _module()
    target = _copy_register(tmp_path)
    register_path = target / "claim-status.json"
    register = json.loads(register_path.read_text(encoding="utf-8"))
    claim = register["claims"][0]
    claim["status"] = "VERIFIED"
    claim["verified_by"] = "external reviewer"
    claim["verified_at"] = "2026-08-13T12:00:00+08:00"
    register_path.write_text(json.dumps(register), encoding="utf-8")

    with pytest.raises(module.ExternalEvidenceError, match="one evidence file per requirement"):
        module.validate(tmp_path)


def test_verified_claim_accepts_exact_hashed_release_artifacts(tmp_path):
    target = _copy_register(tmp_path)
    register_path = target / "claim-status.json"
    register = json.loads(register_path.read_text(encoding="utf-8"))
    claim = register["claims"][0]
    claim["status"] = "VERIFIED"
    claim["verified_by"] = "external reviewer"
    claim["verified_at"] = "2026-08-13T12:00:00+08:00"
    claim["evidence_files"] = []
    for requirement in claim["evidence_requirements"]:
        path = target / f"model-driven-{requirement}.json"
        path.write_text(json.dumps({"requirement": requirement, "result": "PASS"}), encoding="utf-8")
        claim["evidence_files"].append(
            {
                "requirement": requirement,
                "path": str(path.relative_to(tmp_path)),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    register_path.write_text(json.dumps(register), encoding="utf-8")

    report = _module().validate(tmp_path)
    assert report["claims"]["model_driven_agentteams"] == "VERIFIED"
    assert report["verified"] == 1
