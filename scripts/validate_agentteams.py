from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

import yaml


ROOT = Path(__file__).resolve().parents[1]
AGENTTEAMS = ROOT / "agentteams"
RUNTIME_EVIDENCE = AGENTTEAMS / "runtime-evidence"
API_VERSION = "agentteams.io/v1beta1"
SCHEMA_RELEASE = "v1.2.0-beta.1"
SCHEMA_COMMIT = "78d0ceda336befa6e62bf89fc1a6b08b965e128d"
QWENPAW_IMAGE = "proofmesh/agentteams-qwenpaw-worker:v1.2.0-beta.1-compat2"

SENSITIVE_EVIDENCE_KEYS = {
    "password", "initialpassword", "token", "accesstoken", "refreshtoken",
    "secret", "secretkey", "apikey", "credential", "credentials",
    "authorization", "cookie", "privatekey",
}
SENSITIVE_EVIDENCE_VALUES = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?:access_token|api_key|password|secret)=[^\s&]{4,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsyt_[A-Za-z0-9_-]{8,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
)

ROLE_SKILL_ENDPOINT = {
    "case-orchestrator": ("orchestrate-refund-case", "create", "orchestrator"),
    "ticket-intake": ("normalize-refund-case", "normalize", "intake"),
    "context-investigator": ("collect-refund-context", "context", "investigator"),
    "risk-policy-sentinel": ("evaluate-refund-policy", "policy", "policy"),
    "action-executor": ("execute-refund-saga", "execute", "executor"),
    "outcome-verifier": ("verify-refund-outcome", "verify", "verifier"),
    "case-memory-curator": ("curate-refund-memory", "memory", "memory"),
}

ROLE_PROTOCOL = {
    "case-orchestrator": {
        "tools": ["proofmesh.create_case", "proofmesh.get_state"],
        "input_fields": {"ticket_id", "tenant_id"},
        "output_statuses": {"RECEIVED"},
        "last_step": "create_case",
    },
    "ticket-intake": {
        "tools": ["proofmesh.normalize_case"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"NORMALIZED"},
        "last_step": "normalize_case",
    },
    "context-investigator": {
        "tools": ["proofmesh.gather_context"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"CONTEXT_READY", "BLOCKED"},
        "last_step": "gather_context",
    },
    "risk-policy-sentinel": {
        "tools": ["proofmesh.evaluate_policy"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"WAITING_APPROVAL", "AUTHORIZED", "BLOCKED"},
        "last_step": "evaluate_policy",
    },
    "action-executor": {
        "tools": ["proofmesh.execute_authorized"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"EXECUTED", "BLOCKED"},
        "last_step": "execute_authorized",
    },
    "outcome-verifier": {
        "tools": ["proofmesh.verify_outcome"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"VERIFIED", "COMPENSATED", "BLOCKED"},
        "last_step": "verify_outcome",
    },
    "case-memory-curator": {
        "tools": ["proofmesh.curate_memory"],
        "input_fields": {"workflow_id", "expected_revision", "task_id"},
        "output_statuses": {"COMPLETED", "COMPENSATED"},
        "last_step": "curate_memory",
    },
}

SUMMARY_FIELDS = {
    "workflow_id", "project_id", "ticket_id", "tenant_id", "requester", "status",
    "revision", "created_at", "updated_at", "context_digest", "policy_digest",
    "plan_digest", "approval_required", "approval_scope", "approval_digest",
    "amount_minor", "currency", "risk_score", "last_step", "next_step",
    "final_message", "proof_bundle", "gateway_receipt_count", "task_receipt_count",
    "agent_count",
}
ROLE_MCP_WIRE_CODES = {"-32600", "-32601", "-32602", "-32003", "-32009"}

OFFICIAL_SPEC_KEYS = {
    "Human": {
        "accessibleTeams",
        "accessibleWorkers",
        "displayName",
        "email",
        "identitySource",
        "note",
        "permissionLevel",
        "username",
    },
    "Worker": {
        "accessEntries",
        "agentIdentity",
        "agents",
        "backendRuntime",
        "channelPolicy",
        "channels",
        "containerManaged",
        "credentialBindings",
        "deployMode",
        "env",
        "expose",
        "identity",
        "idleTimeout",
        "image",
        "labels",
        "mcpServers",
        "model",
        "modelProvider",
        "package",
        "remoteSkills",
        "resources",
        "runtime",
        "serviceEnabled",
        "skills",
        "soul",
        "state",
        "workerName",
    },
    "Team": {
        "admin",
        "channelPolicy",
        "description",
        "heartbeatEvery",
        "humanMembers",
        "leader",
        "peerMentions",
        "teamName",
        "workerMembers",
        "workers",
    },
}


def _read_yaml_documents(path: Path) -> list[dict[str, Any]]:
    try:
        documents = [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item is not None]
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid YAML {path}: {exc}") from exc
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise ValueError(f"{path}: expected one or more YAML objects")
    return documents


def validate_no_file_uri(value: Any, location: str = "resource") -> None:
    """Reject host-local file URIs recursively; AgentTeams Manager cannot receive them via YAML apply."""
    if isinstance(value, str):
        if value.lower().startswith("file://"):
            raise ValueError(f"{location}: file:// package references are not distributable")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            validate_no_file_uri(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_no_file_uri(child, f"{location}[{index}]")


def assert_sanitized_runtime_evidence(value: Any, location: str = "runtime-evidence") -> None:
    """Reject credential-shaped keys and values before runtime evidence can be shipped."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in SENSITIVE_EVIDENCE_KEYS:
                raise ValueError(f"{location}.{key}: sensitive fields are forbidden in runtime evidence")
            if normalized == "runtimeverified" and child is True:
                raise ValueError(f"{location}.{key}: static evidence cannot claim live runtime verification")
            assert_sanitized_runtime_evidence(child, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            assert_sanitized_runtime_evidence(child, f"{location}[{index}]")
        return
    if isinstance(value, str):
        for pattern in SENSITIVE_EVIDENCE_VALUES:
            if pattern.search(value):
                raise ValueError(f"{location}: credential-shaped value is forbidden in runtime evidence")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    assert_sanitized_runtime_evidence(value, path.name)
    return value


def _validate_runtime_evidence() -> int:
    expected_files = {
        "README.md", "control-plane.json", "compatibility-findings.json", "teamharness-direct.json"
    }
    actual_files = {path.name for path in RUNTIME_EVIDENCE.iterdir() if path.is_file()} if RUNTIME_EVIDENCE.is_dir() else set()
    if actual_files != expected_files:
        raise ValueError(
            "agentteams/runtime-evidence: expected only the sanitized evidence set; "
            f"missing={sorted(expected_files - actual_files)}, extra={sorted(actual_files - expected_files)}"
        )

    control = _read_json(RUNTIME_EVIDENCE / "control-plane.json")
    findings = _read_json(RUNTIME_EVIDENCE / "compatibility-findings.json")
    direct = _read_json(RUNTIME_EVIDENCE / "teamharness-direct.json")
    captured_at = control.get("capturedAt")
    if not isinstance(captured_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", captured_at):
        raise ValueError("control-plane.json: capturedAt must be a timezone-qualified ISO-8601 timestamp")
    if findings.get("capturedAt") != captured_at or direct.get("capturedAt") != captured_at:
        raise ValueError("runtime evidence snapshots must share one capturedAt timestamp")
    release_lock = {"release": SCHEMA_RELEASE, "commit": SCHEMA_COMMIT}
    if control.get("agentTeams") != release_lock or findings.get("agentTeams") != release_lock:
        raise ValueError("runtime evidence must pin the audited AgentTeams release and commit")

    if control.get("schemaVersion") != "proofmesh.io/agentteams-runtime-evidence/v1":
        raise ValueError("control-plane.json: unsupported evidence schema")
    expected_scope = {
        "officialControlPlaneExecuted": True,
        "modelDrivenCollaborationVerified": False,
        "directMcpControlPlaneLifecycleVerified": True,
        "staticValidatorQueriesLiveRuntime": False,
    }
    if control.get("scope") != expected_scope:
        raise ValueError("control-plane.json: execution and non-execution boundaries must be explicit")
    if control.get("controller") != {
        "containerState": "running",
        "image": "proofmesh/agentteams-embedded:v1.2.0-beta.1-workerMembers1",
        "imageDigest": "sha256:989e50aa15d185701b814a0f6b950552770082d02665837e4c8117a48d70f067",
        "officialParentDigest": "sha256:4467037493afa32b0a13f7fc6e0054b6e8e694067c7a7c33937cf930b86b8447",
        "patchSha256": "1bb1ae7c2303d6e0c0932565c7391d01d38cb570d60317b25be40be73deac270",
        "binarySha256": "510e744bd5fb207b72cc5ca8dc13cfa07b7e37d932fb6b906e4b074a59cfdc31",
    }:
        raise ValueError("control-plane.json: controller image/state does not match the sanitized observation")
    if control.get("manager") != {
        "name": "default",
        "phase": "Running",
        "runtime": "copaw",
        "model": "proofmesh-local",
        "welcomeSent": False,
        "containerState": "running",
        "image": "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-copaw:v1.2.0-beta.1",
    }:
        raise ValueError("control-plane.json: Manager observation must preserve the placeholder-model boundary")
    if control.get("human") != {"name": "proofmesh-approver", "phase": "Active"}:
        raise ValueError("control-plane.json: sanitized Human status is incomplete")

    evidence_workers = control.get("workers")
    if not isinstance(evidence_workers, list) or len(evidence_workers) != len(ROLE_SKILL_ENDPOINT):
        raise ValueError("control-plane.json: exactly seven Worker observations are required")
    by_name = {item.get("name"): item for item in evidence_workers if isinstance(item, dict)}
    if set(by_name) != set(ROLE_SKILL_ENDPOINT) or len(by_name) != len(evidence_workers):
        raise ValueError("control-plane.json: runtime Worker names must match the seven refund roles")
    for role, (skill, _stage, endpoint) in ROLE_SKILL_ENDPOINT.items():
        worker = by_name[role]
        expected = {
            "name": role,
            "phase": "Running",
            "runtime": "qwenpaw",
            "image": QWENPAW_IMAGE,
            "containerState": "running",
            "skill": skill,
            "assetsPresent": True,
            "teamHarness": {
                "health": {"ok": True, "plugin": "teamharness", "adapter": "qwenpaw"},
                "probeSurface": "qwenpaw-http-plugin-endpoint",
            },
            "mcpProjection": {
                "roleEndpoint": f"http://proofmesh:8000/mcp/roles/{endpoint}",
                "urlMatches": True,
                "transport": "http",
                "authorizationProjected": True,
            },
        }
        if worker != expected:
            raise ValueError(f"control-plane.json: Worker observation for {role} is incomplete or inconsistent")
    team = control.get("team")
    expected_roster = [
        {"name": "case-orchestrator", "role": "team_leader"},
        *[
            {"name": role, "role": "worker"}
            for role in (
                "ticket-intake", "context-investigator", "risk-policy-sentinel",
                "action-executor", "outcome-verifier", "case-memory-curator",
            )
        ],
    ]
    if not isinstance(team, dict) or {
        key: team.get(key)
        for key in (
            "name", "manifestValidated", "submissionAttempted", "resourceCreated",
            "phase", "leaderReady", "readyWorkers", "totalWorkers",
        )
    } != {
        "name": "proofmesh-refund-team",
        "manifestValidated": True,
        "submissionAttempted": True,
        "resourceCreated": True,
        "phase": "Active",
        "leaderReady": True,
        "readyWorkers": 6,
        "totalWorkers": 6,
    } or team.get("workerMembers") != expected_roster:
        raise ValueError("control-plane.json: Active Team observation or seven-member roster is incomplete")
    if not re.fullmatch(r"[0-9a-f-]{36}", str(team.get("uid") or "")):
        raise ValueError("control-plane.json: Team UID is missing or malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(team.get("roomIdSha256") or "")):
        raise ValueError("control-plane.json: Team room must be represented only by a SHA-256 digest")

    if findings.get("schemaVersion") != "proofmesh.io/agentteams-compatibility-findings/v1":
        raise ValueError("compatibility-findings.json: unsupported evidence schema")
    finding_items = findings.get("findings")
    if not isinstance(finding_items, list) or [item.get("id") for item in finding_items] != [
        "ATB-001", "ATB-002", "ATB-003", "ATB-004", "ATB-005", "ATB-006", "ATB-007"
    ]:
        raise ValueError("compatibility-findings.json: the audited beta finding set is incomplete")
    if [item.get("status") for item in finding_items] != [
        "mitigated", "mitigated", "mitigated", "mitigated", "mitigated", "mitigated", "open"
    ] or any(not isinstance(item.get("boundary"), str) or not item["boundary"] for item in finding_items):
        raise ValueError("compatibility-findings.json: finding status/boundary is incomplete")

    if direct.get("schemaVersion") != "proofmesh.io/teamharness-direct-evidence/v1":
        raise ValueError("teamharness-direct.json: unsupported evidence schema")
    if direct.get("testMode") != "direct-mcp-control-plane" or direct.get("executed") is not True:
        raise ValueError("teamharness-direct.json: executed direct lifecycle must remain explicitly true")
    if direct.get("teamResourceCreated") is not True:
        raise ValueError("teamharness-direct.json: Active Team prerequisite must remain explicit")
    if direct.get("directMcpHealthProbe") != {
        "verified": True,
        "workerCount": 7,
        "expectedResult": {"ok": True, "tool": "health", "role": None},
    }:
        raise ValueError("teamharness-direct.json: seven-Worker direct MCP health evidence is incomplete")
    expected_steps = [
        "create_project", "plan_dag", "delegate_task", "ack_task", "submit_task", "check_task", "accept_task"
    ]
    lifecycle = direct.get("lifecycle")
    if not isinstance(lifecycle, list) or [item.get("step") for item in lifecycle] != expected_steps:
        raise ValueError("teamharness-direct.json: direct MCP lifecycle step order is incomplete")
    if any(item.get("status") != "passed" or not item.get("actor") for item in lifecycle):
        raise ValueError("teamharness-direct.json: every direct MCP lifecycle step must be passed and attributed")
    by_step = {item["step"]: item for item in lifecycle}
    if by_step["delegate_task"].get("output", {}).get("synced") is not True:
        raise ValueError("teamharness-direct.json: Leader delegation was not synchronized")
    if by_step["ack_task"].get("output") != {"taskStatus": "in_progress", "pulled": True, "synced": True}:
        raise ValueError("teamharness-direct.json: Worker acknowledgement did not prove pull and push")
    if by_step["check_task"].get("output") != {"pulled": True, "effective": True, "validationErrorCount": 0}:
        raise ValueError("teamharness-direct.json: Leader check did not validate an effective pulled result")
    if by_step["accept_task"].get("output") != {"nodeStatus": "completed", "accepted": True}:
        raise ValueError("teamharness-direct.json: Leader acceptance did not complete the node")
    correlation = direct.get("correlation")
    if not isinstance(correlation, dict) or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(correlation.get(field) or ""))
        for field in ("resultSha256", "taskMetaSha256", "projectMetaSha256", "projectPlanSha256")
    ):
        raise ValueError("teamharness-direct.json: correlation artifact digests are incomplete")
    if direct.get("artifactPublication", {}).get("reasonCode") != "ATB-007" or direct["artifactPublication"].get("verified") is not False:
        raise ValueError("teamharness-direct.json: optional Matrix artifact publication gap is not explicit")
    if direct.get("modelDrivenCollaboration", {}).get("verified") is not False:
        raise ValueError("teamharness-direct.json: model-driven collaboration was not verified")

    readme = (RUNTIME_EVIDENCE / "README.md").read_text(encoding="utf-8")
    assert_sanitized_runtime_evidence(readme, "runtime-evidence/README.md")
    for required in ("去敏", "完整 direct MCP project/task 生命周期已运行", "model-driven", "runtime_verified: false"):
        if required not in readme:
            raise ValueError(f"runtime-evidence/README.md: missing boundary statement {required!r}")
    return 3


def _resource_shell(resource: dict[str, Any], kind: str, source: str) -> tuple[str, dict[str, Any]]:
    if set(resource) != {"apiVersion", "kind", "metadata", "spec"}:
        raise ValueError(f"{source}: resource must contain exactly apiVersion, kind, metadata and spec")
    if resource.get("apiVersion") != API_VERSION or resource.get("kind") != kind:
        raise ValueError(f"{source}: expected {API_VERSION} {kind}")
    metadata = resource.get("metadata")
    spec = resource.get("spec")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise ValueError(f"{source}: metadata.name is required")
    if not isinstance(spec, dict):
        raise ValueError(f"{source}: spec must be an object")
    unknown = set(spec) - OFFICIAL_SPEC_KEYS[kind]
    if unknown:
        raise ValueError(f"{source}: fields are not in the official {SCHEMA_RELEASE} {kind} CRD: {sorted(unknown)}")
    validate_no_file_uri(resource, source)
    return metadata["name"], spec


def _validate_schema_lock() -> dict[str, Any]:
    lock = _read_yaml_documents(AGENTTEAMS / "schema-lock.yaml")[0]
    if lock.get("schemaVersion") != "proofmesh.io/agentteams-schema-lock/v1":
        raise ValueError("schema-lock.yaml: unsupported lock schema")
    if lock.get("release") != SCHEMA_RELEASE or lock.get("commit") != SCHEMA_COMMIT:
        raise ValueError("schema-lock.yaml: release/commit must pin the audited AgentTeams beta commit")
    sources = lock.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"human", "worker", "team", "qwenpawE2E"}:
        raise ValueError("schema-lock.yaml: all official CRD and QwenPaw E2E sources are required")
    if any(SCHEMA_COMMIT not in str(url) for url in sources.values()):
        raise ValueError("schema-lock.yaml: every source URL must pin the audited commit")
    contract = lock.get("contract")
    if not isinstance(contract, dict) or contract.get("apiVersion") != API_VERSION:
        raise ValueError("schema-lock.yaml: missing official apiVersion contract")
    for kind in ("Human", "Worker", "Team"):
        if set(contract.get(kind, {}).get("allowedSpec", [])) != OFFICIAL_SPEC_KEYS[kind]:
            raise ValueError(f"schema-lock.yaml: {kind}.allowedSpec diverges from the audited CRD")
    if set(contract["Worker"].get("runtimes", [])) != {"openclaw", "copaw", "hermes", "qwenpaw"}:
        raise ValueError("schema-lock.yaml: qwenpaw and the complete official runtime enum are required")
    return lock


def _validate_human() -> dict[str, Any]:
    documents = _read_yaml_documents(AGENTTEAMS / "human.yaml")
    if len(documents) != 1:
        raise ValueError("human.yaml: exactly one Human is required")
    name, spec = _resource_shell(documents[0], "Human", "human.yaml")
    if name != "proofmesh-approver":
        raise ValueError("human.yaml: Human must be proofmesh-approver")
    for required in ("displayName", "permissionLevel"):
        if required not in spec:
            raise ValueError(f"human.yaml: spec.{required} is required by the official CRD")
    if spec["permissionLevel"] != 2:
        raise ValueError("human.yaml: proofmesh-approver must use team-scoped permissionLevel 2")
    if spec.get("accessibleTeams") != ["proofmesh-refund-team"]:
        raise ValueError("human.yaml: accessibleTeams must contain only proofmesh-refund-team")
    return documents[0]


def _validate_resources(resources: Any, role: str) -> None:
    if not isinstance(resources, dict) or set(resources) != {"requests", "limits"}:
        raise ValueError(f"{role}: resources must declare requests and limits")
    for section in ("requests", "limits"):
        if not isinstance(resources[section], dict) or set(resources[section]) != {"cpu", "memory"}:
            raise ValueError(f"{role}: resources.{section} must declare cpu and memory")
        if not all(isinstance(value, str) and value for value in resources[section].values()):
            raise ValueError(f"{role}: resource quantities must be non-empty strings")


def _validate_workers() -> list[dict[str, Any]]:
    documents = _read_yaml_documents(AGENTTEAMS / "workers.yaml")
    if len(documents) != len(ROLE_SKILL_ENDPOINT):
        raise ValueError("workers.yaml: exactly seven Worker resources are required")
    seen: set[str] = set()
    for resource in documents:
        role, spec = _resource_shell(resource, "Worker", "workers.yaml")
        if role not in ROLE_SKILL_ENDPOINT or role in seen:
            raise ValueError(f"workers.yaml: unexpected or duplicate Worker {role}")
        seen.add(role)
        _skill, stage, endpoint = ROLE_SKILL_ENDPOINT[role]
        if spec.get("model") != "qwen3.5-plus" or spec.get("runtime") != "qwenpaw":
            raise ValueError(f"{role}: model/runtime must be qwen3.5-plus/qwenpaw")
        if spec.get("image") != QWENPAW_IMAGE:
            raise ValueError(f"{role}: image must pin the locally built official QwenPaw beta image")
        if "package" in spec:
            raise ValueError(f"{role}: workers.yaml is a post-upload overlay and must not overwrite package")
        if "skills" in spec or "remoteSkills" in spec:
            raise ValueError(f"{role}: custom skill must be distributed in the Worker ZIP, not spec.skills")
        servers = spec.get("mcpServers")
        expected_server = {
            "name": "proofmesh",
            "url": f"http://proofmesh:8000/mcp/roles/{endpoint}",
            "transport": "http",
        }
        if servers != [expected_server]:
            raise ValueError(f"{role}: must declare only its role-scoped ProofMesh MCP URL")
        if spec.get("state") != "Running" or spec.get("deployMode") != "Local":
            raise ValueError(f"{role}: state/deployMode must be Running/Local")
        if spec.get("backendRuntime") != "pod" or spec.get("serviceEnabled") is not False:
            raise ValueError(f"{role}: backendRuntime/serviceEnabled must be pod/false")
        _validate_resources(spec.get("resources"), role)
        labels = spec.get("labels")
        if labels != {"proofmesh.io/role": endpoint, "proofmesh.io/workload": "refund"}:
            raise ValueError(f"{role}: runtime labels must match role and workload")
        metadata_labels = resource["metadata"].get("labels", {})
        if metadata_labels.get("proofmesh.io/stage") != stage:
            raise ValueError(f"{role}: metadata stage label must be {stage}")
    if seen != set(ROLE_SKILL_ENDPOINT):
        raise ValueError("workers.yaml: Worker set does not match refund role contract")
    return documents


def _validate_team(workers: list[dict[str, Any]], human: dict[str, Any]) -> dict[str, Any]:
    documents = _read_yaml_documents(AGENTTEAMS / "team.yaml")
    if len(documents) != 1:
        raise ValueError("team.yaml: exactly one Team is required")
    name, spec = _resource_shell(documents[0], "Team", "team.yaml")
    if name != "proofmesh-refund-team":
        raise ValueError("team.yaml: Team must be proofmesh-refund-team")
    if spec.get("admin") != {"name": human["metadata"]["name"]}:
        raise ValueError("team.yaml: admin must reference proofmesh-approver")
    if "leader" in spec or "workers" in spec:
        raise ValueError("team.yaml: use decoupled workerMembers, not deprecated inline leader/workers")
    members = spec.get("workerMembers")
    if not isinstance(members, list) or len(members) != 7:
        raise ValueError("team.yaml: workerMembers must contain seven Workers")
    names = [member.get("name") for member in members if isinstance(member, dict)]
    worker_names = [resource["metadata"]["name"] for resource in workers]
    if len(names) != 7 or set(names) != set(worker_names) or len(names) != len(set(names)):
        raise ValueError("team.yaml: every declared Worker must be referenced exactly once")
    if sum(member.get("role") == "team_leader" for member in members) != 1:
        raise ValueError("team.yaml: exactly one team_leader is required")
    if members[0] != {"name": "case-orchestrator", "role": "team_leader"}:
        raise ValueError("team.yaml: case-orchestrator must be the team_leader")
    if any(member.get("role") not in {"team_leader", "worker"} for member in members):
        raise ValueError("team.yaml: invalid member role")
    if spec.get("peerMentions") is not True or spec.get("heartbeatEvery") != "10m":
        raise ValueError("team.yaml: peerMentions/heartbeatEvery must be true/10m")
    return documents[0]


def _validate_dag() -> dict[str, Any]:
    dag = _read_yaml_documents(AGENTTEAMS / "refund-dag.yaml")[0]
    if dag.get("schemaVersion") != "proofmesh.io/refund-task-graph/v1" or dag.get("kind") != "RefundTaskGraph":
        raise ValueError("refund-dag.yaml: unsupported DAG contract")
    spec = dag.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("refund-dag.yaml: spec is required")
    expected_dependencies = {
        "create": [],
        "normalize": ["create"],
        "context": ["normalize"],
        "policy": ["context"],
        "execute": ["policy"],
        "verify": ["execute"],
        "memory": ["verify"],
    }
    expected_by_stage = {
        stage: (role, skill)
        for role, (skill, stage, _endpoint) in ROLE_SKILL_ENDPOINT.items()
    }
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or [node.get("id") for node in nodes] != list(expected_dependencies):
        raise ValueError("refund-dag.yaml: nodes must use the locked seven-stage order")
    for node in nodes:
        stage = node["id"]
        role, skill = expected_by_stage[stage]
        if stage == "create":
            expected = {
                "id": stage,
                "owner": role,
                "skill": skill,
                "dependsOn": [],
                "executionSurface": "projectflow",
                "actions": ["create_project", "plan_dag"],
                "materializesTask": False,
            }
        else:
            expected = {
                "id": stage,
                "owner": role,
                "skill": skill,
                "dependsOn": expected_dependencies[stage],
                "executionSurface": "taskflow",
                "materializesTask": True,
                "acceptedNodeState": "completed",
            }
        if node != expected:
            raise ValueError(f"refund-dag.yaml: node {stage} diverges from the locked contract")
    project = spec.get("project", {})
    if project.get("plannedTaskNodes") != ["normalize", "context", "policy", "execute", "verify", "memory"]:
        raise ValueError("refund-dag.yaml: only six Worker stages may be materialized as taskflow tasks")
    gate = spec.get("approvalGate", {})
    if gate.get("after") != "policy" or gate.get("before") != "execute":
        raise ValueError("refund-dag.yaml: approval gate must sit between policy and execute")
    if gate.get("authority") != "proofmesh-approver" or gate.get("when") != 'policy.decision == "REQUIRE_APPROVAL"':
        raise ValueError("refund-dag.yaml: approval must be external and policy triggered")
    mapping = spec.get("stateMapping", {})
    expected_project_task_states = {
        "planned", "assigned", "in_progress", "submitted", "completed", "revision", "blocked", "cancelled"
    }
    if set(mapping.get("projectTaskLifecycle", [])) != expected_project_task_states:
        raise ValueError("refund-dag.yaml: TeamHarness project-task state mapping is incomplete")
    if set(mapping.get("projectLifecycle", [])) != {"active", "paused", "completed"}:
        raise ValueError("refund-dag.yaml: TeamHarness project state mapping is incomplete")
    if set(mapping.get("taskMetaLifecycle", [])) != {"assigned", "in_progress", "submitted", "cancelled"}:
        raise ValueError("refund-dag.yaml: TeamHarness TaskMeta state mapping is incomplete")
    if set(mapping.get("taskResultLifecycle", [])) != {
        "SUCCESS", "SUCCESS_WITH_NOTES", "REVISION_NEEDED", "BLOCKED", "FAILED", "PARTIAL"
    }:
        raise ValueError("refund-dag.yaml: QwenPaw TeamHarness result status mapping is incomplete")
    business = mapping.get("businessToTeamHarness", {})
    if set(business) != {
        "RECEIVED", "NORMALIZED", "CONTEXT_READY", "WAITING_APPROVAL", "AUTHORIZED",
        "EXECUTED", "VERIFIED", "COMPLETED", "COMPENSATED", "BLOCKED",
    }:
        raise ValueError("refund-dag.yaml: ProofMesh business state mapping is incomplete")
    waiting = business["WAITING_APPROVAL"]
    if waiting != {"project": "paused", "node": "execute", "nodeState": "planned", "taskMetaState": "absent"}:
        raise ValueError("refund-dag.yaml: WAITING_APPROVAL must pause before execute is delegated")
    on_enter = gate.get("onEnter", {})
    if on_enter.get("executePlanNodeState") != "planned" or on_enter.get("executeTaskMetaState") != "absent":
        raise ValueError("refund-dag.yaml: approval pause must not misuse TeamHarness blocked state")
    return dag


def _skill_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"{path}: SKILL.md requires YAML frontmatter")
    frontmatter = yaml.safe_load(match.group(1))
    if not isinstance(frontmatter, dict) or set(frontmatter) != {"name", "description"}:
        raise ValueError(f"{path}: frontmatter must contain only name and description")
    if not isinstance(frontmatter["description"], str) or len(frontmatter["description"]) < 80:
        raise ValueError(f"{path}: description must explain capability and trigger")
    return frontmatter


def _validate_json_schema(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON Schema: {exc}") from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError(f"{path}: must use JSON Schema Draft 2020-12")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError(f"{path}: root must be a closed object")
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not required or not isinstance(properties, dict):
        raise ValueError(f"{path}: required and properties must be non-empty")
    if not set(required).issubset(properties):
        raise ValueError(f"{path}: every required field must have a property schema")
    return schema


def _validate_skill(role: str, skill: str) -> None:
    root = ROOT / "skills" / skill
    frontmatter = _skill_frontmatter(root / "SKILL.md")
    if frontmatter["name"] != skill:
        raise ValueError(f"{skill}: directory and frontmatter names differ")
    references = root / "references"
    expected_files = {"contract.yaml", "input.schema.json", "output.schema.json", "error-codes.md"}
    actual_files = {path.name for path in references.iterdir() if path.is_file()} if references.is_dir() else set()
    if actual_files != expected_files:
        raise ValueError(f"{skill}: references must be exactly {sorted(expected_files)}")
    input_schema = _validate_json_schema(references / "input.schema.json")
    output_schema = _validate_json_schema(references / "output.schema.json")
    protocol = ROLE_PROTOCOL[role]
    input_fields = set(input_schema["properties"])
    if input_fields != protocol["input_fields"] or set(input_schema["required"]) != input_fields:
        raise ValueError(f"{skill}: closed input schema diverges from the implemented role MCP tool")
    output_fields = set(output_schema["properties"])
    if output_fields != SUMMARY_FIELDS or set(output_schema["required"]) != SUMMARY_FIELDS:
        raise ValueError(f"{skill}: output schema must cover the complete RefundWorkflowSummary")
    status_schema = output_schema["properties"].get("status", {})
    if "const" in status_schema:
        output_statuses = {status_schema["const"]}
    else:
        output_statuses = set(status_schema.get("enum", []))
    if output_statuses != protocol["output_statuses"]:
        raise ValueError(f"{skill}: output statuses diverge from the implemented workflow transition")
    if output_schema["properties"].get("last_step") != {"const": protocol["last_step"]}:
        raise ValueError(f"{skill}: output last_step diverges from the implemented workflow transition")
    contract = _read_yaml_documents(references / "contract.yaml")[0]
    required_contract = {
        "schema_version", "skill", "invocation", "inputs", "outputs", "dependencies",
        "execution", "failure_policy", "security", "verification", "reuse",
    }
    if set(contract) != required_contract:
        raise ValueError(f"{skill}: contract fields must be exactly {sorted(required_contract)}")
    identity = contract.get("skill", {})
    if identity.get("name") != skill or identity.get("role") != role or identity.get("version") != "1.0.0":
        raise ValueError(f"{skill}: contract identity/version/role mismatch")
    if contract.get("inputs", {}).get("schema") != "references/input.schema.json":
        raise ValueError(f"{skill}: input schema reference mismatch")
    if contract.get("outputs", {}).get("schema") != "references/output.schema.json":
        raise ValueError(f"{skill}: output schema reference mismatch")
    if contract.get("dependencies", {}).get("mcp_tools") != protocol["tools"]:
        raise ValueError(f"{skill}: role MCP tool allowlist diverges from the implemented endpoint")
    error_text = (references / "error-codes.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(PM-[A-Z]+-[0-9]{3})`", error_text))
    failure = contract.get("failure_policy", {})
    retry_codes = set(failure.get("retry", {}).get("only", []))
    terminal_codes = set(failure.get("terminal_errors", []))
    if not documented or documented != retry_codes | terminal_codes:
        raise ValueError(f"{skill}: error-code document and failure policy differ")
    wire_codes = set(re.findall(r"`(-[0-9]{5})`", error_text))
    if wire_codes != ROLE_MCP_WIRE_CODES or 'error.data["proofmesh/reason"]' not in error_text:
        raise ValueError(f"{skill}: complete role MCP JSON-RPC error mapping is required")
    if not isinstance(contract.get("security", {}).get("allowed"), list) or not contract["security"].get("denied"):
        raise ValueError(f"{skill}: security allow/deny contract is required")
    if not contract.get("verification", {}).get("checks") or not contract.get("verification", {}).get("evidence"):
        raise ValueError(f"{skill}: verification checks/evidence are required")
    combined_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [root / "SKILL.md", *sorted(references.iterdir())]
        if path.is_file()
    )
    obsolete_states = {"GATHERING_CONTEXT", "EXECUTING"} & set(re.findall(r"\b[A-Z][A-Z_]+\b", combined_text))
    if obsolete_states:
        raise ValueError(f"{skill}: obsolete workflow states are forbidden: {sorted(obsolete_states)}")


def _expected_archive_files(role: str, skill: str) -> set[str]:
    package_root = AGENTTEAMS / "packages" / role
    files = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
    }
    skill_root = ROOT / "skills" / skill
    files.update(
        f"skills/{skill}/{path.relative_to(skill_root).as_posix()}"
        for path in skill_root.rglob("*")
        if path.is_file()
    )
    return files


def validate_zip_package(path: Path, role: str, skill: str) -> str:
    try:
        with ZipFile(path) as archive:
            members = archive.namelist()
            member_set = set(members)
            if len(members) != len(member_set):
                raise ValueError(f"{path}: duplicate ZIP members are forbidden")
            for member in members:
                pure = PurePosixPath(member)
                if pure.is_absolute() or ".." in pure.parts or "\\" in member:
                    raise ValueError(f"{path}: unsafe ZIP member {member!r}")
            expected = _expected_archive_files(role, skill)
            if member_set != expected:
                raise ValueError(
                    f"{path}: package root/layout mismatch; "
                    f"missing={sorted(expected - member_set)}, extra={sorted(member_set - expected)}"
                )
            if {"AGENTS.md", "SOUL.md"} & member_set:
                raise ValueError(f"{path}: config documents must be under config/")
            manifest = json.loads(archive.read("manifest.json"))
    except (OSError, BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid Worker package: {exc}") from exc
    worker = manifest.get("worker", {})
    if manifest.get("type") != "worker" or manifest.get("version") != 1:
        raise ValueError(f"{path}: invalid Worker import manifest")
    if worker.get("suggested_name") != role or worker.get("model") != "qwen3.5-plus" or worker.get("runtime") != "qwenpaw":
        raise ValueError(f"{path}: Worker manifest identity/model/runtime mismatch")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_packages() -> int:
    source_roles = {
        path.name
        for path in (AGENTTEAMS / "packages").iterdir()
        if path.is_dir() and any(item.is_file() for item in path.rglob("*"))
    }
    if source_roles != set(ROLE_SKILL_ENDPOINT):
        raise ValueError("agentteams/packages: source role directories must match the seven refund roles")
    skill_dirs = {
        path.name
        for path in (ROOT / "skills").iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    expected_skills = {item[0] for item in ROLE_SKILL_ENDPOINT.values()}
    if skill_dirs != expected_skills:
        raise ValueError("skills/: active Skill directories must match the seven refund contracts")
    for role, (skill, _stage, _endpoint) in ROLE_SKILL_ENDPOINT.items():
        _validate_skill(role, skill)

    dist = AGENTTEAMS / "dist"
    zips = {path.name for path in dist.glob("*.zip")}
    expected_zips = {f"{role}.zip" for role in ROLE_SKILL_ENDPOINT}
    if zips != expected_zips:
        raise ValueError(f"agentteams/dist: expected only refund ZIPs; missing={sorted(expected_zips-zips)}, extra={sorted(zips-expected_zips)}")
    checksum_path = dist / "SHA256SUMS"
    if not checksum_path.is_file():
        raise ValueError("agentteams/dist/SHA256SUMS is required")
    checksum_map: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([a-z0-9-]+\.zip)", line)
        if not match or match.group(2) in checksum_map:
            raise ValueError("agentteams/dist/SHA256SUMS has an invalid or duplicate line")
        checksum_map[match.group(2)] = match.group(1)
    if set(checksum_map) != expected_zips:
        raise ValueError("agentteams/dist/SHA256SUMS does not cover exactly the seven packages")
    for role, (skill, _stage, _endpoint) in ROLE_SKILL_ENDPOINT.items():
        name = f"{role}.zip"
        digest = validate_zip_package(dist / name, role, skill)
        if checksum_map[name] != digest:
            raise ValueError(f"{name}: checksum mismatch")
    return len(zips)


def validate() -> dict[str, int | bool | str]:
    _validate_schema_lock()
    human = _validate_human()
    workers = _validate_workers()
    _validate_team(workers, human)
    dag = _validate_dag()
    packages = _validate_packages()
    _validate_runtime_evidence()
    return {
        "schema_release": SCHEMA_RELEASE,
        "humans": 1,
        "workers": len(workers),
        "teams": 1,
        "dag_nodes": len(dag["spec"]["nodes"]),
        "skills": len(ROLE_SKILL_ENDPOINT),
        "packages": packages,
        "runtime_verified": False,
    }


def main() -> None:
    result = validate()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(
        "STATIC VALIDATION ONLY: manifests, packages, and sanitized evidence were checked; "
        "this validator did not query live AgentTeams/TeamHarness."
    )


if __name__ == "__main__":
    main()
