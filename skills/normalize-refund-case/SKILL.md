---
name: normalize-refund-case
description: Validate and canonicalize a tenant-bound customer-refund request while producing stable dedupe material inside ProofMesh. Use on the normalize node before any ticket, order, risk or mutation tool is called, especially when a stale revision, malformed assignment or unauthorized transition must fail closed.
---

# Normalize a Refund Case

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the assigned task and validate exactly `workflow_id`, `expected_revision` and `task_id`.
2. Call only `proofmesh.normalize_case`; the server owns canonicalization, tenant binding, dedupe material and the signed step receipt.
3. Require a schema-valid summary with `status=NORMALIZED`, `last_step=normalize_case` and revision incremented by one.
4. If ProofMesh returns a CAS conflict, re-read through the Leader rather than retrying with a guessed revision.
5. Submit the returned summary as the task artifact; never place raw PII in the Team Room.
