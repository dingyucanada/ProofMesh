"""Deterministic authorization-contract replay for public AgentDojo cases.

This module deliberately measures the ProofMesh gateway contract, not an agent or
language model.  It replays normalized user ground-truth calls through the real
``ActionGateway`` with an inert trace backend and verifies the resulting receipts
against a trust bundle supplied outside the case artifact.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import sqlite3
import tempfile
import threading
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

from .public_case import verify_content_digest


REPORT_SCHEMA = "proofmesh.authorization-contract-replay-report/v1"
CALL_ENVELOPE_SCHEMA = "proofmesh.benchmark-call-envelope/v1"
TRACE_RESULT_SCHEMA = "proofmesh.benchmark-trace-result/v1"
TRUST_BUNDLE_SCHEMA = "proofmesh.trust-bundle/v1"
TENANT_ID = "agentdojo-v1"
SUBJECT = "benchmark-contract-runner"
AUDIENCE = "proofmesh-mcp-gateway"
TRACE_SCOPE = "benchmark:trace:user"
POLICY = {
    "schema_version": "proofmesh.benchmark-authorization-policy/v1",
    "allowed_lane": "user",
    "required_scope": TRACE_SCOPE,
    "side_effect": "append-one-deterministic-trace-record",
}
POLICY_DIGEST = sha256_digest(POLICY)
WILSON_Z_95 = 1.959963984540054


class ReplayError(RuntimeError):
    """The replay input or an invariant is invalid."""


@dataclass(frozen=True)
class ReplayCall:
    case_id: str
    suite: str
    case_content_sha256: str
    sequence: int
    function: str
    source_arguments: dict[str, Any]
    source_call_sha256: str

    def envelope(self) -> dict[str, Any]:
        return {
            "schema_version": CALL_ENVELOPE_SCHEMA,
            "case_id": self.case_id,
            "case_content_sha256": self.case_content_sha256,
            "lane": "user",
            "sequence": self.sequence,
            "source_function": self.function,
            "source_arguments": copy.deepcopy(self.source_arguments),
            "source_call_sha256": self.source_call_sha256,
        }


@dataclass(frozen=True)
class CasePlan:
    case_id: str
    suite: str
    user_task_id: str
    injection_task_id: str
    content_sha256: str
    calls: tuple[ReplayCall, ...]


@dataclass(frozen=True)
class LoadedBenchmark:
    artifact_path: Path
    artifact_sha256: str
    artifact_byte_count: int
    plans: tuple[CasePlan, ...]
    registered_functions: tuple[str, ...]
    injection_ground_truth_call_count: int
    placeholder_argument_call_count: int
    unresolved_placeholder_count: int


@dataclass(frozen=True)
class ExpectedExecution:
    call: ReplayCall
    workflow_id: str
    context_digest: str
    claims: ActionPassportClaims
    result: dict[str, Any]


@dataclass(frozen=True)
class AttackOutcome:
    passed_gateway: bool
    reason: str
    latency_ms: float


class TraceBackend:
    """An inert, deterministic backend that records exactly one effect per contract."""

    _ENVELOPE_KEYS = {
        "schema_version",
        "case_id",
        "case_content_sha256",
        "lane",
        "sequence",
        "source_function",
        "source_arguments",
        "source_call_sha256",
    }

    def __init__(
        self,
        calls: Iterable[ReplayCall],
        *,
        registered_functions: Iterable[str],
        tenant_id: str = TENANT_ID,
    ):
        self.tenant_id = tenant_id
        call_list = list(calls)
        self._expected = {(call.case_id, call.sequence): call for call in call_list}
        if len(self._expected) != len(call_list):
            raise ReplayError("duplicate case/sequence trace contract")
        self.specs = {
            function: ToolSpec(
                name=function,
                description="Replay one normalized public-benchmark call into an inert trace sink.",
                required_scopes=(TRACE_SCOPE,),
                mode="execute",
                resource_argument="case_id",
                amount_argument=None,
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(self._ENVELOPE_KEYS),
                    "properties": {
                        "schema_version": {"const": CALL_ENVELOPE_SCHEMA},
                        "case_id": {"type": "string"},
                        "case_content_sha256": {"type": "string"},
                        "lane": {"const": "user"},
                        "sequence": {"type": "integer", "minimum": 0},
                        "source_function": {"type": "string"},
                        "source_arguments": {"type": "object"},
                        "source_call_sha256": {"type": "string"},
                    },
                },
            )
            for function in sorted(set(registered_functions))
        }
        if not self.specs:
            raise ReplayError("trace backend requires at least one registered function")
        self._lock = threading.Lock()
        self._results_by_operation: dict[str, dict[str, Any]] = {}
        self._dispatched_contracts: set[tuple[str, int]] = set()
        self._dispatches_by_case: Counter[str] = Counter()
        self._dispatch_count = 0
        self._duplicate_effect_count = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
                "_meta": {
                    "proofmesh/mode": spec.mode,
                    "proofmesh/requiredScopes": list(spec.required_scopes),
                },
            }
            for spec in self.specs.values()
        ]

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = dict(arguments)
        tenant_id = envelope.pop("_proofmesh_tenant_id", None)
        operation_id = envelope.pop("_proofmesh_operation_id", None)
        if tenant_id != self.tenant_id or not isinstance(operation_id, str) or not operation_id:
            raise ToolExecutionError(
                "benchmark_gateway_context_invalid",
                "TraceBackend requires the gateway-injected tenant and operation identity.",
            )
        if set(envelope) != self._ENVELOPE_KEYS:
            raise ToolExecutionError(
                "benchmark_envelope_invalid",
                "Trace envelope fields differ from the closed benchmark schema.",
            )
        if envelope.get("schema_version") != CALL_ENVELOPE_SCHEMA:
            raise ToolExecutionError("benchmark_envelope_invalid", "Trace envelope schema is unsupported.")
        if envelope.get("lane") != "user":
            raise ToolExecutionError(
                "benchmark_policy_bypass",
                "Only the user ground-truth lane may reach the trace backend.",
            )
        case_id = envelope.get("case_id")
        sequence = envelope.get("sequence")
        if not isinstance(case_id, str) or not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ToolExecutionError("benchmark_envelope_invalid", "Case identity or sequence is invalid.")
        expected = self._expected.get((case_id, sequence))
        if expected is None or tool != expected.function or envelope != expected.envelope():
            raise ToolExecutionError(
                "benchmark_contract_mismatch",
                "Dispatched call does not equal the pinned public-benchmark contract.",
            )
        result = {
            "schema_version": TRACE_RESULT_SCHEMA,
            "accepted": True,
            "case_id": case_id,
            "lane": "user",
            "sequence": sequence,
            "source_function": tool,
            "source_call_sha256": expected.source_call_sha256,
            "trace_sha256": sha256_digest(
                {
                    "case_id": case_id,
                    "sequence": sequence,
                    "source_function": tool,
                    "source_call_sha256": expected.source_call_sha256,
                }
            ),
        }
        contract_key = (case_id, sequence)
        with self._lock:
            existing = self._results_by_operation.get(operation_id)
            if existing is not None:
                return copy.deepcopy(existing)
            if contract_key in self._dispatched_contracts:
                self._duplicate_effect_count += 1
            self._dispatched_contracts.add(contract_key)
            self._results_by_operation[operation_id] = copy.deepcopy(result)
            self._dispatches_by_case[case_id] += 1
            self._dispatch_count += 1
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
        if tenant_id != self.tenant_id:
            return ReconciliationResult("UNKNOWN", reason="benchmark_tenant_mismatch")
        with self._lock:
            result = self._results_by_operation.get(operation_id)
        if result is not None:
            return ReconciliationResult("SUCCEEDED", copy.deepcopy(result))
        return ReconciliationResult("SAFE_TO_RETRY")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "dispatch_count": self._dispatch_count,
                "duplicate_effect_count": self._duplicate_effect_count,
                "dispatches_by_case": dict(sorted(self._dispatches_by_case.items())),
                "operation_count": len(self._results_by_operation),
            }


def load_benchmark(path: str | Path, *, limit: int | None = None) -> LoadedBenchmark:
    """Load and validate normalized cases without importing AgentDojo."""

    artifact_path = Path(path)
    raw = artifact_path.read_bytes()
    if limit is not None and limit < 1:
        raise ReplayError("limit must be at least one")
    plans: list[CasePlan] = []
    functions: set[str] = set()
    seen_case_ids: set[str] = set()
    injection_call_count = 0
    placeholder_call_count = 0
    unresolved_count = 0
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if limit is not None and len(plans) >= limit:
            break
        try:
            payload = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReplayError(f"invalid benchmark JSON at line {line_number}: {exc}") from exc
        if not isinstance(payload, dict) or not verify_content_digest(payload):
            raise ReplayError(f"invalid case content digest at line {line_number}")
        case_id = payload.get("case_id")
        case_digest = payload.get("content_sha256")
        source = payload.get("source")
        user_task = payload.get("user_task")
        injection_task = payload.get("injection_task")
        ground_truth = payload.get("ground_truth")
        if (
            not isinstance(case_id, str)
            or not isinstance(case_digest, str)
            or not isinstance(source, dict)
            or not isinstance(user_task, dict)
            or not isinstance(injection_task, dict)
            or not isinstance(ground_truth, dict)
        ):
            raise ReplayError(f"incomplete benchmark case at line {line_number}")
        if case_id in seen_case_ids:
            raise ReplayError(f"duplicate benchmark case_id: {case_id}")
        seen_case_ids.add(case_id)
        suite = source.get("suite")
        user_task_id = user_task.get("id")
        injection_task_id = injection_task.get("id")
        user_calls = ground_truth.get("user")
        injection_calls = ground_truth.get("injection")
        if (
            not isinstance(suite, str)
            or not isinstance(user_task_id, str)
            or not isinstance(injection_task_id, str)
            or not isinstance(user_calls, list)
            or not user_calls
            or not isinstance(injection_calls, list)
        ):
            raise ReplayError(f"invalid task or ground truth at line {line_number}")
        calls: list[ReplayCall] = []
        for expected_sequence, call_payload in enumerate(user_calls):
            call = _parse_call(
                call_payload,
                case_id=case_id,
                suite=suite,
                case_digest=case_digest,
                expected_sequence=expected_sequence,
                line_number=line_number,
            )
            calls.append(call)
            functions.add(call.function)
            if call_payload.get("placeholder_arguments") is not None:
                placeholder_call_count += 1
            unresolved = call_payload.get("unresolved_placeholders")
            if isinstance(unresolved, list):
                unresolved_count += len(unresolved)
        for expected_sequence, call_payload in enumerate(injection_calls):
            _validate_source_call(call_payload, expected_sequence=expected_sequence, line_number=line_number)
            function = call_payload.get("function")
            if isinstance(function, str):
                functions.add(function)
            injection_call_count += 1
            if call_payload.get("placeholder_arguments") is not None:
                placeholder_call_count += 1
            unresolved = call_payload.get("unresolved_placeholders")
            if isinstance(unresolved, list):
                unresolved_count += len(unresolved)
        plans.append(
            CasePlan(
                case_id=case_id,
                suite=suite,
                user_task_id=user_task_id,
                injection_task_id=injection_task_id,
                content_sha256=case_digest,
                calls=tuple(calls),
            )
        )
    if not plans:
        raise ReplayError("benchmark artifact contains no selected cases")
    return LoadedBenchmark(
        artifact_path=artifact_path,
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
        artifact_byte_count=len(raw),
        plans=tuple(plans),
        registered_functions=tuple(sorted(functions)),
        injection_ground_truth_call_count=injection_call_count,
        placeholder_argument_call_count=placeholder_call_count,
        unresolved_placeholder_count=unresolved_count,
    )


def _validate_source_call(call: Any, *, expected_sequence: int, line_number: int) -> None:
    if not isinstance(call, dict) or not verify_content_digest(call):
        raise ReplayError(f"invalid ground-truth call digest at line {line_number}")
    if call.get("sequence") != expected_sequence:
        raise ReplayError(f"non-contiguous call sequence at line {line_number}")
    if not isinstance(call.get("function"), str) or not isinstance(call.get("arguments"), dict):
        raise ReplayError(f"invalid ground-truth call fields at line {line_number}")


def _parse_call(
    payload: Any,
    *,
    case_id: str,
    suite: str,
    case_digest: str,
    expected_sequence: int,
    line_number: int,
) -> ReplayCall:
    _validate_source_call(payload, expected_sequence=expected_sequence, line_number=line_number)
    return ReplayCall(
        case_id=case_id,
        suite=suite,
        case_content_sha256=case_digest,
        sequence=expected_sequence,
        function=str(payload["function"]),
        source_arguments=copy.deepcopy(payload["arguments"]),
        source_call_sha256=str(payload["content_sha256"]),
    )


def _fixture_signer(*, label: str, key_id: str, issuer: str) -> CompactEd25519Signer:
    """Create a deterministic benchmark-only signer; never use this for deployment."""

    seed = hashlib.sha256(f"proofmesh-public-benchmark-fixture:{label}".encode("utf-8")).digest()
    return CompactEd25519Signer(
        Ed25519PrivateKey.from_private_bytes(seed),
        key_id=key_id,
        issuer=issuer,
    )


def _write_external_trust_bundle(
    path: Path,
) -> tuple[CompactEd25519Signer, CompactEd25519Signer, ExternalTrustVerifier, str]:
    policy_signer = _fixture_signer(
        label="policy-v1",
        key_id="proofmesh-benchmark-policy-v1",
        issuer="proofmesh-policy-control-plane",
    )
    receipt_signer = _fixture_signer(
        label="receipt-v1",
        key_id="proofmesh-benchmark-receipt-v1",
        issuer="proofmesh-action-gateway",
    )

    def key_entry(signer: CompactEd25519Signer, token_types: Sequence[str]) -> dict[str, Any]:
        return {
            "algorithm": "ed25519",
            "public_key": base64.b64encode(signer.public_key_bytes).decode("ascii"),
            "issuers": [signer.issuer],
            "token_types": list(token_types),
        }

    bundle = {
        "schema_version": TRUST_BUNDLE_SCHEMA,
        "keys": {
            policy_signer.key_id: key_entry(policy_signer, [TOKEN_TYPE]),
            receipt_signer.key_id: key_entry(receipt_signer, [RECEIPT_TYPE]),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(bundle), encoding="utf-8")
    return policy_signer, receipt_signer, ExternalTrustVerifier(path), sha256_digest(bundle)


def _workflow_id(case_id: str) -> str:
    return f"bench-{sha256_digest(case_id)[:40]}"


def _context_digest(plan: CasePlan) -> str:
    return sha256_digest(
        {
            "schema_version": "proofmesh.benchmark-context/v1",
            "case_id": plan.case_id,
            "case_content_sha256": plan.content_sha256,
            "lane": "user",
        }
    )


def _idempotency_key(call: ReplayCall, variant: str = "primary") -> str:
    material = {"case_id": call.case_id, "sequence": call.sequence, "variant": variant}
    return f"idem-{sha256_digest(material)[:48]}"


def _issue_passport(
    signer: CompactEd25519Signer,
    gateway: ActionGateway,
    plan: CasePlan,
    call: ReplayCall,
    *,
    now: int,
) -> tuple[ActionPassportClaims, str, str]:
    context_digest = _context_digest(plan)
    claims = ActionPassportClaims.issue(
        issuer=signer.issuer,
        audience=gateway.audience,
        tenant_id=TENANT_ID,
        subject=SUBJECT,
        workflow_id=_workflow_id(plan.case_id),
        tool=call.function,
        resource=plan.case_id,
        scopes=[TRACE_SCOPE],
        arguments=call.envelope(),
        context_digest=context_digest,
        policy_digest=POLICY_DIGEST,
        approval_digest="AUTOMATIC",
        mode="execute",
        max_calls=1,
        max_amount_minor=0,
        budget_limit_minor=0,
        currency="CNY",
        ttl_seconds=300,
        now=now,
    ).model_copy(
        update={
            "jti": f"apt-benchmark-{sha256_digest({'case_id': call.case_id, 'sequence': call.sequence})[:40]}"
        }
    )
    return claims, signer.sign_passport(claims), context_digest


def _variant_passport(
    signer: CompactEd25519Signer,
    claims: ActionPassportClaims,
    call: ReplayCall,
    *,
    variant: str,
) -> str:
    digest = sha256_digest(
        {"case_id": call.case_id, "sequence": call.sequence, "variant": variant}
    )
    variant_claims = claims.model_copy(update={"jti": f"apt-negative-{digest[:40]}"})
    return signer.sign_passport(variant_claims)


def _timed_call(function: Any, /, **kwargs: Any) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    try:
        return function(**kwargs), (time.perf_counter_ns() - started) / 1_000_000
    except Exception as exc:
        setattr(exc, "_proofmesh_benchmark_latency_ms", (time.perf_counter_ns() - started) / 1_000_000)
        raise


def _attack(
    gateway: ActionGateway,
    *,
    expected_reason: str,
    kwargs: dict[str, Any],
) -> AttackOutcome:
    try:
        _, latency_ms = _timed_call(gateway.call_tool, **kwargs)
    except GatewayDenied as exc:
        latency = float(getattr(exc, "_proofmesh_benchmark_latency_ms", 0.0))
        return AttackOutcome(False, exc.reason, latency)
    except ToolExecutionError as exc:
        latency = float(getattr(exc, "_proofmesh_benchmark_latency_ms", 0.0))
        return AttackOutcome(True, f"upstream_error:{exc.code}", latency)
    except Exception as exc:
        raise ReplayError(f"attack execution failed unexpectedly: {type(exc).__name__}: {exc}") from exc
    return AttackOutcome(True, f"unexpectedly_allowed:expected_{expected_reason}", latency_ms)


def _wilson_interval(successes: int, attempts: int) -> tuple[float, float]:
    if attempts <= 0 or successes < 0 or successes > attempts:
        raise ReplayError("Wilson interval requires 0 <= successes <= attempts and attempts > 0")
    proportion = successes / attempts
    z2 = WILSON_Z_95**2
    denominator = 1 + z2 / attempts
    centre = (proportion + z2 / (2 * attempts)) / denominator
    margin = (
        WILSON_Z_95
        * math.sqrt((proportion * (1 - proportion) + z2 / (4 * attempts)) / attempts)
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _rate(successes: int, attempts: int) -> dict[str, Any]:
    low, high = _wilson_interval(successes, attempts)
    return {
        "successes": successes,
        "attempts": attempts,
        "rate": round(successes / attempts, 6),
        "wilson_95": {"low": round(low, 6), "high": round(high, 6)},
    }


def _latency(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    ordered = sorted(values)

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
    }


def _expected_result(call: ReplayCall) -> dict[str, Any]:
    return {
        "schema_version": TRACE_RESULT_SCHEMA,
        "accepted": True,
        "case_id": call.case_id,
        "lane": "user",
        "sequence": call.sequence,
        "source_function": call.function,
        "source_call_sha256": call.source_call_sha256,
        "trace_sha256": sha256_digest(
            {
                "case_id": call.case_id,
                "sequence": call.sequence,
                "source_function": call.function,
                "source_call_sha256": call.source_call_sha256,
            }
        ),
    }


def _verify_receipts(
    *,
    store: GatewayStore,
    verifier: ExternalTrustVerifier,
    receipt_issuer: str,
    plans: Sequence[CasePlan],
    executions_by_case: Mapping[str, Sequence[ExpectedExecution]],
) -> dict[str, Any]:
    external_verified = 0
    chain_verified = 0
    actual_receipt_count = 0
    call_ids: set[str] = set()
    duplicate_call_ids = 0
    per_suite_verified: Counter[str] = Counter()
    for plan in plans:
        expected_executions = list(executions_by_case.get(plan.case_id, ()))
        receipts = store.receipts(_workflow_id(plan.case_id), tenant_id=TENANT_ID)
        actual_receipt_count += len(receipts)
        previous_hash = "GENESIS"
        for index, expected in enumerate(expected_executions):
            if index >= len(receipts):
                continue
            receipt = receipts[index]
            signature_ok = False
            try:
                verified = verifier.verify_compact(
                    str(receipt.get("signature", "")),
                    expected_type=RECEIPT_TYPE,
                    allowed_issuers={receipt_issuer},
                )
                unsigned = {
                    key: value
                    for key, value in receipt.items()
                    if key not in {"signature", "_chain"}
                }
                signature_ok = verified.payload == unsigned
            except Exception:
                signature_ok = False
            request_digest = sha256_digest(
                {
                    "workflow_id": expected.workflow_id,
                    "tool": expected.call.function,
                    "arguments": expected.call.envelope(),
                    "context_digest": expected.context_digest,
                }
            )
            call_id = receipt.get("call_id")
            if isinstance(call_id, str) and call_id:
                if call_id in call_ids:
                    duplicate_call_ids += 1
                call_ids.add(call_id)
            semantic_ok = (
                receipt.get("schema_version") == "proofmesh.execution-receipt/v1"
                and receipt.get("issuer") == receipt_issuer
                and receipt.get("workflow_id") == expected.workflow_id
                and receipt.get("tenant_id") == TENANT_ID
                and receipt.get("subject") == SUBJECT
                and receipt.get("tool") == expected.call.function
                and receipt.get("resource") == expected.call.case_id
                and receipt.get("passport_jti") == expected.claims.jti
                and receipt.get("policy_digest") == POLICY_DIGEST
                and receipt.get("approval_digest") == "AUTOMATIC"
                and receipt.get("request_digest") == request_digest
                and receipt.get("result_digest") == sha256_digest(expected.result)
                and receipt.get("decision") == "allowed"
                and receipt.get("reason") is None
                and receipt.get("recovered") is False
                and receipt.get("operation_generation") == 1
                and isinstance(receipt.get("operation_id"), str)
                and bool(receipt.get("operation_id"))
                and isinstance(call_id, str)
                and bool(call_id)
            )
            if signature_ok and semantic_ok:
                external_verified += 1
                per_suite_verified[plan.suite] += 1
            signed_receipt = {key: value for key, value in receipt.items() if key != "_chain"}
            expected_entry_hash = sha256_digest(
                {"previous_hash": previous_hash, "receipt": signed_receipt}
            )
            chain = receipt.get("_chain")
            chain_ok = (
                isinstance(chain, dict)
                and chain.get("previous_hash") == previous_hash
                and chain.get("entry_hash") == expected_entry_hash
            )
            if chain_ok:
                chain_verified += 1
            previous_hash = str(chain.get("entry_hash", "")) if isinstance(chain, dict) else ""
    return {
        "external_verified": external_verified,
        "chain_verified": chain_verified,
        "actual_receipt_count": actual_receipt_count,
        "duplicate_call_id_count": duplicate_call_ids,
        "per_suite_verified": dict(sorted(per_suite_verified.items())),
    }


def _denial_audit(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT reason, COUNT(*) FROM gateway_denials GROUP BY reason ORDER BY reason"
        ).fetchall()
        operation_rows = connection.execute(
            "SELECT status, COUNT(*) FROM gateway_operations GROUP BY status ORDER BY status"
        ).fetchall()
        usage = connection.execute(
            "SELECT COALESCE(SUM(call_count), 0), COUNT(*) FROM capability_usage_v2"
        ).fetchone()
    return {
        "denial_reasons": {str(reason): int(count) for reason, count in rows},
        "denial_count": sum(int(count) for _, count in rows),
        "operation_statuses": {str(status): int(count) for status, count in operation_rows},
        "capability_call_count": int(usage[0]),
        "capability_count": int(usage[1]),
    }


def run_authorization_contract_replay(benchmark: LoadedBenchmark) -> dict[str, Any]:
    """Run the real gateway over all selected case contracts and return a report."""

    all_calls = [call for plan in benchmark.plans for call in plan.calls]
    if not all_calls:
        raise ReplayError("selected benchmark cases have no user calls")
    if len(benchmark.registered_functions) < 2:
        raise ReplayError("tool-mismatch replay needs at least two registered functions")
    suite_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unique_user_tasks: set[tuple[str, str]] = set()
    unique_injection_tasks: set[tuple[str, str]] = set()
    for plan in benchmark.plans:
        suite_counts[plan.suite]["case_count"] += 1
        suite_counts[plan.suite]["user_call_count"] += len(plan.calls)
        unique_user_tasks.add((plan.suite, plan.user_task_id))
        unique_injection_tasks.add((plan.suite, plan.injection_task_id))

    counters: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    latencies: dict[str, list[float]] = defaultdict(list)
    executions_by_case: dict[str, list[ExpectedExecution]] = defaultdict(list)
    expected_receipt_count = len(all_calls)
    expected_denial_reasons = {
        "arguments_mismatch": len(all_calls),
        "context_mismatch": len(all_calls),
        "passport_exhausted": len(all_calls),
        "tool_mismatch": len(all_calls),
        "tool_not_registered": len(benchmark.plans),
    }

    with tempfile.TemporaryDirectory(prefix="proofmesh-contract-replay-") as directory:
        temporary_root = Path(directory)
        bundle_path = temporary_root / "external-trust" / "benchmark-trust.json"
        policy_signer, receipt_signer, verifier, trust_bundle_digest = _write_external_trust_bundle(
            bundle_path
        )
        store = GatewayStore(temporary_root / "sqlite" / "gateway.db")
        backend = TraceBackend(
            all_calls,
            registered_functions=benchmark.registered_functions,
        )
        gateway = ActionGateway(
            verifier=verifier,
            receipt_signer=receipt_signer,
            store=store,
            upstream=backend,
            audience=AUDIENCE,
        )
        alternate_tools = {
            tool: next(candidate for candidate in benchmark.registered_functions if candidate != tool)
            for tool in benchmark.registered_functions
        }

        for plan in benchmark.plans:
            first_call_material: tuple[ReplayCall, ActionPassportClaims, str] | None = None
            for call in plan.calls:
                claims, token, context_digest = _issue_passport(
                    policy_signer,
                    gateway,
                    plan,
                    call,
                    now=int(time.time()),
                )
                envelope = call.envelope()
                primary_key = _idempotency_key(call)
                common = {
                    "arguments": envelope,
                    "passport": token,
                    "workflow_id": claims.workflow_id,
                    "context_digest": context_digest,
                }
                counters["legal_attempts"] += 1
                suite_counts[plan.suite]["legal_attempts"] += 1
                try:
                    output, latency = _timed_call(
                        gateway.call_tool,
                        tool=call.function,
                        idempotency_key=primary_key,
                        **common,
                    )
                    latencies["legal_first_dispatch"].append(latency)
                    legal_ok = (
                        output.get("idempotent_replay") is False
                        and output.get("result") == _expected_result(call)
                        and isinstance(output.get("receipt"), dict)
                    )
                    if legal_ok:
                        counters["legal_accepted"] += 1
                        suite_counts[plan.suite]["legal_accepted"] += 1
                        executions_by_case[plan.case_id].append(
                            ExpectedExecution(
                                call=call,
                                workflow_id=claims.workflow_id,
                                context_digest=context_digest,
                                claims=claims,
                                result=copy.deepcopy(output["result"]),
                            )
                        )
                except (GatewayDenied, ToolExecutionError) as exc:
                    latencies["legal_first_dispatch"].append(
                        float(getattr(exc, "_proofmesh_benchmark_latency_ms", 0.0))
                    )

                counters["cached_replay_attempts"] += 1
                try:
                    replay, latency = _timed_call(
                        gateway.call_tool,
                        tool=call.function,
                        idempotency_key=primary_key,
                        **common,
                    )
                    latencies["cached_replay"].append(latency)
                    if (
                        replay.get("idempotent_replay") is True
                        and replay.get("result") == _expected_result(call)
                    ):
                        counters["cached_replay_succeeded"] += 1
                except (GatewayDenied, ToolExecutionError) as exc:
                    latencies["cached_replay"].append(
                        float(getattr(exc, "_proofmesh_benchmark_latency_ms", 0.0))
                    )

                mutated_arguments = copy.deepcopy(envelope)
                mutated_arguments["source_arguments"]["__proofmesh_negative_control__"] = (
                    "same-tool-argument-drift"
                )
                argument_outcome = _attack(
                    gateway,
                    expected_reason="arguments_mismatch",
                    kwargs={
                        **common,
                        "tool": call.function,
                        "arguments": mutated_arguments,
                        "passport": _variant_passport(
                            policy_signer, claims, call, variant="argument-mismatch"
                        ),
                        "idempotency_key": _idempotency_key(call, "argument-mismatch"),
                    },
                )
                _record_attack(
                    counters,
                    reasons,
                    latencies,
                    "argument_mismatch",
                    argument_outcome,
                    expected_reason="arguments_mismatch",
                )

                tool_outcome = _attack(
                    gateway,
                    expected_reason="tool_mismatch",
                    kwargs={
                        **common,
                        "tool": alternate_tools[call.function],
                        "passport": _variant_passport(
                            policy_signer, claims, call, variant="tool-mismatch"
                        ),
                        "idempotency_key": _idempotency_key(call, "tool-mismatch"),
                    },
                )
                _record_attack(
                    counters,
                    reasons,
                    latencies,
                    "tool_mismatch",
                    tool_outcome,
                    expected_reason="tool_mismatch",
                )

                wrong_context = sha256_digest(
                    {"expected_context": context_digest, "mutation": "benchmark-negative-control"}
                )
                context_outcome = _attack(
                    gateway,
                    expected_reason="context_mismatch",
                    kwargs={
                        **common,
                        "tool": call.function,
                        "context_digest": wrong_context,
                        "passport": _variant_passport(
                            policy_signer, claims, call, variant="context-mismatch"
                        ),
                        "idempotency_key": _idempotency_key(call, "context-mismatch"),
                    },
                )
                _record_attack(
                    counters,
                    reasons,
                    latencies,
                    "context_mismatch",
                    context_outcome,
                    expected_reason="context_mismatch",
                )

                reuse_outcome = _attack(
                    gateway,
                    expected_reason="passport_exhausted",
                    kwargs={
                        **common,
                        "tool": call.function,
                        "idempotency_key": _idempotency_key(call, "new-idempotency-key"),
                    },
                )
                _record_attack(
                    counters,
                    reasons,
                    latencies,
                    "single_use_new_key_reuse",
                    reuse_outcome,
                    expected_reason="passport_exhausted",
                )
                if first_call_material is None:
                    first_call_material = (call, claims, context_digest)

            if first_call_material is None:
                raise ReplayError(f"case contains no user ground-truth calls: {plan.case_id}")
            first_call, first_claims, first_context = first_call_material
            unregistered_claims = first_claims.model_copy(
                update={"jti": f"apt-unregistered-{sha256_digest(plan.case_id)[:40]}"}
            )
            unregistered_outcome = _attack(
                gateway,
                expected_reason="tool_not_registered",
                kwargs={
                    "tool": "proofmesh.benchmark.unregistered",
                    "arguments": first_call.envelope(),
                    "passport": policy_signer.sign_passport(unregistered_claims),
                    "workflow_id": _workflow_id(plan.case_id),
                    "context_digest": first_context,
                    "idempotency_key": _idempotency_key(first_call, "unregistered"),
                },
            )
            _record_attack(
                counters,
                reasons,
                latencies,
                "unregistered_call",
                unregistered_outcome,
                expected_reason="tool_not_registered",
            )

        receipt_checks = _verify_receipts(
            store=store,
            verifier=ExternalTrustVerifier(bundle_path),
            receipt_issuer=receipt_signer.issuer,
            plans=benchmark.plans,
            executions_by_case=executions_by_case,
        )
        trace_snapshot = backend.snapshot()
        denial_audit = _denial_audit(store.db_path)

    metrics = {
        "legal_contract_acceptance_rate": _rate(
            counters["legal_accepted"], counters["legal_attempts"]
        ),
        "unregistered_call_pass_rate": _rate(
            counters["unregistered_call_passed"], counters["unregistered_call_attempts"]
        ),
        "same_tool_argument_mismatch_pass_rate": _rate(
            counters["argument_mismatch_passed"], counters["argument_mismatch_attempts"]
        ),
        "tool_mismatch_pass_rate": _rate(
            counters["tool_mismatch_passed"], counters["tool_mismatch_attempts"]
        ),
        "context_mismatch_pass_rate": _rate(
            counters["context_mismatch_passed"], counters["context_mismatch_attempts"]
        ),
        "same_idempotency_key_cached_replay_success_rate": _rate(
            counters["cached_replay_succeeded"], counters["cached_replay_attempts"]
        ),
        "different_idempotency_key_single_use_rejection_rate": _rate(
            counters["single_use_new_key_reuse_expected_reason"],
            counters["single_use_new_key_reuse_attempts"],
        ),
        "receipt_external_verification_rate": _rate(
            receipt_checks["external_verified"], expected_receipt_count
        ),
        "receipt_chain_verification_rate": _rate(
            receipt_checks["chain_verified"], expected_receipt_count
        ),
    }
    expected_denial_count = sum(expected_denial_reasons.values())
    invariant_checks = {
        "all_legal_contracts_accepted": counters["legal_accepted"] == len(all_calls),
        "all_negative_controls_blocked_with_expected_reason": all(
            counters[f"{name}_expected_reason"] == counters[f"{name}_attempts"]
            for name in (
                "argument_mismatch",
                "tool_mismatch",
                "context_mismatch",
                "single_use_new_key_reuse",
                "unregistered_call",
            )
        ),
        "side_effect_count_exact": trace_snapshot["dispatch_count"] == len(all_calls),
        "no_duplicate_side_effects": trace_snapshot["duplicate_effect_count"] == 0,
        "receipt_count_exact": receipt_checks["actual_receipt_count"] == expected_receipt_count,
        "receipt_call_ids_unique": receipt_checks["duplicate_call_id_count"] == 0,
        "denial_audit_count_exact": denial_audit["denial_count"] == expected_denial_count,
        "denial_reasons_exact": denial_audit["denial_reasons"] == expected_denial_reasons,
        "capability_usage_exact": denial_audit["capability_call_count"] == len(all_calls),
        "all_operations_succeeded_once": denial_audit["operation_statuses"]
        == {"SUCCEEDED": len(all_calls)},
    }
    metric_expectations = {
        "legal_contract_acceptance_rate": 1.0,
        "unregistered_call_pass_rate": 0.0,
        "same_tool_argument_mismatch_pass_rate": 0.0,
        "tool_mismatch_pass_rate": 0.0,
        "context_mismatch_pass_rate": 0.0,
        "same_idempotency_key_cached_replay_success_rate": 1.0,
        "different_idempotency_key_single_use_rejection_rate": 1.0,
        "receipt_external_verification_rate": 1.0,
        "receipt_chain_verification_rate": 1.0,
    }
    passed = all(invariant_checks.values()) and all(
        metrics[name]["rate"] == expected for name, expected in metric_expectations.items()
    )
    suite_report = {
        suite: dict(sorted(values.items())) for suite, values in sorted(suite_counts.items())
    }
    return {
        "schema_version": REPORT_SCHEMA,
        "passed": passed,
        "evaluation_scope": {
            "name": "AgentDojo authorization-contract consistency replay",
            "measures": [
                "exact capability binding",
                "gateway rejection controls",
                "idempotent replay semantics",
                "trace side-effect cardinality",
                "externally anchored receipt signature and hash-chain integrity",
            ],
            "does_not_measure": [
                "model attack success rate (ASR)",
                "AgentDojo task utility",
                "prompt-injection detection quality",
                "end-to-end autonomous-agent behavior",
            ],
            "disclaimer": (
                "This is an offline authorization-contract regression over official ground-truth "
                "tool trajectories. It is not a model ASR or utility evaluation."
            ),
        },
        "dataset": {
            "benchmark": "AgentDojo",
            "benchmark_version": "v1",
            "case_count": len(benchmark.plans),
            "unique_user_task_count": len(unique_user_tasks),
            "unique_injection_task_count": len(unique_injection_tasks),
            "user_ground_truth_call_count": len(all_calls),
            "injection_ground_truth_call_count": benchmark.injection_ground_truth_call_count,
            "registered_function_count": len(benchmark.registered_functions),
            "placeholder_argument_call_count": benchmark.placeholder_argument_call_count,
            "unresolved_placeholder_count": benchmark.unresolved_placeholder_count,
            "artifact_path": benchmark.artifact_path.name,
            "artifact_byte_count": benchmark.artifact_byte_count,
            "artifact_sha256": benchmark.artifact_sha256,
            "suites": suite_report,
        },
        "configuration": {
            "gateway": "proofmesh.gateway.ActionGateway",
            "backend": "deterministic in-process TraceBackend",
            "storage": "temporary file-backed SQLite (WAL)",
            "trust_anchor": "external two-key Ed25519 benchmark trust bundle",
            "receipt_verifier": "fresh ExternalTrustVerifier loaded after execution",
            "trust_bundle_sha256": trust_bundle_digest,
            "policy_digest": POLICY_DIGEST,
            "passport_max_calls": 1,
            "latency_method": "wall-clock milliseconds, nearest-rank percentiles",
            "fixture_key_warning": "Deterministic benchmark-only keys are not deployment credentials.",
        },
        "metrics": metrics,
        "negative_control_expected_reason_rates": {
            name: _rate(
                counters[f"{name}_expected_reason"], counters[f"{name}_attempts"]
            )
            for name in (
                "unregistered_call",
                "argument_mismatch",
                "tool_mismatch",
                "context_mismatch",
                "single_use_new_key_reuse",
            )
        },
        "latency_ms": {
            name: _latency(values)
            for name, values in sorted(latencies.items())
        },
        "invariants": {
            "checks": invariant_checks,
            "expected_side_effect_count": len(all_calls),
            "observed_side_effect_count": trace_snapshot["dispatch_count"],
            "duplicate_side_effect_count": trace_snapshot["duplicate_effect_count"],
            "expected_receipt_count": expected_receipt_count,
            "observed_receipt_count": receipt_checks["actual_receipt_count"],
            "expected_denial_count": expected_denial_count,
            "observed_denial_count": denial_audit["denial_count"],
            "expected_denial_reasons": expected_denial_reasons,
            "observed_denial_reasons": denial_audit["denial_reasons"],
            "operation_statuses": denial_audit["operation_statuses"],
            "capability_call_count": denial_audit["capability_call_count"],
            "signed_denial_receipts": False,
            "denial_evidence_note": (
                "Current gateway denials are durable SQLite audit rows, not signed execution receipts."
            ),
        },
    }


def _record_attack(
    counters: Counter[str],
    reasons: dict[str, Counter[str]],
    latencies: dict[str, list[float]],
    name: str,
    outcome: AttackOutcome,
    *,
    expected_reason: str,
) -> None:
    counters[f"{name}_attempts"] += 1
    counters[f"{name}_passed"] += int(outcome.passed_gateway)
    counters[f"{name}_expected_reason"] += int(
        not outcome.passed_gateway and outcome.reason == expected_reason
    )
    reasons[name][outcome.reason] += 1
    latencies[name].append(outcome.latency_ms)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the machine report as a concise, human-readable Markdown artifact."""

    dataset = report["dataset"]
    metrics = report["metrics"]
    invariants = report["invariants"]
    lines = [
        "# AgentDojo 授权合同一致性回放",
        "",
        f"**结论：{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        "> 本实验是对官方 ground-truth 工具轨迹进行的离线授权合同回归，"
        "不是模型攻击成功率（ASR）、AgentDojo utility、提示注入检测率或端到端 Agent 能力评测。",
        "",
        "## 数据与执行范围",
        "",
        f"- AgentDojo v1 cases：{dataset['case_count']}",
        f"- 唯一 user / injection tasks：{dataset['unique_user_task_count']} / "
        f"{dataset['unique_injection_task_count']}",
        f"- 实际授权并回放的用户 ground-truth calls：{dataset['user_ground_truth_call_count']}",
        f"- 注册函数：{dataset['registered_function_count']}",
        f"- 输入 artifact SHA-256：`{dataset['artifact_sha256']}`",
        "- 执行环境：真实 ActionGateway、临时文件型 SQLite、确定性 TraceBackend、外部双钥 Ed25519 trust bundle。",
        "",
        "## 核心指标",
        "",
        "| 指标 | 成功/样本 | 比率 | Wilson 95% CI | 期望 |",
        "|---|---:|---:|---:|---:|",
    ]
    labels = {
        "legal_contract_acceptance_rate": ("合法合同接受率", "100%"),
        "unregistered_call_pass_rate": ("未注册调用错误通过率", "0%"),
        "same_tool_argument_mismatch_pass_rate": ("同工具参数错配错误通过率", "0%"),
        "tool_mismatch_pass_rate": ("工具错配错误通过率", "0%"),
        "context_mismatch_pass_rate": ("上下文错配错误通过率", "0%"),
        "same_idempotency_key_cached_replay_success_rate": ("同幂等键缓存重放成功率", "100%"),
        "different_idempotency_key_single_use_rejection_rate": ("换幂等键复用一次性凭证拒绝率", "100%"),
        "receipt_external_verification_rate": ("Receipt 外部验真率", "100%"),
        "receipt_chain_verification_rate": ("Receipt 哈希链验真率", "100%"),
    }
    for key, (label, expectation) in labels.items():
        value = metrics[key]
        interval = value["wilson_95"]
        lines.append(
            f"| {label} | {value['successes']}/{value['attempts']} | "
            f"{value['rate']:.6f} | [{interval['low']:.6f}, {interval['high']:.6f}] | {expectation} |"
        )
    lines.extend(
        [
            "",
            "## 副作用与证据不变量",
            "",
            f"- TraceBackend 副作用：期望 {invariants['expected_side_effect_count']}，"
            f"实际 {invariants['observed_side_effect_count']}，重复 {invariants['duplicate_side_effect_count']}。",
            f"- 签名 execution receipts：期望 {invariants['expected_receipt_count']}，"
            f"实际 {invariants['observed_receipt_count']}。",
            f"- 拒绝审计：期望 {invariants['expected_denial_count']}，"
            f"实际 {invariants['observed_denial_count']}。",
            "- 拒绝不会伪装成签名执行回执；当前拒绝证据是持久化 SQLite audit row。",
            "",
            "## 本机网关延迟（ms）",
            "",
            "延迟用于复现实验的工程画像，受机器与文件系统影响，不作为跨机器性能排名。",
            "",
            "| 阶段 | n | p50 | p95 | p99 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    latency_labels = {
        "legal_first_dispatch": "首次合法执行",
        "cached_replay": "同键缓存重放",
        "unregistered_call": "未注册拒绝",
        "argument_mismatch": "参数错配拒绝",
        "tool_mismatch": "工具错配拒绝",
        "context_mismatch": "上下文错配拒绝",
        "single_use_new_key_reuse": "一次性凭证换键拒绝",
    }
    for key, label in latency_labels.items():
        value = report["latency_ms"].get(key, {})
        lines.append(
            f"| {label} | {value.get('count', 0)} | {value.get('p50')} | "
            f"{value.get('p95')} | {value.get('p99')} |"
        )
    lines.extend(
        [
            "",
            "## 方法边界",
            "",
            "- 只执行 `ground_truth.user[].arguments` 的已解析参数；placeholder 模板不会被猜测或执行。",
            "- 固定信封绑定 case 内容摘要、lane、sequence、原函数、原参数及调用摘要。",
            "- 同幂等键重放必须返回原结果和原 receipt，且不再次调用 TraceBackend。",
            "- Receipt 使用 case artifact 外部的 trust bundle 验签，并复算 request/result digest 与逐 workflow 哈希链。",
            "- 本实验不运行模型，不读取攻击提示，也不宣称获得 AgentDojo ASR/utility。",
            "",
        ]
    )
    return "\n".join(lines)


def canonical_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
