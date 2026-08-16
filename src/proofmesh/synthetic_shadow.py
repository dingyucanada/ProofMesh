from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request

from .business import ToolExecutionError
from .capabilities import (
    RECEIPT_TYPE,
    TOKEN_TYPE,
    ActionPassportClaims,
    ExternalTrustVerifier,
    load_or_create_signer,
    sha256_digest,
)
from .gateway import ActionGateway, GatewayDenied, GatewayStore
from .synthetic_http_backend import SyntheticHttpBackend, SyntheticHttpTransportError
from .timeutil import utc_now


SCHEMA_VERSION = "proofmesh.synthetic-shadow-workload/v1"
REPORT_VERSION = "proofmesh.synthetic-shadow-report/v1"
PROVENANCE = {
    "synthetic": True,
    "customer_data": False,
    "enterprise_historical_data": False,
    "third_party_provider": False,
    "blind_workload": True,
    "shadow_write_mode": "isolated-local-http-sandbox",
}
FAULT_PROFILES = (
    "normal",
    "crm_close_error",
    "payment_timeout_after_commit",
    "reconcile_unknown_after_commit",
)
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return round(ordered[index], 3)


def _wilson_interval(successes: int, attempts: int, *, z: float = 1.959963984540054) -> list[float] | None:
    if attempts <= 0:
        return None
    rate = successes / attempts
    denominator = 1 + z * z / attempts
    centre = (rate + z * z / (2 * attempts)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * attempts)) / attempts) / denominator
    return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]


def generate_workload(case_count: int = 240, *, seed: int = 20260813) -> dict[str, Any]:
    if not 200 <= case_count <= 500:
        raise ValueError("synthetic blind workload must contain 200-500 cases")
    reasons = ("包装破损", "配件缺失", "商品损坏", "尺码不符", "重复下单")
    cases: list[dict[str, Any]] = []
    for index in range(case_count):
        sequence = index + 1
        amount = 500 + ((seed * 17 + sequence * 7919) % 9000)
        profile = FAULT_PROFILES[index % len(FAULT_PROFILES)]
        workflow_id = f"syn-shadow-wf-{sequence:04d}"
        cases.append(
            {
                "case_id": f"SYN-SHADOW-{sequence:04d}",
                "workflow_id": workflow_id,
                "tenant_id": "synthetic-acme",
                "ticket_id": f"SYN-TKT-{sequence:04d}",
                "order_id": f"SYN-ORD-{sequence:04d}",
                "amount_minor": amount,
                "currency": "CNY",
                "reason": reasons[index % len(reasons)],
                "fault_profile": profile,
                "response_delay_seconds": 0.18,
                "expected_terminal": {
                    "normal": "COMPLETED",
                    "crm_close_error": "COMPENSATED",
                    "payment_timeout_after_commit": "RECOVERED_COMPLETED",
                    "reconcile_unknown_after_commit": "UNKNOWN_MANUAL",
                }[profile],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2026-08-13T00:00:00+00:00",
        "seed": seed,
        "case_count": case_count,
        "provenance": PROVENANCE,
        "blind_protocol": {
            "case_plan_frozen_before_execution": True,
            "report_aggregates_only_after_all_cases": True,
            "enterprise_baseline": None,
            "customer_roi_claim": False,
        },
        "cases": cases,
    }


@dataclass
class SyntheticServicePair:
    root: Path
    payment_port: int
    crm_port: int
    payment_process: subprocess.Popen[str] | None = None
    crm_process: subprocess.Popen[str] | None = None

    @property
    def payment_url(self) -> str:
        return f"http://127.0.0.1:{self.payment_port}"

    @property
    def crm_url(self) -> str:
        return f"http://127.0.0.1:{self.crm_port}"

    def __enter__(self) -> "SyntheticServicePair":
        child_environment = os.environ.copy()
        inherited_pythonpath = child_environment.get("PYTHONPATH")
        child_environment["PYTHONPATH"] = (
            f"{SOURCE_ROOT}{os.pathsep}{inherited_pythonpath}" if inherited_pythonpath else str(SOURCE_ROOT)
        )
        commands = (
            (
                "payment_process",
                "payment",
                self.payment_port,
                self.root / "payment.db",
            ),
            ("crm_process", "crm", self.crm_port, self.root / "crm.db"),
        )
        for attribute, service, port, db_path in commands:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "proofmesh.synthetic_http_service",
                    "--service",
                    service,
                    "--port",
                    str(port),
                    "--db",
                    str(db_path),
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=child_environment,
            )
            setattr(self, attribute, process)
        try:
            self._wait_ready(self.payment_url)
            self._wait_ready(self.crm_url)
        except RuntimeError as exc:
            diagnostics = self._stop_and_collect()
            raise RuntimeError(
                f"{exc}; synthetic child diagnostics="
                f"{json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)}"
            ) from exc
        return self

    @staticmethod
    def _wait_ready(base_url: str) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                with request.urlopen(f"{base_url}/health", timeout=0.2) as response:
                    payload = json.loads(response.read())
                    if response.status == 200 and payload.get("ok") is True:
                        return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError(f"synthetic HTTP service did not become ready: {base_url}")

    def _stop_and_collect(self) -> list[dict[str, Any]]:
        processes = (
            ("payment", self.payment_process),
            ("crm", self.crm_process),
        )
        for _, process in processes:
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
        diagnostics: list[dict[str, Any]] = []
        for service, process in processes:
            if process is None:
                continue
            try:
                _, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=2)
            sanitized = (stderr or "").replace(str(self.root), "<synthetic-work-dir>")
            sanitized = sanitized.replace(str(SOURCE_ROOT), "<proofmesh-src>")
            diagnostics.append(
                {
                    "service": service,
                    "returncode": process.returncode,
                    "stderr": sanitized[-2000:],
                }
            )
        return diagnostics

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop_and_collect()


def build_shadow_gateway(root: Path, backend: SyntheticHttpBackend) -> tuple[ActionGateway, GatewayStore, Any]:
    key_dir = root / "keys"
    trust_bundle = root / "trust-bundle.json"
    policy_signer = load_or_create_signer(
        private_key_path=key_dir / "policy.ed25519",
        trust_bundle_path=trust_bundle,
        key_id="synthetic-shadow-policy-v1",
        issuer="proofmesh-policy-control-plane",
        allow_bootstrap=True,
        token_types=[TOKEN_TYPE],
    )
    receipt_signer = load_or_create_signer(
        private_key_path=key_dir / "receipt.ed25519",
        trust_bundle_path=trust_bundle,
        key_id="synthetic-shadow-receipt-v1",
        issuer="proofmesh-action-gateway",
        allow_bootstrap=True,
        token_types=[RECEIPT_TYPE],
    )
    store = GatewayStore(root / "gateway.db")
    gateway = ActionGateway(
        verifier=ExternalTrustVerifier(trust_bundle),
        receipt_signer=receipt_signer,
        store=store,
        upstream=backend,
    )
    return gateway, store, policy_signer


def _issue_passport(
    *,
    gateway: ActionGateway,
    signer: Any,
    workflow_id: str,
    tool: str,
    arguments: dict[str, Any],
    context_digest: str,
    resource: str,
    scopes: list[str],
    mode: str,
    amount_minor: int = 0,
) -> str:
    claims = ActionPassportClaims.issue(
        issuer=signer.issuer,
        audience=gateway.audience,
        tenant_id="synthetic-acme",
        subject="synthetic-shadow-runner",
        workflow_id=workflow_id,
        tool=tool,
        resource=resource,
        scopes=scopes,
        arguments=arguments,
        context_digest=context_digest,
        policy_digest="a" * 64,
        approval_digest="AUTOMATIC",
        mode=mode,
        max_calls=1,
        max_amount_minor=amount_minor,
        budget_limit_minor=200000,
        currency="CNY",
        ttl_seconds=60,
    )
    return signer.sign_passport(claims)


def _call(
    *,
    gateway: ActionGateway,
    signer: Any,
    workflow_id: str,
    tool: str,
    arguments: dict[str, Any],
    resource: str,
    scopes: list[str],
    mode: str,
    idempotency_key: str,
    amount_minor: int = 0,
) -> tuple[dict[str, Any], float]:
    context_digest = sha256_digest({"synthetic_shadow_workflow": workflow_id})
    token = _issue_passport(
        gateway=gateway,
        signer=signer,
        workflow_id=workflow_id,
        tool=tool,
        arguments=arguments,
        context_digest=context_digest,
        resource=resource,
        scopes=scopes,
        mode=mode,
        amount_minor=amount_minor,
    )
    started = time.perf_counter()
    result = gateway.call_tool(
        tool=tool,
        arguments=arguments,
        passport=token,
        workflow_id=workflow_id,
        context_digest=context_digest,
        idempotency_key=idempotency_key,
    )
    return result, (time.perf_counter() - started) * 1000


def _expire_operation(store: GatewayStore, workflow_id: str, tool: str) -> None:
    """Test-only lease-expiry injection hook; this is not a production/public API.

    The hook advances only the synthetic runner's isolated GatewayStore lease timestamp so the
    next ordinary ``call_tool`` takes the real reconcile-before-retry path without waiting for
    the production-shaped 15-second lease. It does not modify either HTTP service's state.
    """
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE gateway_operations SET lease_until = 0 WHERE workflow_id = ? AND tool = ?",
            (workflow_id, tool),
        )


def _argument_drift_reached_service(
    *,
    case: dict[str, Any],
    gateway: ActionGateway,
    signer: Any,
    refund_arguments: dict[str, Any],
) -> bool:
    """Return true only if a passport-bound amount drift was not rejected before dispatch."""
    workflow_id = case["workflow_id"]
    context_digest = sha256_digest({"synthetic_shadow_workflow": workflow_id})
    token = _issue_passport(
        gateway=gateway,
        signer=signer,
        workflow_id=workflow_id,
        tool="payments.issue_refund",
        arguments=refund_arguments,
        context_digest=context_digest,
        resource=case["order_id"],
        scopes=["refund:write"],
        mode="execute",
        amount_minor=case["amount_minor"],
    )
    drifted_arguments = {**refund_arguments, "amount_minor": int(case["amount_minor"]) + 1}
    try:
        gateway.call_tool(
            tool="payments.issue_refund",
            arguments=drifted_arguments,
            passport=token,
            workflow_id=workflow_id,
            context_digest=context_digest,
            idempotency_key=f"synthetic-invalid-{case['case_id']}",
        )
    except GatewayDenied as exc:
        if exc.reason != "arguments_mismatch":
            raise
        return False
    return True


def _run_case(
    case: dict[str, Any],
    *,
    gateway: ActionGateway,
    store: GatewayStore,
    signer: Any,
    backend: SyntheticHttpBackend,
) -> dict[str, Any]:
    workflow_id = case["workflow_id"]
    refund_arguments = {
        "ticket_id": case["ticket_id"],
        "order_id": case["order_id"],
        "amount_minor": case["amount_minor"],
        "currency": case["currency"],
        "expected_order_version": 0,
        "workflow_id": workflow_id,
    }
    refund_key = f"synthetic-refund-{case['case_id']}"
    timings: list[float] = []
    invalid_attempts = 1
    invalid_passed = int(
        _argument_drift_reached_service(
            case=case,
            gateway=gateway,
            signer=signer,
            refund_arguments=refund_arguments,
        )
    )
    profile = case["fault_profile"]
    try:
        refund, duration = _call(
            gateway=gateway,
            signer=signer,
            workflow_id=workflow_id,
            tool="payments.issue_refund",
            arguments=refund_arguments,
            resource=case["order_id"],
            scopes=["refund:write"],
            mode="execute",
            idempotency_key=refund_key,
            amount_minor=case["amount_minor"],
        )
        timings.append(duration)
    except SyntheticHttpTransportError:
        _expire_operation(store, workflow_id, "payments.issue_refund")
        try:
            refund, duration = _call(
                gateway=gateway,
                signer=signer,
                workflow_id=workflow_id,
                tool="payments.issue_refund",
                arguments=refund_arguments,
                resource=case["order_id"],
                scopes=["refund:write"],
                mode="execute",
                idempotency_key=refund_key,
                amount_minor=case["amount_minor"],
            )
            timings.append(duration)
        except GatewayDenied as exc:
            if exc.reason != "operation_unknown_manual_reconciliation_required":
                raise
            audit_started = time.perf_counter()
            snapshot = backend.workflow_snapshot(workflow_id, tenant_id="synthetic-acme")
            receipts = store.receipts(workflow_id, tenant_id="synthetic-acme")
            audit_seconds = time.perf_counter() - audit_started
            return {
                "case_id": case["case_id"],
                "fault_profile": profile,
                "terminal": "UNKNOWN_MANUAL",
                "refund_status": (snapshot.get("refund") or {}).get("status"),
                "ticket_status": (snapshot.get("ticket") or {}).get("status"),
                "invalid_attempts": invalid_attempts,
                "invalid_passed": invalid_passed,
                "latency_ms": timings,
                "receipt_count": len(receipts),
                "audit_seconds": round(audit_seconds, 6),
            }

    replay = gateway.call_tool(
        tool="payments.issue_refund",
        arguments=refund_arguments,
        passport=_issue_passport(
            gateway=gateway,
            signer=signer,
            workflow_id=workflow_id,
            tool="payments.issue_refund",
            arguments=refund_arguments,
            context_digest=sha256_digest({"synthetic_shadow_workflow": workflow_id}),
            resource=case["order_id"],
            scopes=["refund:write"],
            mode="execute",
            amount_minor=case["amount_minor"],
        ),
        workflow_id=workflow_id,
        context_digest=sha256_digest({"synthetic_shadow_workflow": workflow_id}),
        idempotency_key=refund_key,
    )
    assert replay["idempotent_replay"] is True

    refund_result = refund["result"]
    close_arguments = {
        "ticket_id": case["ticket_id"],
        "workflow_id": workflow_id,
        "resolution": f"synthetic shadow refund {case['amount_minor']} CNY",
    }
    terminal = "COMPLETED"
    try:
        _, duration = _call(
            gateway=gateway,
            signer=signer,
            workflow_id=workflow_id,
            tool="crm.close_ticket",
            arguments=close_arguments,
            resource=case["ticket_id"],
            scopes=["ticket:write"],
            mode="execute",
            idempotency_key=f"synthetic-close-{case['case_id']}",
        )
        timings.append(duration)
        if profile == "payment_timeout_after_commit":
            terminal = "RECOVERED_COMPLETED"
    except ToolExecutionError as exc:
        if exc.code != "crm_dependency_unavailable":
            raise
        compensation_arguments = {
            "refund_id": refund_result["refund_id"],
            "workflow_id": workflow_id,
            "reason": "synthetic injected CRM close failure",
        }
        _, duration = _call(
            gateway=gateway,
            signer=signer,
            workflow_id=workflow_id,
            tool="payments.compensate_refund",
            arguments=compensation_arguments,
            resource=refund_result["refund_id"],
            scopes=["refund:compensate"],
            mode="compensate",
            idempotency_key=f"synthetic-compensate-{case['case_id']}",
        )
        timings.append(duration)
        terminal = "COMPENSATED"

    audit_started = time.perf_counter()
    snapshot = backend.workflow_snapshot(workflow_id, tenant_id="synthetic-acme")
    receipts = store.receipts(workflow_id, tenant_id="synthetic-acme")
    audit_seconds = time.perf_counter() - audit_started
    return {
        "case_id": case["case_id"],
        "fault_profile": profile,
        "terminal": terminal,
        "refund_status": (snapshot.get("refund") or {}).get("status"),
        "ticket_status": (snapshot.get("ticket") or {}).get("status"),
        "receipt_count": len(receipts),
        "invalid_attempts": invalid_attempts,
        "invalid_passed": invalid_passed,
        "latency_ms": timings,
        "audit_seconds": round(audit_seconds, 6),
    }


def _profile_matrix(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for profile in FAULT_PROFILES:
        selected = [case for case in cases if case["fault_profile"] == profile]
        rows.append(
            {
                "fault_profile": profile,
                "cases": len(selected),
                "expected_terminal": sorted({case["expected_terminal"] for case in selected}),
                "injection_point": {
                    "normal": "none",
                    "crm_close_error": "CRM close before commit",
                    "payment_timeout_after_commit": "payment response after committed refund",
                    "reconcile_unknown_after_commit": "payment reconcile visibility after committed refund",
                }[profile],
            }
        )
    return rows


def _report(workload: dict[str, Any], results: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    total = len(results)
    terminal_successes = sum(
        result["terminal"] in {"COMPLETED", "RECOVERED_COMPLETED", "COMPENSATED"} for result in results
    )
    invalid_attempts = sum(result["invalid_attempts"] for result in results)
    invalid_passed = sum(result["invalid_passed"] for result in results)
    unknown = sum(result["terminal"] == "UNKNOWN_MANUAL" for result in results)
    latencies = [value for result in results for value in result["latency_ms"]]
    audits = [result["audit_seconds"] for result in results if isinstance(result["audit_seconds"], float)]
    duplicate_effects = int(stats["payment"]["duplicate_refund_effects"]) + int(
        stats["crm"]["duplicate_close_effects"]
    )
    return {
        "schema_version": REPORT_VERSION,
        "generated_at": utc_now(),
        "workload_sha256": sha256_digest(workload),
        "provenance": PROVENANCE,
        "method": {
            "services": "two independent local OS processes over HTTP",
            "payment_and_crm_databases": "separate SQLite files owned by their service processes",
            "gateway": "proofmesh.gateway.ActionGateway",
            "scope": "gateway authorization, HTTP dispatch, idempotency, reconciliation, compensation",
            "passport_approval_mode": "AUTOMATIC synthetic low-value cases only",
            "pinned_approval_policy_enabled": False,
            "human_approval_assertion_covered": False,
            "lease_expiry_injection": {
                "test_only": True,
                "production_api": False,
                "mechanism": "direct isolated GatewayStore lease timestamp update",
                "purpose": "exercise real reconcile-before-retry without a 15-second wait per injected case",
            },
            "blind_protocol": workload["blind_protocol"],
            "not_claimed": [
                "customer pilot",
                "enterprise historical data",
                "third-party payment provider",
                "production SLA",
                "financial ROI",
                "Human approval assertion coverage",
            ],
        },
        "sample": {
            "cases": total,
            "seed": workload["seed"],
            "fault_matrix": _profile_matrix(workload["cases"]),
        },
        "kpis": {
            "duplicate_side_effect_rate": {
                "numerator": duplicate_effects,
                "denominator": total,
                "rate": duplicate_effects / total if total else None,
                "wilson_95": _wilson_interval(duplicate_effects, total),
                "definition": "extra payment or CRM write effects / synthetic cases",
            },
            "invalid_call_pass_rate": {
                "numerator": invalid_passed,
                "denominator": invalid_attempts,
                "rate": invalid_passed / invalid_attempts if invalid_attempts else None,
                "wilson_95": _wilson_interval(invalid_passed, invalid_attempts),
                "definition": "argument-drift attempts that reached the synthetic service / drift attempts",
            },
            "recoverable_terminal_rate": {
                "numerator": terminal_successes,
                "denominator": total - unknown,
                "rate": terminal_successes / (total - unknown) if total != unknown else None,
                "wilson_95": _wilson_interval(terminal_successes, total - unknown),
                "definition": "completed, reconciliation-recovered, or compensated cases / cases with knowable upstream state",
            },
            "unknown_fail_closed_rate": {
                "numerator": unknown,
                "denominator": sum(
                    result["fault_profile"] == "reconcile_unknown_after_commit" for result in results
                ),
                "rate": unknown
                / sum(result["fault_profile"] == "reconcile_unknown_after_commit" for result in results),
                "definition": "ambiguous reconcile cases that stopped in manual UNKNOWN / ambiguous reconcile cases",
            },
            "gateway_http_latency_ms": {
                "samples": len(latencies),
                "p50": _percentile(latencies, 0.5),
                "p95": _percentile(latencies, 0.95),
                "p99": _percentile(latencies, 0.99),
            },
            "audit_readback_seconds": {
                "samples": len(audits),
                "p50": _percentile(audits, 0.5),
                "p95": _percentile(audits, 0.95),
                "definition": "local programmatic snapshot plus receipt retrieval; not human audit time",
            },
        },
        "service_stats": stats,
        "terminal_counts": {
            terminal: sum(result["terminal"] == terminal for result in results)
            for terminal in ("COMPLETED", "RECOVERED_COMPLETED", "COMPENSATED", "UNKNOWN_MANUAL")
        },
        "case_result_digest": sha256_digest(results),
    }


def render_markdown(report: dict[str, Any]) -> str:
    kpis = report["kpis"]
    counts = report["terminal_counts"]
    return f"""# 独立进程 HTTP 合成沙箱盲测报告

> **数据声明：本报告只使用确定性生成的合成数据。不是企业历史工单，不是客户试点，不是第三方支付接入，不代表生产 SLA 或财务 ROI。**

## 实验边界

- 样本：{report['sample']['cases']} 个合成案例；冻结 seed `{report['sample']['seed']}`。
- 进程边界：支付服务、CRM 服务、ProofMesh runner 是独立 OS 进程，通过回环 HTTP 通信。
- 状态边界：支付与 CRM 各自拥有独立 SQLite 文件；ProofMesh Gateway 另有自己的操作账本。
- 盲测：案例计划先冻结，全部运行后才聚合 KPI；没有企业现网基线。
- 参数漂移负测：全部 {report['sample']['cases']} 个案例各执行一次无副作用的 passport 参数漂移；UNKNOWN 样本也包含在分母内。
- 租约到期注入：仅测试 runner 直接推进隔离 GatewayStore 的租约时间；不是生产 API，不修改支付或 CRM 状态。
- 审批边界：本专项只使用 `AUTOMATIC` 合成低金额 passport，未启用 pinned approval policy，不覆盖 Human approval assertion。

## 故障矩阵

| 故障 | 案例数 | 注入点 | 预期终态 |
|---|---:|---|---|
""" + "\n".join(
        f"| `{row['fault_profile']}` | {row['cases']} | {row['injection_point']} | {', '.join(row['expected_terminal'])} |"
        for row in report["sample"]["fault_matrix"]
    ) + f"""

## KPI

| 指标 | 结果 | 95% 边界 / 口径 |
|---|---:|---|
| 重复副作用率 | {kpis['duplicate_side_effect_rate']['numerator']} / {kpis['duplicate_side_effect_rate']['denominator']} | Wilson {kpis['duplicate_side_effect_rate']['wilson_95']} |
| 错误放行率 | {kpis['invalid_call_pass_rate']['numerator']} / {kpis['invalid_call_pass_rate']['denominator']} | Wilson {kpis['invalid_call_pass_rate']['wilson_95']} |
| 可恢复终态率 | {kpis['recoverable_terminal_rate']['numerator']} / {kpis['recoverable_terminal_rate']['denominator']} | 只以可判定上游状态为分母；Wilson {kpis['recoverable_terminal_rate']['wilson_95']} |
| UNKNOWN fail-closed | {kpis['unknown_fail_closed_rate']['numerator']} / {kpis['unknown_fail_closed_rate']['denominator']} | 不把 UNKNOWN 伪装为成功 |
| HTTP 调用 p50 / p95 / p99 | {kpis['gateway_http_latency_ms']['p50']} / {kpis['gateway_http_latency_ms']['p95']} / {kpis['gateway_http_latency_ms']['p99']} ms | 本机回环 HTTP；非生产 SLA |
| 审计读回 p50 / p95 | {kpis['audit_readback_seconds']['p50']} / {kpis['audit_readback_seconds']['p95']} s | 程序化 snapshot+receipt 读取；非人工审计耗时 |

终态计数：`COMPLETED={counts['COMPLETED']}`、`RECOVERED_COMPLETED={counts['RECOVERED_COMPLETED']}`、`COMPENSATED={counts['COMPENSATED']}`、`UNKNOWN_MANUAL={counts['UNKNOWN_MANUAL']}`。

## 可证伪边界

- 这证明两个独立 HTTP 合成服务上的调用、幂等、对账、补偿与 UNKNOWN 行为可重放。
- 它不证明真实支付机构、真实 CRM、客户数据、业务收益、生产吞吐、外部上游签名或人工审批断言。
- 零观察失败不等于真实失败率为零；报告保留 Wilson 95% 区间。
- 审计指标是程序读取时间，不应当作企业审计人员节省时间。
"""


def run_shadow_workload(
    workload: dict[str, Any],
    *,
    work_dir: Path | None = None,
    payment_port: int = 18765,
    crm_port: int = 18766,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if workload.get("schema_version") != SCHEMA_VERSION or workload.get("provenance") != PROVENANCE:
        raise ValueError("synthetic shadow workload provenance/schema mismatch")
    if not 200 <= int(workload.get("case_count", 0)) <= 500:
        raise ValueError("synthetic shadow workload must contain 200-500 cases")
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="proofmesh-synthetic-shadow-")
        work_dir = Path(owned_temp.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with SyntheticServicePair(work_dir / "services", payment_port, crm_port) as services:
            backend = SyntheticHttpBackend(
                payment_url=services.payment_url,
                crm_url=services.crm_url,
                timeout_seconds=0.12,
            )
            health = backend.health()
            payment_pid = health["payment"].get("pid")
            crm_pid = health["crm"].get("pid")
            if not isinstance(payment_pid, int) or not isinstance(crm_pid, int) or payment_pid == crm_pid:
                raise RuntimeError("payment and CRM must run in different OS processes")
            backend.seed(workload["cases"])
            gateway, store, signer = build_shadow_gateway(work_dir / "proofmesh", backend)
            results = [
                _run_case(case, gateway=gateway, store=store, signer=signer, backend=backend)
                for case in workload["cases"]
            ]
            stats = backend.stats()
            stats["process_evidence"] = {
                "payment_pid": payment_pid,
                "crm_pid": crm_pid,
                "different_os_processes": payment_pid != crm_pid,
            }
            return _report(workload, results, stats), results
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()


def write_artifacts(
    output_dir: Path,
    *,
    case_count: int = 240,
    seed: int = 20260813,
    payment_port: int = 18765,
    crm_port: int = 18766,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    workload = generate_workload(case_count, seed=seed)
    report, results = run_shadow_workload(
        workload,
        payment_port=payment_port,
        crm_port=crm_port,
    )
    (output_dir / "workload.json").write_text(
        json.dumps(workload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "case-results.jsonl").write_text(
        "".join(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n" for result in results),
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    manifest = {
        "schema_version": "proofmesh.synthetic-shadow-manifest/v1",
        "provenance": PROVENANCE,
        "files": [],
    }
    for name in ("workload.json", "case-results.jsonl", "report.json", "report.md"):
        path = output_dir / name
        manifest["files"].append(
            {"path": name, "bytes": path.stat().st_size, "sha256": sha256_digest(path.read_bytes())}
        )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
