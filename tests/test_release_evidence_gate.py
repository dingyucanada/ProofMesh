from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest


def _load_release_module():
    script = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
    spec = importlib.util.spec_from_file_location("proofmesh_build_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_synthetic_shadow_release_gate_accepts_frozen_evidence(tmp_path):
    module = _load_release_module()
    source = Path(__file__).resolve().parents[1] / "artifacts/synthetic-http-shadow"
    target = tmp_path / "artifacts/synthetic-http-shadow"
    shutil.copytree(source, target)

    module._validate_synthetic_shadow(tmp_path)


def test_synthetic_shadow_release_gate_rejects_rehashed_metric_drift(tmp_path):
    module = _load_release_module()
    source = Path(__file__).resolve().parents[1] / "artifacts/synthetic-http-shadow"
    target = tmp_path / "artifacts/synthetic-http-shadow"
    shutil.copytree(source, target)

    report_path = target / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["kpis"]["duplicate_side_effect_rate"]["numerator"] = 1
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_entry = next(entry for entry in manifest["files"] if entry["path"] == "report.json")
    report_entry["bytes"] = report_path.stat().st_size
    report_entry["sha256"] = _sha256(report_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="metrics or claim boundaries"):
        module._validate_synthetic_shadow(tmp_path)


def test_release_credential_scan_rejects_provider_key_shapes_without_echoing_secret(tmp_path):
    module = _load_release_module()
    secret = "sk_" + "test_" + "A" * 24
    fixture = tmp_path / "accidental-secret.txt"
    fixture.write_text(f"PROVIDER_KEY={secret}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="credential-shaped text") as caught:
        module._validate_no_credential_shapes([fixture])
    assert secret not in str(caught.value)
