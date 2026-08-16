import sqlite3

from proofmesh.ledger import EvidenceLedger


def test_hash_chain_detects_tampering(tmp_path):
    db = tmp_path / "ledger.db"
    ledger = EvidenceLedger(db)
    ledger.append("run-1", "agent-a", "step.started", {"value": 1})
    ledger.append("run-1", "agent-b", "step.completed", {"value": 2})
    assert ledger.verify("run-1")[0]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET payload_json = ? WHERE seq = 1", ('{"value":999}',))
    assert not ledger.verify("run-1")[0]

