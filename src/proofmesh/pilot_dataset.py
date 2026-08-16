"""Strict, privacy-minimized contracts for a blind historical-ticket pilot.

The validator intentionally accepts only enumerated operational facts. Free text,
source-system identifiers, direct identifiers, and exact timestamps have no field
in the schema and therefore fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CASE_SCHEMA = "proofmesh.pilot-case/v1"
LABEL_SCHEMA = "proofmesh.pilot-label/v1"
MANIFEST_SCHEMA = "proofmesh.pilot-dataset-report/v1"
CASE_KEYS = {
    "schema_version",
    "case_id",
    "domain",
    "risk_tier",
    "amount_bucket",
    "currency",
    "order_age_bucket",
    "duplicate_request",
    "requested_action",
    "failure_class",
}
LABEL_KEYS = {"schema_version", "case_id", "expected_route", "expected_terminal"}
CASE_ID_RE = re.compile(r"pmc_[0-9a-f]{32}")
AUTHORIZATION_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,63}")
REVIEWER_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{2,127}")
ENUMS = {
    "domain": {"REFUND"},
    "risk_tier": {"LOW", "MEDIUM", "HIGH"},
    "amount_bucket": {"LE_50", "GT_50_LE_100", "GT_100_LE_500", "GT_500"},
    "currency": {"CNY", "USD", "EUR"},
    "order_age_bucket": {"LT_1D", "D1_7", "D8_30", "GT_30D"},
    "requested_action": {"REFUND"},
    "failure_class": {"NONE", "PAYMENT_ACK_LOST", "CRM_5XX", "CRM_CONFLICT", "RECONCILE_UNKNOWN"},
    "expected_route": {"AUTOMATIC", "HUMAN", "DENY"},
    "expected_terminal": {"COMPLETED", "COMPENSATED", "UNKNOWN_MANUAL", "REJECTED"},
}
MINIMUM_COVERAGE = {
    "risk_tier.HIGH": 20,
    "duplicate_request.true": 10,
    "expected_route.AUTOMATIC": 10,
    "expected_route.HUMAN": 10,
    "expected_route.DENY": 10,
    "failure_class.PAYMENT_ACK_LOST": 5,
    "failure_class.CRM_5XX": 5,
    "failure_class.CRM_CONFLICT": 5,
    "failure_class.RECONCILE_UNKNOWN": 5,
}


class PilotDatasetError(ValueError):
    """A pilot dataset failed the closed schema or evidence contract."""


@dataclass(frozen=True)
class ValidatedDataset:
    cases_path: Path
    labels_path: Path
    origin: str
    authorization_ref: str | None
    reviewed_by: str
    cases: tuple[dict[str, Any], ...]
    labels: tuple[dict[str, Any], ...]

    def report(self) -> dict[str, Any]:
        case_by_id = {case["case_id"]: case for case in self.cases}
        label_by_id = {label["case_id"]: label for label in self.labels}
        ordered_ids = sorted(case_by_id)
        canonical_cases = [case_by_id[case_id] for case_id in ordered_ids]
        canonical_labels = [label_by_id[case_id] for case_id in ordered_ids]
        counts = {
            "risk_tier": _counts(case["risk_tier"] for case in self.cases),
            "amount_bucket": _counts(case["amount_bucket"] for case in self.cases),
            "currency": _counts(case["currency"] for case in self.cases),
            "order_age_bucket": _counts(case["order_age_bucket"] for case in self.cases),
            "duplicate_request": _counts(str(case["duplicate_request"]).lower() for case in self.cases),
            "failure_class": _counts(case["failure_class"] for case in self.cases),
            "expected_route": _counts(label["expected_route"] for label in self.labels),
            "expected_terminal": _counts(label["expected_terminal"] for label in self.labels),
        }
        return {
            "schema_version": MANIFEST_SCHEMA,
            "provenance": {
                "origin": self.origin,
                "customer_data": self.origin == "AUTHORIZED_HISTORICAL",
                "authorization_operator_asserted": self.authorization_ref is not None,
                "authorization_ref": self.authorization_ref,
                "reviewed_by": self.reviewed_by,
                "external_claim_verified": False,
                "note": "This validator proves schema, separation, coverage and hashes; the external claim gate separately requires a hashed authorization record and independent review.",
            },
            "privacy_contract": {
                "closed_enumerated_case_schema": True,
                "free_text_fields_allowed": False,
                "source_identifiers_allowed": False,
                "exact_timestamps_allowed": False,
                "labels_separate_from_cases": True,
            },
            "case_count": len(self.cases),
            "case_ids_unique": True,
            "case_and_label_ids_equal": True,
            "coverage": counts,
            "minimum_coverage": dict(sorted(MINIMUM_COVERAGE.items())),
            "files": {
                "cases": {
                    "bytes": self.cases_path.stat().st_size,
                    "sha256": _sha256_file(self.cases_path),
                },
                "labels": {
                    "bytes": self.labels_path.stat().st_size,
                    "sha256": _sha256_file(self.labels_path),
                },
            },
            "canonical_dataset_sha256": _sha256_json(
                {"cases": canonical_cases, "labels": canonical_labels}
            ),
        }


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_jsonl(path: Path, expected_keys: set[str], schema: str) -> tuple[dict[str, Any], ...]:
    if not path.is_file() or path.is_symlink():
        raise PilotDatasetError(f"not a regular JSONL file: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.strip()
            if not line:
                raise PilotDatasetError(f"{path.name}:{line_number}: blank lines are forbidden")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PilotDatasetError(f"{path.name}:{line_number}: invalid JSON") from exc
            if not isinstance(record, dict) or set(record) != expected_keys:
                raise PilotDatasetError(f"{path.name}:{line_number}: fields must exactly match the closed schema")
            if record["schema_version"] != schema:
                raise PilotDatasetError(f"{path.name}:{line_number}: unsupported schema")
            if not isinstance(record["case_id"], str) or not CASE_ID_RE.fullmatch(record["case_id"]):
                raise PilotDatasetError(f"{path.name}:{line_number}: case_id must be a project-random pmc_ identifier")
            records.append(record)
    if not 200 <= len(records) <= 500:
        raise PilotDatasetError(f"{path.name}: expected 200..500 records, found {len(records)}")
    return tuple(records)


def _validate_enums(records: Iterable[dict[str, Any]], keys: Iterable[str], file_label: str) -> None:
    for index, record in enumerate(records, 1):
        for key in keys:
            value = record[key]
            if not isinstance(value, str) or value not in ENUMS[key]:
                raise PilotDatasetError(f"{file_label}:{index}: invalid {key}")


def _validate_coverage(cases: tuple[dict[str, Any], ...], labels: tuple[dict[str, Any], ...]) -> None:
    label_by_id = {label["case_id"]: label for label in labels}
    counters = Counter()
    for case in cases:
        label = label_by_id[case["case_id"]]
        counters[f"risk_tier.{case['risk_tier']}"] += 1
        counters[f"duplicate_request.{str(case['duplicate_request']).lower()}"] += 1
        counters[f"failure_class.{case['failure_class']}"] += 1
        counters[f"expected_route.{label['expected_route']}"] += 1
    failures = [f"{key}={counters[key]}<{minimum}" for key, minimum in MINIMUM_COVERAGE.items() if counters[key] < minimum]
    if failures:
        raise PilotDatasetError("insufficient stratified coverage: " + ", ".join(failures))


def validate_pilot_dataset(
    cases_path: Path,
    labels_path: Path,
    *,
    origin: str,
    reviewed_by: str,
    authorization_ref: str | None = None,
) -> ValidatedDataset:
    """Validate schema, privacy minimization, label separation, and coverage.

    This function does not authenticate a data owner. For historical data the
    separate release claim gate must bind a real authorization artifact.
    """

    if origin not in {"SYNTHETIC", "AUTHORIZED_HISTORICAL"}:
        raise PilotDatasetError("origin must be SYNTHETIC or AUTHORIZED_HISTORICAL")
    if not isinstance(reviewed_by, str) or not REVIEWER_REF_RE.fullmatch(reviewed_by):
        raise PilotDatasetError("reviewed_by must be a non-secret reviewer reference")
    if origin == "AUTHORIZED_HISTORICAL":
        if not isinstance(authorization_ref, str) or not AUTHORIZATION_REF_RE.fullmatch(authorization_ref):
            raise PilotDatasetError("historical data requires a non-secret authorization_ref")
    elif authorization_ref is not None:
        raise PilotDatasetError("synthetic data must not claim an authorization_ref")

    cases_path = cases_path.resolve()
    labels_path = labels_path.resolve()
    if cases_path == labels_path:
        raise PilotDatasetError("cases and blind labels must be separate files")
    cases = _load_jsonl(cases_path, CASE_KEYS, CASE_SCHEMA)
    labels = _load_jsonl(labels_path, LABEL_KEYS, LABEL_SCHEMA)
    _validate_enums(
        cases,
        (
            "domain",
            "risk_tier",
            "amount_bucket",
            "currency",
            "order_age_bucket",
            "requested_action",
            "failure_class",
        ),
        cases_path.name,
    )
    _validate_enums(labels, ("expected_route", "expected_terminal"), labels_path.name)
    for index, case in enumerate(cases, 1):
        if type(case["duplicate_request"]) is not bool:
            raise PilotDatasetError(f"{cases_path.name}:{index}: duplicate_request must be a JSON boolean")
    case_ids = [case["case_id"] for case in cases]
    label_ids = [label["case_id"] for label in labels]
    if len(case_ids) != len(set(case_ids)):
        raise PilotDatasetError("case ids must be unique")
    if len(label_ids) != len(set(label_ids)):
        raise PilotDatasetError("label ids must be unique")
    if set(case_ids) != set(label_ids):
        raise PilotDatasetError("case and blind-label ids must match exactly")
    _validate_coverage(cases, labels)
    return ValidatedDataset(
        cases_path=cases_path,
        labels_path=labels_path,
        origin=origin,
        authorization_ref=authorization_ref,
        reviewed_by=reviewed_by,
        cases=cases,
        labels=labels,
    )

