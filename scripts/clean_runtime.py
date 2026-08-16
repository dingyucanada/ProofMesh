from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_MARKER = ROOT / "pyproject.toml"


def _assert_project_root() -> None:
    text = PROJECT_MARKER.read_text(encoding="utf-8")
    if 'name = "proofmesh"' not in text:
        raise RuntimeError(f"refusing to clean an unrecognized project root: {ROOT}")


def _remove_file(path: Path, removed: list[str]) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink()
        removed.append(str(path.relative_to(ROOT)))


def _remove_dir(path: Path, removed: list[str]) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        removed.append(f"{path.relative_to(ROOT)}/")


def main() -> None:
    _assert_project_root()
    removed: list[str] = []
    runtime_dir = ROOT / "var"
    if runtime_dir.is_dir():
        for pattern in ("**/*.db", "**/*.db-wal", "**/*.db-shm", "**/*.json"):
            for path in runtime_dir.glob(pattern):
                _remove_file(path, removed)
        _remove_dir(runtime_dir / "keys", removed)
        for directory in sorted((path for path in runtime_dir.rglob("*") if path.is_dir()), reverse=True):
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
                removed.append(f"{directory.relative_to(ROOT)}/")

    workflow_artifacts = ROOT / "artifacts" / "workflows"
    _remove_dir(workflow_artifacts, removed)
    operations_workflow_artifacts = ROOT / "artifacts" / "operations-workflows"
    _remove_dir(operations_workflow_artifacts, removed)

    for cache_name in (".pytest_cache", ".ruff_cache"):
        _remove_dir(ROOT / cache_name, removed)
    for cache_dir in sorted(ROOT.rglob("__pycache__"), reverse=True):
        _remove_dir(cache_dir, removed)
    for metadata_file in ROOT.rglob(".DS_Store"):
        _remove_file(metadata_file, removed)
    _remove_file(ROOT / ".coverage", removed)

    # These screenshots belong to the superseded release-governance prototype
    # or to an older 8-identity console.  Keeping them beside the current 9/9
    # refund evidence makes the source tree itself misleading even though the
    # release builder excludes them.
    for stale_screenshot in (
        "demo-waiting-approval.png",
        "demo-final-rollback.png",
        "judge-console-compensated.png",
    ):
        _remove_file(ROOT / "docs" / stale_screenshot, removed)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
    (ROOT / "artifacts" / "operations-workflows").mkdir(parents=True, exist_ok=True)
    print(f"removed {len(removed)} runtime target(s)")
    for item in removed:
        print(f"  {item}")
    print(
        "preserved artifacts/public-benchmark, artifacts/reference, "
        "artifacts/operations-reference and all source materials"
    )


if __name__ == "__main__":
    main()
