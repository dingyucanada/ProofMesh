---
name: orchestrate-refund-case
description: Create and coordinate a seven-stage AgentTeams TeamHarness project for a ProofMesh customer-refund request. Use when a new refund workflow must be materialized as create, normalize, context, policy, execute, verify and memory tasks with digest-bound handoffs and an external approval gate.
---

# Orchestrate a Refund Case

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Validate exactly `ticket_id` and `tenant_id`; authenticated ProofMesh identity supplies the requester.
2. Call `proofmesh.create_case` and require a schema-valid `RECEIVED`, revision-0 summary. Use its `workflow_id` as the TeamHarness `project_id`.
3. Run `create` as the Leader, then use `projectflow(plan_dag)` to materialize the six Worker tasks from normalize through memory.
4. Delegate each ready task with `taskflow`, then send the mandatory Team Room assignment through `message`; attach `workflow_id`, current revision, `task_id` and predecessor receipt digest.
5. Pause before delegating execution when policy returns `WAITING_APPROVAL`; leave execute planned until an external Human approval changes ProofMesh to `AUTHORIZED` and the project resumes.
6. Check and accept every submitted result before releasing its successor. Preserve the ProofMesh summary as the task artifact and never report completion until memory returns `COMPLETED` or verified `COMPENSATED`.
