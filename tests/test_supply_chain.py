import json
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build_sbom(root: Path):
    script = ROOT / "scripts/generate_sbom.py"
    spec = importlib.util.spec_from_file_location("proofmesh_generate_sbom", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_sbom(root)


def test_checked_in_sbom_is_deterministic_and_complete():
    checked_in = json.loads((ROOT / "sbom.cdx.json").read_text(encoding="utf-8"))
    assert checked_in == _build_sbom(ROOT)
    assert checked_in["bomFormat"] == "CycloneDX"
    assert {item["name"].lower() for item in checked_in["components"]} >= {
        "fastapi", "uvicorn", "pydantic", "pyyaml", "cryptography", "psycopg"
    }


def test_ci_uses_commit_pinned_actions_oidc_provenance_and_locked_dependencies():
    workflow = (ROOT / ".github/workflows/ci-release.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements-ci.lock" in workflow
    assert "id-token: write" in workflow
    assert "attest-build-provenance@" in workflow
    for line in workflow.splitlines():
        if "uses:" in line:
            revision = line.rsplit("@", 1)[-1].strip()
            assert len(revision) == 40 and all(character in "0123456789abcdef" for character in revision)


def test_production_postgres_dependencies_are_hash_locked():
    lock = (ROOT / "requirements-production.lock").read_text(encoding="utf-8")
    assert "psycopg==3.2.9" in lock
    assert "psycopg-binary==3.2.9" in lock
    assert lock.count("--hash=sha256:") == 2
    assert "--require-hashes -r requirements-production.lock" in (
        ROOT / "Dockerfile"
    ).read_text(encoding="utf-8")
