#!/usr/bin/env python3
"""Export AgentDojo 0.1.35 / benchmark v1 official ground truth.

This command is deliberately model-free: it loads the packaged task suites and
calls their deterministic ``ground_truth`` methods. It never runs an agent,
attack, model, external API, or benchmark environment side effect.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proofmesh.benchmarks.public_case import (  # noqa: E402
    BenchmarkSource,
    PublicSecurityCase,
    ToolCallRecord,
    canonical_json,
)

LOCK_SCHEMA = "proofmesh.public-benchmark-lock/v1"
SUPPORTED_PACKAGE = "agentdojo"
SUPPORTED_PACKAGE_VERSION = "0.1.35"
SUPPORTED_BENCHMARK_VERSION = "v1"


class ExportError(RuntimeError):
    """An explicit, contextual export failure."""


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExportError(f"benchmark lock does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExportError(f"benchmark lock is invalid JSON at {path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != LOCK_SCHEMA:
        raise ExportError(f"unsupported benchmark lock schema in {path}: {payload.get('schema_version')!r}")
    return payload


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExportError(f"{path} must be an object")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ExportError(f"{path} must be an array")
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution_file(distribution: importlib.metadata.Distribution, suffix: str) -> Path:
    matches = [file for file in (distribution.files or ()) if str(file).endswith(suffix)]
    if len(matches) != 1:
        rendered = ", ".join(str(match) for match in matches) or "none"
        raise ExportError(f"expected exactly one installed {suffix!r} file; found {rendered}")
    path = Path(distribution.locate_file(matches[0])).resolve()
    if not path.is_file():
        raise ExportError(f"installed distribution file is missing: {path}")
    return path


def _verify_distribution(lock: Mapping[str, Any]) -> importlib.metadata.Distribution:
    benchmark = _require_mapping(lock.get("benchmark"), "benchmark")
    expected_package = benchmark.get("package")
    expected_version = benchmark.get("package_version")
    expected_benchmark_version = benchmark.get("benchmark_version")
    if (expected_package, expected_version, expected_benchmark_version) != (
        SUPPORTED_PACKAGE,
        SUPPORTED_PACKAGE_VERSION,
        SUPPORTED_BENCHMARK_VERSION,
    ):
        raise ExportError(
            "this exporter only supports the locked tuple "
            f"{SUPPORTED_PACKAGE}=={SUPPORTED_PACKAGE_VERSION}/{SUPPORTED_BENCHMARK_VERSION}; "
            f"lock requested {expected_package}=={expected_version}/{expected_benchmark_version}"
        )
    try:
        distribution = importlib.metadata.distribution(SUPPORTED_PACKAGE)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ExportError(
            f"{SUPPORTED_PACKAGE} is not installed; use an environment with "
            f"{SUPPORTED_PACKAGE}=={SUPPORTED_PACKAGE_VERSION}"
        ) from exc

    installed_version = distribution.version
    if installed_version != SUPPORTED_PACKAGE_VERSION:
        raise ExportError(
            f"installed {SUPPORTED_PACKAGE} version is {installed_version}, expected {SUPPORTED_PACKAGE_VERSION}"
        )

    classifiers = distribution.metadata.get_all("Classifier") or []
    if "License :: OSI Approved :: MIT License" not in classifiers:
        raise ExportError("installed AgentDojo metadata does not declare the locked MIT license classifier")

    license_path = _distribution_file(distribution, ".dist-info/licenses/LICENSE")
    expected_license_hash = benchmark.get("license_sha256")
    actual_license_hash = _hash_file(license_path)
    if actual_license_hash != expected_license_hash:
        raise ExportError(
            f"AgentDojo license hash mismatch: installed {actual_license_hash}, locked {expected_license_hash}"
        )

    metadata_path = _distribution_file(distribution, ".dist-info/METADATA")
    expected_metadata_hash = benchmark.get("installed_metadata_sha256")
    actual_metadata_hash = _hash_file(metadata_path)
    if actual_metadata_hash != expected_metadata_hash:
        raise ExportError(
            f"AgentDojo METADATA hash mismatch: installed {actual_metadata_hash}, locked {expected_metadata_hash}"
        )
    return distribution


def _task_number(task_id: str, prefix: str) -> int:
    expected_prefix = f"{prefix}_task_"
    if not task_id.startswith(expected_prefix) or not task_id[len(expected_prefix) :].isdigit():
        raise ExportError(f"unexpected AgentDojo task id {task_id!r}; expected {expected_prefix}<integer>")
    return int(task_id[len(expected_prefix) :])


def _difficulty(task: Any) -> str:
    value = getattr(task, "DIFFICULTY", None)
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        raise ExportError(f"task {getattr(task, 'ID', '<unknown>')} has no enum DIFFICULTY name")
    return name.lower()


def _task_text(task: Any, attribute: str) -> str:
    value = getattr(task, attribute, None)
    if not isinstance(value, str):
        raise ExportError(
            f"task {getattr(task, 'ID', '<unknown>')}.{attribute} is "
            f"{type(value).__name__}, expected string"
        )
    return value


def _ground_truth(task: Any, environment: Any, *, context: str) -> tuple[ToolCallRecord, ...]:
    try:
        raw_calls = task.ground_truth(environment.model_copy(deep=True))
    except Exception as exc:
        raise ExportError(f"official ground_truth failed for {context}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw_calls, list):
        raise ExportError(f"official ground_truth for {context} returned {type(raw_calls).__name__}, expected list")

    records: list[ToolCallRecord] = []
    for sequence, call in enumerate(raw_calls):
        try:
            records.append(
                ToolCallRecord.from_function_call(
                    call,
                    sequence=sequence,
                    path=f"{context}.ground_truth[{sequence}]",
                )
            )
        except Exception as exc:
            if isinstance(exc, ExportError):
                raise
            raise ExportError(
                f"could not normalize official ground_truth for {context} call {sequence}: {exc}"
            ) from exc
    return tuple(records)


def _assert_locked_ids(
    *, actual: Mapping[str, Any], expected: Sequence[Any], suite_name: str, kind: str
) -> list[str]:
    actual_ids = sorted(actual, key=lambda task_id: _task_number(task_id, kind))
    expected_ids = list(expected)
    if any(not isinstance(task_id, str) for task_id in expected_ids):
        raise ExportError(f"lock suites[{suite_name}].{kind}_task_ids must contain only strings")
    if actual_ids != expected_ids:
        raise ExportError(
            f"{suite_name} {kind} task IDs differ from lock: installed={actual_ids}, locked={expected_ids}"
        )
    return actual_ids


def _export_records(lock: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        from agentdojo.task_suite.load_suites import get_suite
    except Exception as exc:
        raise ExportError(f"could not import AgentDojo suite loader: {type(exc).__name__}: {exc}") from exc

    benchmark = _require_mapping(lock.get("benchmark"), "benchmark")
    suite_locks = _require_sequence(lock.get("suites"), "suites")
    benchmark_version = str(benchmark["benchmark_version"])
    records: list[dict[str, Any]] = []
    suite_summaries: dict[str, dict[str, int]] = {}

    for suite_index, raw_suite_lock in enumerate(suite_locks):
        suite_lock = _require_mapping(raw_suite_lock, f"suites[{suite_index}]")
        suite_name = suite_lock.get("name")
        if not isinstance(suite_name, str) or not suite_name:
            raise ExportError(f"suites[{suite_index}].name must be a non-empty string")
        try:
            suite = get_suite(benchmark_version, suite_name)
        except Exception as exc:
            raise ExportError(
                f"could not load AgentDojo suite {benchmark_version}/{suite_name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        user_ids = _assert_locked_ids(
            actual=suite.user_tasks,
            expected=_require_sequence(suite_lock.get("user_task_ids"), f"suites[{suite_index}].user_task_ids"),
            suite_name=suite_name,
            kind="user",
        )
        injection_ids = _assert_locked_ids(
            actual=suite.injection_tasks,
            expected=_require_sequence(
                suite_lock.get("injection_task_ids"), f"suites[{suite_index}].injection_task_ids"
            ),
            suite_name=suite_name,
            kind="injection",
        )

        expected_counts = (
            suite_lock.get("user_task_count"),
            suite_lock.get("injection_task_count"),
            suite_lock.get("case_count"),
        )
        actual_counts = (len(user_ids), len(injection_ids), len(user_ids) * len(injection_ids))
        if actual_counts != expected_counts:
            raise ExportError(
                f"{suite_name} count mismatch: installed={actual_counts}, locked={expected_counts}"
            )

        try:
            base_environment = suite.load_and_inject_default_environment({})
        except Exception as exc:
            raise ExportError(
                f"could not load default environment for {suite_name}: {type(exc).__name__}: {exc}"
            ) from exc

        suite_case_count = 0
        for user_id in user_ids:
            user_task = suite.user_tasks[user_id]
            try:
                task_environment = user_task.init_environment(base_environment.model_copy(deep=True))
            except Exception as exc:
                raise ExportError(
                    f"init_environment failed for {suite_name}/{user_id}: {type(exc).__name__}: {exc}"
                ) from exc
            user_calls = _ground_truth(
                user_task,
                task_environment,
                context=f"{suite_name}/{user_id}",
            )

            for injection_id in injection_ids:
                injection_task = suite.injection_tasks[injection_id]
                injection_calls = _ground_truth(
                    injection_task,
                    task_environment,
                    context=f"{suite_name}/{user_id}/{injection_id}",
                )
                source = BenchmarkSource(
                    benchmark=str(benchmark["name"]),
                    package=str(benchmark["package"]),
                    package_version=str(benchmark["package_version"]),
                    benchmark_version=benchmark_version,
                    suite=suite_name,
                    repository=str(benchmark["repository"]),
                    release=str(benchmark["release"]),
                    license=str(benchmark["license"]),
                )
                case = PublicSecurityCase(
                    case_id=f"agentdojo:{benchmark_version}:{suite_name}:{user_id}:{injection_id}",
                    source=source,
                    user_task={
                        "difficulty": _difficulty(user_task),
                        "ground_truth_output": _task_text(user_task, "GROUND_TRUTH_OUTPUT"),
                        "id": user_id,
                        "prompt": _task_text(user_task, "PROMPT"),
                    },
                    injection_task={
                        "difficulty": _difficulty(injection_task),
                        "goal": _task_text(injection_task, "GOAL"),
                        "ground_truth_output": _task_text(injection_task, "GROUND_TRUTH_OUTPUT"),
                        "id": injection_id,
                    },
                    user_ground_truth=user_calls,
                    injection_ground_truth=injection_calls,
                )
                try:
                    record = case.to_dict()
                except Exception as exc:
                    raise ExportError(
                        f"case normalization failed for {suite_name}/{user_id}/{injection_id}: {exc}"
                    ) from exc
                records.append(record)
                suite_case_count += 1

        suite_summaries[suite_name] = {
            "case_count": suite_case_count,
            "injection_task_count": len(injection_ids),
            "user_task_count": len(user_ids),
        }

    totals = _require_mapping(lock.get("totals"), "totals")
    actual_totals = {
        "case_count": len(records),
        "injection_task_count": sum(item["injection_task_count"] for item in suite_summaries.values()),
        "suite_count": len(suite_summaries),
        "user_task_count": sum(item["user_task_count"] for item in suite_summaries.values()),
    }
    locked_totals = {key: totals.get(key) for key in actual_totals}
    if actual_totals != locked_totals:
        raise ExportError(f"total count mismatch: installed={actual_totals}, locked={locked_totals}")

    return records, {"suites": suite_summaries, "totals": actual_totals}


def _serialize_and_verify_artifact(
    records: Sequence[Mapping[str, Any]], lock: Mapping[str, Any]
) -> tuple[bytes, str]:
    try:
        data = "".join(f"{canonical_json(record)}\n" for record in records).encode("utf-8")
    except Exception as exc:
        raise ExportError(f"canonical JSONL serialization failed: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    artifact_lock = _require_mapping(lock.get("artifact"), "artifact")
    expected_lines = artifact_lock.get("line_count")
    if len(records) != expected_lines:
        raise ExportError(f"artifact line count is {len(records)}, lock requires {expected_lines}")
    expected_bytes = artifact_lock.get("byte_count")
    if expected_bytes is not None and len(data) != expected_bytes:
        raise ExportError(f"artifact byte count is {len(data)}, lock requires {expected_bytes}")
    expected_digest = artifact_lock.get("sha256")
    if expected_digest is not None and digest != expected_digest:
        raise ExportError(f"artifact SHA-256 is {digest}, lock requires {expected_digest}")
    return data, digest


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def export(lock_path: Path, output_path: Path) -> dict[str, Any]:
    lock = _load_lock(lock_path)
    distribution = _verify_distribution(lock)
    records, summary = _export_records(lock)
    data, digest = _serialize_and_verify_artifact(records, lock)
    _atomic_write(output_path, data)

    placeholder_calls = sum(
        int(record["content_summary"]["placeholder_argument_call_count"]) for record in records
    )
    unresolved_placeholders = sum(
        int(record["content_summary"]["unresolved_placeholder_count"]) for record in records
    )
    return {
        "agentdojo_version": distribution.version,
        "benchmark_version": SUPPORTED_BENCHMARK_VERSION,
        "byte_count": len(data),
        "output": str(output_path),
        "placeholder_argument_call_count": placeholder_calls,
        "sha256": digest,
        **summary,
        "unresolved_placeholder_count": unresolved_placeholders,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=PROJECT_ROOT / "data/benchmarks/public-benchmark.lock.json",
        help="pinned benchmark/source/count lock",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts/public-benchmark/cases.jsonl",
        help="canonical JSONL output path",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = export(args.lock.resolve(), args.output.resolve())
    except ExportError as exc:
        print(f"AgentDojo export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
