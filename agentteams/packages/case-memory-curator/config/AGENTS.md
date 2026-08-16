# Operating Contract

Use `curate-refund-memory` only for an assigned `memory` task.

1. Call `taskflow(ack_task)` and validate exactly `workflow_id`, `expected_revision` and `task_id`; persisted ProofMesh status must be `VERIFIED` or verified `COMPENSATED`. Never hand-edit TeamHarness state.
2. Call only `proofmesh.curate_memory`; ProofMesh owns redaction, memory storage, final proof sealing, CAS and signed task receipt.
3. Require one revision increment and a schema-valid final `COMPLETED` or `COMPENSATED` summary with canonical `proof_bundle`.
4. Preserve any memory-write warning in `final_message`; do not turn a warning into a false unconditional storage claim.
5. Submit `SUCCESS` for `COMPLETED` and `SUCCESS_WITH_NOTES` for `COMPENSATED`, then reply in the current assignment room with the exact task event. Do not use the Leader-only `message` tool.

Never use memory as approval, policy or current business state.
