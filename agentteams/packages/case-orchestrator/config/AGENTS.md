# Operating Contract

Use `orchestrate-refund-case` for the `create` node. AgentTeams TeamHarness system skills remain authoritative for project, task and message mechanics.

1. Run `create` as a Leader operation: call `proofmesh.create_case(ticket_id, tenant_id)`, require `RECEIVED` revision 0, set the returned immutable `workflow_id` as TeamHarness project id, call `projectflow(create_project)`, then plan only `normalize -> context -> policy -> execute -> verify -> memory`.
2. Use `projectflow(ready_nodes)` and `taskflow(delegate_task)` for ready nodes. Extract the assignee Matrix localpart mechanically. Immediately send the mandatory Team Room assignment with `message` and the Worker’s full Matrix ID; delegation intent text is not an assignment.
3. Treat Project, plan-node and TaskMeta state as tool-owned. Never edit `shared/projects/**`, `shared/tasks/**`, `meta.json`, `plan.md` or `result.md` directly.
4. Put only identifiers, schema versions and content digests in coordination messages. Store substantive outputs under the assigned task artifact path.
5. On ProofMesh `WAITING_APPROVAL`, pause the project before delegating execute. Leave execute `planned` with no TaskMeta; `blocked` is reserved for an accepted `BLOCKED` Worker result. Use `proofmesh.get_state` to observe the external Human transition to `AUTHORIZED` before resuming.
6. After a Worker submits, call `taskflow(check_task)` and then `projectflow(accept_task_result)` before resolving the next ready node. Reject self-approval, missing predecessor digests and schema-invalid outputs.
7. Complete the project only when memory returns `COMPLETED` or verified `COMPENSATED`, a canonical `proof_bundle`, and all six plan nodes are Leader-accepted `completed`.

Do not expose credentials, approval challenges, raw PII, action passports or signing keys in Matrix messages.
