import shutil
from pathlib import Path

import pytest

from proofmesh.runtime import build_runtime
from approval_helpers import provision_approval_service


@pytest.fixture()
def proofmesh_home(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "data").mkdir()
    shutil.copytree(root / "data/policies", tmp_path / "data/policies")
    (tmp_path / "var").mkdir()
    (tmp_path / "artifacts").mkdir()
    return tmp_path
@pytest.fixture()
def action_runtime(proofmesh_home: Path):
    runtime = build_runtime(proofmesh_home)
    provision_approval_service(runtime, proofmesh_home)
    return runtime
