#!/usr/bin/env python3
"""Verify and apply the pinned AgentTeams Manager workerMembers patch offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "source-lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(text: str, fragment: str, source: str) -> None:
    if fragment not in text:
        raise RuntimeError(f"{source}: missing required contract fragment: {fragment!r}")


def load_lock() -> dict:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if lock.get("schemaVersion") != "proofmesh.io/agentteams-upstream-patch-lock/v1":
        raise RuntimeError("unsupported source-lock schema")
    return lock


def verify_package() -> dict:
    """Verify the vendored patch bundle without requiring an upstream checkout."""
    lock = load_lock()
    patch_path = HERE / lock["patch"]["path"]
    if sha256(patch_path) != lock["patch"]["sha256"]:
        raise RuntimeError("patch SHA-256 does not match source-lock.json")

    contract = lock.get("expectedContract", {})
    required_flags = (
        "createRequestAcceptsWorkerMembers",
        "workerMembersBypassesLegacyLeaderRequirement",
        "workerMembersCopiedToTeamSpec",
        "legacyLeaderValidationPreservedWhenWorkerMembersEmpty",
        "legacyLeaderAndWorkersMappingPreserved",
    )
    for name in required_flags:
        if contract.get(name) is not True:
            raise RuntimeError(f"source-lock contract flag is not true: {name}")
    if len(lock.get("baseFiles", {})) != 3 or len(contract.get("testsAdded", [])) != 3:
        raise RuntimeError("source-lock must pin three base files and three upstream tests")

    patch_text = patch_path.read_text(encoding="utf-8")
    require(patch_text, "WorkerMembers", patch_path.name)
    require(patch_text, "len(req.WorkerMembers) == 0", patch_path.name)
    require(patch_text, "WorkerMembers: req.WorkerMembers", patch_path.name)
    for test_name in contract["testsAdded"]:
        require(patch_text, test_name, patch_path.name)

    for document in ("UPSTREAM-ISSUE.md", "PR-DESCRIPTION.md"):
        if not (HERE / document).is_file():
            raise RuntimeError(f"missing vendored upstream document: {document}")

    return {
        "ok": True,
        "upstream_release": lock["upstream"]["release"],
        "upstream_commit": lock["upstream"]["commit"],
        "base_files_pinned": len(lock["baseFiles"]),
        "tests_pinned": len(contract["testsAdded"]),
        "patch_sha256": lock["patch"]["sha256"],
    }


def verify(source: Path) -> dict:
    source = source.resolve()
    lock = load_lock()
    package = verify_package()
    patch_path = HERE / lock["patch"]["path"]

    for relative, expected in lock["baseFiles"].items():
        actual_path = source / relative
        if not actual_path.is_file():
            raise RuntimeError(f"missing pinned source file: {relative}")
        if sha256(actual_path) != expected:
            raise RuntimeError(f"base file hash mismatch: {relative}")

    commit_verified = False
    try:
        commit_result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        commit_result = None
    if commit_result is not None and commit_result.returncode == 0:
        commit = commit_result.stdout.strip()
        if commit != lock["upstream"]["commit"]:
            raise RuntimeError(f"upstream commit mismatch: {commit}")
        commit_verified = True

    with tempfile.TemporaryDirectory(prefix="proofmesh-agentteams-patch-") as temp_dir:
        temp = Path(temp_dir)
        for relative in lock["baseFiles"]:
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)

        apply_result = subprocess.run(
            ["patch", "--batch", "--forward", "-p1", "-d", str(temp)],
            input=patch_path.read_bytes(),
            capture_output=True,
        )
        if apply_result.returncode != 0:
            detail = (apply_result.stdout + apply_result.stderr).decode("utf-8", errors="replace")
            raise RuntimeError(f"patch did not apply cleanly:\n{detail}")

        types_text = (temp / "hiclaw-controller/internal/server/types.go").read_text(encoding="utf-8")
        handler_text = (temp / "hiclaw-controller/internal/server/resource_handler.go").read_text(encoding="utf-8")
        tests_text = (temp / "hiclaw-controller/internal/server/resource_handler_test.go").read_text(encoding="utf-8")

        require(types_text, 'WorkerMembers []v1beta1.TeamWorkerRef    `json:"workerMembers,omitempty"`', "types.go")
        require(types_text, 'Leader        TeamLeaderRequest          `json:"leader,omitempty"`', "types.go")
        require(handler_text, 'if len(req.WorkerMembers) == 0 && req.Leader.Name == "" {', "resource_handler.go")
        require(handler_text, "WorkerMembers: req.WorkerMembers,", "resource_handler.go")

        # These fragments guard backward compatibility: the legacy requirement,
        # Leader mapping and Worker mapping all remain in the patched source.
        require(handler_text, '"leader.name is required"', "resource_handler.go")
        require(handler_text, "Leader: v1beta1.LeaderSpec{", "resource_handler.go")
        require(handler_text, "for _, tw := range req.Workers {", "resource_handler.go")
        require(handler_text, "team.Spec.Workers = append", "resource_handler.go")

        for test_name in lock["expectedContract"]["testsAdded"]:
            require(tests_text, f"func {test_name}(t *testing.T)", "resource_handler_test.go")

        # A second application must fail, ensuring this verifier cannot silently
        # treat an already-modified or unrelated tree as the pinned base.
        second = subprocess.run(
            ["patch", "--batch", "--forward", "-p1", "-d", str(temp)],
            input=patch_path.read_bytes(),
            capture_output=True,
        )
        if second.returncode == 0:
            raise RuntimeError("patch unexpectedly applied twice")

    return {
        "ok": True,
        "upstream_release": lock["upstream"]["release"],
        "upstream_commit": lock["upstream"]["commit"],
        "upstream_commit_verified": commit_verified,
        "base_files_verified": len(lock["baseFiles"]),
        "patch_sha256": package["patch_sha256"],
        "patch_applied_to_temporary_copy": True,
        "go_test_executed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="AgentTeams v1.2.0-beta.1 source root")
    args = parser.parse_args()
    print(json.dumps(verify(args.source), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
