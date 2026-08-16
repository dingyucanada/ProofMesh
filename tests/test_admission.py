from __future__ import annotations

from pathlib import Path

import yaml

from proofmesh.admission import handle_admission_review, validate_agent_workload


ACTIVE_DIGEST = "a" * 64
DIGEST_IMAGE = f"example.invalid/refund-agent@sha256:{'b' * 64}"


def compliant_container(*, name: str = "agent", image: str = "example.invalid/refund-agent:1.0.0"):
    return {
        "name": name,
        "image": image,
        "securityContext": {
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
        },
        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
        "env": [
            {"name": "PROOFMESH_GATEWAY_URL", "value": "https://proofmesh/mcp"},
            {"name": "PROOFMESH_TRUST_BUNDLE", "value": "/var/run/proofmesh/trust.json"},
            {"name": "PROOFMESH_FAIL_CLOSED", "value": "true"},
        ],
    }


def compliant_deployment(*, image: str = "example.invalid/refund-agent:1.0.0"):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "refund-agent"},
        "spec": {
            "template": {
                "metadata": {
                    "labels": {"agentteams.io/worker": "refund-agent", "proofmesh.io/enforced": "true"},
                    "annotations": {
                        "proofmesh.io/agent-workload": "true",
                        "proofmesh.io/policy-digest": ACTIVE_DIGEST,
                    },
                },
                "spec": {
                    "serviceAccountName": "refund-agent",
                    "automountServiceAccountToken": False,
                    "containers": [compliant_container(image=image)],
                },
            }
        },
    }


def admission_review(obj, *, uid: str = "uid-test", namespace: str = "agentteams"):
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {"uid": uid, "namespace": namespace, "object": obj},
    }


def test_compliant_agent_workload_is_allowed_with_exact_active_policy_digest():
    decision = validate_agent_workload(
        compliant_deployment(), active_policy_digests={ACTIVE_DIGEST}
    )
    assert decision.allowed is True
    assert decision.violations == []


def test_policy_digest_must_exactly_match_configured_active_whitelist():
    deployment = compliant_deployment()
    decision = validate_agent_workload(
        deployment,
        active_policy_digests={"c" * 64},
    )
    assert decision.allowed is False
    assert any("not an active policy digest" in item for item in decision.violations)


def test_missing_or_invalid_active_policy_configuration_fails_closed():
    missing = validate_agent_workload(compliant_deployment(), active_policy_digests=set())
    invalid = validate_agent_workload(
        compliant_deployment(), active_policy_digests={"not-a-digest"}
    )
    assert missing.allowed is False
    assert any("no active policy digest" in item for item in missing.violations)
    assert invalid.allowed is False
    assert any("contains invalid policy digests" in item for item in invalid.violations)


def test_unprotected_privileged_agent_is_denied_with_actionable_reasons():
    deployment = compliant_deployment()
    template = deployment["spec"]["template"]
    template["metadata"]["labels"].pop("proofmesh.io/enforced")
    container = template["spec"]["containers"][0]
    container["securityContext"]["privileged"] = True
    container["env"] = []
    decision = validate_agent_workload(
        deployment, active_policy_digests={ACTIVE_DIGEST}
    )
    assert decision.allowed is False
    assert any("proofmesh.io/enforced" in item for item in decision.violations)
    assert any("privileged" in item for item in decision.violations)
    assert any("PROOFMESH_GATEWAY_URL" in item for item in decision.violations)


def test_required_environment_values_must_be_non_empty_literals():
    deployment = compliant_deployment()
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [
        {"name": "PROOFMESH_GATEWAY_URL", "value": "  "},
        {"name": "PROOFMESH_TRUST_BUNDLE", "valueFrom": {"secretKeyRef": {"name": "x", "key": "path"}}},
        {"name": "PROOFMESH_FAIL_CLOSED", "value": "false"},
    ]
    decision = validate_agent_workload(
        deployment, active_policy_digests={ACTIVE_DIGEST}
    )
    assert decision.allowed is False
    assert any("non-empty literal PROOFMESH_GATEWAY_URL" in item for item in decision.violations)
    assert any("non-empty literal PROOFMESH_TRUST_BUNDLE" in item for item in decision.violations)
    assert any("PROOFMESH_FAIL_CLOSED=true" in item for item in decision.violations)


def test_init_and_ephemeral_containers_receive_the_same_security_checks():
    deployment = compliant_deployment(image=DIGEST_IMAGE)
    pod_spec = deployment["spec"]["template"]["spec"]
    init_container = compliant_container(name="bootstrap", image=DIGEST_IMAGE)
    init_container["securityContext"]["privileged"] = True
    ephemeral_container = compliant_container(name="debugger", image="example.invalid/debugger:latest")
    ephemeral_container["env"][0]["value"] = ""
    pod_spec["initContainers"] = [init_container]
    pod_spec["ephemeralContainers"] = [ephemeral_container]

    decision = validate_agent_workload(
        deployment,
        active_policy_digests={ACTIVE_DIGEST},
        require_image_digest=True,
    )
    assert decision.allowed is False
    assert any("initContainer bootstrap must not be privileged" in item for item in decision.violations)
    assert any("ephemeralContainer debugger image must be pinned by @sha256" in item for item in decision.violations)
    assert any(
        "ephemeralContainer debugger must set a non-empty literal PROOFMESH_GATEWAY_URL" in item
        for item in decision.violations
    )


def test_compliant_init_and_ephemeral_containers_are_accepted():
    deployment = compliant_deployment(image=DIGEST_IMAGE)
    pod_spec = deployment["spec"]["template"]["spec"]
    pod_spec["initContainers"] = [compliant_container(name="bootstrap", image=DIGEST_IMAGE)]
    pod_spec["ephemeralContainers"] = [compliant_container(name="debugger", image=DIGEST_IMAGE)]
    decision = validate_agent_workload(
        deployment,
        active_policy_digests={ACTIVE_DIGEST},
        require_image_digest=True,
    )
    assert decision.allowed is True


def test_cronjob_nested_pod_template_is_validated():
    deployment = compliant_deployment(image=DIGEST_IMAGE)
    cronjob = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "refund-agent-cron"},
        "spec": {
            "schedule": "0 * * * *",
            "jobTemplate": {
                "spec": {"template": deployment["spec"]["template"]},
            },
        },
    }
    decision = validate_agent_workload(
        cronjob,
        force_agent_policy=True,
        active_policy_digests={ACTIVE_DIGEST},
        require_image_digest=True,
    )
    assert decision.allowed is True


def test_registry_port_without_tag_is_not_mistaken_for_a_pinned_image():
    deployment = compliant_deployment(image="registry.example.invalid:5000/refund-agent")
    decision = validate_agent_workload(
        deployment, active_policy_digests={ACTIVE_DIGEST}
    )
    assert decision.allowed is False
    assert any("image must be pinned" in item for item in decision.violations)


def test_admission_review_preserves_uid_and_fails_closed(monkeypatch):
    monkeypatch.setenv("PROOFMESH_ACTIVE_POLICY_DIGESTS", ACTIVE_DIGEST)
    review = admission_review(
        {
            "kind": "Pod",
            "metadata": {"annotations": {"proofmesh.io/agent-workload": "true"}},
            "spec": {"containers": []},
        },
        uid="uid-123",
    )
    response = handle_admission_review(review)["response"]
    assert response["uid"] == "uid-123"
    assert response["allowed"] is False
    assert response["status"]["code"] == 403


def test_webhook_treats_every_selected_namespace_request_as_protected(monkeypatch):
    monkeypatch.setenv("PROOFMESH_ACTIVE_POLICY_DIGESTS", ACTIVE_DIGEST)
    review = admission_review(
        {
            "kind": "Pod",
            "metadata": {"name": "unlabelled-agent"},
            "spec": {"containers": [{"name": "agent", "image": DIGEST_IMAGE}]},
        },
        uid="uid-namespace",
    )
    response = handle_admission_review(review)["response"]
    assert response["allowed"] is False
    assert "proofmesh.io/enforced" in response["status"]["message"]


def test_enforced_namespace_requires_digest_image_and_accepts_compliant_digest(monkeypatch):
    monkeypatch.setenv("PROOFMESH_ACTIVE_POLICY_DIGESTS", ACTIVE_DIGEST)
    tagged = handle_admission_review(admission_review(compliant_deployment()))["response"]
    digest_pinned = handle_admission_review(
        admission_review(compliant_deployment(image=DIGEST_IMAGE))
    )["response"]
    assert tagged["allowed"] is False
    assert "@sha256 digest" in tagged["status"]["message"]
    assert digest_pinned["allowed"] is True


def test_webhook_and_network_policy_manifests_encode_fail_closed_boundary():
    root = Path(__file__).resolve().parents[1]
    webhook_documents = list(
        yaml.safe_load_all((root / "deploy/kubernetes/validating-webhook.yaml").read_text(encoding="utf-8"))
    )
    webhook = next(item for item in webhook_documents if item["kind"] == "ValidatingWebhookConfiguration")
    entry = webhook["webhooks"][0]
    assert entry["failurePolicy"] == "Fail"
    assert entry["sideEffects"] == "None"
    assert entry["namespaceSelector"]["matchLabels"]["proofmesh.io/agent-policy"] == "enforced"
    assert "objectSelector" not in entry

    policies = list(
        yaml.safe_load_all((root / "deploy/kubernetes/network-policy.yaml").read_text(encoding="utf-8"))
    )
    policy_names = {item["metadata"]["name"] for item in policies if item}
    assert "proofmesh-agent-default-deny-egress" in policy_names
    assert "proofmesh-agent-allow-gateway-egress" in policy_names
    assert "proofmesh-protected-tools-gateway-only" in policy_names
