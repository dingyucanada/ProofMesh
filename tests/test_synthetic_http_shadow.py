from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from proofmesh.synthetic_shadow import PROVENANCE, generate_workload, run_shadow_workload, write_artifacts


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ports() -> tuple[int, int]:
    first = _port()
    second = _port()
    while second == first:
        second = _port()
    return first, second


def test_workload_is_deterministic_and_explicitly_synthetic():
    first = generate_workload(240, seed=20260813)
    second = generate_workload(240, seed=20260813)
    assert first == second
    assert first["case_count"] == 240
    assert first["provenance"] == PROVENANCE
    assert first["provenance"]["customer_data"] is False
    assert first["provenance"]["enterprise_historical_data"] is False
    assert first["provenance"]["third_party_provider"] is False
    assert {case["fault_profile"] for case in first["cases"]} == {
        "normal",
        "crm_close_error",
        "payment_timeout_after_commit",
        "reconcile_unknown_after_commit",
    }
    with pytest.raises(ValueError, match="200-500"):
        generate_workload(199)


def test_independent_process_http_fault_matrix_and_kpis(tmp_path: Path):
    payment_port, crm_port = _ports()
    workload = generate_workload(200, seed=20260813)
    report, results = run_shadow_workload(
        workload,
        work_dir=tmp_path,
        payment_port=payment_port,
        crm_port=crm_port,
    )
    assert len(results) == 200
    assert report["provenance"] == PROVENANCE
    assert report["service_stats"]["process_evidence"]["different_os_processes"] is True
    assert report["terminal_counts"] == {
        "COMPLETED": 50,
        "RECOVERED_COMPLETED": 50,
        "COMPENSATED": 50,
        "UNKNOWN_MANUAL": 50,
    }
    assert report["kpis"]["duplicate_side_effect_rate"]["numerator"] == 0
    assert report["kpis"]["invalid_call_pass_rate"]["numerator"] == 0
    assert report["kpis"]["invalid_call_pass_rate"]["denominator"] == 200
    assert report["kpis"]["recoverable_terminal_rate"]["rate"] == 1.0
    assert report["kpis"]["unknown_fail_closed_rate"]["rate"] == 1.0
    assert report["kpis"]["audit_readback_seconds"]["samples"] == 200
    assert report["method"]["human_approval_assertion_covered"] is False
    assert report["method"]["lease_expiry_injection"]["production_api"] is False
    assert all(result["invalid_attempts"] == 1 for result in results)


def test_artifacts_are_replayable_and_keep_non_customer_claim_boundary(tmp_path: Path):
    payment_port, crm_port = _ports()
    report = write_artifacts(
        tmp_path / "artifacts",
        case_count=200,
        seed=7,
        payment_port=payment_port,
        crm_port=crm_port,
    )
    workload = json.loads((tmp_path / "artifacts/workload.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "artifacts/manifest.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "artifacts/report.md").read_text(encoding="utf-8")
    assert workload["provenance"] == PROVENANCE
    assert manifest["provenance"] == PROVENANCE
    assert len(manifest["files"]) == 4
    assert "不是企业历史工单" in markdown
    assert "不是客户试点" in markdown
    assert report["method"]["not_claimed"] == [
        "customer pilot",
        "enterprise historical data",
        "third-party payment provider",
        "production SLA",
        "financial ROI",
        "Human approval assertion coverage",
    ]
    assert "不覆盖 Human approval assertion" in markdown
    assert "不是生产 API" in markdown
