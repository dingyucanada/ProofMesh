---
name: execute-refund-saga
description: Execute a frozen ProofMesh refund-and-ticket-close saga through a role-scoped action gateway with idempotency, compare-and-swap preconditions, digest-bound approval and compensating refund reversal. Use only on an authorized execute node; reconcile unknown outcomes before retrying and never expose action passports or signing keys.
---

# Execute a Refund Saga

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the task and validate exactly `workflow_id`, `expected_revision` and `task_id`; ProofMesh must already be `AUTHORIZED`.
2. Call only `proofmesh.execute_authorized`. The server, not the Worker, validates the frozen plan/approval, mints internal action passports and invokes refund, ticket close or compensation.
3. Require a schema-valid summary in `EXECUTED` or fail-closed `BLOCKED`, with one revision increment and signed receipt counts.
4. On timeout or CAS conflict, do not call again blindly; ask the Leader to read the current workflow revision/state.
5. Submit `SUCCESS` for normal `EXECUTED`, `SUCCESS_WITH_NOTES` when the server reports a compensated execution candidate, and `BLOCKED` for unresolved failure. Independent verification remains mandatory.
