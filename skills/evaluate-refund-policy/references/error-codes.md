# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-POLICY-001` | Closed tool input, task id or expected revision is invalid | No | Block; do not force policy evaluation. |
| `PM-POLICY-002` | Policy version or digest is missing/stale | No | Fail closed and require an authoritative policy. |
| `PM-POLICY-003` | Read-only risk service temporarily unavailable | Yes | Retry without changing inputs. |
| `PM-POLICY-004` | Amount, currency, revision or scope violates policy | No | Return `DENY` with reason codes. |
| `PM-POLICY-005` | Frozen plan lacks an allowed action, precondition or compensation | No | Reject the plan and alert the leader. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-POLICY-*` codes classify the task outcome; they do not replace the wire code.
