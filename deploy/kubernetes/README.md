# ProofMesh Kubernetes enforcement boundary

These manifests are a fail-closed reference deployment boundary, not a complete
cluster installer.

## Required rollout order

1. Run the admission service in `proofmesh-system` with
   `PROOFMESH_ACTIVE_POLICY_DIGESTS` set to the comma-separated SHA-256 digests
   of the policies currently allowed to authorize Agent workloads. Use the
   canonical ProofMesh digest emitted as `policy_digest` by the validated
   control-plane build or `/ready`; this is a canonical-JSON digest, not the
   raw policy file checksum. Multiple values support a bounded rotation window.
2. Replace `caBundle` in `validating-webhook.yaml` with the CA for the admission
   Service certificate. Keep `failurePolicy: Fail`.
3. Apply the webhook and NetworkPolicies, verify the admission Service is ready,
   then label each protected namespace with
   `proofmesh.io/agent-policy=enforced`.
4. Change the example namespaces, labels and ports to the cluster's real values.

The webhook treats every AdmissionReview it receives as protected. Its
`namespaceSelector`, controlled by the cluster administrator, is the security
boundary; workload authors cannot opt out by deleting an Agent label.

## Gateway-only tool access

`network-policy.yaml` denies egress from enforced Agent Pods except DNS and the
ProofMesh gateway, and only permits ingress to protected tools from gateway
Pods. This containment is necessary but is not cryptographic identity.

Every protected upstream tool must additionally require mutual TLS or an
equivalent workload identity and accept only the gateway identity, for example
`spiffe://cluster.local/ns/proofmesh-system/sa/proofmesh-gateway`. It must reject
Agent service-account identities and must not expose another route outside this
policy. Without that upstream identity check, Kubernetes labels alone are not a
sufficient authorization boundary.

Use a CNI that enforces Kubernetes NetworkPolicy and verify the boundary from a
real Agent Pod before production rollout.
