# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-MEMORY-001` | Persisted independent verification is false or missing | No | Reject memory write and keep project open. |
| `PM-MEMORY-002` | Business status is not a permitted verified terminal status | No | Reject the record. |
| `PM-MEMORY-003` | Redaction or retention classification failed | No | Do not persist any payload. |
| `PM-MEMORY-004` | Role MCP is temporarily unavailable before a response | Yes | Re-read state, then retry with the same workflow, task and revision only if unchanged. |
| `PM-MEMORY-005` | Sealing or persisted memory fingerprint is inconsistent | No | Preserve evidence, freeze the conflicting write and alert Human. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Reconcile through the Leader; never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-MEMORY-*` codes classify the task outcome; they do not replace the wire code.
