# Operating Contract

Use `evaluate-refund-policy` only for an assigned `policy` task.

1. Call `taskflow(ack_task)` and validate exactly `workflow_id`, `expected_revision` and `task_id`. Never hand-edit Project or Task state.
2. Call only `proofmesh.evaluate_policy`; risk scoring, policy pinning, plan freezing, compensation and digests are server-owned.
3. Require one revision increment and a schema-valid `WAITING_APPROVAL`, `AUTHORIZED` or fail-closed `BLOCKED` summary. Preserve all returned digest and approval fields.
4. Submit `SUCCESS` for `WAITING_APPROVAL` or `AUTHORIZED`; the Leader must pause scheduling for `WAITING_APPROVAL`. Submit TeamHarness `BLOCKED` only for ProofMesh `BLOCKED`.
5. Reply in the current assignment room with the exact task event; do not use the Leader-only `message` tool.

Never claim that an AgentTeams task message is approval and never expose passport-signing material.
