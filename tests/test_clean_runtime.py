from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_clean_module():
    script = Path(__file__).resolve().parents[1] / "scripts/clean_runtime.py"
    spec = importlib.util.spec_from_file_location("proofmesh_clean_runtime", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_runtime_removes_only_ephemeral_targets(tmp_path, capsys):
    module = _load_clean_module()
    module.ROOT = tmp_path
    module.PROJECT_MARKER = tmp_path / "pyproject.toml"
    module.PROJECT_MARKER.write_text('[project]\nname = "proofmesh"\n', encoding="utf-8")

    (tmp_path / "var/keys").mkdir(parents=True)
    (tmp_path / "var/control-plane.db").write_text("state", encoding="utf-8")
    (tmp_path / "var/control-plane.db-wal").write_text("wal", encoding="utf-8")
    (tmp_path / "var/keys/local.ed25519").write_text("key", encoding="utf-8")
    (tmp_path / "artifacts/workflows/wf-1").mkdir(parents=True)
    (tmp_path / "artifacts/workflows/wf-1/proof.json").write_text("{}", encoding="utf-8")
    (tmp_path / "artifacts/public-benchmark").mkdir(parents=True)
    public_report = tmp_path / "artifacts/public-benchmark/report.json"
    public_report.write_text("{}", encoding="utf-8")
    (tmp_path / "artifacts/reference").mkdir(parents=True)
    reference_report = tmp_path / "artifacts/reference/manifest.json"
    reference_report.write_text("{}", encoding="utf-8")
    (tmp_path / "src/proofmesh/__pycache__").mkdir(parents=True)
    (tmp_path / "src/proofmesh/__pycache__/stale.pyc").write_text("cache", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/.DS_Store").write_text("metadata", encoding="utf-8")
    (tmp_path / "docs/demo-waiting-approval.png").write_text("legacy", encoding="utf-8")
    (tmp_path / "docs/demo-final-rollback.png").write_text("legacy", encoding="utf-8")
    (tmp_path / "docs/judge-console-compensated.png").write_text("legacy", encoding="utf-8")
    current_console = tmp_path / "docs/judge-console-compensated-current.png"
    current_console.write_text("current", encoding="utf-8")

    module.main()

    assert not (tmp_path / "var/control-plane.db").exists()
    assert not (tmp_path / "var/keys").exists()
    assert (tmp_path / "artifacts/workflows").is_dir()
    assert public_report.read_text(encoding="utf-8") == "{}"
    assert reference_report.read_text(encoding="utf-8") == "{}"
    assert not (tmp_path / "src/proofmesh/__pycache__").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / "docs/.DS_Store").exists()
    assert not (tmp_path / "docs/demo-waiting-approval.png").exists()
    assert not (tmp_path / "docs/demo-final-rollback.png").exists()
    assert not (tmp_path / "docs/judge-console-compensated.png").exists()
    assert current_console.read_text(encoding="utf-8") == "current"
    assert "preserved artifacts/public-benchmark" in capsys.readouterr().out


def test_clean_runtime_refuses_unknown_root(tmp_path):
    module = _load_clean_module()
    module.ROOT = tmp_path
    module.PROJECT_MARKER = tmp_path / "pyproject.toml"
    module.PROJECT_MARKER.write_text('[project]\nname = "another-project"\n', encoding="utf-8")

    try:
        module.main()
    except RuntimeError as exc:
        assert "refusing to clean" in str(exc)
    else:
        raise AssertionError("cleaner accepted an unrelated directory")
