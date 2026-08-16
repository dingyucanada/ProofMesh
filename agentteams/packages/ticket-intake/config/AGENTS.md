# Operating Contract

Use `normalize-refund-case` only for an assigned `normalize` task.

1. Call `taskflow(ack_task)` before work; use the returned spec/meta as the immutable assignment. Task state is tool-owned, so never edit task files directly.
2. Validate exactly `workflow_id`, `expected_revision` and `task_id` against the Skill input schema; never add fields rejected by the role MCP contract.
3. Call only `proofmesh.normalize_case`. Canonicalization, tenant binding, dedupe material, CAS and signed task receipt are server-owned.
4. Require `status=NORMALIZED`, `last_step=normalize_case` and one revision increment. Store the returned summary unchanged under the task deliverable path.
5. Finish with `taskflow(submit_task)` using `SUCCESS` and the deliverable path, then reply in the current assignment room with `TASK_COMPLETED: {task_id}`. On failure submit `BLOCKED` with a declared error code; do not use the Leader-only `message` tool.

Never call policy, payment, CRM mutation or approval tools.
