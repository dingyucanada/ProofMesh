import json
from pathlib import Path

from proofmesh.benchmarks.banking77_routing import (
    REPORT_SCHEMA,
    Example,
    TfidfCentroidClassifier,
    run_gateway_controls,
)
from proofmesh.capabilities import sha256_digest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = PROJECT_ROOT / "artifacts/public-domain-evaluation/banking77"


def test_transparent_classifier_is_deterministic() -> None:
    examples = [
        Example("where is my refund", "refund_status"),
        Example("refund still missing", "refund_status"),
        Example("what is the exchange rate", "exchange_rate"),
        Example("show current exchange rate", "exchange_rate"),
    ]
    first = TfidfCentroidClassifier().fit(examples).predict("my refund is missing")
    second = TfidfCentroidClassifier().fit(examples).predict("my refund is missing")
    assert first == second
    assert first.label == "refund_status"
    assert first.margin > 0


def test_real_gateway_audits_routes_and_blocks_unapproved_sensitive_control() -> None:
    controls = run_gateway_controls(
        [
            {"case_digest": "a" * 64, "route": "COLLECT_REFUND_CONTEXT"},
            {"case_digest": "b" * 64, "route": "OUT_OF_SCOPE_HANDOFF"},
        ]
    )
    assert controls["route_audit_acceptance"]["successes"] == 2
    assert controls["externally_verified_receipts"]["successes"] == 2
    assert controls["unapproved_sensitive_bypass_rejection"]["rate"] == 1.0
    assert controls["unapproved_sensitive_bypass_rejection"]["attempts"] == 2
    assert controls["unapproved_sensitive_bypass_reasons"] == {
        "approval_assertion_missing": 2
    }
    assert controls["sensitive_upstream_dispatches"] == 0
    assert controls["business_backends_reachable"] is False


def test_committed_official_report_is_complete_and_honest() -> None:
    report = json.loads((ARTIFACTS / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == REPORT_SCHEMA
    assert report["benchmark_execution_passed"] is True
    assert report["readiness_verdict"] == "NOT_PRODUCTION_READY"
    assert report["dataset"]["source_commit"] == "57ec275d8078af65b7731c2a98be812d844a6d6b"
    assert report["dataset"]["train"]["rows"] == 10_003
    assert report["dataset"]["test"]["rows"] == 3_080
    assert report["dataset"]["intent_count"] == 77
    assert report["method"]["test_label_leakage"] is False
    assert report["method"]["public_text_in_artifacts"] is False
    assert report["metrics"]["intent_77_way"]["accuracy"]["attempts"] == 3_080
    assert report["metrics"]["refund_focus_detection"]["recall"]["attempts"] == 80
    assert report["metrics"]["action_gateway_controls"][
        "unapproved_sensitive_bypass_rejection"
    ]["attempts"] == 80
    assert report["metrics"]["action_gateway_controls"]["sensitive_upstream_dispatches"] == 0
    assert "real refund execution or payment/CRM integration" in report["interpretation_limits"]["does_not_measure"]


def test_case_results_cover_test_split_without_raw_text() -> None:
    raw = (ARTIFACTS / "case-results.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert len(records) == 3_080
    assert [record["row_index"] for record in records] == list(range(1, 3_081))
    assert len({record["record_sha256"] for record in records}) == 3_080
    for record in records:
        assert "text" not in record
        assert len(record["text_sha256"]) == 64
        assert set(record) == {
            "schema_version",
            "row_index",
            "text_sha256",
            "gold_intent",
            "predicted_intent",
            "score",
            "margin",
            "gold_refund_route",
            "predicted_refund_route",
            "policy_gold_risk_lane",
            "policy_predicted_risk_lane",
            "final_risk_route",
            "record_sha256",
        }
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        assert record["record_sha256"] == sha256_digest(unsigned)
