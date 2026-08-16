import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_reference_artifacts.py"


def _module():
    spec = importlib.util.spec_from_file_location("generate_reference_artifacts", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_evidence_is_executed_verified_and_secret_free(tmp_path):
    module = _module()
    output = Path(__file__).resolve().parents[1] / "artifacts" / "test-reference-temp"
    try:
        manifest = module.generate(output)
        by_ticket = {case["ticket_id"]: case for case in manifest["cases"]}
        assert by_ticket["TKT-LOW-001"]["terminal_status"] == "COMPLETED"
        assert by_ticket["TKT-HIGH-001"]["terminal_status"] == "COMPLETED"
        assert by_ticket["TKT-HIGH-001"]["waiting_evidence"]
        assert by_ticket["TKT-SAGA-001"]["terminal_status"] == "COMPENSATED"
        assert all(case["external_verification_valid"] for case in manifest["cases"])
        assert manifest["private_keys_included"] is False
        assert manifest["databases_included"] is False
        assert not list(output.rglob("*.ed25519"))
        assert not list(output.rglob("*.db"))
        waiting = json.loads((output / by_ticket["TKT-HIGH-001"]["waiting_evidence"]).read_text())
        assert waiting["assertions"] == {
            "status_is_waiting_approval": True,
            "refund_not_executed": True,
            "frozen_plan_digest_present": True,
            "approval_scope_present": True,
        }
    finally:
        if output.exists():
            import shutil

            shutil.rmtree(output)
