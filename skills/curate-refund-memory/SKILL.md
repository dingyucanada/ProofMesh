---
name: curate-refund-memory
description: Seal an independently verified ProofMesh refund case through the role-scoped curate_memory step into privacy-minimized memory and a final proof bundle. Use only on the memory node from VERIFIED or verified COMPENSATED state; return COMPLETED or COMPENSATED and reject unverified or conflicting records.
---

# Curate Refund Memory

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the task and validate exactly `workflow_id`, `expected_revision` and `task_id`; persisted status must be `VERIFIED` or verified `COMPENSATED`.
2. Call only `proofmesh.curate_memory`. The server owns redaction, memory write, proof sealing and the signed step receipt.
3. Require a schema-valid final summary in `COMPLETED` or `COMPENSATED`, with one revision increment and a canonical `proof_bundle` path.
4. Preserve a memory-write warning in the returned final message; never hide it or reinterpret it as an unqualified storage success.
5. Submit the returned summary unchanged. Never store secrets, approval challenges or action passports in TeamHarness artifacts.
