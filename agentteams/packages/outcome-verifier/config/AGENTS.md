# Operating Contract

Use `verify-refund-outcome` only for an assigned `verify` task.

1. Call `taskflow(ack_task)` and validate exactly `workflow_id`, `expected_revision` and `task_id`. Never hand-edit task or evidence state.
2. Call only `proofmesh.verify_outcome`; ProofMesh performs the independent business read-back and persisted plan/execution binding.
3. Require one revision increment and a schema-valid `VERIFIED`, verified `COMPENSATED` or fail-closed `BLOCKED` summary.
4. Submit `SUCCESS` for `VERIFIED`, `SUCCESS_WITH_NOTES` for verified `COMPENSATED`, and `BLOCKED` for ProofMesh `BLOCKED`. Never convert an inconsistent postcondition into success.
5. Reply in the current assignment room with the exact task event; do not use the executor route or the Leader-only `message` tool.

Do not mutate artifacts or use the executor’s MCP route.
