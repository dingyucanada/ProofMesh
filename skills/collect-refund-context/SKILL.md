---
name: collect-refund-context
description: Collect minimum, read-only ticket and order evidence for a tenant-bound ProofMesh refund case. Use on the context node after normalization to retrieve authoritative facts, enforce cross-source tenant consistency, redact PII and emit source receipt digests without making policy or mutation decisions.
---

# Collect Refund Context

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the task and validate exactly `workflow_id`, `expected_revision` and `task_id` from the accepted predecessor.
2. Call only `proofmesh.gather_context`; the server internally performs tenant-scoped ticket/order reads through the action gateway.
3. Require a schema-valid `CONTEXT_READY` summary or an explicit fail-closed `BLOCKED` summary, with one revision increment.
4. Treat gateway and signed task receipt counts as evidence; do not reconstruct private context from chat.
5. Submit the returned summary unchanged. A `BLOCKED` workflow must be a TeamHarness `BLOCKED` result, never success.
