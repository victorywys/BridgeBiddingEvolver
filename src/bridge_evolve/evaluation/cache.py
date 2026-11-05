"""Exact-seat DDS cache, partitioned by the actual solver provenance."""

import json
import sqlite3
from pathlib import Path

from bridge_evolve.core import Deal
from bridge_evolve.evaluation.data import canonical_hash, deal_hash
from bridge_evolve.oracle import DDTable, DDSAdapter


class DDSCache:
    def __init__(self, path: Path, adapter: DDSAdapter):
        self.adapter = adapter
        self.provenance = adapter.provenance.to_dict()
        if self.provenance.get("oracle_kind") != "dds":
            raise ValueError("Formal DDS cache refuses fixture/mock solver provenance.")
        self.oracle_hash = canonical_hash(self.provenance)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS dd_tables (deal_hash TEXT NOT NULL, oracle_hash TEXT NOT NULL, "
            "table_json TEXT NOT NULL, PRIMARY KEY(deal_hash,oracle_hash))"
        )
        self.connection.commit()
        self.hits = 0
        self.solves = 0

    def solve(self, deal: Deal) -> DDTable:
        key = deal_hash(deal)
        row = self.connection.execute(
            "SELECT table_json FROM dd_tables WHERE deal_hash=? AND oracle_hash=?", (key, self.oracle_hash),
        ).fetchone()
        if row is not None:
            self.hits += 1
            return DDTable.from_dict(json.loads(row[0]))
        table = self.adapter.solve(deal)
        # Validate the serialized object before it becomes a successful cache row.
        encoded = json.dumps(table.to_dict(), sort_keys=True)
        DDTable.from_dict(json.loads(encoded))
        self.connection.execute("INSERT OR IGNORE INTO dd_tables VALUES(?,?,?)", (key, self.oracle_hash, encoded))
        stored = self.connection.execute(
            "SELECT table_json FROM dd_tables WHERE deal_hash=? AND oracle_hash=?", (key, self.oracle_hash),
        ).fetchone()
        if stored is None or DDTable.from_dict(json.loads(stored[0])) != table:
            self.connection.rollback()
            raise ValueError("Concurrent DDS cache entry disagrees with the verified solve.")
        self.connection.commit()
        self.solves += 1
        return table

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
