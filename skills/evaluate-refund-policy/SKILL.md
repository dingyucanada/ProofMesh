---
name: evaluate-refund-policy
description: Evaluate versioned refund policy and risk against verified ProofMesh context, then freeze an exact compensated action plan and return ALLOW, REQUIRE_APPROVAL or DENY. Use on the policy node before any business mutation, with fail-closed handling for stale policy, excessive amount, inconsistent evidence or ambiguous scope.
---

# Evaluate Refund Policy

Read [contract.yaml](references/contract.yaml), [input.schema.json](references/input.schema.json), [output.schema.json](references/output.schema.json) and [error-codes.md](references/error-codes.md) before acting.

1. Acknowledge the task and validate `workflow_id`, `expected_revision` and `task_id` from the accepted context result.
2. Call only `proofmesh.evaluate_policy`; ProofMesh internally scores risk, applies the pinned policy and freezes the action/compensation plan.
3. Require a schema-valid summary in `WAITING_APPROVAL`, `AUTHORIZED` or fail-closed `BLOCKED`, with one revision increment.
4. Preserve `policy_digest`, `plan_digest`, risk and approval fields exactly as returned.
5. Treat `WAITING_APPROVAL` as a successful policy task that pauses scheduling; never accept Team Room text as approval or alter the frozen plan.
