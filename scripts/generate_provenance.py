#!/usr/bin/env python3
"""Create an unsigned in-toto/SLSA provenance predicate for later CI attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-uri", default=os.getenv("GITHUB_SERVER_URL", "local") + "/" + os.getenv("GITHUB_REPOSITORY", "proofmesh/local"))
    parser.add_argument("--source-digest", default=os.getenv("GITHUB_SHA", "UNVERIFIED-LOCAL-SOURCE"))
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    instant = datetime.fromtimestamp(int(os.getenv("SOURCE_DATE_EPOCH", "1786492800")), tz=timezone.utc).isoformat()
    payload = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": artifact.name, "digest": {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/ProofMesh/release@v1",
                "externalParameters": {"sourceDateEpoch": os.getenv("SOURCE_DATE_EPOCH", "1786492800")},
                "internalParameters": {},
                "resolvedDependencies": [{"uri": args.source_uri, "digest": {"gitCommit": args.source_digest}}],
            },
            "runDetails": {
                "builder": {"id": os.getenv("GITHUB_WORKFLOW_REF", "local-untrusted-builder")},
                "metadata": {"invocationId": os.getenv("GITHUB_RUN_ID", "local"), "startedOn": instant, "finishedOn": instant},
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
