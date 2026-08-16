import json
import subprocess
import sys
from pathlib import Path


def test_committed_agentdojo_v1_artifact_matches_the_full_locked_cartesian_product() -> None:
    project_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/benchmarks/validate_agentdojo_export.py"),
            "--artifact",
            str(project_root / "artifacts/public-benchmark/cases.jsonl"),
            "--lock",
            str(project_root / "data/benchmarks/public-benchmark.lock.json"),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["totals"] == {
        "case_count": 629,
        "injection_task_count": 27,
        "suite_count": 4,
        "user_task_count": 97,
    }
    assert summary["suites"] == {
        "workspace": {
            "case_count": 240,
            "injection_task_count": 6,
            "user_task_count": 40,
        },
        "slack": {
            "case_count": 105,
            "injection_task_count": 5,
            "user_task_count": 21,
        },
        "travel": {
            "case_count": 140,
            "injection_task_count": 7,
            "user_task_count": 20,
        },
        "banking": {
            "case_count": 144,
            "injection_task_count": 9,
            "user_task_count": 16,
        },
    }
    assert summary["placeholder_argument_call_count"] == 1105
    assert summary["unresolved_placeholder_count"] == 794
    assert summary["byte_count"] == 1863974
    assert summary["sha256"] == "20146525d139f83d732bc47e0286b0eccdf67118cdc5eba84f5f079863f5546b"
