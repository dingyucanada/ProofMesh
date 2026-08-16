import json
from pathlib import Path

import pytest

from proofmesh.benchmarks.authorization_replay import (
    REPORT_SCHEMA,
    _wilson_interval,
    load_benchmark,
    render_markdown,
    run_authorization_contract_replay,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES = PROJECT_ROOT / "artifacts/public-benchmark/cases.jsonl"
REPORT = PROJECT_ROOT / "artifacts/public-benchmark/authorization-contract-replay.json"


def test_two_case_replay_exercises_real_gateway_without_duplicate_trace_effects() -> None:
    benchmark = load_benchmark(CASES, limit=2)
    report = run_authorization_contract_replay(benchmark)

    assert report["passed"] is True
    assert report["schema_version"] == REPORT_SCHEMA
    assert report["dataset"]["case_count"] == 2
    assert report["dataset"]["user_ground_truth_call_count"] == 2
    assert report["metrics"]["legal_contract_acceptance_rate"]["rate"] == 1.0
    assert report["metrics"]["unregistered_call_pass_rate"]["rate"] == 0.0
    assert report["metrics"]["same_tool_argument_mismatch_pass_rate"]["rate"] == 0.0
    assert report["metrics"]["tool_mismatch_pass_rate"]["rate"] == 0.0
    assert report["metrics"]["context_mismatch_pass_rate"]["rate"] == 0.0
    assert (
        report["metrics"]["same_idempotency_key_cached_replay_success_rate"]["rate"]
        == 1.0
    )
    assert (
        report["metrics"]["different_idempotency_key_single_use_rejection_rate"]["rate"]
        == 1.0
    )
    assert report["metrics"]["receipt_external_verification_rate"]["rate"] == 1.0
    assert report["metrics"]["receipt_chain_verification_rate"]["rate"] == 1.0
    assert report["invariants"]["observed_side_effect_count"] == 2
    assert report["invariants"]["duplicate_side_effect_count"] == 0
    assert report["invariants"]["observed_receipt_count"] == 2
    assert report["invariants"]["observed_denial_reasons"] == {
        "arguments_mismatch": 2,
        "context_mismatch": 2,
        "passport_exhausted": 2,
        "tool_mismatch": 2,
        "tool_not_registered": 2,
    }
    markdown = render_markdown(report)
    assert "不是模型攻击成功率（ASR）" in markdown
    assert "实际 2，重复 0" in markdown


def test_wilson_interval_is_bounded_and_non_degenerate_for_extreme_rates() -> None:
    zero_low, zero_high = _wilson_interval(0, 629)
    one_low, one_high = _wilson_interval(629, 629)

    assert zero_low == 0.0
    assert zero_high == pytest.approx(0.006070, abs=1e-6)
    assert one_low == pytest.approx(0.993930, abs=1e-6)
    assert one_high == 1.0


def test_committed_full_replay_report_covers_all_public_cases() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["passed"] is True
    assert report["schema_version"] == REPORT_SCHEMA
    assert report["dataset"]["case_count"] == 629
    assert report["dataset"]["unique_user_task_count"] == 97
    assert report["dataset"]["unique_injection_task_count"] == 27
    assert report["dataset"]["user_ground_truth_call_count"] == 2159
    assert report["dataset"]["injection_ground_truth_call_count"] == 1105
    assert report["dataset"]["registered_function_count"] == 56
    assert report["dataset"]["artifact_sha256"] == (
        "20146525d139f83d732bc47e0286b0eccdf67118cdc5eba84f5f079863f5546b"
    )
    assert report["invariants"]["observed_side_effect_count"] == 2159
    assert report["invariants"]["duplicate_side_effect_count"] == 0
    assert report["invariants"]["observed_receipt_count"] == 2159
    assert report["invariants"]["observed_denial_count"] == 9265
    assert report["invariants"]["checks"] == {
        "all_legal_contracts_accepted": True,
        "all_negative_controls_blocked_with_expected_reason": True,
        "all_operations_succeeded_once": True,
        "capability_usage_exact": True,
        "denial_audit_count_exact": True,
        "denial_reasons_exact": True,
        "no_duplicate_side_effects": True,
        "receipt_call_ids_unique": True,
        "receipt_count_exact": True,
        "side_effect_count_exact": True,
    }
    assert "model attack success rate (ASR)" in report["evaluation_scope"]["does_not_measure"]
