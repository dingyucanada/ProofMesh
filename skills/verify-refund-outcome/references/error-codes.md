# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-VERIFY-001` | Closed tool input, task id or expected revision is invalid | No | Block and request a corrected assignment. |
| `PM-VERIFY-002` | Persisted workflow is not in `EXECUTED` | No | Reconcile through the Leader; do not force verification. |
| `PM-VERIFY-003` | Persisted plan, execution outcome or business binding failed | No | Return invalid and block terminal completion. |
| `PM-VERIFY-004` | Fresh read-only business-state query is temporarily unavailable | Yes | Retry read-only verification only. |
| `PM-VERIFY-005` | Tool, budget, idempotency or compensation invariant failed | No | Return invalid and alert the leader. |
| `PM-VERIFY-006` | Claimed terminal state differs from fresh read-back | No | Return invalid; do not repair the claim. |

The final proof bundle is created by the following memory step, so it is not a verifier input.

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-VERIFY-*` codes classify the task outcome; they do not replace the wire code.
