"""Dependency-free, deterministic records for public agent-security benchmarks.

The adapter deliberately has no AgentDojo or Pydantic import.  Exporters may
pass Pydantic objects through the small ``model_dump`` protocol, while readers
can validate committed JSONL artifacts with only the Python standard library.
Unsupported values fail with a precise path rather than being stringified.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import enum
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePath
from typing import Any, TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

CASE_SCHEMA_VERSION = "proofmesh.public-security-case/v1"
_PLACEHOLDER_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_.-]*")


class NormalizationError(ValueError):
    """Raised when a value cannot be represented without lossy coercion."""

    def __init__(self, path: str, value: Any, reason: str) -> None:
        value_type = f"{type(value).__module__}.{type(value).__qualname__}"
        super().__init__(f"cannot normalize {value_type} at {path}: {reason}")
        self.path = path
        self.value_type = value_type
        self.reason = reason


def _child_path(path: str, key: str) -> str:
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def normalize_json(value: Any, *, path: str = "$", _active: set[int] | None = None) -> JSONValue:
    """Convert a value into deterministic JSON data or fail explicitly.

    Mapping keys are sorted, tuples become arrays, sets are sorted by their
    canonical JSON representation, datetimes use ISO-8601, and bytes carry an
    explicit base64 encoding marker.  NaN and infinities are rejected because
    they are outside RFC 8259 JSON.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NormalizationError(path, value, "non-finite floats are not valid JSON")
        return value
    if isinstance(value, enum.Enum):
        return normalize_json(value.value, path=path, _active=_active)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NormalizationError(path, value, "non-finite Decimal values are not supported")
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"$binary": {"data": encoded, "encoding": "base64"}}

    if _active is None:
        _active = set()

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="python", by_alias=True, exclude_none=False)
        except Exception as exc:  # pragma: no cover - exact exception is provider-specific
            raise NormalizationError(path, value, f"model_dump failed: {exc}") from exc
        return normalize_json(dumped, path=path, _active=_active)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        marker = id(value)
        if marker in _active:
            raise NormalizationError(path, value, "cyclic dataclass reference")
        _active.add(marker)
        try:
            result: dict[str, JSONValue] = {}
            for field in sorted(dataclasses.fields(value), key=lambda item: item.name):
                result[field.name] = normalize_json(
                    getattr(value, field.name), path=_child_path(path, field.name), _active=_active
                )
            return result
        finally:
            _active.remove(marker)

    if isinstance(value, Mapping):
        marker = id(value)
        if marker in _active:
            raise NormalizationError(path, value, "cyclic mapping reference")
        _active.add(marker)
        try:
            items: list[tuple[str, Any]] = []
            for key, item in value.items():
                if not isinstance(key, str):
                    raise NormalizationError(path, key, "mapping keys must be strings")
                items.append((key, item))
            return {
                key: normalize_json(item, path=_child_path(path, key), _active=_active)
                for key, item in sorted(items, key=lambda pair: pair[0])
            }
        finally:
            _active.remove(marker)

    if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
        marker = id(value)
        if marker in _active:
            raise NormalizationError(path, value, "cyclic set reference")
        _active.add(marker)
        try:
            normalized = [normalize_json(item, path=f"{path}[*]", _active=_active) for item in value]
            return sorted(normalized, key=canonical_json)
        finally:
            _active.remove(marker)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        marker = id(value)
        if marker in _active:
            raise NormalizationError(path, value, "cyclic sequence reference")
        _active.add(marker)
        try:
            return [
                normalize_json(item, path=f"{path}[{index}]", _active=_active)
                for index, item in enumerate(value)
            ]
        finally:
            _active.remove(marker)

    raise NormalizationError(path, value, "no lossless JSON representation is registered")


def canonical_json(value: Any) -> str:
    """Serialize using one cross-run canonical representation."""

    normalized = normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_digest(value: Any) -> str:
    """Return the SHA-256 of the canonical UTF-8 JSON representation."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def find_unresolved_placeholders(value: Any, *, path: str = "$") -> list[dict[str, str]]:
    """Find AgentDojo-style ``$name`` tokens without inventing resolutions."""

    normalized = normalize_json(value, path=path)
    unresolved: list[dict[str, str]] = []

    def visit(item: JSONValue, item_path: str) -> None:
        if isinstance(item, str):
            for match in _PLACEHOLDER_RE.finditer(item):
                unresolved.append({"path": item_path, "token": match.group(0), "value": item})
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}[{index}]")
            return
        if isinstance(item, dict):
            for key in sorted(item):
                visit(item[key], _child_path(item_path, key))

    visit(normalized, path)
    return unresolved


def _attach_content_digest(payload: dict[str, JSONValue]) -> dict[str, JSONValue]:
    result = dict(payload)
    result["content_sha256"] = sha256_digest(payload)
    return result


def verify_content_digest(payload: Mapping[str, Any]) -> bool:
    """Verify a record whose digest is stored in ``content_sha256``."""

    expected = payload.get("content_sha256")
    if not isinstance(expected, str):
        return False
    digest_input = {key: value for key, value in payload.items() if key != "content_sha256"}
    return expected == sha256_digest(digest_input)


@dataclass(frozen=True)
class BenchmarkSource:
    """Pinned public benchmark provenance for a case."""

    benchmark: str
    package: str
    package_version: str
    benchmark_version: str
    suite: str
    repository: str
    release: str
    license: str

    def to_dict(self) -> dict[str, JSONValue]:
        return normalize_json(dataclasses.asdict(self))  # type: ignore[return-value]


@dataclass(frozen=True)
class ToolCallRecord:
    """A normalized ground-truth tool call with explicit placeholder state."""

    sequence: int
    function: str
    arguments: dict[str, JSONValue]
    call_id: JSONValue
    placeholder_arguments: dict[str, JSONValue] | None
    unresolved_placeholders: tuple[dict[str, str], ...]

    @classmethod
    def from_function_call(cls, function_call: Any, *, sequence: int, path: str) -> "ToolCallRecord":
        function = getattr(function_call, "function", None)
        if not isinstance(function, str) or not function:
            raise NormalizationError(f"{path}.function", function_call, "function must be a non-empty string")

        arguments = normalize_json(getattr(function_call, "args", None), path=f"{path}.args")
        if not isinstance(arguments, dict):
            raise NormalizationError(f"{path}.args", arguments, "arguments must normalize to an object")

        placeholder_source = getattr(function_call, "placeholder_args", None)
        placeholder_arguments: dict[str, JSONValue] | None
        unresolved: list[dict[str, str]]
        if placeholder_source is None:
            placeholder_arguments = None
            unresolved = []
        else:
            normalized_placeholders = normalize_json(
                placeholder_source, path=f"{path}.placeholder_args"
            )
            if not isinstance(normalized_placeholders, dict):
                raise NormalizationError(
                    f"{path}.placeholder_args",
                    normalized_placeholders,
                    "placeholder arguments must normalize to an object",
                )
            placeholder_arguments = normalized_placeholders
            unresolved = find_unresolved_placeholders(
                normalized_placeholders, path="$.placeholder_arguments"
            )

        return cls(
            sequence=sequence,
            function=function,
            arguments=arguments,
            call_id=normalize_json(getattr(function_call, "id", None), path=f"{path}.id"),
            placeholder_arguments=placeholder_arguments,
            unresolved_placeholders=tuple(unresolved),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "arguments": self.arguments,
            "call_id": self.call_id,
            "function": self.function,
            "placeholder_arguments": self.placeholder_arguments,
            "sequence": self.sequence,
            "unresolved_placeholders": [dict(item) for item in self.unresolved_placeholders],
        }
        return _attach_content_digest(payload)


@dataclass(frozen=True)
class PublicSecurityCase:
    """One Cartesian user-task/injection-task case from a public benchmark."""

    case_id: str
    source: BenchmarkSource
    user_task: dict[str, JSONValue]
    injection_task: dict[str, JSONValue]
    user_ground_truth: tuple[ToolCallRecord, ...]
    injection_ground_truth: tuple[ToolCallRecord, ...]

    def to_dict(self) -> dict[str, JSONValue]:
        user_calls = [call.to_dict() for call in self.user_ground_truth]
        injection_calls = [call.to_dict() for call in self.injection_ground_truth]
        all_calls = [*user_calls, *injection_calls]
        placeholder_call_count = sum(
            call["placeholder_arguments"] is not None for call in all_calls
        )
        unresolved_count = sum(len(call["unresolved_placeholders"]) for call in all_calls)  # type: ignore[arg-type]
        payload: dict[str, JSONValue] = {
            "case_id": self.case_id,
            "content_summary": {
                "injection_ground_truth_call_count": len(injection_calls),
                "placeholder_argument_call_count": placeholder_call_count,
                "unresolved_placeholder_count": unresolved_count,
                "user_ground_truth_call_count": len(user_calls),
            },
            "ground_truth": {
                "injection": injection_calls,
                "user": user_calls,
            },
            "injection_task": normalize_json(self.injection_task),
            "schema_version": CASE_SCHEMA_VERSION,
            "source": self.source.to_dict(),
            "user_task": normalize_json(self.user_task),
        }
        return _attach_content_digest(payload)
