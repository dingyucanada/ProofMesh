from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_public_demo.py"


def _module():
    spec = importlib.util.spec_from_file_location("proofmesh_public_demo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_demo_build_is_interactive_but_has_no_backend(tmp_path: Path):
    report = _module().build(tmp_path / "site")
    assert report["mode"] == "INTERACTIVE_DECISION_LAB_WITH_FROZEN_EVIDENCE"
    assert report["decision_engine"] == "CLIENT_SIDE_DETERMINISTIC_SIMULATION"
    assert report["live_backend"] is False
    assert report["model_driven"] is False
    assert report["customer_data"] is False

    html = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    javascript = (tmp_path / "site/app.js").read_text(encoding="utf-8")
    assert "给系统一个场景，看它会不会冒险" in html
    assert "evaluateLab" in javascript
    assert "UNKNOWN_MANUAL" in javascript
    assert "fetch(" in javascript  # frozen evidence only
    assert "/api/" not in javascript


def test_public_demo_contains_only_selected_evidence(tmp_path: Path):
    output = tmp_path / "site"
    report = _module().build(output)
    manifest = json.loads((output / "PUBLIC_DEMO_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["file_count"] == report["file_count"]
    assert not list(output.rglob("*.ed25519"))
    assert not list(output.rglob("*.db"))
    assert not list(output.rglob(".env"))
    assert (output / "evidence/reference/automatic/workflow-proof.json").is_file()
