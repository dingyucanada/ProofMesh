# REST: accept decoupled Team workerMembers on create

## Summary

Align `POST /api/v1/teams` with the v1beta1 decoupled Team contract by transporting `spec.workerMembers` to `TeamSpec.WorkerMembers`.

## Changes

- add `workerMembers []TeamWorkerRef` to `CreateTeamRequest`;
- make `leader` optional only when `workerMembers` is non-empty;
- copy `workerMembers` to the Team CR;
- retain the existing inline Leader/Workers construction and validation for legacy requests;
- add positive decoupled, negative legacy-validation and mixed migration-contract tests.

## Why this is minimal

The Team controller already selects the decoupled path whenever `spec.workerMembers` is populated, resolves standalone Worker CRs, validates exactly one `team_leader`, provisions rooms and injects coordination context. The REST layer should transport that established API shape rather than reimplement its validation or translate it into deprecated inline members.

## Test plan

```bash
go test ./internal/server -run 'TestCreateTeam(WithWorkerMembers|WithoutWorkerMembers)'
go test ./internal/server
```

ProofMesh also ships a dependency-free offline contract check:

```bash
python3 outputs/ProofMesh/agentteams/upstream/verify_patch.py \
  --source work/AgentTeams-v1.2.0-beta.1
```

The offline verifier is not a substitute for `go test`; it protects the pin, clean application and intended source contract when Go is unavailable.

## Backward compatibility

- Existing legacy `{leader, workers}` requests take the same path and retain the same validation.
- Requests with no `workerMembers` and no `leader.name` still return HTTP 400.
- No CRD or reconciler behavior changes.
- Mixed migration payloads retain both shapes; the existing Team reconciler continues to give `workerMembers` precedence.

## Not included

- `UpdateTeamRequest` and Team response round-trip support;
- validation of member existence or leader cardinality in the REST handler;
- image build or live runtime claims.
