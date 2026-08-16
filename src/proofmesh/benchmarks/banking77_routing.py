"""Reproducible Banking77 utility and safe-routing evaluation.

The official training split is used to fit a transparent TF-IDF centroid
classifier.  The official test split is read exactly once for evaluation; its
labels are never used to fit the classifier or choose the abstention threshold.

Banking77 supplies intent labels, not action-risk labels.  The three risk lanes
below are therefore an explicit ProofMesh policy projection, never represented
as dataset ground truth.  All public utterances are routed only to an inert
triage decision.  A separate synthetic negative control verifies that the real
ActionGateway rejects a sensitive execute passport without an external approval
assertion; it never calls a payment or CRM backend.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import re
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from proofmesh.business import ReconciliationResult, ToolExecutionError, ToolSpec
from proofmesh.capabilities import (
    RECEIPT_TYPE,
    TOKEN_TYPE,
    ActionPassportClaims,
    CompactEd25519Signer,
    ExternalTrustVerifier,
    canonical_json,
    sha256_digest,
)
from proofmesh.gateway import ActionGateway, GatewayDenied, GatewayStore


REPORT_SCHEMA = "proofmesh.banking77-routing-evaluation/v1"
CASE_SCHEMA = "proofmesh.banking77-routing-result/v1"
SOURCE_LOCK_SCHEMA = "proofmesh.public-dataset-source-lock/v1"
SOURCE_COMMIT = "57ec275d8078af65b7731c2a98be812d844a6d6b"
TRAIN_SHA256 = "b06e26ac675513959a63135f11b94ea7786ed02da65db93a5650d8838cbc664b"
TEST_SHA256 = "d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d"
LICENSE_SHA256 = "7e7170e3cebf88a9f60c7b8421418323c09304da1af4d5e90f4da1dc1c8a2661"
TRAIN_ROWS = 10_003
TEST_ROWS = 3_080
INTENT_COUNT = 77
WILSON_Z_95 = 1.959963984540054

SOURCE_URL = "https://github.com/PolyAI-LDN/task-specific-datasets"
PAPER_URL = "https://arxiv.org/abs/2003.04807"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

REFUND_ROUTES = {
    "request_refund": "COLLECT_REFUND_CONTEXT",
    "Refund_not_showing_up": "READ_ONLY_REFUND_INVESTIGATION",
}
OUT_OF_SCOPE_ROUTE = "OUT_OF_SCOPE_HANDOFF"
REFUND_INTENTS = frozenset(REFUND_ROUTES)

# Project-declared policy projection.  These labels are not supplied by Banking77.
AUTO_READ_INTENTS = frozenset(
    {
        "age_limit",
        "apple_pay_or_google_pay",
        "atm_support",
        "card_about_to_expire",
        "card_acceptance",
        "card_arrival",
        "card_delivery_estimate",
        "card_payment_fee_charged",
        "card_payment_wrong_exchange_rate",
        "cash_withdrawal_charge",
        "country_support",
        "disposable_card_limits",
        "exchange_charge",
        "exchange_rate",
        "exchange_via_app",
        "extra_charge_on_statement",
        "fiat_currency_support",
        "receiving_money",
        "supported_cards_and_currencies",
        "top_up_by_bank_transfer_charge",
        "top_up_by_card_charge",
        "top_up_by_cash_or_cheque",
        "top_up_limits",
        "topping_up_by_card",
        "transfer_fee_charged",
        "transfer_into_account",
        "transfer_timing",
        "visa_or_mastercard",
        "why_verify_identity",
        "wrong_exchange_rate_for_cash_withdrawal",
    }
)
BLOCKED_DIRECT_ACTION_INTENTS = frozenset(
    {
        "beneficiary_not_allowed",
        "cancel_transfer",
        "card_payment_not_recognised",
        "card_swallowed",
        "cash_withdrawal_not_recognised",
        "compromised_card",
        "direct_debit_payment_not_recognised",
        "edit_personal_details",
        "lost_or_stolen_card",
        "lost_or_stolen_phone",
        "passcode_forgotten",
        "pin_blocked",
        "request_refund",
        "terminate_account",
        "transaction_charged_twice",
        "unable_to_verify_identity",
        "verify_source_of_funds",
        "wrong_amount_of_cash_received",
    }
)
RISK_LANES = ("AUTO_READ", "HUMAN_REVIEW", "BLOCKED_DIRECT_ACTION")

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


class Banking77EvaluationError(RuntimeError):
    """The public dataset or evaluation contract is invalid."""


@dataclass(frozen=True)
class Example:
    text: str
    category: str


@dataclass(frozen=True)
class Prediction:
    label: str
    score: float
    margin: float


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_examples(path: str | Path) -> list[Example]:
    source = Path(path)
    with source.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["text", "category"]:
            raise Banking77EvaluationError(
                f"{source} must have exactly the text,category CSV header"
            )
        rows = [Example(str(row["text"]), str(row["category"])) for row in reader]
    if not rows or any(not row.text.strip() or not row.category.strip() for row in rows):
        raise Banking77EvaluationError(f"{source} contains empty or no examples")
    return rows


def validate_official_source(dataset_root: str | Path) -> dict[str, Any]:
    root = Path(dataset_root)
    train_path = root / "banking_data/train.csv"
    test_path = root / "banking_data/test.csv"
    license_path = root / "LICENSE"
    for path in (train_path, test_path, license_path):
        if not path.is_file():
            raise Banking77EvaluationError(f"missing official source file: {path}")
    actual = {
        "train": _file_sha256(train_path),
        "test": _file_sha256(test_path),
        "license": _file_sha256(license_path),
    }
    expected = {
        "train": TRAIN_SHA256,
        "test": TEST_SHA256,
        "license": LICENSE_SHA256,
    }
    if actual != expected:
        raise Banking77EvaluationError(
            f"official source digest mismatch: expected {expected}, observed {actual}"
        )
    train = load_examples(train_path)
    test = load_examples(test_path)
    categories = sorted({row.category for row in train})
    if (
        len(train) != TRAIN_ROWS
        or len(test) != TEST_ROWS
        or len(categories) != INTENT_COUNT
        or {row.category for row in test} != set(categories)
    ):
        raise Banking77EvaluationError("official Banking77 split dimensions changed")
    if set(categories) != AUTO_READ_INTENTS | BLOCKED_DIRECT_ACTION_INTENTS | (
        set(categories) - AUTO_READ_INTENTS - BLOCKED_DIRECT_ACTION_INTENTS
    ):
        raise Banking77EvaluationError("invalid project risk-lane projection")
    return {
        "schema_version": SOURCE_LOCK_SCHEMA,
        "dataset": "Banking77",
        "source_repository": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "license": "CC BY 4.0",
        "license_url": LICENSE_URL,
        "paper": PAPER_URL,
        "train": {"relative_path": "banking_data/train.csv", "sha256": actual["train"], "rows": len(train)},
        "test": {"relative_path": "banking_data/test.csv", "sha256": actual["test"], "rows": len(test)},
        "license_file": {"relative_path": "LICENSE", "sha256": actual["license"]},
        "intent_count": len(categories),
        "raw_text_redistributed": False,
    }


def _features(text: str) -> Counter[str]:
    tokens = TOKEN_PATTERN.findall(text.lower())
    result: Counter[str] = Counter(f"w:{token}" for token in tokens)
    result.update(f"b:{left}_{right}" for left, right in zip(tokens, tokens[1:]))
    return result


class TfidfCentroidClassifier:
    """Small, deterministic, standard-library intent baseline.

    It is intentionally transparent rather than state of the art: word unigrams
    and adjacent bigrams, smoothed IDF, L2-normalized document vectors and one
    normalized centroid per intent.
    """

    def __init__(self) -> None:
        self.idf: dict[str, float] = {}
        self.centroids: dict[str, dict[str, float]] = {}
        self.labels: tuple[str, ...] = ()

    def fit(self, examples: Sequence[Example]) -> "TfidfCentroidClassifier":
        if not examples:
            raise Banking77EvaluationError("classifier needs training examples")
        document_frequency: Counter[str] = Counter()
        documents: list[tuple[Counter[str], str]] = []
        for example in examples:
            features = _features(example.text)
            documents.append((features, example.category))
            document_frequency.update(features)
        count = len(examples)
        self.idf = {
            feature: math.log((count + 1) / (frequency + 1)) + 1
            for feature, frequency in document_frequency.items()
        }
        sums: dict[str, Counter[str]] = defaultdict(Counter)
        for features, category in documents:
            vector = {
                feature: (1 + math.log(frequency)) * self.idf[feature]
                for feature, frequency in features.items()
            }
            norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
            for feature, value in vector.items():
                sums[category][feature] += value / norm
        self.centroids = {}
        for category, vector in sums.items():
            norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
            self.centroids[category] = {
                feature: value / norm for feature, value in vector.items()
            }
        self.labels = tuple(sorted(self.centroids))
        return self

    def predict(self, text: str) -> Prediction:
        if not self.labels:
            raise Banking77EvaluationError("classifier has not been fitted")
        features = _features(text)
        vector = {
            feature: (1 + math.log(frequency)) * self.idf[feature]
            for feature, frequency in features.items()
            if feature in self.idf
        }
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        scores = {
            category: sum(
                value / norm * self.centroids[category].get(feature, 0.0)
                for feature, value in vector.items()
            )
            for category in self.labels
        }
        ranked = sorted(self.labels, key=lambda category: (scores[category], category), reverse=True)
        best = ranked[0]
        second_score = scores[ranked[1]] if len(ranked) > 1 else 0.0
        return Prediction(best, scores[best], scores[best] - second_score)


def refund_route(intent: str) -> str:
    return REFUND_ROUTES.get(intent, OUT_OF_SCOPE_ROUTE)


def risk_lane(intent: str, all_categories: Iterable[str]) -> str:
    categories = set(all_categories)
    if intent not in categories:
        raise Banking77EvaluationError(f"risk projection received unknown intent: {intent}")
    if intent in AUTO_READ_INTENTS:
        return "AUTO_READ"
    if intent in BLOCKED_DIRECT_ACTION_INTENTS:
        return "BLOCKED_DIRECT_ACTION"
    return "HUMAN_REVIEW"


def calibration_split(examples: Sequence[Example]) -> tuple[list[Example], list[Example]]:
    """Deterministic per-class 80/20 split using only the official train data."""

    seen: Counter[str] = Counter()
    fit: list[Example] = []
    calibration: list[Example] = []
    for example in examples:
        index = seen[example.category]
        seen[example.category] += 1
        (calibration if index % 5 == 0 else fit).append(example)
    return fit, calibration


def _final_risk_route(prediction: Prediction, categories: set[str], threshold: float) -> str:
    predicted_lane = risk_lane(prediction.label, categories)
    if predicted_lane == "AUTO_READ" and prediction.margin < threshold:
        return "HUMAN_REVIEW"
    return predicted_lane


def calibrate_abstention_threshold(
    train: Sequence[Example], *, protected_recall_target: float = 0.99
) -> tuple[float, dict[str, Any]]:
    """Choose the smallest train-only margin that reaches protected-lane recall."""

    if not 0 < protected_recall_target <= 1:
        raise Banking77EvaluationError("protected recall target must be in (0, 1]")
    fit, calibration = calibration_split(train)
    classifier = TfidfCentroidClassifier().fit(fit)
    categories = {row.category for row in train}
    predictions = [(row, classifier.predict(row.text)) for row in calibration]
    candidates = {0.0}
    for row, prediction in predictions:
        if risk_lane(row.category, categories) != "AUTO_READ" and risk_lane(
            prediction.label, categories
        ) == "AUTO_READ":
            candidates.add(math.nextafter(prediction.margin, math.inf))

    def outcome(threshold: float) -> tuple[int, int, int]:
        protected = 0
        protected_safe = 0
        automatic = 0
        for row, prediction in predictions:
            route = _final_risk_route(prediction, categories, threshold)
            if risk_lane(row.category, categories) != "AUTO_READ":
                protected += 1
                protected_safe += route != "AUTO_READ"
            automatic += route == "AUTO_READ"
        return protected_safe, protected, automatic

    selected = max(candidates)
    for candidate in sorted(candidates):
        protected_safe, protected, _ = outcome(candidate)
        if protected_safe / protected >= protected_recall_target:
            selected = candidate
            break
    protected_safe, protected, automatic = outcome(selected)
    return selected, {
        "method": "per-class every-fifth-row holdout from official train only",
        "fit_rows": len(fit),
        "calibration_rows": len(calibration),
        "protected_recall_target": protected_recall_target,
        "selected_margin": round(selected, 12),
        "protected_safe": protected_safe,
        "protected_total": protected,
        "automatic_routes": automatic,
        "test_labels_used": False,
    }


def _wilson(successes: int, attempts: int) -> dict[str, Any]:
    if attempts <= 0 or not 0 <= successes <= attempts:
        raise Banking77EvaluationError("Wilson interval requires 0 <= successes <= attempts")
    proportion = successes / attempts
    z2 = WILSON_Z_95**2
    denominator = 1 + z2 / attempts
    centre = (proportion + z2 / (2 * attempts)) / denominator
    margin = (
        WILSON_Z_95
        * math.sqrt((proportion * (1 - proportion) + z2 / (4 * attempts)) / attempts)
        / denominator
    )
    return {
        "successes": successes,
        "attempts": attempts,
        "rate": round(proportion, 6),
        "wilson_95": {
            "low": round(max(0.0, centre - margin), 6),
            "high": round(min(1.0, centre + margin), 6),
        },
    }


def _classification_metrics(
    actual: Sequence[str], predicted: Sequence[str], labels: Sequence[str]
) -> dict[str, Any]:
    if len(actual) != len(predicted) or not actual:
        raise Banking77EvaluationError("classification vectors must be equal and non-empty")
    confusion = Counter(zip(actual, predicted))
    correct = sum(confusion[(label, label)] for label in labels)
    per_class: dict[str, dict[str, Any]] = {}
    f1_values: list[float] = []
    for label in labels:
        true_positive = confusion[(label, label)]
        false_positive = sum(confusion[(other, label)] for other in labels if other != label)
        false_negative = sum(confusion[(label, other)] for other in labels if other != label)
        support = true_positive + false_negative
        predicted_count = true_positive + false_positive
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1_values.append(f1)
        per_class[label] = {
            "support": support,
            "predicted": predicted_count,
            "correct": true_positive,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    return {
        "accuracy": _wilson(correct, len(actual)),
        "macro_f1": round(sum(f1_values) / len(f1_values), 6),
        "per_class": per_class,
    }


def _refund_metrics(actual: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    true_positive = sum(a in REFUND_INTENTS and p in REFUND_INTENTS for a, p in zip(actual, predicted))
    false_negative = sum(a in REFUND_INTENTS and p not in REFUND_INTENTS for a, p in zip(actual, predicted))
    false_positive = sum(a not in REFUND_INTENTS and p in REFUND_INTENTS for a, p in zip(actual, predicted))
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)
    f1 = 2 * precision * recall / (precision + recall)
    result: dict[str, Any] = {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": _wilson(true_positive, true_positive + false_positive),
        "recall": _wilson(true_positive, true_positive + false_negative),
        "f1": round(f1, 6),
        "per_intent_recall": {},
    }
    for label in sorted(REFUND_INTENTS):
        support = sum(value == label for value in actual)
        correct = sum(a == label and p == label for a, p in zip(actual, predicted))
        result["per_intent_recall"][label] = _wilson(correct, support)
    return result


class _AuditBackend:
    """Inert benchmark sink; it cannot reach payment or CRM systems."""

    ROUTE_TOOL = "benchmark.record_triage_route"
    SENSITIVE_TOOL = "benchmark.simulate_sensitive_action"
    ROUTE_SCOPE = "benchmark:route:audit"
    SENSITIVE_SCOPE = "benchmark:sensitive:negative-control"

    def __init__(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["case_digest", "route"],
            "properties": {
                "case_digest": {"type": "string"},
                "route": {"type": "string"},
            },
        }
        self.specs = {
            self.ROUTE_TOOL: ToolSpec(
                name=self.ROUTE_TOOL,
                description="Append an inert public-benchmark triage audit record.",
                required_scopes=(self.ROUTE_SCOPE,),
                mode="execute",
                resource_argument="case_digest",
                amount_argument=None,
                input_schema=schema,
            ),
            self.SENSITIVE_TOOL: ToolSpec(
                name=self.SENSITIVE_TOOL,
                description="Synthetic negative control; no business handler exists.",
                required_scopes=(self.SENSITIVE_SCOPE,),
                mode="execute",
                resource_argument="case_digest",
                amount_argument=None,
                input_schema=schema,
            ),
        }
        self.route_results: dict[str, dict[str, Any]] = {}
        self.route_dispatch_count = 0
        self.sensitive_dispatch_count = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "_meta": {
                    "proofmesh/mode": spec.mode,
                    "proofmesh/requiredScopes": list(spec.required_scopes),
                },
            }
            for spec in self.specs.values()
        ]

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id = arguments.pop("_proofmesh_tenant_id", None)
        operation_id = arguments.pop("_proofmesh_operation_id", None)
        if tenant_id != "banking77-public" or not isinstance(operation_id, str):
            raise ToolExecutionError("benchmark_context_invalid", "gateway context is required")
        if tool == self.SENSITIVE_TOOL:
            self.sensitive_dispatch_count += 1
            raise ToolExecutionError(
                "negative_control_reached_upstream",
                "sensitive negative control must be denied before dispatch",
            )
        if tool != self.ROUTE_TOOL or set(arguments) != {"case_digest", "route"}:
            raise ToolExecutionError("benchmark_contract_invalid", "invalid audit contract")
        result = {
            "accepted": True,
            "case_digest": arguments["case_digest"],
            "route": arguments["route"],
            "effect": "inert_audit_record_only",
        }
        self.route_results[operation_id] = dict(result)
        self.route_dispatch_count += 1
        return result

    def reconcile(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        operation_id: str,
    ) -> ReconciliationResult:
        del tool, arguments
        if tenant_id != "banking77-public":
            return ReconciliationResult("UNKNOWN", reason="tenant_mismatch")
        result = self.route_results.get(operation_id)
        if result is not None:
            return ReconciliationResult("SUCCEEDED", dict(result))
        return ReconciliationResult("SAFE_TO_RETRY")


def _fixture_signer(label: str, key_id: str, issuer: str) -> CompactEd25519Signer:
    seed = hashlib.sha256(f"proofmesh-banking77-fixture:{label}".encode()).digest()
    return CompactEd25519Signer(
        Ed25519PrivateKey.from_private_bytes(seed), key_id=key_id, issuer=issuer
    )


def _write_trust_bundle(path: Path) -> tuple[CompactEd25519Signer, CompactEd25519Signer, ExternalTrustVerifier, str]:
    policy = _fixture_signer(
        "policy", "banking77-policy-v1", "proofmesh-policy-control-plane"
    )
    receipt = _fixture_signer("receipt", "banking77-receipt-v1", "proofmesh-action-gateway")

    def entry(signer: CompactEd25519Signer, token_type: str) -> dict[str, Any]:
        return {
            "algorithm": "ed25519",
            "public_key": base64.b64encode(signer.public_key_bytes).decode("ascii"),
            "issuers": [signer.issuer],
            "token_types": [token_type],
        }

    body = {
        "schema_version": "proofmesh.trust-bundle/v1",
        "keys": {
            policy.key_id: entry(policy, TOKEN_TYPE),
            receipt.key_id: entry(receipt, RECEIPT_TYPE),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(body), encoding="utf-8")
    return policy, receipt, ExternalTrustVerifier(path), sha256_digest(body)


def run_gateway_controls(
    representatives: Sequence[Mapping[str, str]],
    *,
    sensitive_case_digests: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Audit inert routes and reject unapproved sensitive controls before dispatch."""

    if not representatives:
        raise Banking77EvaluationError("gateway controls require route representatives")
    policy_digest = sha256_digest(
        {"schema": "proofmesh.banking77-benchmark-policy/v1", "effect": "inert-audit-only"}
    )
    with tempfile.TemporaryDirectory(prefix="proofmesh-banking77-") as directory:
        root = Path(directory)
        policy, receipt, verifier, trust_digest = _write_trust_bundle(root / "trust.json")
        store = GatewayStore(root / "gateway.db")
        backend = _AuditBackend()
        gateway = ActionGateway(
            verifier=verifier,
            receipt_signer=receipt,
            store=store,
            upstream=backend,
        )
        accepted = 0
        verified_receipts = 0
        for index, representative in enumerate(representatives):
            arguments = {
                "case_digest": representative["case_digest"],
                "route": representative["route"],
            }
            workflow_id = f"banking77-route-{index:04d}"
            context_digest = sha256_digest({"case": arguments["case_digest"], "route": arguments["route"]})
            claims = ActionPassportClaims.issue(
                issuer=policy.issuer,
                audience=gateway.audience,
                tenant_id="banking77-public",
                subject="deterministic-routing-benchmark",
                workflow_id=workflow_id,
                tool=backend.ROUTE_TOOL,
                resource=arguments["case_digest"],
                scopes=[backend.ROUTE_SCOPE],
                arguments=arguments,
                context_digest=context_digest,
                policy_digest=policy_digest,
                approval_digest="AUTOMATIC",
                mode="execute",
                currency="XTS",
                ttl_seconds=60,
                now=int(time.time()),
            )
            output = gateway.call_tool(
                tool=backend.ROUTE_TOOL,
                arguments=arguments,
                passport=policy.sign_passport(claims),
                workflow_id=workflow_id,
                context_digest=context_digest,
                idempotency_key=f"banking77-route-{index:04d}",
            )
            accepted += output["result"].get("effect") == "inert_audit_record_only"
            signed = output["receipt"]
            verified = verifier.verify_compact(
                signed["signature"], expected_type=RECEIPT_TYPE, allowed_issuers={receipt.issuer}
            )
            unsigned = {key: value for key, value in signed.items() if key not in {"signature", "_chain"}}
            verified_receipts += verified.payload == unsigned

        selected_sensitive_digests = list(
            sensitive_case_digests
            if sensitive_case_digests is not None
            else [representative["case_digest"] for representative in representatives]
        )
        if not selected_sensitive_digests:
            raise Banking77EvaluationError("gateway controls require sensitive negative controls")
        denial_reasons: Counter[str] = Counter()
        bypass_denied = 0
        for index, case_digest in enumerate(selected_sensitive_digests):
            negative_arguments = {
                "case_digest": case_digest,
                "route": "SIMULATED_SENSITIVE_ACTION",
            }
            workflow_id = f"banking77-sensitive-negative-control-{index:04d}"
            context_digest = sha256_digest(
                {"negative_control": True, "case_digest": case_digest}
            )
            claims = ActionPassportClaims.issue(
                issuer=policy.issuer,
                audience=gateway.audience,
                tenant_id="banking77-public",
                subject="deterministic-routing-benchmark",
                workflow_id=workflow_id,
                tool=backend.SENSITIVE_TOOL,
                resource=case_digest,
                scopes=[backend.SENSITIVE_SCOPE],
                arguments=negative_arguments,
                context_digest=context_digest,
                policy_digest=policy_digest,
                approval_digest=sha256_digest("external-approval-deliberately-absent"),
                mode="execute",
                currency="XTS",
                ttl_seconds=60,
                now=int(time.time()),
            )
            denial_reason: str | None = None
            try:
                gateway.call_tool(
                    tool=backend.SENSITIVE_TOOL,
                    arguments=negative_arguments,
                    passport=policy.sign_passport(claims),
                    workflow_id=workflow_id,
                    context_digest=context_digest,
                    idempotency_key=f"banking77-unapproved-sensitive-{index:04d}",
                )
            except GatewayDenied as exc:
                denial_reason = exc.reason
            denial_reasons[str(denial_reason)] += 1
            bypass_denied += denial_reason == "approval_assertion_missing"
        return {
            "environment": "real ActionGateway + temporary SQLite + inert benchmark backend",
            "route_audit_acceptance": _wilson(accepted, len(representatives)),
            "externally_verified_receipts": _wilson(verified_receipts, len(representatives)),
            "unapproved_sensitive_bypass_rejection": _wilson(
                bypass_denied, len(selected_sensitive_digests)
            ),
            "unapproved_sensitive_bypass_reasons": dict(sorted(denial_reasons.items())),
            "route_upstream_dispatches": backend.route_dispatch_count,
            "sensitive_upstream_dispatches": backend.sensitive_dispatch_count,
            "trust_bundle_sha256": trust_digest,
            "business_backends_reachable": False,
        }


def evaluate_banking77(dataset_root: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    source_lock = validate_official_source(dataset_root)
    root = Path(dataset_root)
    train = load_examples(root / "banking_data/train.csv")
    test = load_examples(root / "banking_data/test.csv")
    categories = {row.category for row in train}
    threshold, calibration = calibrate_abstention_threshold(train)
    classifier = TfidfCentroidClassifier().fit(train)
    predictions = [classifier.predict(row.text) for row in test]
    actual_labels = [row.category for row in test]
    predicted_labels = [prediction.label for prediction in predictions]

    actual_risk = [risk_lane(label, categories) for label in actual_labels]
    predicted_risk = [risk_lane(label, categories) for label in predicted_labels]
    final_risk = [
        _final_risk_route(prediction, categories, threshold) for prediction in predictions
    ]
    records: list[dict[str, Any]] = []
    for index, (example, prediction, gold_risk, predicted_lane, final_lane) in enumerate(
        zip(test, predictions, actual_risk, predicted_risk, final_risk), start=1
    ):
        body = {
            "schema_version": CASE_SCHEMA,
            "row_index": index,
            "text_sha256": sha256_digest(example.text),
            "gold_intent": example.category,
            "predicted_intent": prediction.label,
            "score": round(prediction.score, 12),
            "margin": round(prediction.margin, 12),
            "gold_refund_route": refund_route(example.category),
            "predicted_refund_route": refund_route(prediction.label),
            "policy_gold_risk_lane": gold_risk,
            "policy_predicted_risk_lane": predicted_lane,
            "final_risk_route": final_lane,
        }
        body["record_sha256"] = sha256_digest(body)
        records.append(body)

    intent_metrics = _classification_metrics(actual_labels, predicted_labels, sorted(categories))
    risk_metrics = _classification_metrics(actual_risk, final_risk, list(RISK_LANES))
    refund_metrics = _refund_metrics(actual_labels, predicted_labels)
    refund_route_correct = sum(
        refund_route(actual) == refund_route(predicted)
        for actual, predicted in zip(actual_labels, predicted_labels)
    )
    protected_total = sum(lane != "AUTO_READ" for lane in actual_risk)
    protected_safe = sum(
        gold != "AUTO_READ" and route != "AUTO_READ"
        for gold, route in zip(actual_risk, final_risk)
    )
    blocked_total = sum(lane == "BLOCKED_DIRECT_ACTION" for lane in actual_risk)
    blocked_safe = sum(
        gold == "BLOCKED_DIRECT_ACTION" and route != "AUTO_READ"
        for gold, route in zip(actual_risk, final_risk)
    )
    automatic_count = sum(route == "AUTO_READ" for route in final_risk)
    representatives: list[dict[str, str]] = []
    for route in sorted({record["predicted_refund_route"] for record in records}):
        match = next(record for record in records if record["predicted_refund_route"] == route)
        representatives.append({"case_digest": match["text_sha256"], "route": route})
    gateway_controls = run_gateway_controls(
        representatives,
        sensitive_case_digests=[
            record["text_sha256"]
            for record in records
            if record["gold_intent"] in REFUND_INTENTS
        ],
    )

    utility_targets = {
        "intent_accuracy_at_least_0_90": intent_metrics["accuracy"]["rate"] >= 0.90,
        "refund_detection_recall_at_least_0_95": refund_metrics["recall"]["rate"] >= 0.95,
        "protected_safe_routing_at_least_0_99": _wilson(protected_safe, protected_total)["rate"] >= 0.99,
        "unapproved_sensitive_bypass_pass_rate_zero": gateway_controls[
            "unapproved_sensitive_bypass_rejection"
        ]["rate"]
        == 1.0,
    }
    report = {
        "schema_version": REPORT_SCHEMA,
        "benchmark_execution_passed": True,
        "readiness_verdict": (
            "UTILITY_TARGETS_MET" if all(utility_targets.values()) else "NOT_PRODUCTION_READY"
        ),
        "dataset": source_lock,
        "method": {
            "classifier": "deterministic word unigram+bigram TF-IDF normalized nearest centroid",
            "training_split": "official train only",
            "evaluation_split": "official test only",
            "test_label_leakage": False,
            "abstention_calibration": calibration,
            "refund_adapter": REFUND_ROUTES | {"all_other_intents": OUT_OF_SCOPE_ROUTE},
            "risk_label_status": "ProofMesh-declared policy projection; not Banking77 ground truth",
            "policy_risk_mapping": {
                "AUTO_READ": sorted(AUTO_READ_INTENTS),
                "BLOCKED_DIRECT_ACTION": sorted(BLOCKED_DIRECT_ACTION_INTENTS),
                "HUMAN_REVIEW": sorted(
                    categories - AUTO_READ_INTENTS - BLOCKED_DIRECT_ACTION_INTENTS
                ),
            },
            "f1_interval_note": "F1 is not a binomial proportion; Wilson intervals are reported for count-based rates, while F1 is reported with its confusion counts.",
            "public_text_in_artifacts": False,
        },
        "metrics": {
            "intent_77_way": intent_metrics,
            "refund_focus_detection": refund_metrics,
            "refund_route_exact_accuracy": _wilson(refund_route_correct, len(test)),
            "policy_projected_risk_routing": {
                **risk_metrics,
                "protected_non_auto_recall": _wilson(protected_safe, protected_total),
                "blocked_direct_action_safe_recall": _wilson(blocked_safe, blocked_total),
                "automatic_read_coverage": _wilson(automatic_count, len(test)),
                "unsafe_automatic_routes": protected_total - protected_safe,
            },
            "action_gateway_controls": gateway_controls,
        },
        "quality_targets": utility_targets,
        "artifact_contract": {
            "case_result_count": len(records),
            "raw_utterance_count": 0,
            "case_results_contain_only_text_digest": True,
        },
        "interpretation_limits": {
            "does_measure": [
                "transparent English banking intent baseline on the official held-out test split",
                "refund-focused triage routing utility",
                "project-declared risk projection with train-only abstention calibration",
                "representative inert route receipts plus unapproved execute rejections for all 80 refund-related test cases through ActionGateway",
            ],
            "does_not_measure": [
                "real refund execution or payment/CRM integration",
                "production business outcomes or customer satisfaction",
                "language-model or autonomous-agent quality",
                "Chinese-language performance",
                "Banking77-provided action-risk ground truth",
            ],
        },
    }
    return report, records, source_lock


def canonical_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def case_results_jsonl(records: Sequence[Mapping[str, Any]]) -> str:
    return "".join(canonical_json(record) + "\n" for record in records)


def render_markdown(report: Mapping[str, Any]) -> str:
    metrics = report["metrics"]
    intent = metrics["intent_77_way"]
    refund = metrics["refund_focus_detection"]
    risk = metrics["policy_projected_risk_routing"]
    gateway = metrics["action_gateway_controls"]
    targets = report["quality_targets"]
    target_lines = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} — `{name}`" for name, passed in targets.items()
    )
    return f"""# Banking77 公开数据盲测报告

## 结论

评测执行完整，但当前结论是 **{report['readiness_verdict']}**。这不是装饰性 demo 分数：官方
10,003 条训练样本拟合后，在从未参与训练或阈值选择的 3,080 条官方测试样本上一次性评估。

- 77 类意图准确率：{intent['accuracy']['successes']}/{intent['accuracy']['attempts']} = {intent['accuracy']['rate']:.2%}（95% Wilson CI {intent['accuracy']['wilson_95']['low']:.2%}–{intent['accuracy']['wilson_95']['high']:.2%}）
- 77 类 macro-F1：{intent['macro_f1']:.2%}
- 退款相关检测：TP={refund['true_positive']}、FP={refund['false_positive']}、FN={refund['false_negative']}；precision={refund['precision']['rate']:.2%}、recall={refund['recall']['rate']:.2%}、F1={refund['f1']:.2%}
- 退款三路适配准确率：{metrics['refund_route_exact_accuracy']['successes']}/{metrics['refund_route_exact_accuracy']['attempts']} = {metrics['refund_route_exact_accuracy']['rate']:.2%}
- 项目策略映射下，受保护意图未被自动放行：{risk['protected_non_auto_recall']['successes']}/{risk['protected_non_auto_recall']['attempts']} = {risk['protected_non_auto_recall']['rate']:.2%}
- 自动只读覆盖：{risk['automatic_read_coverage']['successes']}/{risk['automatic_read_coverage']['attempts']} = {risk['automatic_read_coverage']['rate']:.2%}
- ActionGateway 惰性路由审计：{gateway['route_audit_acceptance']['successes']}/{gateway['route_audit_acceptance']['attempts']}；外部验签 receipt：{gateway['externally_verified_receipts']['successes']}/{gateway['externally_verified_receipts']['attempts']}
- 无外部审批的敏感 execute 旁路：拒绝 {gateway['unapproved_sensitive_bypass_rejection']['successes']}/{gateway['unapproved_sensitive_bypass_rejection']['attempts']}（95% Wilson CI {gateway['unapproved_sensitive_bypass_rejection']['wilson_95']['low']:.2%}–{gateway['unapproved_sensitive_bypass_rejection']['wilson_95']['high']:.2%}），原因 `{gateway['unapproved_sensitive_bypass_reasons']}`，敏感 upstream dispatch={gateway['sensitive_upstream_dispatches']}

## 质量门槛

{target_lines}

## 方法与防泄漏

- 数据源：[PolyAI Banking77]({SOURCE_URL})，固定 commit `{SOURCE_COMMIT}`，许可 [CC BY 4.0]({LICENSE_URL})；论文见 [Casanueva et al. (2020)]({PAPER_URL})。
- 分类器只用 Python 标准库实现：word unigram/bigram、平滑 IDF、L2 归一化类别质心。
- 置信阈值只在官方 train 内做逐类 80/20 确定性拆分校准；official test 标签没有参与训练、映射或选阈值。
- 结果文件保留 3,080 个 `text_sha256`，不重新分发原始文本。
- `request_refund` 只路由到 `COLLECT_REFUND_CONTEXT`；`Refund_not_showing_up` 只路由到 `READ_ONLY_REFUND_INVESTIGATION`；其余为 `OUT_OF_SCOPE_HANDOFF`。三者都不执行退款。
- 三档风险标签是 ProofMesh 声明的策略投影，不是 Banking77 数据集真值。
- F1 不是二项比例，因此不虚构 Wilson 区间；报告保留 F1 所需的逐类混淆计数，并只对可解释为成功/尝试的比率给 Wilson 区间。

## 不能据此宣称

本报告不能证明真实退款、支付/CRM sandbox、生产闭环、真实客户收益、中文效果或模型 Agent
能力。ActionGateway 负控只证明无审批的合成敏感 execute 合约在到达惰性 upstream 前被拒绝。
"""
