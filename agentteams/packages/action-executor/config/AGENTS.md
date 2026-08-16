# Operating Contract

Use `execute-refund-saga` only for an assigned, unblocked `execute` task.

1. Call `taskflow(ack_task)` and validate exactly `workflow_id`, `expected_revision` and `task_id`. Never hand-edit TaskMeta or result state.
2. Call only `proofmesh.execute_authorized`. ProofMesh loads the frozen plan and approval, performs revision CAS, mints private action passports and owns issue-refund, close-ticket, reconciliation and compensation.
3. Require one revision increment and a schema-valid `EXECUTED` or fail-closed `BLOCKED` summary with signed receipt counts. Never call internal payment/CRM tools directly.
4. On timeout or CAS conflict, stop and ask the Leader to read current state; do not retry a side effect blindly.
5. Submit `SUCCESS` for normal `EXECUTED`, `SUCCESS_WITH_NOTES` when the returned message identifies a compensated execution candidate, and `BLOCKED` for ProofMesh `BLOCKED`. Reply in the current assignment room with the exact task event; do not use the Leader-only `message` tool.

The gateway owns action-passport minting and validation. Never request or transmit signing keys or bearer passports through TeamHarness.
