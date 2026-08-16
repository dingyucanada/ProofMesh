import json
import sys

import pytest

from proofmesh import cli
from proofmesh.workflow import RefundWorkflowSummary, WorkflowStatus


def test_verify_command_is_read_only_and_fails_closed(monkeypatch, tmp_path, capsys):
    proof = tmp_path / "proof.json"
    trust = tmp_path / "trust.json"
    policy = tmp_path / "policy.json"
    proof.write_text("{}", encoding="utf-8")
    trust.write_text("{}", encoding="utf-8")
    policy.write_text("{}", encoding="utf-8")

    def must_not_build_runtime(_home):
        raise AssertionError("offline verification must not initialize runtime state")

    monkeypatch.setattr(cli, "build_runtime", must_not_build_runtime)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proofmesh",
            "--home",
            str(tmp_path),
            "verify-proof",
            "proof.json",
            "--trust-bundle",
            "trust.json",
            "--policy",
            "policy.json",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is False
    assert not (tmp_path / "var").exists()


def test_local_approval_cli_is_disabled_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFMESH_ENV", "production")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proofmesh",
            "--home",
            str(tmp_path),
            "approve",
            "wf-example",
            "--approver",
            "untrusted-cli-value",
            "--reason",
            "this reason is intentionally long enough",
            "--approval-assertion-file",
            str(tmp_path / "assertion.jws"),
        ],
    )

    class DummyRuntime:
        control_plane = object()

    monkeypatch.setattr(cli, "build_runtime", lambda _home: DummyRuntime())
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2


def test_high_risk_demo_prints_external_challenge_not_stale_direct_approve(monkeypatch, tmp_path, capsys):
    summary = RefundWorkflowSummary(
        workflow_id="wf-00000000000000000000000000000000",
        project_id="project-reference",
        ticket_id="TKT-HIGH-001",
        tenant_id="acme-cn",
        requester="operator-a",
        status=WorkflowStatus.WAITING_APPROVAL,
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
        context_digest="a" * 64,
        policy_digest="b" * 64,
    )

    class Control:
        def run_until_gate_or_terminal(self, _request):
            return summary

    class Runtime:
        control_plane = Control()

    monkeypatch.delenv("PROOFMESH_ENV", raising=False)
    monkeypatch.setattr(cli, "build_runtime", lambda _home: Runtime())
    monkeypatch.setattr(
        sys,
        "argv",
        ["proofmesh", "--home", str(tmp_path), "demo-refund", "TKT-HIGH-001"],
    )
    cli.main()
    output = capsys.readouterr().out
    assert "approval-challenge" in output
    assert "docs/deployment.md" in output
    assert "--approver reviewer" not in output
