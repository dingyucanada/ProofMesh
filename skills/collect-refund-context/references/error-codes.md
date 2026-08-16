# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-CONTEXT-001` | Closed tool input, task id or expected revision is invalid | No | Block and request a corrected assignment. |
| `PM-CONTEXT-002` | Source identity or tenant binding mismatched | No | Treat as a security incident; do not return facts. |
| `PM-CONTEXT-003` | Read-only source temporarily unavailable | Yes | Retry the failed read with the same identifiers. |
| `PM-CONTEXT-004` | Ticket and order relationship or currency is inconsistent | No | Block and preserve receipt digests. |
| `PM-CONTEXT-005` | Required source receipt is missing or unverifiable | No | Reject the context artifact. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-CONTEXT-*` codes classify the task outcome; they do not replace the wire code.
