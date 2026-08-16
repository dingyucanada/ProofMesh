from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import yaml

from proofmesh import api
from proofmesh.workflow import RefundWorkflowSummary, WorkflowStatus


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_agentteams_beta_static_contract_and_packages_are_valid():
    packager = _load_script("package_agentteams")
    validator = _load_script("validate_agentteams")
    first = packager.package_all()
    first_bytes = {name: (ROOT / "agentteams" / "dist" / name).read_bytes() for name in first}
    second = packager.package_all()
    second_bytes = {name: (ROOT / "agentteams" / "dist" / name).read_bytes() for name in second}
    assert first == second
    assert first_bytes == second_bytes

    result = validator.validate()
    assert result == {
        "schema_release": "v1.2.0-beta.1",
        "humans": 1,
        "workers": 7,
        "teams": 1,
        "dag_nodes": 7,
        "skills": 7,
        "packages": 7,
        "runtime_verified": False,
    }


def test_workers_use_qwenpaw_role_scoped_mcp_and_upload_overlay():
    workers = list(yaml.safe_load_all((ROOT / "agentteams" / "workers.yaml").read_text(encoding="utf-8")))
    assert len(workers) == 7
    urls = set()
    for worker in workers:
        spec = worker["spec"]
        assert spec["runtime"] == "qwenpaw"
        assert spec["model"] == "qwen3.5-plus"
        assert spec["image"] == "proofmesh/agentteams-qwenpaw-worker:v1.2.0-beta.1-compat2"
        assert "package" not in spec
        assert "skills" not in spec
        assert len(spec["mcpServers"]) == 1
        server = spec["mcpServers"][0]
        assert server["name"] == "proofmesh"
        assert server["transport"] == "http"
        assert server["url"].startswith("http://proofmesh:8000/mcp/roles/")
        urls.add(server["url"])
    assert len(urls) == 7


def test_static_skill_protocol_matches_implemented_role_mcp_and_summary():
    validator = _load_script("validate_agentteams")
    for role, (_skill, _stage, endpoint) in validator.ROLE_SKILL_ENDPOINT.items():
        locked = validator.ROLE_PROTOCOL[role]
        implemented = api.ROLE_TOOLS[endpoint]
        assert [f"proofmesh.{tool['name']}" for tool in implemented] == locked["tools"]
        primary = implemented[0]
        fields = set(primary["inputSchema"]["properties"])
        assert fields == set(primary["inputSchema"]["required"]) == locked["input_fields"]
        if role == "case-orchestrator":
            state_fields = set(implemented[1]["inputSchema"]["properties"])
            assert state_fields == set(implemented[1]["inputSchema"]["required"]) == {"workflow_id"}
    assert validator.SUMMARY_FIELDS == set(RefundWorkflowSummary.model_fields)
    dag = yaml.safe_load((ROOT / "agentteams" / "refund-dag.yaml").read_text(encoding="utf-8"))
    assert set(dag["spec"]["stateMapping"]["businessToTeamHarness"]) == {
        item.value for item in WorkflowStatus
    }


def test_file_uri_is_explicitly_rejected():
    validator = _load_script("validate_agentteams")
    with pytest.raises(ValueError, match="not distributable"):
        validator.validate_no_file_uri({"spec": {"package": "file://./dist/worker.zip"}}, "worker")


def test_wrong_zip_root_layout_is_rejected(tmp_path):
    validator = _load_script("validate_agentteams")
    role = "case-orchestrator"
    skill = "orchestrate-refund-case"
    bad_zip = tmp_path / "bad.zip"
    with ZipFile(bad_zip, "w", ZIP_DEFLATED) as archive:
        archive.writestr(f"{role}/manifest.json", "{}")
        archive.writestr(f"{role}/config/AGENTS.md", "wrong root")
        archive.writestr(f"{role}/config/SOUL.md", "wrong root")
    with pytest.raises(ValueError, match="package root/layout mismatch"):
        validator.validate_zip_package(bad_zip, role, skill)


def test_wrong_skill_role_tool_contract_is_rejected(tmp_path):
    validator = _load_script("validate_agentteams")
    skill = "normalize-refund-case"
    role = "ticket-intake"
    target = tmp_path / "skills" / skill
    shutil.copytree(ROOT / "skills" / skill, target)
    contract_path = target / "references" / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["dependencies"]["mcp_tools"] = ["proofmesh.execute_authorized"]
    contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    validator.ROOT = tmp_path
    with pytest.raises(ValueError, match="role MCP tool allowlist"):
        validator._validate_skill(role, skill)


def test_human_team_and_runtime_boundary_documentation_are_explicit():
    human = yaml.safe_load((ROOT / "agentteams" / "human.yaml").read_text(encoding="utf-8"))
    team = yaml.safe_load((ROOT / "agentteams" / "team.yaml").read_text(encoding="utf-8"))
    dag = yaml.safe_load((ROOT / "agentteams" / "refund-dag.yaml").read_text(encoding="utf-8"))
    assert human["kind"] == "Human"
    assert human["metadata"]["name"] == "proofmesh-approver"
    assert team["kind"] == "Team"
    assert team["spec"]["admin"] == {"name": "proofmesh-approver"}
    assert sum(member["role"] == "team_leader" for member in team["spec"]["workerMembers"]) == 1
    assert dag["spec"]["nodes"][0]["id"] == "create"
    assert dag["spec"]["nodes"][0]["executionSurface"] == "projectflow"
    assert dag["spec"]["nodes"][0]["materializesTask"] is False
    assert dag["spec"]["project"]["plannedTaskNodes"] == [
        "normalize", "context", "policy", "execute", "verify", "memory"
    ]
    assert dag["spec"]["stateMapping"]["businessToTeamHarness"]["WAITING_APPROVAL"]["nodeState"] == "planned"
    assert {
        "RECEIVED", "NORMALIZED", "CONTEXT_READY", "AUTHORIZED", "EXECUTED", "VERIFIED"
    }.issubset(dag["spec"]["stateMapping"]["businessToTeamHarness"])

    readme = (ROOT / "agentteams" / "README.md").read_text(encoding="utf-8")
    assert "BLOCKER" in readme
    assert "v1.2.0-beta.1" in readme
    assert "hiclaw apply" in readme
    assert "官方 beta 控制面" in readme
    assert "7/7 `Running`" in readme
    assert "已缓解 BLOCKER" in readme
    assert "Team=`Active`" in readme
    assert "直接 TeamHarness MCP 生命周期" in readme
    assert "模型驱动协同" in readme
    assert "Docker daemon 未运行" not in readme
    assert "尚未挂载 `/mcp/roles/*`" not in readme
    assert "同步串行调用七个角色标签" not in readme


def test_runtime_evidence_is_sanitized_and_separates_direct_from_model_driven_lifecycle():
    validator = _load_script("validate_agentteams")
    assert validator._validate_runtime_evidence() == 3

    with pytest.raises(ValueError, match="sensitive fields"):
        validator.assert_sanitized_runtime_evidence({"initialPassword": "do-not-store"})
    with pytest.raises(ValueError, match="credential-shaped value"):
        validator.assert_sanitized_runtime_evidence({"note": "Bearer abcdefghijklmnop"})
    with pytest.raises(ValueError, match="cannot claim live runtime"):
        validator.assert_sanitized_runtime_evidence({"runtime_verified": True})

    evidence = ROOT / "agentteams" / "runtime-evidence"
    control = json.loads((evidence / "control-plane.json").read_text(encoding="utf-8"))
    direct = json.loads((evidence / "teamharness-direct.json").read_text(encoding="utf-8"))
    assert control["scope"]["officialControlPlaneExecuted"] is True
    assert control["team"]["resourceCreated"] is True
    assert control["team"]["phase"] == "Active"
    assert len(control["team"]["workerMembers"]) == 7
    assert direct["executed"] is True
    assert direct["directMcpHealthProbe"]["verified"] is True
    assert direct["directMcpHealthProbe"]["expectedResult"]["tool"] == "health"
    assert {item["status"] for item in direct["lifecycle"]} == {"passed"}
    assert direct["lifecycle"][-1]["output"] == {"nodeStatus": "completed", "accepted": True}
    assert direct["artifactPublication"]["reasonCode"] == "ATB-007"
    assert direct["modelDrivenCollaboration"]["verified"] is False


def test_qwenpaw_compatibility_image_and_team_bootstrap_fail_closed():
    dockerfile = (ROOT / "agentteams" / "qwenpaw-compat.Dockerfile").read_text(encoding="utf-8")
    bootstrap = (ROOT / "agentteams" / "bootstrap.sh").read_text(encoding="utf-8")
    assert dockerfile.startswith("FROM agentteams/qwenpaw-worker:v1.2.0-beta.1")
    assert "ln -s /opt/hiclaw/qwenpaw-builtin /opt/agentteams/qwenpaw-builtin" in dockerfile
    assert 'agent-client-protocol==0.10.1' in dockerfile
    assert "Manager REST does not map spec.workerMembers" in bootstrap
    assert "never the legacy inline Team shape" in bootstrap
    assert "jq '{name, displayName, phase}'" in bootstrap


def test_upstream_worker_members_patch_is_pinned_and_contract_verified():
    upstream = ROOT / "agentteams" / "upstream"
    lock = json.loads((upstream / "source-lock.json").read_text(encoding="utf-8"))
    patch = upstream / lock["patch"]["path"]
    assert lock["upstream"] == {
        "repository": "https://github.com/agentscope-ai/AgentTeams",
        "release": "v1.2.0-beta.1",
        "commit": "78d0ceda336befa6e62bf89fc1a6b08b965e128d",
    }
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == lock["patch"]["sha256"]
    assert lock["expectedContract"]["legacyLeaderValidationPreservedWhenWorkerMembersEmpty"] is True
    assert lock["expectedContract"]["legacyLeaderAndWorkersMappingPreserved"] is True

    verifier_path = upstream / "verify_patch.py"
    spec = importlib.util.spec_from_file_location("verify_agentteams_upstream_patch", verifier_path)
    verifier = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(verifier)
    package_result = verifier.verify_package()
    assert package_result == {
        "ok": True,
        "upstream_release": "v1.2.0-beta.1",
        "upstream_commit": "78d0ceda336befa6e62bf89fc1a6b08b965e128d",
        "base_files_pinned": 3,
        "tests_pinned": 3,
        "patch_sha256": lock["patch"]["sha256"],
    }

    # The release archive deliberately does not vendor the upstream repository.
    # When the pinned checkout is available in this workspace, also exercise the
    # deeper hash/commit/apply/double-apply verifier.
    source = ROOT.parents[1] / "work" / "AgentTeams-v1.2.0-beta.1"
    if source.is_dir():
        result = verifier.verify(source)
        assert result == {
            "ok": True,
            "upstream_release": "v1.2.0-beta.1",
            "upstream_commit": "78d0ceda336befa6e62bf89fc1a6b08b965e128d",
            "upstream_commit_verified": True,
            "base_files_verified": 3,
            "patch_sha256": lock["patch"]["sha256"],
            "patch_applied_to_temporary_copy": True,
            "go_test_executed": False,
        }

    issue = (upstream / "UPSTREAM-ISSUE.md").read_text(encoding="utf-8")
    pr = (upstream / "PR-DESCRIPTION.md").read_text(encoding="utf-8")
    assert "HTTP 400: leader.name is required" in issue
    assert "does not claim" in issue
    assert "Backward compatibility" in pr
    assert "go test ./internal/server" in pr
