from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
ACTIVE_POLICY_DIGESTS_ENV = "PROOFMESH_ACTIVE_POLICY_DIGESTS"


@dataclass
class AdmissionDecision:
    allowed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _pod_template(obj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    kind = obj.get("kind")
    if kind == "Pod":
        return obj.get("metadata", {}), obj.get("spec", {})
    spec = obj.get("spec", {})
    if kind == "CronJob":
        template = spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
    else:
        template = spec.get("template", {})
    return template.get("metadata", {}), template.get("spec", {})


def _configured_policy_digests() -> set[str]:
    return {
        item.strip()
        for item in os.getenv(ACTIVE_POLICY_DIGESTS_ENV, "").split(",")
        if item.strip()
    }


def _container_groups(pod_spec: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for field_name, display_name in (
        ("containers", "container"),
        ("initContainers", "initContainer"),
        ("ephemeralContainers", "ephemeralContainer"),
    ):
        values = pod_spec.get(field_name, []) or []
        if not isinstance(values, list):
            yield display_name, {"name": "invalid-container-list", "_proofmesh_invalid_container": True}
            continue
        for value in values:
            if isinstance(value, dict):
                yield display_name, value
            else:
                yield display_name, {"name": "invalid-container", "_proofmesh_invalid_container": True}


def _has_pinned_tag_or_digest(image: str) -> bool:
    if IMAGE_DIGEST_RE.fullmatch(image):
        return True
    final_component = image.rsplit("/", 1)[-1]
    return ":" in final_component and not final_component.endswith(":latest")


def _validate_required_environment(
    container_kind: str,
    name: str,
    container: dict[str, Any],
    violations: list[str],
) -> None:
    env_items = container.get("env", []) or []
    env = {
        item.get("name"): item
        for item in env_items
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for variable in ("PROOFMESH_GATEWAY_URL", "PROOFMESH_TRUST_BUNDLE", "PROOFMESH_FAIL_CLOSED"):
        item = env.get(variable)
        if item is None:
            violations.append(f"{container_kind} {name} is missing {variable}")
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            violations.append(f"{container_kind} {name} must set a non-empty literal {variable}")
            continue
        if variable == "PROOFMESH_FAIL_CLOSED" and value.strip().lower() != "true":
            violations.append(f"{container_kind} {name} must set {variable}=true")


def validate_agent_workload(
    obj: dict[str, Any],
    *,
    force_agent_policy: bool = False,
    active_policy_digests: set[str] | None = None,
    require_image_digest: bool = False,
) -> AdmissionDecision:
    metadata, pod_spec = _pod_template(obj)
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    grouped_containers = list(_container_groups(pod_spec))
    env_names = {
        item.get("name")
        for _, container in grouped_containers
        for item in (container.get("env", []) or [])
        if isinstance(item, dict)
    }
    is_agent = (
        force_agent_policy
        or annotations.get("proofmesh.io/agent-workload") == "true"
        or labels.get("agentteams.io/worker") is not None
        or "AGENTTEAMS_WORKER_NAME" in env_names
    )
    if not is_agent:
        return AdmissionDecision(allowed=True, warnings=["non-agent workload: ProofMesh policy not applied"])

    violations: list[str] = []
    if labels.get("proofmesh.io/enforced") != "true":
        violations.append("missing label proofmesh.io/enforced=true")

    configured_digests = (
        _configured_policy_digests() if active_policy_digests is None else set(active_policy_digests)
    )
    invalid_configured_digests = sorted(item for item in configured_digests if not DIGEST_RE.fullmatch(item))
    if invalid_configured_digests:
        violations.append(f"{ACTIVE_POLICY_DIGESTS_ENV} contains invalid policy digests")
    trusted_digests = configured_digests - set(invalid_configured_digests)
    supplied_digest = annotations.get("proofmesh.io/policy-digest", "")
    if not DIGEST_RE.fullmatch(supplied_digest):
        violations.append("missing or invalid proofmesh.io/policy-digest annotation")
    elif not trusted_digests:
        violations.append(f"no active policy digest is configured in {ACTIVE_POLICY_DIGESTS_ENV}")
    elif supplied_digest not in trusted_digests:
        violations.append("proofmesh.io/policy-digest is not an active policy digest")

    if pod_spec.get("serviceAccountName") in {None, "", "default"}:
        violations.append("agent workload must use a dedicated service account")
    if pod_spec.get("automountServiceAccountToken", True) is not False:
        violations.append("automountServiceAccountToken must be false")
    if pod_spec.get("hostNetwork") is True or pod_spec.get("hostPID") is True:
        violations.append("hostNetwork and hostPID are forbidden for agent workloads")
    for volume in pod_spec.get("volumes", []) or []:
        if isinstance(volume, dict) and "hostPath" in volume:
            violations.append(f"hostPath volume is forbidden: {volume.get('name', 'unnamed')}")

    main_containers = pod_spec.get("containers", []) or []
    if not isinstance(main_containers, list) or not main_containers:
        violations.append("agent workload must declare at least one application container")

    for container_kind, container in grouped_containers:
        name = str(container.get("name", "unnamed"))
        if container.get("_proofmesh_invalid_container") is True:
            violations.append(f"{container_kind} {name} is malformed")
            continue
        security = container.get("securityContext", {}) or {}
        if security.get("privileged") is True:
            violations.append(f"{container_kind} {name} must not be privileged")
        if security.get("allowPrivilegeEscalation", True) is not False:
            violations.append(f"{container_kind} {name} must set allowPrivilegeEscalation=false")
        if security.get("runAsNonRoot") is not True:
            violations.append(f"{container_kind} {name} must set runAsNonRoot=true")
        image = str(container.get("image", ""))
        if require_image_digest:
            if not IMAGE_DIGEST_RE.fullmatch(image):
                violations.append(
                    f"{container_kind} {name} image must be pinned by @sha256 digest in an enforced namespace"
                )
        elif not image or not _has_pinned_tag_or_digest(image):
            violations.append(
                f"{container_kind} {name} image must be pinned to a non-latest tag or sha256 digest"
            )
        _validate_required_environment(container_kind, name, container, violations)
        resources = container.get("resources", {}) or {}
        if not resources.get("limits"):
            violations.append(f"{container_kind} {name} must declare resource limits")
    return AdmissionDecision(allowed=not violations, violations=violations)


def handle_admission_review(review: dict[str, Any]) -> dict[str, Any]:
    request = review.get("request", {})
    uid = request.get("uid", "")
    obj = request.get("object", {})
    # The ValidatingWebhookConfiguration namespaceSelector is the authoritative scope.
    # Every request reaching this dedicated endpoint is therefore treated as protected,
    # even when the workload author omits all ProofMesh labels and annotations.
    decision = validate_agent_workload(
        obj,
        force_agent_policy=True,
        active_policy_digests=_configured_policy_digests(),
        require_image_digest=True,
    )
    response: dict[str, Any] = {
        "uid": uid,
        "allowed": decision.allowed,
        "auditAnnotations": {
            "proofmesh.io/admission": "allowed" if decision.allowed else "denied",
            "proofmesh.io/violation-count": str(len(decision.violations)),
        },
    }
    if decision.warnings:
        response["warnings"] = decision.warnings
    if not decision.allowed:
        response["status"] = {
            "code": 403,
            "reason": "ProofMeshPolicyViolation",
            "message": "; ".join(decision.violations),
        }
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}
