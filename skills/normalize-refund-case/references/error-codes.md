# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-INTAKE-001` | Closed tool input or task assignment is invalid | No | Block and request a corrected assignment. |
| `PM-INTAKE-002` | Persisted workflow is not in `RECEIVED` | No | Reconcile through the Leader; do not force a transition. |
| `PM-INTAKE-003` | Role MCP is temporarily unavailable before a response | Yes | Re-read state, then retry with the same task and revision only if unchanged. |
| `PM-INTAKE-004` | Expected revision is stale or the task id conflicts | No | Stop and ask the Leader to reconcile authoritative state. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-INTAKE-*` codes classify the task outcome; they do not replace the wire code.
