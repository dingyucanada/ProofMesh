from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofmesh.pilot_dataset import PilotDatasetError, validate_pilot_dataset


def _records(count: int = 200):
    cases = []
    labels = []
    failures = ["NONE", "PAYMENT_ACK_LOST", "CRM_5XX", "CRM_CONFLICT", "RECONCILE_UNKNOWN"]
    routes = ["AUTOMATIC", "HUMAN", "DENY"]
    terminals = ["COMPLETED", "COMPENSATED", "REJECTED"]
    for index in range(count):
        case_id = f"pmc_{index:032x}"
        cases.append(
            {
                "schema_version": "proofmesh.pilot-case/v1",
                "case_id": case_id,
                "domain": "REFUND",
                "risk_tier": ["LOW", "MEDIUM", "HIGH"][index % 3],
                "amount_bucket": ["LE_50", "GT_50_LE_100", "GT_100_LE_500", "GT_500"][index % 4],
                "currency": "CNY",
                "order_age_bucket": ["LT_1D", "D1_7", "D8_30", "GT_30D"][index % 4],
                "duplicate_request": index % 4 == 0,
                "requested_action": "REFUND",
                "failure_class": failures[index % len(failures)],
            }
        )
        labels.append(
            {
                "schema_version": "proofmesh.pilot-label/v1",
                "case_id": case_id,
                "expected_route": routes[index % len(routes)],
                "expected_terminal": terminals[index % len(terminals)],
            }
        )
    return cases, labels


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _dataset(tmp_path: Path, count: int = 200):
    cases, labels = _records(count)
    cases_path = tmp_path / "cases.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(cases_path, cases)
    _write_jsonl(labels_path, labels)
    return cases, labels, cases_path, labels_path


def test_historical_pilot_dataset_is_closed_blind_hashed_and_covered(tmp_path):
    _, _, cases_path, labels_path = _dataset(tmp_path)
    dataset = validate_pilot_dataset(
        cases_path,
        labels_path,
        origin="AUTHORIZED_HISTORICAL",
        authorization_ref="AUTH-2026-PM-001",
        reviewed_by="reviewer@example.org",
    )
    report = dataset.report()
    assert report["case_count"] == 200
    assert report["privacy_contract"]["free_text_fields_allowed"] is False
    assert report["privacy_contract"]["labels_separate_from_cases"] is True
    assert len(report["canonical_dataset_sha256"]) == 64
    assert report["provenance"]["external_claim_verified"] is False


def test_free_text_or_source_identifier_field_fails_closed(tmp_path):
    cases, labels, cases_path, labels_path = _dataset(tmp_path)
    cases[0]["customer_message"] = "call me at a phone number"
    _write_jsonl(cases_path, cases)
    with pytest.raises(PilotDatasetError, match="closed schema"):
        validate_pilot_dataset(
            cases_path,
            labels_path,
            origin="SYNTHETIC",
            reviewed_by="reviewer@example.org",
        )


def test_labels_must_be_separate_and_match_every_case(tmp_path):
    cases, labels, cases_path, labels_path = _dataset(tmp_path)
    labels.pop()
    _write_jsonl(labels_path, labels)
    with pytest.raises(PilotDatasetError, match="expected 200..500"):
        validate_pilot_dataset(
            cases_path,
            labels_path,
            origin="SYNTHETIC",
            reviewed_by="reviewer@example.org",
        )


def test_historical_origin_requires_authorization_reference(tmp_path):
    _, _, cases_path, labels_path = _dataset(tmp_path)
    with pytest.raises(PilotDatasetError, match="requires a non-secret authorization_ref"):
        validate_pilot_dataset(
            cases_path,
            labels_path,
            origin="AUTHORIZED_HISTORICAL",
            reviewed_by="reviewer@example.org",
        )


def test_insufficient_fault_and_route_coverage_is_rejected(tmp_path):
    cases, labels, cases_path, labels_path = _dataset(tmp_path)
    for case in cases:
        case["failure_class"] = "NONE"
    for label in labels:
        label["expected_route"] = "AUTOMATIC"
    _write_jsonl(cases_path, cases)
    _write_jsonl(labels_path, labels)
    with pytest.raises(PilotDatasetError, match="insufficient stratified coverage"):
        validate_pilot_dataset(
            cases_path,
            labels_path,
            origin="SYNTHETIC",
            reviewed_by="reviewer@example.org",
        )

