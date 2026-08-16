from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agentteams" / "packages"
TARGET = ROOT / "agentteams" / "dist"

ROLE_SKILL = {
    "case-orchestrator": "orchestrate-refund-case",
    "ticket-intake": "normalize-refund-case",
    "context-investigator": "collect-refund-context",
    "risk-policy-sentinel": "evaluate-refund-policy",
    "action-executor": "execute-refund-saga",
    "outcome-verifier": "verify-refund-outcome",
    "case-memory-curator": "curate-refund-memory",
}

# ZIP metadata is fixed so identical source files produce byte-identical packages.
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def _manifest(role: str) -> dict:
    path = SOURCE / role / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{role}: invalid manifest.json: {exc}") from exc
    worker = value.get("worker")
    if value.get("type") != "worker" or value.get("version") != 1 or not isinstance(worker, dict):
        raise ValueError(f"{role}: manifest must be an official Worker import manifest")
    expected = {
        "suggested_name": role,
        "model": "qwen3.5-plus",
        "runtime": "qwenpaw",
    }
    for key, expected_value in expected.items():
        if worker.get(key) != expected_value:
            raise ValueError(f"{role}: manifest worker.{key} must be {expected_value!r}")
    return value


def _source_files(role: str, skill: str) -> list[tuple[Path, PurePosixPath]]:
    package_root = SOURCE / role
    _manifest(role)
    required = [
        package_root / "manifest.json",
        package_root / "config" / "SOUL.md",
        package_root / "config" / "AGENTS.md",
    ]
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{role}: required package file is missing or empty: {path}")
    if (package_root / "AGENTS.md").exists() or (package_root / "SOUL.md").exists():
        raise ValueError(f"{role}: AGENTS.md and SOUL.md must be under config/")

    skill_root = ROOT / "skills" / skill
    if not (skill_root / "SKILL.md").is_file():
        raise ValueError(f"{role}: missing custom skill {skill}")

    files: list[tuple[Path, PurePosixPath]] = []
    for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
        files.append((path, PurePosixPath(path.relative_to(package_root).as_posix())))
    for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
        archive_path = PurePosixPath("skills") / skill / PurePosixPath(path.relative_to(skill_root).as_posix())
        files.append((path, archive_path))
    return files


def _write_deterministic_zip(output: Path, files: list[tuple[Path, PurePosixPath]]) -> None:
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for source, archive_path in files:
            info = ZipInfo(str(archive_path), date_time=ZIP_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, source.read_bytes())


def package_all() -> dict[str, str]:
    source_roles = {path.name for path in SOURCE.iterdir() if path.is_dir() and any(path.iterdir())}
    expected_roles = set(ROLE_SKILL)
    if source_roles != expected_roles:
        raise ValueError(
            "Worker package source directories must match the seven refund roles; "
            f"missing={sorted(expected_roles - source_roles)}, extra={sorted(source_roles - expected_roles)}"
        )

    TARGET.mkdir(parents=True, exist_ok=True)
    for stale in TARGET.glob("*.zip"):
        stale.unlink()

    digests: dict[str, str] = {}
    for role, skill in ROLE_SKILL.items():
        output = TARGET / f"{role}.zip"
        _write_deterministic_zip(output, _source_files(role, skill))
        digests[output.name] = hashlib.sha256(output.read_bytes()).hexdigest()

    checksum_lines = [f"{digest}  {name}" for name, digest in sorted(digests.items())]
    (TARGET / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return digests


def main() -> None:
    digests = package_all()
    for name, digest in sorted(digests.items()):
        print(f"agentteams/dist/{name}  sha256={digest}")


if __name__ == "__main__":
    main()
