#!/usr/bin/env bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROOFMESH_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
CONTAINER_CMD=${AGENTTEAMS_CONTAINER_CMD:-docker}
MANAGER_CONTAINER=${AGENTTEAMS_MANAGER_CONTAINER:-agentteams-manager}
CONTROLLER_CONTAINER=${AGENTTEAMS_CONTROLLER_CONTAINER:-agentteams-controller}
QWENPAW_IMAGE=proofmesh/agentteams-qwenpaw-worker:v1.2.0-beta.1-compat2

ROLES="case-orchestrator ticket-intake context-investigator risk-policy-sentinel action-executor outcome-verifier case-memory-curator"

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

require_manager() {
  command -v "${CONTAINER_CMD}" >/dev/null 2>&1 || die "container command not found: ${CONTAINER_CMD}"
  command -v jq >/dev/null 2>&1 || die "jq is required for secret-safe status and readiness checks"
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" true >/dev/null 2>&1 || \
    die "Manager container is absent or not running: ${MANAGER_CONTAINER}"
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" hiclaw version >/dev/null 2>&1 || \
    die "hiclaw CLI is unavailable in ${MANAGER_CONTAINER}"
  "${CONTAINER_CMD}" exec "${CONTROLLER_CONTAINER}" mc --version >/dev/null 2>&1 || \
    die "controller mc is unavailable in ${CONTROLLER_CONTAINER}"
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" mkdir -p /tmp/proofmesh-agentteams
}

require_qwenpaw_image() {
  "${CONTAINER_CMD}" image inspect "${QWENPAW_IMAGE}" >/dev/null 2>&1 || \
    die "missing ${QWENPAW_IMAGE}; first build the official beta image, then run: docker build -f agentteams/qwenpaw-compat.Dockerfile -t ${QWENPAW_IMAGE} ."
}

seed_embedded_qwenpaw_probe() {
  # v1.2.0-beta.1 embedded Docker always probes agents/<role>/openclaw.json
  # before creating a container, even for QwenPaw, whose actual desired state
  # is runtime/runtime.yaml. Seed a credential-free compatibility object only
  # when the authoritative QwenPaw runtime.yaml already exists.
  "${CONTAINER_CMD}" exec "${CONTROLLER_CONTAINER}" mc --version >/dev/null 2>&1 || \
    die "controller mc is unavailable; cannot apply the v1.2.0-beta.1 embedded QwenPaw readiness compatibility probe"

  attempt=0
  while test "${attempt}" -lt 120; do
    missing=0
    for role in ${ROLES}; do
      runtime_object="hiclaw/agentteams-storage/agents/${role}/runtime/runtime.yaml"
      probe_object="hiclaw/agentteams-storage/agents/${role}/openclaw.json"
      if "${CONTAINER_CMD}" exec "${CONTROLLER_CONTAINER}" mc stat "${runtime_object}" >/dev/null 2>&1; then
        if ! "${CONTAINER_CMD}" exec "${CONTROLLER_CONTAINER}" mc stat "${probe_object}" >/dev/null 2>&1; then
          printf '{}\n' | "${CONTAINER_CMD}" exec -i "${CONTROLLER_CONTAINER}" \
            mc pipe "${probe_object}" >/dev/null
        fi
      else
        missing=$((missing + 1))
      fi
    done
    test "${missing}" -eq 0 && return
    attempt=$((attempt + 1))
    sleep 2
  done
  die "timed out waiting for all seven QwenPaw runtime.yaml objects"
}

copy_to_manager() {
  source_path=$1
  target_name=$2
  "${CONTAINER_CMD}" cp "${source_path}" "${MANAGER_CONTAINER}:/tmp/proofmesh-agentteams/${target_name}"
}

publish_scoped_worker_package() {
  role=$1
  archive=$2
  package_object="hiclaw/agentteams-storage/agents/${role}/packages/${role}.zip"
  "${CONTAINER_CMD}" exec -i "${CONTROLLER_CONTAINER}" mc pipe "${package_object}" < "${archive}" >/dev/null
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
    hiclaw update worker --name "${role}" --package "oss://agents/${role}/packages/${role}.zip"
}

apply_yaml() {
  source_path=$1
  target_name=$(basename -- "${source_path}")
  copy_to_manager "${source_path}" "${target_name}"
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
    hiclaw apply -f "/tmp/proofmesh-agentteams/${target_name}"
}

apply_team_yaml() {
  source_path=$1
  target_name=$(basename -- "${source_path}")
  copy_to_manager "${source_path}" "${target_name}"
  if output=$("${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
      hiclaw apply -f "/tmp/proofmesh-agentteams/${target_name}" 2>&1); then
    return
  fi
  case "${output}" in
    *"leader.name is required"*)
      die "AgentTeams v1.2.0-beta.1 Manager REST does not map spec.workerMembers. The decoupled Team was not created; use a credential-safe Kubernetes operator context for team.yaml, never the legacy inline Team shape."
      ;;
    *)
      die "Team submission failed without a recognized safe diagnostic; inspect the Manager logs without copying credentials into evidence."
      ;;
  esac
}

phase_is() {
  plural=$1
  name=$2
  expected=$3
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
    hiclaw get "${plural}" "${name}" -o json 2>/dev/null | \
    jq -e --arg expected "${expected}" '.phase == $expected' >/dev/null
}

resource_exists() {
  plural=$1
  name=$2
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
    hiclaw get "${plural}" "${name}" -o json >/dev/null 2>&1
}

prepare() {
  python3 "${PROOFMESH_ROOT}/scripts/package_agentteams.py"
  python3 "${PROOFMESH_ROOT}/scripts/validate_agentteams.py"
  require_manager
  require_qwenpaw_image

  for role in ${ROLES}; do
    archive="${PROOFMESH_ROOT}/agentteams/dist/${role}.zip"
    test -f "${archive}" || die "missing package: ${archive}"
    copy_to_manager "${archive}" "${role}.zip"
    "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" \
      hiclaw apply worker --name "${role}" \
      --zip "/tmp/proofmesh-agentteams/${role}.zip" \
      --runtime qwenpaw
    # The beta upload endpoint stores packages under agentteams-config/, but
    # its generated Worker policy cannot read that prefix. Republish the same
    # archive into the Worker's own authorized prefix and update only package.
    publish_scoped_worker_package "${role}" "${archive}"
  done

  # The upload assigned each Worker an object-storage package URI. This overlay
  # deliberately omits spec.package, so AgentTeams beta's merge update preserves it.
  apply_yaml "${PROOFMESH_ROOT}/agentteams/workers.yaml"
  # The beta API creates Humans but returns HTTP 405 for update. Preserve an
  # existing Human so prepare can safely resume after a partial reconciliation.
  if resource_exists humans proofmesh-approver; then
    printf '%s\n' 'human/proofmesh-approver already exists; beta update is unsupported, preserving it'
  else
    apply_yaml "${PROOFMESH_ROOT}/agentteams/human.yaml"
  fi
  seed_embedded_qwenpaw_probe

  printf '%s\n' \
    'PREPARE SUBMITTED. This is not runtime verification.' \
    'Wait for proofmesh-approver=Active and all seven Workers=Running, then run: bootstrap.sh team'
}

apply_team() {
  python3 "${PROOFMESH_ROOT}/scripts/validate_agentteams.py"
  require_manager
  phase_is humans proofmesh-approver Active || \
    die "proofmesh-approver is not Active; inspect: hiclaw get humans proofmesh-approver -o json"
  for role in ${ROLES}; do
    phase_is workers "${role}" Running || \
      die "Worker ${role} is not Running; inspect it before Team creation"
  done
  apply_team_yaml "${PROOFMESH_ROOT}/agentteams/team.yaml"
  printf '%s\n' \
    'TEAM SUBMITTED. Submission success is not TeamHarness success.' \
    'Poll hiclaw get teams proofmesh-refund-team -o json until phase is Active, then run the documented E2E acceptance.'
}

status() {
  require_manager
  # Never print raw Human resources: beta includes initialPassword in that JSON.
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" hiclaw get managers -o json | \
    jq '{total, managers: [.managers[] | {name, phase, runtime, model, welcomeSent}]}'
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" hiclaw get humans proofmesh-approver -o json | \
    jq '{name, displayName, phase}'
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" hiclaw get workers -o json | \
    jq '{total, workers: [.workers[] | {name, phase, runtime, containerState}]}'
  "${CONTAINER_CMD}" exec "${MANAGER_CONTAINER}" hiclaw get teams -o json | \
    jq '{total, teams: [.teams[] | {name, phase}]}'
}

case "${1:-}" in
  prepare) prepare ;;
  team) apply_team ;;
  status) status ;;
  *) die "usage: $0 {prepare|team|status}" ;;
esac
