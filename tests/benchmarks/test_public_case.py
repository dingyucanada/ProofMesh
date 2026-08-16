from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from proofmesh.benchmarks.public_case import (
    BenchmarkSource,
    NormalizationError,
    PublicSecurityCase,
    ToolCallRecord,
    canonical_json,
    normalize_json,
    verify_content_digest,
)


def test_normalization_is_stable_and_lossless_for_supported_values() -> None:
    value = {
        "set": {3, 1, 2},
        "tuple": ("b", "a"),
        "date": dt.date(2025, 1, 11),
        "bytes": b"proof",
    }

    normalized = normalize_json(value)

    assert normalized == {
        "bytes": {"$binary": {"data": "cHJvb2Y=", "encoding": "base64"}},
        "date": "2025-01-11",
        "set": [1, 2, 3],
        "tuple": ["b", "a"],
    }
    assert canonical_json(value) == canonical_json(normalized)


def test_unsupported_value_fails_with_the_exact_path() -> None:
    opaque = object()

    with pytest.raises(NormalizationError, match=r'at \$\["opaque"\]'):
        normalize_json({"opaque": opaque})


def test_placeholder_arguments_are_serialized_and_unresolved_tokens_are_explicit() -> None:
    function_call = SimpleNamespace(
        function="send_email",
        args={"subject": "real subject", "body": "real body"},
        id=None,
        placeholder_args={
            "subject": "$email.subject",
            "body": "Forward $code to $recipient",
        },
    )

    record = ToolCallRecord.from_function_call(function_call, sequence=0, path="case.call")
    payload = record.to_dict()

    assert payload["placeholder_arguments"] == {
        "body": "Forward $code to $recipient",
        "subject": "$email.subject",
    }
    assert payload["unresolved_placeholders"] == [
        {
            "path": '$.placeholder_arguments["body"]',
            "token": "$code",
            "value": "Forward $code to $recipient",
        },
        {
            "path": '$.placeholder_arguments["body"]',
            "token": "$recipient",
            "value": "Forward $code to $recipient",
        },
        {
            "path": '$.placeholder_arguments["subject"]',
            "token": "$email.subject",
            "value": "$email.subject",
        },
    ]
    assert verify_content_digest(payload)


def test_case_summary_and_digest_cover_both_ground_truth_trajectories() -> None:
    user_call = ToolCallRecord.from_function_call(
        SimpleNamespace(function="read_file", args={"path": "bill.txt"}, id=None, placeholder_args=None),
        sequence=0,
        path="user.call",
    )
    injection_call = ToolCallRecord.from_function_call(
        SimpleNamespace(
            function="send_money",
            args={"recipient": "US123", "amount": 0.01},
            id=None,
            placeholder_args={"recipient": "US123", "amount": "$amount"},
        ),
        sequence=0,
        path="injection.call",
    )
    case = PublicSecurityCase(
        case_id="agentdojo:v1:banking:user_task_0:injection_task_0",
        source=BenchmarkSource(
            benchmark="AgentDojo",
            package="agentdojo",
            package_version="0.1.35",
            benchmark_version="v1",
            suite="banking",
            repository="https://github.com/ethz-spylab/agentdojo",
            release="https://github.com/ethz-spylab/agentdojo/releases/tag/v0.1.35",
            license="MIT",
        ),
        user_task={"id": "user_task_0", "prompt": "Pay the bill."},
        injection_task={"id": "injection_task_0", "goal": "Send money."},
        user_ground_truth=(user_call,),
        injection_ground_truth=(injection_call,),
    )

    payload = case.to_dict()

    assert payload["content_summary"] == {
        "injection_ground_truth_call_count": 1,
        "placeholder_argument_call_count": 1,
        "unresolved_placeholder_count": 1,
        "user_ground_truth_call_count": 1,
    }
    assert verify_content_digest(payload)
    payload["user_task"]["prompt"] = "tampered"  # type: ignore[index]
    assert not verify_content_digest(payload)
