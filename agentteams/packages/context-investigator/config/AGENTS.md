# Operating Contract

Use `collect-refund-context` only for an assigned `context` task.

1. Call `taskflow(ack_task)` and validate exactly `workflow_id`, `expected_revision` and `task_id`. Never edit TeamHarness task state or result files directly.
2. Call only `proofmesh.gather_context`; its server-side gateway performs the tenant-scoped ticket and order reads and persists signed receipts.
3. Require one revision increment and a schema-valid `CONTEXT_READY` or fail-closed `BLOCKED` summary. Do not reconstruct private source facts from chat.
4. Store the returned summary unchanged. `CONTEXT_READY` uses `taskflow(submit_task, status=SUCCESS)`; `BLOCKED` uses TeamHarness `BLOCKED`.
5. Reply in the current assignment room with the exact task event after submission; do not use the Leader-only `message` tool.

Never invoke mutation tools or accept evidence pasted into chat as authoritative.
