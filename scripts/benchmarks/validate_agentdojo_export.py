#!/usr/bin/env python3
"""Validate the committed AgentDojo export without importing AgentDojo."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proofmesh.benchmarks.public_case import (  # noqa: E402
    CASE_SCHEMA_VERSION,
    canonical_json,
    find_unresolved_placeholders,
    verify_content_digest,
)

LOCK_SCHEMA = "proofmesh.public-benchmark-lock/v1"


class ValidationError(RuntimeError):
    """A benchmark artifact integrity or schema failure."""


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} must be an array")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError as exc:
        raise ValidationError(f"file does not exist: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid UTF-8 JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def _validate_call(call: Any, *, path: str, sequence: int) -> tuple[bool, int]:
    payload = _mapping(call, path)
    required = {
        "arguments",
        "call_id",
        "content_sha256",
        "function",
        "placeholder_arguments",
        "sequence",
        "unresolved_placeholders",
    }
    if set(payload) != required:
        raise ValidationError(f"{path} keys differ from schema: {sorted(payload)}")
    if payload["sequence"] != sequence:
        raise ValidationError(f"{path}.sequence is {payload['sequence']!r}, expected {sequence}")
    if not isinstance(payload["function"], str) or not payload["function"]:
        raise ValidationError(f"{path}.function must be a non-empty string")
    _mapping(payload["arguments"], f"{path}.arguments")
    if not verify_content_digest(payload):
        raise ValidationError(f"{path}.content_sha256 does not match canonical call content")

    placeholders = payload["placeholder_arguments"]
    unresolved = _sequence(payload["unresolved_placeholders"], f"{path}.unresolved_placeholders")
    if placeholders is None:
        expected_unresolved: list[dict[str, str]] = []
        has_placeholders = False
    else:
        _mapping(placeholders, f"{path}.placeholder_arguments")
        expected_unresolved = find_unresolved_placeholders(
            placeholders, path="$.placeholder_arguments"
        )
        has_placeholders = True
    if list(unresolved) != expected_unresolved:
        raise ValidationError(
            f"{path}.unresolved_placeholders does not exactly describe its serialized placeholder arguments"
        )
    return has_placeholders, len(expected_unresolved)


def _validate_case(
    record: Any,
    *,
    line_number: int,
    benchmark_lock: Mapping[str, Any],
) -> tuple[tuple[str, str, str], int, int]:
    path = f"line {line_number}"
    payload = _mapping(record, path)
    required = {
        "case_id",
        "content_sha256",
        "content_summary",
        "ground_truth",
        "injection_task",
        "schema_version",
        "source",
        "user_task",
    }
    if set(payload) != required:
        raise ValidationError(f"{path} keys differ from schema: {sorted(payload)}")
    if payload["schema_version"] != CASE_SCHEMA_VERSION:
        raise ValidationError(f"{path}.schema_version is {payload['schema_version']!r}")
    if not verify_content_digest(payload):
        raise ValidationError(f"{path}.content_sha256 does not match canonical case content")

    source = _mapping(payload["source"], f"{path}.source")
    expected_source_fields = {
        "benchmark": benchmark_lock.get("name"),
        "benchmark_version": benchmark_lock.get("benchmark_version"),
        "license": benchmark_lock.get("license"),
        "package": benchmark_lock.get("package"),
        "package_version": benchmark_lock.get("package_version"),
        "release": benchmark_lock.get("release"),
        "repository": benchmark_lock.get("repository"),
    }
    for key, expected in expected_source_fields.items():
        if source.get(key) != expected:
            raise ValidationError(f"{path}.source.{key} is {source.get(key)!r}, expected {expected!r}")
    if set(source) != {*expected_source_fields, "suite"}:
        raise ValidationError(f"{path}.source keys differ from schema: {sorted(source)}")
    suite = source["suite"]
    if not isinstance(suite, str) or not suite:
        raise ValidationError(f"{path}.source.suite must be a non-empty string")

    user_task = _mapping(payload["user_task"], f"{path}.user_task")
    injection_task = _mapping(payload["injection_task"], f"{path}.injection_task")
    if set(user_task) != {"difficulty", "ground_truth_output", "id", "prompt"}:
        raise ValidationError(f"{path}.user_task keys differ from schema: {sorted(user_task)}")
    if set(injection_task) != {"difficulty", "goal", "ground_truth_output", "id"}:
        raise ValidationError(f"{path}.injection_task keys differ from schema: {sorted(injection_task)}")
    user_id = user_task["id"]
    injection_id = injection_task["id"]
    if not isinstance(user_id, str) or not isinstance(injection_id, str):
        raise ValidationError(f"{path} task IDs must be strings")
    for field_path, value in (
        ("user_task.prompt", user_task["prompt"]),
        ("user_task.ground_truth_output", user_task["ground_truth_output"]),
        ("injection_task.goal", injection_task["goal"]),
        ("injection_task.ground_truth_output", injection_task["ground_truth_output"]),
    ):
        if not isinstance(value, str):
            raise ValidationError(f"{path}.{field_path} must be a string")
    if user_task["difficulty"] not in {"easy", "medium", "hard"}:
        raise ValidationError(f"{path}.user_task.difficulty is invalid")
    if injection_task["difficulty"] not in {"easy", "medium", "hard"}:
        raise ValidationError(f"{path}.injection_task.difficulty is invalid")

    expected_case_id = (
        f"agentdojo:{benchmark_lock['benchmark_version']}:{suite}:{user_id}:{injection_id}"
    )
    if payload["case_id"] != expected_case_id:
        raise ValidationError(f"{path}.case_id is {payload['case_id']!r}, expected {expected_case_id!r}")

    ground_truth = _mapping(payload["ground_truth"], f"{path}.ground_truth")
    if set(ground_truth) != {"injection", "user"}:
        raise ValidationError(f"{path}.ground_truth keys differ from schema")
    user_calls = _sequence(ground_truth["user"], f"{path}.ground_truth.user")
    injection_calls = _sequence(ground_truth["injection"], f"{path}.ground_truth.injection")
    placeholder_call_count = 0
    unresolved_count = 0
    for trajectory_name, calls in (("user", user_calls), ("injection", injection_calls)):
        for index, call in enumerate(calls):
            has_placeholders, call_unresolved = _validate_call(
                call,
                path=f"{path}.ground_truth.{trajectory_name}[{index}]",
                sequence=index,
            )
            placeholder_call_count += int(has_placeholders)
            unresolved_count += call_unresolved

    expected_summary = {
        "injection_ground_truth_call_count": len(injection_calls),
        "placeholder_argument_call_count": placeholder_call_count,
        "unresolved_placeholder_count": unresolved_count,
        "user_ground_truth_call_count": len(user_calls),
    }
    summary = _mapping(payload["content_summary"], f"{path}.content_summary")
    if dict(summary) != expected_summary:
        raise ValidationError(
            f"{path}.content_summary is {dict(summary)!r}, expected {expected_summary!r}"
        )
    return (suite, user_id, injection_id), placeholder_call_count, unresolved_count


def validate_export(artifact_path: Path, lock_path: Path) -> dict[str, Any]:
    lock = _load_json(lock_path)
    if lock.get("schema_version") != LOCK_SCHEMA:
        raise ValidationError(f"unsupported lock schema {lock.get('schema_version')!r}")
    benchmark_lock = _mapping(lock.get("benchmark"), "benchmark")
    suite_locks = _sequence(lock.get("suites"), "suites")
    totals_lock = _mapping(lock.get("totals"), "totals")
    artifact_lock = _mapping(lock.get("artifact"), "artifact")

    try:
        raw = artifact_path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"artifact does not exist: {artifact_path}") from exc
    if raw and not raw.endswith(b"\n"):
        raise ValidationError("artifact must end with one LF record terminator")
    if b"\r" in raw:
        raise ValidationError("artifact contains CR bytes; canonical serialization requires LF only")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"artifact is not valid UTF-8: {exc}") from exc
    lines = text.splitlines()

    expected_line_count = artifact_lock.get("line_count")
    if len(lines) != expected_line_count:
        raise ValidationError(f"artifact has {len(lines)} lines, lock requires {expected_line_count}")
    expected_byte_count = artifact_lock.get("byte_count")
    if not isinstance(expected_byte_count, int) or expected_byte_count < 1:
        raise ValidationError("artifact.byte_count must be a positive pinned integer")
    if len(raw) != expected_byte_count:
        raise ValidationError(f"artifact has {len(raw)} bytes, lock requires {expected_byte_count}")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = artifact_lock.get("sha256")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValidationError("artifact.sha256 must be a pinned 64-character digest")
    if actual_sha256 != expected_sha256:
        raise ValidationError(f"artifact SHA-256 is {actual_sha256}, lock requires {expected_sha256}")

    observed_combinations: list[tuple[str, str, str]] = []
    placeholder_call_count = 0
    unresolved_placeholder_count = 0
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise ValidationError(f"line {line_number} is empty")
        try:
            record = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValidationError(f"line {line_number} is invalid JSON: {exc}") from exc
        if canonical_json(record) != line:
            raise ValidationError(f"line {line_number} is not canonical compact sorted-key JSON")
        combination, case_placeholder_calls, case_unresolved = _validate_case(
            record,
            line_number=line_number,
            benchmark_lock=benchmark_lock,
        )
        observed_combinations.append(combination)
        placeholder_call_count += case_placeholder_calls
        unresolved_placeholder_count += case_unresolved

    expected_combinations: list[tuple[str, str, str]] = []
    suite_summary: dict[str, dict[str, int]] = {}
    total_users = 0
    total_injections = 0
    for suite_index, raw_suite_lock in enumerate(suite_locks):
        suite_lock = _mapping(raw_suite_lock, f"suites[{suite_index}]")
        suite_name = suite_lock.get("name")
        if not isinstance(suite_name, str):
            raise ValidationError(f"suites[{suite_index}].name must be a string")
        user_ids = list(_sequence(suite_lock.get("user_task_ids"), f"suites[{suite_index}].user_task_ids"))
        injection_ids = list(
            _sequence(suite_lock.get("injection_task_ids"), f"suites[{suite_index}].injection_task_ids")
        )
        expected_case_count = len(user_ids) * len(injection_ids)
        expected_counts = {
            "case_count": expected_case_count,
            "injection_task_count": len(injection_ids),
            "user_task_count": len(user_ids),
        }
        locked_counts = {key: suite_lock.get(key) for key in expected_counts}
        if expected_counts != locked_counts:
            raise ValidationError(
                f"{suite_name} lock counts are internally inconsistent: {locked_counts}, expected {expected_counts}"
            )
        expected_combinations.extend(
            (suite_name, str(user_id), str(injection_id))
            for user_id in user_ids
            for injection_id in injection_ids
        )
        suite_summary[suite_name] = expected_counts
        total_users += len(user_ids)
        total_injections += len(injection_ids)

    if observed_combinations != expected_combinations:
        for index, (observed, expected) in enumerate(
            zip(observed_combinations, expected_combinations), start=1
        ):
            if observed != expected:
                raise ValidationError(
                    f"case order/coverage differs at line {index}: observed={observed}, expected={expected}"
                )
        raise ValidationError(
            f"case coverage length differs: observed={len(observed_combinations)}, expected={len(expected_combinations)}"
        )
    if len(set(observed_combinations)) != len(observed_combinations):
        raise ValidationError("artifact contains duplicate suite/user/injection combinations")

    actual_totals = {
        "case_count": len(observed_combinations),
        "injection_task_count": total_injections,
        "suite_count": len(suite_summary),
        "user_task_count": total_users,
    }
    if actual_totals != dict(totals_lock):
        raise ValidationError(f"total counts are {actual_totals}, lock requires {dict(totals_lock)}")
    if artifact_lock.get("case_schema_version") != CASE_SCHEMA_VERSION:
        raise ValidationError("artifact.case_schema_version does not match the records")

    return {
        "artifact": str(artifact_path),
        "byte_count": len(raw),
        "placeholder_argument_call_count": placeholder_call_count,
        "sha256": actual_sha256,
        "suites": suite_summary,
        "totals": actual_totals,
        "unresolved_placeholder_count": unresolved_placeholder_count,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=PROJECT_ROOT / "data/benchmarks/public-benchmark.lock.json",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=PROJECT_ROOT / "artifacts/public-benchmark/cases.jsonl",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = validate_export(args.artifact.resolve(), args.lock.resolve())
    except ValidationError as exc:
        print(f"AgentDojo artifact validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
