from pathlib import Path
import json

from fastapi.testclient import TestClient

from proofmesh import api
from proofmesh.auth import Principal, TokenAuthenticator
from approval_helpers import issue_approval
from proofmesh.workflow import RefundWorkflowRequest


def configure_api(monkeypatch, action_runtime):
    monkeypatch.setattr(api, "runtime", action_runtime)
    monkeypatch.setattr(
        api,
        "authenticator",
        TokenAuthenticator(
            [
                ("operator-secret", Principal("operator-a", frozenset({"operator", "read"}))),
                (
                    "orchestrator-secret",
                    Principal("orchestrator-a", frozenset({"orchestrator", "read"})),
                ),
                ("approver-secret", Principal("approver-b", frozenset({"approver", "read"}))),
                ("intake-secret", Principal("intake-a", frozenset({"intake", "read"}))),
                (
                    "investigator-secret",
                    Principal("investigator-a", frozenset({"investigator", "read"})),
                ),
                ("policy-secret", Principal("policy-a", frozenset({"policy", "read"}))),
                ("executor-secret", Principal("executor-a", frozenset({"executor", "read"}))),
                ("verifier-secret", Principal("verifier-a", frozenset({"verifier", "read"}))),
                ("memory-secret", Principal("memory-a", frozenset({"memory", "read"}))),
                ("auditor-secret", Principal("auditor-a", frozenset({"auditor", "read"}))),
                ("gateway-secret", Principal("gateway-a", frozenset({"gateway"}))),
            ]
        ),
    )
    return TestClient(api.app)


def test_workflow_creation_requires_orchestrator_identity(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    denied = client.post("/api/v1/refund-workflows", json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"})
    assert denied.status_code == 401
    wrong_role = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer operator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"},
    )
    assert wrong_role.status_code == 401
    allowed = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["requester"] == "orchestrator-a"
    assert allowed.json()["status"] == "RECEIVED"
    assert allowed.json()["next_step"] == "normalize_case"


def test_api_rejects_identity_spoofing_and_inline_approval(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    response = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={
            "ticket_id": "TKT-HIGH-001",
            "tenant_id": "acme-cn",
            "requester": "spoofed-admin",
            "approve": True,
        },
    )
    assert response.status_code == 422


def test_internal_mcp_endpoint_requires_trusted_gateway_identity(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "crm.get_ticket", "arguments": {"ticket_id": "TKT-LOW-001"}},
        },
    )
    assert response.status_code == 401
    authenticated = client.post(
        "/mcp",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
    )
    assert authenticated.status_code == 200
    assert any(tool["name"] == "payments.issue_refund" for tool in authenticated.json()["result"]["tools"])


def test_role_mcp_lists_only_role_tools_and_denies_privilege_escalation(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    listed = client.post(
        "/mcp/roles/intake",
        headers={"Authorization": "Bearer intake-secret"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["normalize_case"]
    escalated = client.post(
        "/mcp/roles/intake",
        headers={"Authorization": "Bearer intake-secret"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "execute_authorized", "arguments": {}},
        },
    )
    assert escalated.json()["error"]["data"]["proofmesh/reason"] == "role_tool_not_allowed"
    wrong_endpoint = client.post(
        "/mcp/roles/executor",
        headers={"Authorization": "Bearer intake-secret"},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert wrong_endpoint.status_code == 401


def test_role_mcp_enforces_closed_strict_argument_schemas(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    created = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"},
    ).json()

    for arguments in (
        {
            "workflow_id": created["workflow_id"],
            "expected_revision": "0",
            "task_id": "task-intake",
        },
        {
            "workflow_id": created["workflow_id"],
            "expected_revision": 0,
            "task_id": None,
        },
        [created["workflow_id"], 0, "task-intake"],
    ):
        invalid = client.post(
            "/mcp/roles/intake",
            headers={"Authorization": "Bearer intake-secret"},
            json={
                "jsonrpc": "2.0",
                "id": "strict-input",
                "method": "tools/call",
                "params": {"name": "normalize_case", "arguments": arguments},
            },
        ).json()
        assert invalid["error"]["code"] == -32602
        assert invalid["error"]["data"]["proofmesh/reason"] == "arguments_schema_invalid"

    extra_create = client.post(
        "/mcp/roles/orchestrator",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={
            "jsonrpc": "2.0",
            "id": "extra-create",
            "method": "tools/call",
            "params": {
                "name": "create_case",
                "arguments": {"ticket_id": "TKT-HIGH-001", "tenant_id": "acme-cn", "approve": True},
            },
        },
    ).json()
    assert extra_create["error"]["code"] == -32602

    valid = client.post(
        "/mcp/roles/intake",
        headers={"Authorization": "Bearer intake-secret"},
        json={
            "jsonrpc": "2.0",
            "id": "valid-input",
            "method": "tools/call",
            "params": {
                "name": "normalize_case",
                "arguments": {
                    "workflow_id": created["workflow_id"],
                    "expected_revision": 0,
                    "task_id": "task-intake",
                },
            },
        },
    ).json()
    assert json.loads(valid["result"]["content"][0]["text"]) == valid["result"]["structuredContent"]


def test_api_steps_bind_authenticated_worker_and_revision(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    created = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"},
    ).json()
    denied = client.post(
        f"/api/v1/refund-workflows/{created['workflow_id']}/steps/normalize",
        headers={"Authorization": "Bearer executor-secret"},
        json={"expected_revision": created["revision"], "task_id": "task-intake"},
    )
    assert denied.status_code == 401
    normalized = client.post(
        f"/api/v1/refund-workflows/{created['workflow_id']}/steps/normalize",
        headers={"Authorization": "Bearer intake-secret"},
        json={"expected_revision": created["revision"], "task_id": "task-intake"},
    )
    assert normalized.status_code == 200
    stale = client.post(
        f"/api/v1/refund-workflows/{created['workflow_id']}/steps/context",
        headers={"Authorization": "Bearer investigator-secret"},
        json={"expected_revision": created["revision"], "task_id": "task-context"},
    )
    assert stale.status_code == 409


def test_approval_api_requires_external_assertion_and_matches_bearer_subject(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    waiting = action_runtime.control_plane.run_until_gate_or_terminal(
        RefundWorkflowRequest(ticket_id="TKT-HIGH-001", tenant_id="acme-cn", requester="orchestrator-a")
    )
    challenge = client.get(
        f"/api/v1/refund-workflows/{waiting.workflow_id}/approval-challenge",
        headers={"Authorization": "Bearer approver-secret"},
    )
    assert challenge.status_code == 200
    reason = "Reviewed the exact frozen challenge through the external approval service"
    assertion = issue_approval(action_runtime, waiting, subject="approver-b", reason=reason)
    missing = client.post(
        f"/api/v1/refund-workflows/{waiting.workflow_id}/approve",
        headers={"Authorization": "Bearer approver-secret"},
        json={"expected_revision": waiting.revision, "reason": reason},
    )
    assert missing.status_code == 422
    accepted = client.post(
        f"/api/v1/refund-workflows/{waiting.workflow_id}/approve",
        headers={"Authorization": "Bearer approver-secret"},
        json={
            "expected_revision": waiting.revision,
            "reason": reason,
            "approval_assertion": assertion,
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "AUTHORIZED"


def test_tenant_is_derived_from_principal_scope_not_arbitrary_request(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    denied = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "other-tenant"},
    )
    assert denied.status_code == 403

    created = client.post(
        "/api/v1/refund-workflows",
        headers={"Authorization": "Bearer orchestrator-secret"},
        json={"ticket_id": "TKT-LOW-001", "tenant_id": "acme-cn"},
    ).json()
    monkeypatch.setattr(
        api,
        "authenticator",
        TokenAuthenticator(
            [
                (
                    "other-auditor",
                    Principal("auditor-other", frozenset({"read"}), frozenset({"other-tenant"})),
                )
            ]
        ),
    )
    cross_tenant = client.get(
        f"/api/v1/refund-workflows/{created['workflow_id']}",
        headers={"Authorization": "Bearer other-auditor"},
    )
    assert cross_tenant.status_code == 403


def test_readiness_requires_every_security_boundary_identity(monkeypatch, action_runtime):
    client = configure_api(monkeypatch, action_runtime)
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["auth"] == "bearer-rbac-tenant-scoped"

    monkeypatch.setattr(
        api,
        "authenticator",
        TokenAuthenticator(
            [("orchestrator-secret", Principal("orchestrator-a", frozenset({"orchestrator", "read"})))]
        ),
    )
    unavailable = client.get("/ready")
    assert unavailable.status_code == 503
    assert "gateway" in unavailable.json()["detail"]["missing_authenticated_roles"]


def test_operator_console_targets_the_role_separated_api_contract():
    static_dir = Path(__file__).resolve().parents[1] / "src/proofmesh/static"
    app_source = (static_dir / "app.js").read_text(encoding="utf-8")
    page_source = (static_dir / "index.html").read_text(encoding="utf-8")

    assert "/api/v1/refund-workflows" in app_source
    for endpoint in ("normalize", "context", "policy", "execute", "verify", "memory"):
        assert endpoint in app_source
    assert "expected_revision" in app_source
    assert "approval_assertion" in app_source
    assert "approvalAssertion" in page_source
    assert "sessionStorage" in app_source
    assert '["auditor", "Independent Auditor"]' in app_source
    assert 'authHeaders("auditor", false)' in app_source
    assert "/api/runs" not in app_source
    assert "/api/cases" not in app_source
    assert "mock-canary" not in app_source + page_source
    assert "源码不内置任何通行令牌" in page_source
