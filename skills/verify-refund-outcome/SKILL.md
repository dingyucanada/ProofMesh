---
name: verify-refund-outcome
description: Independently verify a ProofMesh executed refund or compensation through the role-scoped verify_outcome step and fresh business-state read-back. Use on the verify node only from EXECUTED state; release memory only for VERIFIED or verified COMPENSATED, and fail closed on any inconsistent postcondition.
---

# Verify a Refund Outcome

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the task and validate exactly `workflow_id`, `expected_revision` and `task_id`; ProofMesh must be `EXECUTED`.
2. Call only `proofmesh.verify_outcome`. The server independently reads business state and binds it to the persisted plan, execution outcome and signed evidence.
3. Require a schema-valid summary in `VERIFIED`, verified `COMPENSATED` or fail-closed `BLOCKED`, with one revision increment.
4. Only `VERIFIED` or `COMPENSATED` may release the memory node; `BLOCKED` must stop the DAG.
5. Submit the returned summary unchanged and never use executor claims as a substitute for server verification.
