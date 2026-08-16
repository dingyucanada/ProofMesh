#!/usr/bin/env python3
"""Generate a deterministic CycloneDX SBOM without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_sbom(root: Path) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    components = []
    runtime_requirements = list(project["dependencies"])
    runtime_requirements.extend(project.get("optional-dependencies", {}).get("production", []))
    for requirement in runtime_requirements:
        normalized = requirement.replace(" ", "")
        name = normalized.split("[")[0].split("==")[0].split(">=")[0].split("<")[0]
        version = normalized.split("==", 1)[1].split(",", 1)[0] if "==" in normalized else None
        component = {
            "type": "library",
            "name": name,
            "purl": f"pkg:pypi/{name.lower().replace('_', '-')}" + (f"@{version}" if version else ""),
        }
        if version:
            component["version"] = version
        components.append(component)
    source_files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and not any(part in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "var"} for part in path.relative_to(root).parts)
        and path.name not in {"sbom.cdx.json", "RELEASE_MANIFEST.json"}
        and path.suffix not in {".pyc", ".pyo", ".db", ".sqlite", ".ed25519"}
        and path.relative_to(root).parts[:2]
        not in {("artifacts", "workflows"), ("artifacts", "operations-workflows")}
        and not (path.relative_to(root).parts and path.relative_to(root).parts[0] == "artifacts")
        and (
            path.relative_to(root).parts[0]
            in {".github", "src", "skills", "scripts", "deploy", "agentteams"}
            or path.relative_to(root)
            in {
                Path("Dockerfile"),
                Path("Makefile"),
                Path("docker-compose.yml"),
                Path("docker-compose.production.yml"),
                Path("pyproject.toml"),
                Path("requirements.txt"),
                Path("requirements-ci.lock"),
                Path("requirements-production.txt"),
                Path("requirements-production.lock"),
            }
        )
    ]
    source_manifest = [
        {"path": str(path.relative_to(root)), "sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in source_files
    ]
    source_digest = hashlib.sha256(
        json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{source_digest[:8]}-{source_digest[8:12]}-{source_digest[12:16]}-{source_digest[16:20]}-{source_digest[20:32]}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "properties": [
                    {"name": "proofmesh:source-manifest-sha256", "value": source_digest},
                    {"name": "proofmesh:source-file-count", "value": str(len(source_manifest))},
                ],
            }
        },
        "components": sorted(components, key=lambda item: item["name"].lower()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "sbom.cdx.json")
    args = parser.parse_args()
    payload = build_sbom(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
