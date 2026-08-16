# Error codes

| Code | Meaning | Retry | Required response |
|---|---|---:|---|
| `PM-ORCH-001` | Request failed input schema or identity binding | No | Block; request corrected trusted input. |
| `PM-ORCH-002` | Authentication, tenant binding or role authorization failed | No | Reject without creating a workflow and escalate the identity mismatch. |
| `PM-ORCH-003` | ProofMesh orchestration endpoint unavailable | Yes | Retry twice without creating duplicate work. |
| `PM-ORCH-004` | TeamHarness project/task operation transiently failed | Yes | Re-read project before retry. |
| `PM-ORCH-005` | Materialized DAG differs from the locked contract | No | Pause project and repair coordination. |
| `PM-ORCH-006` | Approval or terminal-state invariant was bypassed | No | Pause project, preserve evidence and alert Human. |

## Role MCP JSON-RPC errors

| Wire code | Meaning | Worker response |
|---:|---|---|
| `-32600` | Invalid JSON-RPC request | Correct the envelope; do not retry unchanged. |
| `-32601` | Unsupported method | Use `initialize`, `tools/list` or `tools/call`. |
| `-32602` | Required tool argument missing | Revalidate the closed input schema. |
| `-32003` | Authentication, tenant or role-tool authorization denied | Stop and escalate; never switch role endpoints. |
| `-32009` | Workflow state/revision conflict or invalid value | Call `get_state`, reconcile, and never guess a revision. |

The server returns the stable machine reason in `error.data["proofmesh/reason"]`. `PM-ORCH-*` codes classify the task outcome; they do not replace the wire code.
