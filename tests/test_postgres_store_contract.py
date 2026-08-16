from pathlib import Path

import pytest

from proofmesh.postgres_store import PostgresGatewayStore


def test_postgres_store_rejects_non_postgres_dsn():
    with pytest.raises(ValueError, match="postgresql"):
        PostgresGatewayStore("sqlite:///tmp.db")


def test_postgres_adapter_contains_atomic_locking_and_fencing_contract():
    source = (Path(__file__).resolve().parents[1] / "src/proofmesh/postgres_store.py").read_text(encoding="utf-8")
    assert "FOR UPDATE" in source
    assert "pg_advisory_xact_lock" in source
    assert "ON CONFLICT(issuer, jti) DO UPDATE" in source
    assert "generation = %s" in source
    assert "operation_fence_lost" in source
    assert "UNKNOWN" in source
