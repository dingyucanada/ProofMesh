from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .admission import handle_admission_review
from .auth import AuthenticationError, AuthenticationUnavailable, Principal, build_authenticator_from_environment
from .capabilities import canonical_json
from .runtime import ProofMeshRuntime, build_runtime
from .workflow import RefundWorkflowRequest, WorkerRoleError, WorkflowConflict
from .workflow_verifier import verify_workflow_proof


PACKAGE_HOME = Path(__file__).resolve().parents[2]
HOME = Path(os.getenv("PROOFMESH_HOME", PACKAGE_HOME))
runtime: ProofMeshRuntime = build_runtime(HOME)
authenticator = build_authenticator_from_environment()
app = FastAPI(
    title="ProofMesh Agent Action Control Plane",
    version="1.0.0",
    description="Role-separated AgentTeams tasks, execution-time MCP authorization, and independently verifiable outcomes.",
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class WorkflowStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticket_id: str = Field(pattern=r"^[A-Z0-9-]{3,64}$")
    tenant_id: str = Field(pattern=r"^[a-z0-9-]{2,64}$")


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=0)
    task_id: StrictStr | None = Field(default=None, min_length=3, max_length=256)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=0)
    reason: str = Field(min_length=12, max_length=1000)
    approval_assertion: StrictStr = Field(min_length=100, max_length=32768)
    task_id: str | None = Field(default=None, min_length=3, max_length=256)


class McpGetStateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workflow_id: StrictStr = Field(pattern=r"^wf-[a-f0-9]{32}$")


class McpStepArguments(McpGetStateArguments):
    expected_revision: StrictInt = Field(ge=0)
    task_id: StrictStr = Field(min_length=3, max_length=256)


def _principal(authorization: str | None, role: str) -> Principal:
    try:
        return authenticator.authenticate(authorization, required_role=role)
    except AuthenticationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def _require_tenant(principal: Principal, tenant_id: str) -> None:
    if not principal.allows_tenant(tenant_id):
        raise HTTPException(status_code=403, detail="principal is not authorized for this tenant")


def _load_for_principal(workflow_id: str, principal: Principal):
    try:
        summary = runtime.control_plane.get(workflow_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workflow not found") from exc
    _require_tenant(principal, summary.tenant_id)
    return summary


def _step_call(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None,
    *,
    required_role: str,
    method: Callable[..., Any],
):
    principal = _principal(authorization, required_role)
    _load_for_principal(workflow_id, principal)
    try:
        return method(
            workflow_id,
            expected_revision=request.expected_revision,
            actor=principal.subject,
            role=required_role,
            task_id=request.task_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workflow not found") from exc
    except (WorkflowConflict, WorkerRoleError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static/index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "data_plane": "mcp-action-gateway",
        "workflow_protocol": "proofmesh-role-tasks/v1",
        "trust_anchor": "external",
    }


@app.get("/ready")
def ready():
    required_roles = {
        "orchestrator",
        "approver",
        "auditor",
        "intake",
        "investigator",
        "policy",
        "executor",
        "verifier",
        "memory",
        "gateway",
    }
    missing = sorted(role for role in required_roles if not authenticator.has_role(role))
    if missing:
        raise HTTPException(status_code=503, detail={"missing_authenticated_roles": missing})
    return {
        "status": "ready",
        "auth": authenticator.mode,
        "policy_digest": runtime.control_plane.policy_digest,
        "trust_bundle": str(runtime.trust_bundle_path.relative_to(runtime.home)),
    }


@app.post("/api/v1/refund-workflows")
def create_refund_workflow(
    request: WorkflowStartRequest,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization, "orchestrator")
    _require_tenant(principal, request.tenant_id)
    return runtime.control_plane.create_case(
        RefundWorkflowRequest(
            ticket_id=request.ticket_id,
            tenant_id=request.tenant_id,
            requester=principal.subject,
        ),
        actor=principal.subject,
        role="orchestrator",
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/normalize")
def normalize_refund_workflow(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="intake",
        method=runtime.control_plane.normalize_case,
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/context")
def gather_refund_context(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="investigator",
        method=runtime.control_plane.gather_context,
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/policy")
def evaluate_refund_policy(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="policy",
        method=runtime.control_plane.evaluate_policy,
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/approve")
def approve_refund_workflow(
    workflow_id: str,
    request: ApprovalRequest,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization, "approver")
    _load_for_principal(workflow_id, principal)
    try:
        return runtime.control_plane.approve(
            workflow_id,
            expected_revision=request.expected_revision,
            approver=principal.subject,
            reason=request.reason,
            approval_assertion=request.approval_assertion,
            task_id=request.task_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workflow not found") from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/refund-workflows/{workflow_id}/approval-challenge")
def get_approval_challenge(workflow_id: str, authorization: str | None = Header(default=None)):
    principal = _principal(authorization, "approver")
    _load_for_principal(workflow_id, principal)
    try:
        return runtime.control_plane.approval_challenge(workflow_id)
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/execute")
def execute_refund_workflow(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="executor",
        method=runtime.control_plane.execute_authorized,
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/verify")
def verify_refund_outcome(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="verifier",
        method=runtime.control_plane.verify_outcome,
    )


@app.post("/api/v1/refund-workflows/{workflow_id}/steps/memory")
def curate_refund_memory(
    workflow_id: str,
    request: StepRequest,
    authorization: str | None = Header(default=None),
):
    return _step_call(
        workflow_id,
        request,
        authorization,
        required_role="memory",
        method=runtime.control_plane.curate_memory,
    )


@app.get("/api/v1/refund-workflows/{workflow_id}")
def get_refund_workflow(workflow_id: str, authorization: str | None = Header(default=None)):
    principal = _principal(authorization, "read")
    return _load_for_principal(workflow_id, principal)


@app.get("/api/v1/refund-workflows/{workflow_id}/events")
def get_refund_events(workflow_id: str, authorization: str | None = Header(default=None)):
    principal = _principal(authorization, "read")
    _load_for_principal(workflow_id, principal)
    events = runtime.control_plane.events(workflow_id)
    valid, head = runtime.control_plane.ledger.verify(workflow_id)
    return {"events": events, "chain": {"valid": valid, "head": head}}


@app.get("/api/v1/refund-workflows/{workflow_id}/verify")
def verify_refund_proof(workflow_id: str, authorization: str | None = Header(default=None)):
    principal = _principal(authorization, "read")
    summary = _load_for_principal(workflow_id, principal)
    try:
        if not summary.proof_bundle:
            raise WorkflowConflict("workflow proof is not sealed yet")
        return verify_workflow_proof(
            runtime.home / summary.proof_bundle,
            trust_bundle_path=runtime.trust_bundle_path,
            pinned_policy_path=runtime.home / "data/policies/refund_policy.json",
        ).as_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workflow proof not found") from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


ROLE_TOOLS: dict[str, list[dict[str, Any]]] = {
    "orchestrator": [
        {
            "name": "create_case",
            "description": "Create a refund case without executing downstream tasks.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ticket_id", "tenant_id"],
                "properties": {
                    "ticket_id": {"type": "string", "pattern": "^[A-Z0-9-]{3,64}$"},
                    "tenant_id": {"type": "string", "pattern": "^[a-z0-9-]{2,64}$"},
                },
            },
        },
        {
            "name": "get_state",
            "description": "Read the current CAS revision and next permitted step.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["workflow_id"],
                "properties": {"workflow_id": {"type": "string", "pattern": "^wf-[a-f0-9]{32}$"}},
            },
        },
    ],
    "intake": [],
    "investigator": [],
    "policy": [],
    "executor": [],
    "verifier": [],
    "memory": [],
}
for role_name, tool_name in {
    "intake": "normalize_case",
    "investigator": "gather_context",
    "policy": "evaluate_policy",
    "executor": "execute_authorized",
    "verifier": "verify_outcome",
    "memory": "curate_memory",
}.items():
    ROLE_TOOLS[role_name] = [
        {
            "name": tool_name,
            "description": f"Run the {role_name} step with optimistic concurrency control.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["workflow_id", "expected_revision", "task_id"],
                "properties": {
                    "workflow_id": {"type": "string", "pattern": "^wf-[a-f0-9]{32}$"},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "task_id": {"type": "string", "minLength": 3, "maxLength": 256},
                },
            },
        }
    ]


def _rpc_error(request_id: Any, code: int, message: str, reason: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"proofmesh/reason": reason}},
    }


@app.post("/mcp/roles/{role}")
def role_mcp_gateway(
    role: str,
    request: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    if role not in ROLE_TOOLS:
        raise HTTPException(status_code=404, detail="unknown worker role")
    principal = _principal(authorization, role)
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _rpc_error(request_id, -32600, "Invalid Request", "invalid_jsonrpc")
    method = request.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"ProofMesh {role} task gateway", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": ROLE_TOOLS[role]}}
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found", "method_not_supported")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params", "params_must_be_object")
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        return _rpc_error(request_id, -32602, "Invalid params", "tool_name_must_be_string")
    arguments = params.get("arguments", {})
    allowed_names = {tool["name"] for tool in ROLE_TOOLS[role]}
    if tool_name not in allowed_names:
        return _rpc_error(request_id, -32003, "Action denied", "role_tool_not_allowed")
    try:
        if role == "orchestrator" and tool_name == "create_case":
            parsed_create = WorkflowStartRequest.model_validate(arguments)
            _require_tenant(principal, parsed_create.tenant_id)
            result = runtime.control_plane.create_case(
                RefundWorkflowRequest(
                    ticket_id=parsed_create.ticket_id,
                    tenant_id=parsed_create.tenant_id,
                    requester=principal.subject,
                ),
                actor=principal.subject,
                role=role,
            )
        elif role == "orchestrator" and tool_name == "get_state":
            parsed_state = McpGetStateArguments.model_validate(arguments)
            _load_for_principal(parsed_state.workflow_id, principal)
            result = runtime.control_plane.get_state(parsed_state.workflow_id)
        else:
            parsed_step = McpStepArguments.model_validate(arguments)
            workflow_id = parsed_step.workflow_id
            _load_for_principal(workflow_id, principal)
            method_by_role = {
                "intake": runtime.control_plane.normalize_case,
                "investigator": runtime.control_plane.gather_context,
                "policy": runtime.control_plane.evaluate_policy,
                "executor": runtime.control_plane.execute_authorized,
                "verifier": runtime.control_plane.verify_outcome,
                "memory": runtime.control_plane.curate_memory,
            }
            result = method_by_role[role](
                workflow_id,
                expected_revision=parsed_step.expected_revision,
                actor=principal.subject,
                role=role,
                task_id=parsed_step.task_id,
            )
        structured = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": canonical_json(structured)}],
                "structuredContent": structured,
                "isError": False,
            },
        }
    except ValidationError:
        return _rpc_error(request_id, -32602, "Invalid params", "arguments_schema_invalid")
    except HTTPException as exc:
        return _rpc_error(request_id, -32003, "Action denied", str(exc.detail))
    except (WorkflowConflict, WorkerRoleError, ValueError) as exc:
        return _rpc_error(request_id, -32009, "Workflow conflict", str(exc))


@app.post("/mcp")
def internal_mcp_gateway(
    request: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    """Internal data-plane endpoint. Only the trusted proxy may supply Action Passports."""

    _principal(authorization, "gateway")
    return runtime.gateway.handle_jsonrpc(request)


@app.post("/admission/validate")
def kubernetes_admission(review: dict[str, Any]):
    if review.get("apiVersion") != "admission.k8s.io/v1" or review.get("kind") != "AdmissionReview":
        raise HTTPException(status_code=400, detail="expected admission.k8s.io/v1 AdmissionReview")
    return handle_admission_review(review)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return runtime.gateway.prometheus_metrics()
