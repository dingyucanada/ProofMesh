#!/usr/bin/env python3
"""Build the narrowly scoped, read-only GitHub Pages artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

COPY_TREES = {
    ROOT / "demo": Path("."),
    ROOT / "artifacts/reference": Path("evidence/reference"),
}

COPY_FILES = {
    ROOT / "artifacts/public-benchmark/authorization-contract-replay.json": Path(
        "evidence/public-benchmark/authorization-contract-replay.json"
    ),
    ROOT / "artifacts/synthetic-http-shadow/report.json": Path(
        "evidence/synthetic-http-shadow/report.json"
    ),
    ROOT / "agentteams/runtime-evidence/control-plane.json": Path(
        "evidence/agentteams/control-plane.json"
    ),
    ROOT / "agentteams/runtime-evidence/teamharness-direct.json": Path(
        "evidence/agentteams/teamharness-direct.json"
    ),
}

FORBIDDEN_NAMES = {".env", ".DS_Store"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".ed25519",
    ".key",
    ".pem",
    ".pyc",
    ".sqlite",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in public demo: {path}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def build(output: Path) -> dict:
    output = output.resolve()
    if output in {Path("/"), ROOT, ROOT.parent}:
        raise ValueError(f"unsafe output directory: {output}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for source, relative_destination in COPY_TREES.items():
        _copy_tree(source, output / relative_destination)
    for source, relative_destination in COPY_FILES.items():
        target = output / relative_destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    files = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in public demo: {path}")
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"sensitive runtime file in public demo: {path}")
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    manifest = {
        "schema_version": "proofmesh.public-demo-manifest/v1",
        "mode": "READ_ONLY_EVIDENCE_REPLAY",
        "live_backend": False,
        "model_driven": False,
        "customer_data": False,
        "file_count": len(files),
        "files": files,
    }
    (output / "PUBLIC_DEMO_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    manifest = build(args.output)
    print(
        f"built {manifest['file_count']} read-only files at "
        f"{args.output.resolve()} (live_backend=false, model_driven=false)"
    )


if __name__ == "__main__":
    main()
