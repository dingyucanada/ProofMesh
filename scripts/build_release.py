#!/usr/bin/env python3
"""Build a scrubbed, self-describing ProofMesh release archive.

The builder fails closed when runtime secrets/state, stale caches, inconsistent
reference evidence, or legacy submission language is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# The release gate scans the source tree for generated caches before packaging.
# Prevent this script's own project import from creating __pycache__ and tripping that gate.
sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT.parent / "ProofMesh-release.zip"
DEFAULT_RELEASE_INSTANT = datetime(2026, 8, 12, tzinfo=timezone.utc)
SKIP_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
SKIP_EXACT = {
    Path("docs/demo-final-rollback.png"),
    Path("docs/demo-waiting-approval.png"),
    Path("docs/judge-console-compensated.png"),
}
LEGACY_TERMS = {
    "mock-canary",
    "proofmesh-lead",
    "adversarial-auditor",
    "safe-remediator",
    "release-policy",
    "attack-matrix",
    "72 synthetic",
    "28 passed",
    "automatic rollback",
}
RELEASE_TEXT_ROOTS = [
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "docs",
    ROOT / "agentteams",
    ROOT / "skills",
    ROOT / "src/proofmesh/static",
]
REQUIRED = {
    Path("src/proofmesh/gateway.py"),
    Path("src/proofmesh/workflow.py"),
    Path("src/proofmesh/workflow_verifier.py"),
    Path("src/proofmesh/business.py"),
    Path("src/proofmesh/synthetic_http_backend.py"),
    Path("src/proofmesh/synthetic_http_service.py"),
    Path("src/proofmesh/synthetic_shadow.py"),
    Path("docker-compose.production.yml"),
    Path("artifacts/public-benchmark/cases.jsonl"),
    Path("artifacts/public-benchmark/authorization-contract-replay.json"),
    Path("artifacts/synthetic-http-shadow/manifest.json"),
    Path("artifacts/synthetic-http-shadow/report.json"),
    Path("artifacts/synthetic-http-shadow/workload.json"),
    Path("artifacts/reference/manifest.json"),
    Path("artifacts/reference/trust-bundle.json"),
    Path("artifacts/reference/pinned-refund-policy.json"),
    Path("docs/agent-identity-register.md"),
    Path("docs/pilot-value-scorecard.md"),
    Path("docs/aliyun-skill-adoption.md"),
    Path("docs/synthetic-http-shadow-evaluation.md"),
    Path("docs/templates/data-authorization-and-anonymization-checklist.md"),
    Path("docs/templates/independent-reproduction-attestation.md"),
    Path("artifacts/external-evidence/claim-status.json"),
    Path("scripts/validate_external_evidence.py"),
    Path("src/proofmesh/pilot_dataset.py"),
    Path("scripts/pilot/validate_pilot_dataset.py"),
    Path("src/proofmesh/domain_protocol.py"),
    Path("src/proofmesh/operations.py"),
    Path("src/proofmesh/operations_runtime.py"),
    Path("src/proofmesh/operations_verifier.py"),
    Path("data/policies/operations_change_policy.json"),
    Path("scripts/generate_operations_reference.py"),
    Path("scripts/validate_operations_reference.py"),
    Path("artifacts/operations-reference/manifest.json"),
    Path("artifacts/operations-reference/report.json"),
    Path("artifacts/operations-reference/trust-bundle.json"),
    Path("artifacts/operations-reference/pinned-operations-policy.json"),
    Path("docs/second-domain-operations.md"),
    Path("src/proofmesh/vendor_sandbox.py"),
    Path("scripts/vendor_readiness_probe.py"),
    Path("docs/vendor-sandbox-integration.md"),
    Path("docs/vendor-sandbox-boundary.md"),
    Path("src/proofmesh/postgres_store.py"),
    Path("sbom.cdx.json"),
    Path("requirements-ci.lock"),
    Path("requirements-production.txt"),
    Path("requirements-production.lock"),
    Path("scripts/generate_sbom.py"),
    Path("scripts/generate_provenance.py"),
    Path(".github/workflows/ci-release.yml"),
    Path(".github/ISSUE_TEMPLATE/independent-reproduction.yml"),
    Path(".github/pull_request_template.md"),
    Path("CITATION.cff"),
    Path("scripts/benchmarks/run_synthetic_http_shadow.py"),
    Path("src/proofmesh/benchmarks/banking77_routing.py"),
    Path("scripts/benchmarks/run_banking77_routing.py"),
    Path("tests/benchmarks/test_banking77_routing.py"),
    Path("artifacts/public-domain-evaluation/banking77/source-lock.json"),
    Path("artifacts/public-domain-evaluation/banking77/report.json"),
    Path("artifacts/public-domain-evaluation/banking77/report.md"),
    Path("artifacts/public-domain-evaluation/banking77/case-results.jsonl"),
    Path("artifacts/public-domain-evaluation/banking77/ATTRIBUTION.md"),
    Path("src/proofmesh/benchmarks/cfpb_shadow.py"),
    Path("scripts/benchmarks/run_cfpb_shadow.py"),
    Path("tests/benchmarks/test_cfpb_shadow.py"),
    Path("artifacts/public-domain-evaluation/cfpb/manifest.json"),
    Path("artifacts/public-domain-evaluation/cfpb/provenance.json"),
    Path("artifacts/public-domain-evaluation/cfpb/report.json"),
    Path("artifacts/public-domain-evaluation/cfpb/report.md"),
    Path("artifacts/public-domain-evaluation/cfpb/case-results.jsonl"),
    Path("docs/public-dataset-benchmark.md"),
    Path("docs/agentteams-official-mapping.md"),
    Path("scripts/reference_approval_service.py"),
    Path("agentteams/upstream/agentteams-v1.2.0-beta.1-manager-worker-members.patch"),
    Path("agentteams/upstream/source-lock.json"),
    Path("agentteams/upstream/UPSTREAM-ISSUE.md"),
    Path("agentteams/upstream/PR-DESCRIPTION.md"),
    Path("agentteams/upstream/verify_patch.py"),
}
SECRET_TEXT = re.compile(
    r"(?:"
    r"sk-(?=[A-Za-z0-9_-]{20,})(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+"
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|pat-[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{40,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r")"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(root: Path = ROOT) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if relative in SKIP_EXACT:
            continue
        if relative.parts[:2] in {
            ("artifacts", "workflows"),
            ("artifacts", "operations-workflows"),
        }:
            continue
        if relative.parts and relative.parts[0] == "var":
            continue
        if (
            relative.name.startswith(".env")
            and relative.name != ".env.example"
            or ".secrets" in relative.parts
            or relative.name in {".coverage", ".DS_Store"}
        ):
            continue
        result.append(path)
    return result


def _find_sensitive_runtime(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            findings.append(f"symlink:{relative}")
            continue
        if not path.is_file():
            continue
        if (
            relative.parts and relative.parts[0] == "var"
            or relative.parts[:2]
            in {("artifacts", "workflows"), ("artifacts", "operations-workflows")}
            or any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in relative.parts)
            or relative.suffix in {".pyc", ".pyo", ".db", ".sqlite", ".ed25519"}
            or relative.name.startswith(".env") and relative.name != ".env.example"
            or ".secrets" in relative.parts
            or relative.name in {".coverage", ".DS_Store"}
            or ".db-" in relative.name
        ):
            findings.append(str(relative))
    return findings


def _iter_release_text() -> Iterable[Path]:
    for candidate in RELEASE_TEXT_ROOTS:
        if candidate.is_file():
            yield candidate
        elif candidate.is_dir():
            for path in candidate.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".md", ".yaml", ".yml", ".json", ".html", ".js"}:
                    yield path


def _validate_no_legacy_or_secret_text(files: Iterable[Path]) -> None:
    failures: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        for term in LEGACY_TERMS:
            if term in lowered:
                failures.append(f"legacy term {term!r} in {path.relative_to(ROOT)}")
        if SECRET_TEXT.search(text):
            failures.append(f"credential-shaped text in {path.relative_to(ROOT)}")
    if failures:
        raise RuntimeError("release text scan failed:\n" + "\n".join(failures))


def _validate_no_credential_shapes(files: Iterable[Path]) -> None:
    failures: list[str] = []
    for path in files:
        if path.stat().st_size > 10 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if SECRET_TEXT.search(text):
            try:
                label = str(path.relative_to(ROOT))
            except ValueError:
                label = str(path)
            failures.append(f"credential-shaped text in {label}")
    if failures:
        raise RuntimeError("release credential scan failed:\n" + "\n".join(failures))


def _validate_reference(root: Path = ROOT) -> None:
    from proofmesh.workflow_verifier import verify_workflow_proof

    reference = root / "artifacts/reference"
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    declared = {entry["path"]: entry for entry in manifest["files"]}
    actual = {
        str(path.relative_to(reference))
        for path in reference.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(declared) != actual:
        raise RuntimeError("reference manifest file set differs from packaged files")
    for relative, entry in declared.items():
        path = reference / relative
        if path.stat().st_size != entry["bytes"] or _sha256(path) != entry["sha256"]:
            raise RuntimeError(f"reference artifact hash mismatch: {relative}")
    expected_status = {
        "TKT-LOW-001": "COMPLETED",
        "TKT-HIGH-001": "COMPLETED",
        "TKT-SAGA-001": "COMPENSATED",
    }
    for case in manifest["cases"]:
        if case["terminal_status"] != expected_status.get(case["ticket_id"]):
            raise RuntimeError(f"unexpected reference terminal state: {case['ticket_id']}")
        report = verify_workflow_proof(
            reference / case["proof"],
            trust_bundle_path=reference / "trust-bundle.json",
            pinned_policy_path=reference / "pinned-refund-policy.json",
        )
        if not report.valid:
            raise RuntimeError(f"reference proof is not externally valid: {case['ticket_id']} {report.errors}")
    high = next(case for case in manifest["cases"] if case["ticket_id"] == "TKT-HIGH-001")
    waiting = json.loads((reference / high["waiting_evidence"]).read_text(encoding="utf-8"))
    if not all(waiting["assertions"].values()) or waiting["business_snapshot"].get("refund") is not None:
        raise RuntimeError("high-risk waiting evidence does not prove a zero-side-effect approval gate")


def _validate_public_benchmark(root: Path = ROOT) -> None:
    lock = json.loads((root / "data/benchmarks/public-benchmark.lock.json").read_text(encoding="utf-8"))
    artifact = root / "artifacts/public-benchmark/cases.jsonl"
    expected = lock.get("artifact", {}).get("sha256") or lock.get("artifact_sha256")
    if expected != _sha256(artifact):
        raise RuntimeError("public AgentDojo artifact differs from its locked SHA-256")


def _validate_public_domain_evaluations(root: Path = ROOT) -> None:
    """Validate no-raw public-data artifacts and their claim boundaries."""

    banking = root / "artifacts/public-domain-evaluation/banking77"
    report = json.loads((banking / "report.json").read_text(encoding="utf-8"))
    source_lock = json.loads((banking / "source-lock.json").read_text(encoding="utf-8"))
    case_path = banking / "case-results.jsonl"
    case_lines = case_path.read_text(encoding="utf-8").splitlines()
    if (
        report.get("readiness_verdict") != "NOT_PRODUCTION_READY"
        or report.get("benchmark_execution_passed") is not True
        or report.get("dataset", {}).get("test", {}).get("rows") != 3_080
        or report.get("method", {}).get("public_text_in_artifacts") is not False
        or report.get("metrics", {}).get("action_gateway_controls", {})
        .get("unapproved_sensitive_bypass_rejection", {})
        .get("successes")
        != 80
        or report.get("metrics", {}).get("action_gateway_controls", {})
        .get("sensitive_upstream_dispatches")
        != 0
        or source_lock.get("raw_text_redistributed") is not False
        or len(case_lines) != 3_080
        or '"text"' in case_path.read_text(encoding="utf-8")
    ):
        raise RuntimeError("Banking77 evaluation or non-claim boundary is inconsistent")

    from proofmesh.benchmarks.cfpb_shadow import validate_artifacts

    cfpb_report = validate_artifacts(root / "artifacts/public-domain-evaluation/cfpb")
    if (
        cfpb_report.get("data_label") != "PUBLIC_REAL_OBSERVATIONAL"
        or cfpb_report.get("metrics", {}).get("ingested_case_count") != 240
        or cfpb_report.get("metrics", {}).get("raw_narrative_artifact_count") != 0
        or cfpb_report.get("metrics", {}).get("forbidden_identity_field_artifact_count") != 0
        or cfpb_report.get("metrics", {}).get("write_capable_tool_invocation_count") != 0
        or cfpb_report.get("metrics", {}).get("unsafe_write_count") != 0
        or cfpb_report.get("claim_boundary", {}).get("accuracy_reported") is not False
    ):
        raise RuntimeError("CFPB shadow evaluation or non-claim boundary is inconsistent")


def _validate_synthetic_shadow(root: Path = ROOT) -> None:
    evidence = root / "artifacts" / "synthetic-http-shadow"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    provenance = manifest.get("provenance")
    expected_provenance = {
        "synthetic": True,
        "customer_data": False,
        "enterprise_historical_data": False,
        "third_party_provider": False,
        "blind_workload": True,
        "shadow_write_mode": "isolated-local-http-sandbox",
    }
    if provenance != expected_provenance:
        raise RuntimeError("synthetic shadow provenance is missing or overstated")
    declared = {entry["path"]: entry for entry in manifest.get("files", [])}
    expected_files = {"workload.json", "case-results.jsonl", "report.json", "report.md"}
    if set(declared) != expected_files:
        raise RuntimeError("synthetic shadow manifest file set is incomplete")
    for relative, entry in declared.items():
        path = evidence / relative
        if (
            not path.is_file()
            or path.stat().st_size != entry.get("bytes")
            or _sha256(path) != entry.get("sha256")
        ):
            raise RuntimeError(f"synthetic shadow artifact hash mismatch: {relative}")
    workload = json.loads((evidence / "workload.json").read_text(encoding="utf-8"))
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    if workload.get("case_count") != 240 or workload.get("provenance") != expected_provenance:
        raise RuntimeError("synthetic shadow workload is not the frozen 240-case synthetic set")
    expected_terminals = {
        "COMPLETED": 60,
        "RECOVERED_COMPLETED": 60,
        "COMPENSATED": 60,
        "UNKNOWN_MANUAL": 60,
    }
    kpis = report.get("kpis", {})
    method = report.get("method", {})
    if (
        report.get("provenance") != expected_provenance
        or report.get("terminal_counts") != expected_terminals
        or kpis.get("duplicate_side_effect_rate", {}).get("numerator") != 0
        or kpis.get("duplicate_side_effect_rate", {}).get("denominator") != 240
        or kpis.get("invalid_call_pass_rate", {}).get("numerator") != 0
        or kpis.get("invalid_call_pass_rate", {}).get("denominator") != 240
        or kpis.get("recoverable_terminal_rate", {}).get("numerator") != 180
        or kpis.get("recoverable_terminal_rate", {}).get("denominator") != 180
        or kpis.get("unknown_fail_closed_rate", {}).get("numerator") != 60
        or kpis.get("unknown_fail_closed_rate", {}).get("denominator") != 60
        or method.get("human_approval_assertion_covered") is not False
        or method.get("lease_expiry_injection", {}).get("production_api") is not False
    ):
        raise RuntimeError("synthetic shadow report metrics or claim boundaries are inconsistent")


def _validate_operations_reference(root: Path = ROOT) -> None:
    from proofmesh.operations_verifier import verify_operations_proof

    evidence = root / "artifacts/operations-reference"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    declared = {entry["path"]: entry for entry in manifest.get("files", [])}
    actual = {
        str(path.relative_to(evidence))
        for path in evidence.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(declared) != actual:
        raise RuntimeError("operations reference manifest file set differs from packaged files")
    for relative, entry in declared.items():
        path = evidence / relative
        if path.stat().st_size != entry.get("bytes") or _sha256(path) != entry.get("sha256"):
            raise RuntimeError(f"operations reference artifact hash mismatch: {relative}")
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    by_change = {case["change_id"]: case for case in report.get("cases", [])}
    expected = {
        "CHG-LOW-001": "COMPLETED",
        "CHG-HIGH-001": "COMPLETED",
        "CHG-ROLLBACK-001": "COMPENSATED",
        "CHG-UNKNOWN-001": "UNKNOWN_MANUAL",
    }
    if report.get("domain") != "production-operations-change" or {
        key: value.get("terminal_status") for key, value in by_change.items()
    } != expected:
        raise RuntimeError("operations reference cases or terminal states are inconsistent")
    for change_id in ("CHG-LOW-001", "CHG-HIGH-001", "CHG-ROLLBACK-001"):
        case = by_change[change_id]
        proof = evidence / case["proof"]
        verification = verify_operations_proof(
            proof,
            trust_bundle_path=evidence / "trust-bundle.json",
            pinned_policy_path=evidence / "pinned-operations-policy.json",
        )
        if not verification.valid:
            raise RuntimeError(f"operations reference proof is not externally valid: {change_id}")
    unknown = by_change["CHG-UNKNOWN-001"]
    boundaries = report.get("claim_boundaries", {})
    if (
        unknown.get("proof") is not None
        or unknown.get("external_verification") is not None
        or boundaries.get("production_cluster") is not False
        or boundaries.get("customer_data") is not False
        or boundaries.get("external_cloud_account") is not False
        or boundaries.get("unknown_lease_expiry_hook_is_production_api") is not False
    ):
        raise RuntimeError("operations reference UNKNOWN or claim boundary is overstated")


def _validate_sbom(root: Path = ROOT) -> None:
    import importlib.util

    script = root / "scripts/generate_sbom.py"
    spec = importlib.util.spec_from_file_location("proofmesh_release_sbom", script)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load deterministic SBOM generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checked_in = json.loads((root / "sbom.cdx.json").read_text(encoding="utf-8"))
    if checked_in != module.build_sbom(root):
        raise RuntimeError("checked-in CycloneDX SBOM differs from deterministic source scan")


def _run_gates(cwd: Path) -> None:
    commands = [
        ["pytest", "-q", "-p", "no:cacheprovider"],
        ["ruff", "check", "--no-cache", "src", "tests", "scripts"],
        ["node", "--check", "src/proofmesh/static/app.js"],
        [str(Path(os.sys.executable)), "scripts/validate_skills.py"],
        [str(Path(os.sys.executable)), "scripts/validate_agentteams.py"],
        [str(Path(os.sys.executable)), "scripts/validate_external_evidence.py"],
        [str(Path(os.sys.executable)), "scripts/validate_operations_reference.py"],
        [str(Path(os.sys.executable)), "scripts/benchmarks/run_cfpb_shadow.py", "--validate-only"],
    ]
    with tempfile.TemporaryDirectory(prefix="proofmesh-gate-home-") as runtime_home:
        runtime_root = Path(runtime_home)
        shutil.copytree(cwd / "data", runtime_root / "data")
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "src",
                "PROOFMESH_HOME": str(runtime_root),
            }
        )
        for command in commands:
            subprocess.run(command, cwd=cwd, env=environment, check=True)


def _copy_release_tree(destination: Path, files: list[Path]) -> Path:
    release_root = destination / "ProofMesh"
    for source in files:
        relative = source.relative_to(ROOT)
        target = release_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return release_root


def _release_instant() -> datetime:
    """Return one reproducible build instant, with SOURCE_DATE_EPOCH override support."""

    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch is None:
        return DEFAULT_RELEASE_INSTANT
    try:
        instant = datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError("SOURCE_DATE_EPOCH must be a valid integer Unix timestamp") from exc
    if not 1980 <= instant.year <= 2107:
        raise RuntimeError("SOURCE_DATE_EPOCH must fit the ZIP timestamp range 1980..2107")
    return instant.replace(microsecond=0)


def _write_release_manifest(release_root: Path, release_instant: datetime) -> None:
    files = sorted(path for path in release_root.rglob("*") if path.is_file())
    manifest = {
        "schema_version": "proofmesh.release-manifest/v1",
        "generated_at": release_instant.isoformat(),
        "project_version": "1.0.0",
        "release_gates": {
            "runtime_secret_scan": "PASS",
            "legacy_submission_scan": "PASS",
            "reference_proofs_external_verification": "3/3 PASS",
            "public_benchmark_lock": "PASS",
            "public_domain_evaluations": "Banking77 3,080 + CFPB 240 integrity and non-claim gates PASS",
            "synthetic_http_shadow_evidence": "240/240 integrity PASS",
            "external_claim_register": "fail-closed PASS; see artifacts/external-evidence/claim-status.json",
            "operations_second_domain": "4 paths integrity PASS; 3 proof-valid + 1 UNKNOWN fail-closed",
            "cyclonedx_sbom": "deterministic PASS",
            "source_and_extracted_test_suite": "PASS",
            "reproducible_archive": "PASS",
        },
        "files": [
            {
                "path": str(path.relative_to(release_root)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
    }
    (release_root / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_deterministic_zip(
    release_root: Path,
    destination: Path,
    release_instant: datetime,
) -> None:
    zip_timestamp = release_instant.utctimetuple()[:6]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(path for path in release_root.rglob("*") if path.is_file()):
            relative = Path("ProofMesh") / source.relative_to(release_root)
            info = zipfile.ZipInfo(str(relative), date_time=zip_timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (source.stat().st_mode & 0xFFFF) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(output: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    release_instant = _release_instant()
    sensitive = _find_sensitive_runtime()
    if sensitive:
        raise RuntimeError("run scripts/clean_runtime.py before release:\n" + "\n".join(sensitive))
    files = _source_files()
    relative_files = {path.relative_to(ROOT) for path in files}
    missing = sorted(str(path) for path in REQUIRED - relative_files)
    if missing:
        raise RuntimeError("required release files are missing: " + ", ".join(missing))
    if len(list((ROOT / "skills").glob("*/SKILL.md"))) != 7:
        raise RuntimeError("release must contain exactly seven reusable Skills")
    if len(list((ROOT / "agentteams/dist").glob("*.zip"))) != 7:
        raise RuntimeError("release must contain exactly seven AgentTeams worker packages")
    _validate_no_legacy_or_secret_text(_iter_release_text())
    _validate_no_credential_shapes(files)
    _validate_reference()
    _validate_public_benchmark()
    _validate_public_domain_evaluations()
    _validate_synthetic_shadow()
    _validate_operations_reference()
    _validate_sbom()
    _run_gates(ROOT)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="proofmesh-release-") as temp:
        stage = Path(temp)
        release_root = _copy_release_tree(stage, files)
        _write_release_manifest(release_root, release_instant)
        candidate = stage / "ProofMesh-release.zip"
        _write_deterministic_zip(release_root, candidate, release_instant)
        reproducibility_probe = stage / "ProofMesh-release.reproducibility-probe.zip"
        _write_deterministic_zip(release_root, reproducibility_probe, release_instant)
        if _sha256(candidate) != _sha256(reproducibility_probe):
            raise RuntimeError("release archive is not reproducible from identical inputs")
        extracted = stage / "extracted"
        with zipfile.ZipFile(candidate) as archive:
            archive.extractall(extracted)
        _validate_reference(extracted / "ProofMesh")
        _validate_public_benchmark(extracted / "ProofMesh")
        _validate_public_domain_evaluations(extracted / "ProofMesh")
        _validate_synthetic_shadow(extracted / "ProofMesh")
        _validate_operations_reference(extracted / "ProofMesh")
        _validate_sbom(extracted / "ProofMesh")
        _run_gates(extracted / "ProofMesh")
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        shutil.copy2(candidate, temporary_output)
        os.replace(temporary_output, output)
    return {"output": str(output), "bytes": output.stat().st_size, "sha256": _sha256(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
