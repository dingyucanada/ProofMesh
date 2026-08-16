# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-EXEC-001` | Plan, revision or task assignment is invalid/stale | No | Block before mutation. |
| `PM-EXEC-002` | Approval is missing, invalid or not bound to the plan/scope | No | Keep execute blocked and notify Human. |
| `PM-EXEC-003` | Gateway denied passport, tool, budget or precondition | No | Preserve denial receipt and block. |
| `PM-EXEC-004` | Tool outcome is unknown because of timeout/transport failure | Conditional | Read back state, then retry only if absence is proven. |
| `PM-EXEC-005` | Ticket close failed after refund and compensation completed | No | Return `EXECUTED` as a compensation candidate with all receipts; only verifier may produce `COMPENSATED`. |
| `PM-EXEC-006` | Compensation failed or final state is inconsistent | No | Return `BLOCKED`, alert Human and prohibit further automation. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision or repeat a side effect. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-EXEC-*` codes classify the task outcome; they do not replace the wire code.
