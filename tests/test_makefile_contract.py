from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_makefile_has_no_developer_workspace_path_and_separates_export_from_validation():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "../../work/" not in makefile
    assert "AGENTDOJO_PYTHON ?= $(PYTHON)" in makefile
    assert "$(AGENTDOJO_PYTHON) scripts/benchmarks/export_agentdojo.py" in makefile
    assert "PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_authorization_contract_replay.py" in makefile
    assert "$(PYTHON) scripts/benchmarks/validate_agentdojo_export.py" in makefile
    assert "$(PYTHON) scripts/validate_external_evidence.py" in makefile
