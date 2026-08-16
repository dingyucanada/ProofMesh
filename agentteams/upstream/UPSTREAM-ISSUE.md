# Manager REST drops decoupled Team `workerMembers`

## Suggested issue title

`v1.2.0-beta.1: POST /api/v1/teams cannot create a decoupled Team with workerMembers`

## Version

- Repository: `agentscope-ai/AgentTeams`
- Release: `v1.2.0-beta.1`
- Commit: `78d0ceda336befa6e62bf89fc1a6b08b965e128d`
- Affected surface: Manager/controller REST `POST /api/v1/teams`, including `hiclaw apply -f`

## Reproduction

Create standalone Worker CRs, then apply a Team whose membership references them:

```yaml
apiVersion: agentteams.io/v1beta1
kind: Team
metadata:
  name: decoupled-team
spec:
  workerMembers:
    - name: existing-leader
      role: team_leader
    - name: existing-worker
      role: worker
```

Observed response from `hiclaw apply -f team.yaml`:

```text
HTTP 400: leader.name is required
```

## Root cause

The CRD and `TeamReconciler` support the decoupled contract: `TeamSpec.WorkerMembers` references existing Worker CRs and the legacy `leader/workers` fields are ignored when the list is non-empty. However, the REST DTO `CreateTeamRequest` has no `workerMembers` field. `hiclaw apply` forwards the Team spec to the REST endpoint, JSON decoding drops that field, and `CreateTeam` unconditionally requires `leader.name` before it creates the CR.

This is a transport-layer mismatch, not a reason to fall back to an inline Team. Falling back would create a second set of managed Workers instead of composing the existing standalone Workers.

## Expected behavior

1. `CreateTeamRequest` accepts `workerMembers` as `[]v1beta1.TeamWorkerRef`.
2. When `workerMembers` is non-empty, `leader.name` is not required.
3. The list is copied unchanged to `TeamSpec.WorkerMembers`; the existing reconciler owns validation and the decoupled lifecycle.
4. When `workerMembers` is empty, the current legacy validation and inline `leader/workers` mapping remain unchanged.

## Proposed patch and verification

`agentteams-v1.2.0-beta.1-manager-worker-members.patch` implements the minimal change and adds three handler tests:

- a decoupled request succeeds without `leader` and persists the two references;
- an empty-membership request still returns `leader.name is required`;
- a migration payload carrying both shapes preserves the existing controller precedence contract.

`source-lock.json` pins the exact release commit, base-file hashes and patch hash. `verify_patch.py` checks those hashes, applies the patch to a temporary copy and verifies both the new and legacy contracts without changing the source checkout.

## Compatibility and scope

The patch deliberately does not change the CRD, Team controller, Worker lifecycle, CLI request construction, update semantics, or response shape. It is limited to Team creation because that is the reproduced failure. A separate follow-up may add `workerMembers` to update/response DTOs if the API intends to support roster mutation and full REST round-tripping.

## Runtime evidence boundary

The patch package is source-level and contract-tested. It does not claim that an AgentTeams controller image containing the patch was built or that a Team reached `Active`. Those require a Go toolchain, an image rebuild and a live AgentTeams integration run.
