"""Fresh common-deal evaluation, bounded workers and hash-checked resume cache."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import multiprocessing
import random
import sqlite3
import statistics
import sys
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor, wait
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

from bridge_evolve.evaluation.data import canonical_hash, deal_hash, generate_dataset, load_dataset, board_data
from bridge_evolve.evaluation.record_log import RecordWriter, decode_records, encode_records, iter_records
from bridge_evolve.oracle import DDSAdapter
from bridge_evolve.systems import load_system
from bridge_evolve.systems.inputs import parse_board
from bridge_evolve.evolution.config import ROOT, engine_identity, save_json

_worker_engine = None
_worker_systems = None


@lru_cache(maxsize=4)
def frozen_system(directory: str, identity: str):
    path = Path(directory) / f"{identity}.json"
    system = load_system(path)
    if system.content_hash != identity or system.scope != "general":
        raise ValueError("Frozen evolution system identity mismatch.")
    return system


@lru_cache(maxsize=1)
def audit_module():
    spec = importlib.util.spec_from_file_location("evolution_board_viewer", ROOT / "tools/board_viewer.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the trusted board audit engine.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def initialize_worker(project: str, systems: str):
    global _worker_engine, _worker_systems
    _worker_engine = audit_module().BoardAuditEngine(Path(project), {})
    _worker_systems = systems


def evaluate_pair(task: tuple) -> dict:
    if _worker_engine is None or _worker_systems is None:
        raise RuntimeError("Evolution worker is not initialized.")
    candidate, opponent, raw, count = task
    definitions = {identity: frozen_system(_worker_systems, identity) for identity in (candidate, opponent)}
    _worker_engine.systems = {identity: {"system": system} for identity, system in definitions.items()}
    record, _, findings = _worker_engine.batch_board(
        raw, {"system_a": candidate, "system_b": opponent, "count": count}, definitions,
        fresh_dds=raw["index"] < min(5, count),
    )
    for finding in findings:
        table = next(table for table in record["tables"] if table["id"] == finding["table"])
        step = table["auction"][finding["turn"]]
        finding.update(actor=step["seat"], system_hash=step["system_hash"])
    record["audit_result"]["findings"] = findings
    return record


class EvolutionStopped(RuntimeError):
    pass


def column_summary(columns: list[list[float]], *, seed: int, alpha: float, resamples: int) -> list[dict]:
    if (not columns or not columns[0] or any(len(column) != len(columns[0]) for column in columns)
            or not 0 < alpha < .5 or resamples < 100
            or any(type(value) not in (int, float) or not math.isfinite(value) for column in columns for value in column)):
        raise ValueError("Confidence summaries require complete, finite paired columns.")
    count = len(columns[0])
    means = [statistics.mean(column) for column in columns]
    constant = all(all(value == column[0] for value in column) for column in columns)
    samples = [[] for _ in columns]
    if not constant:
        rng = random.Random(seed)
        for _ in range(resamples):
            indices = [rng.randrange(count) for _ in range(count)]
            for column, output in zip(columns, samples, strict=True):
                output.append(sum(column[index] for index in indices) / count)
    return [{"boards": count, "mean": mean,
             "lower": mean if constant else sorted(sample)[math.floor((resamples - 1) * alpha)],
             "alpha": alpha, "resamples": resamples, "seed": seed}
            for mean, sample in zip(means, samples, strict=True)]


def paired_gain(result: dict, candidate: str, parent: str, anchors: list[str], *, seed: int, alpha: float, resamples: int) -> dict:
    columns = [[new - old for new, old in zip(result["scores"][candidate][anchor], result["scores"][parent][anchor], strict=True)]
               for anchor in anchors]
    delta = [sum(values) / len(anchors) for values in zip(*columns, strict=True)]
    return {**column_summary([delta], seed=seed, alpha=alpha, resamples=resamples)[0],
            "opponent_deltas": [statistics.mean(column) for column in columns]}


class Evaluator:
    def __init__(self, directory: Path, config: dict, state: dict, checkpoint, stop):
        self.directory, self.config, self.state = directory, config, state
        self.checkpoint, self.stop = checkpoint, stop
        self.connection = sqlite3.connect(directory / "cache.sqlite")
        self.connection.execute("CREATE TABLE IF NOT EXISTS pairs (key TEXT PRIMARY KEY, body BLOB NOT NULL, sha256 TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS families (family TEXT PRIMARY KEY, round_id TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS usage (name TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        self.connection.execute("INSERT OR IGNORE INTO usage VALUES ('pairs',?)", (state["evaluated_pairs"],))
        self.connection.commit()
        state["evaluated_pairs"] = self.connection.execute("SELECT count FROM usage WHERE name='pairs'").fetchone()[0]
        oracle = DDSAdapter().provenance.to_dict()
        if "oracle" in state and state["oracle"] != oracle:
            self.connection.close()
            raise ValueError("The frozen DDS provenance changed; do not mix solver versions.")
        state["oracle"] = oracle
        self.oracle_hash = canonical_hash(oracle)

    def close(self):
        self.connection.close()

    def reserve_pair(self):
        reserved = self.connection.execute(
            "UPDATE usage SET count=count+1 WHERE name='pairs' AND count < ?", (self.config["max_pairs"],))
        self.connection.commit()
        if reserved.rowcount != 1:
            raise EvolutionStopped("Paired evaluation budget exhausted.")
        self.state["evaluated_pairs"] += 1

    def _seed(self, identity: str) -> int:
        known = {round["seed"] for round in self.state["rounds"].values()}
        if self.config["random_seed"] is None:
            value = random.SystemRandom().randrange(2**53)
        else:
            value = int(canonical_hash({"seed": self.config["random_seed"], "round": identity})[:13], 16)
        while value in known:
            value = (value + 1) % 2**53
        return value

    def _round_data(self, identity: str, systems: list[str], count: int) -> tuple[dict, list]:
        saved = self.state["rounds"].get(identity)
        if saved is None:
            saved = {"id": identity, "systems": systems, "boards": count, "seed": self._seed(identity), "status": "preparing"}
            self.state["rounds"][identity] = saved
            self.checkpoint()
        if saved["systems"] != systems or saved["boards"] != count:
            raise ValueError("Resume round differs from its frozen candidates or board count.")
        if "dataset" not in saved:
            parent = self.directory / "rounds" / identity
            parent.mkdir(parents=True, exist_ok=True)
            attempt = 1
            target = parent / f"draw-{attempt}"
            while target.exists() and not (target / "manifest.json").is_file():
                attempt += 1
                target = parent / f"draw-{attempt}"
            manifest = target / "manifest.json" if (target / "manifest.json").is_file() else generate_dataset(
                target, count=count, seed=saved["seed"], split="validation",
            )
            saved["dataset"] = str(manifest.relative_to(self.directory))
            self.checkpoint()
        manifest, boards = load_dataset(self.directory / saved["dataset"])
        if manifest["count"] != count or manifest["seed"] != saved["seed"]:
            raise ValueError("Fresh round dataset identity mismatch.")
        for board in boards:
            previous = self.connection.execute("SELECT round_id FROM families WHERE family=?", (board.family_id,)).fetchone()
            if previous is not None and previous[0] != identity:
                raise ValueError("A supposedly fresh round overlaps another round's base family.")
            self.connection.execute("INSERT OR IGNORE INTO families VALUES (?,?)", (board.family_id, identity))
        self.connection.commit()
        raws = [{"index": board.index, "family_id": board.family_id, "board": board_data(board.board)} for board in boards]
        return saved, raws

    def evaluate(self, identity: str, systems: list[str], count: int) -> dict:
        self.stop()
        systems = list(dict.fromkeys(systems))
        saved, boards = self._round_data(identity, systems, count)
        parent = self.directory / "rounds" / identity
        summary_path = parent / "summary.json"
        if saved.get("status") == "checked":
            contents = summary_path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
                raise ValueError("Cached round summary hash mismatch.")
            result = json.loads(contents)
            for name, digest in result["log_sha256"].items():
                if hashlib.sha256((self.directory / name).read_bytes()).hexdigest() != digest:
                    raise ValueError("Cached evolution log hash mismatch.")
            return result
        if engine_identity() != self.state["engine_hash"]:
            raise ValueError("Trusted evaluator source changed during evolution.")
        for name, digest in self.state["system_files"].items():
            if hashlib.sha256((self.directory / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Frozen candidate system file changed.")
        anchors = [baseline["hash"] for baseline in self.config["baselines"]]
        saved["status"] = "running"
        self.state["phase"] = identity
        self.checkpoint()
        scores = {candidate: {anchor: [] for anchor in anchors} for candidate in systems}
        failures = {candidate: [] for candidate in systems}
        logs, hashes = {}, {}
        pending = deque()
        completed = 0
        attempts = 1
        while (parent / f"logs-{attempts}").exists():
            attempts += 1
        output = parent / f"logs-{attempts}"
        output.mkdir()
        executor = ProcessPoolExecutor(max_workers=self.config["workers"], mp_context=multiprocessing.get_context("spawn"),
                                       initializer=initialize_worker,
                                       initargs=(self.config["project"], str(self.directory / "systems")))
        try:
            with ExitStack() as stack:
                writers = {}
                for candidate in systems:
                    for anchor in anchors:
                        path = output / f"{candidate[:16]}-{anchor[:16]}.jsonl"
                        writers[(candidate, anchor)] = stack.enter_context(RecordWriter(path))
                        logs[f"{candidate}/{anchor}"] = str(path.relative_to(self.directory))
                tasks = iter((candidate, anchor, raw, count) for candidate in systems for anchor in anchors for raw in boards)

                def fill():
                    while len(pending) < 2 * self.config["workers"]:
                        task = next(tasks, None)
                        if task is None:
                            return
                        self.stop()
                        candidate, opponent, raw, _ = task
                        board = parse_board(raw["board"])
                        key = canonical_hash({"candidate": candidate, "opponent": opponent, "deal": deal_hash(board.deal),
                                              "dealer": board.dealer.value, "vulnerability": board.vulnerability.value,
                                              "engine": self.state["engine_hash"], "oracle": self.oracle_hash})
                        cached = self.connection.execute("SELECT body,sha256 FROM pairs WHERE key=?", (key,)).fetchone()
                        if cached is not None:
                            if hashlib.sha256(cached[0]).hexdigest() != cached[1]:
                                raise ValueError("Evolution score cache hash mismatch.")
                            pending.append((task, key, decode_records(zlib.decompress(cached[0]))[0]))
                        else:
                            self.reserve_pair()
                            pending.append((task, key, executor.submit(evaluate_pair, task)))

                fill()
                while pending:
                    self.stop()
                    task, key, value = pending[0]
                    if not isinstance(value, dict):
                        while not value.done():
                            self.stop()
                            wait((value,), timeout=.2)
                        record = value.result()
                        if record.get("dds") and canonical_hash(record["dds"]["oracle"]) != self.oracle_hash:
                            raise RuntimeError("Worker DDS provenance differs from the frozen evaluator.")
                        if record["audit_result"]["status"] != "failed":
                            body = zlib.compress(encode_records(iter([record])), level=1)
                            self.connection.execute("INSERT OR IGNORE INTO pairs VALUES (?,?,?)",
                                                    (key, body, hashlib.sha256(body).hexdigest()))
                            self.connection.commit()
                    else:
                        record = value
                    pending.popleft()
                    candidate, anchor, raw, _ = task
                    record.update(source=f"evolution:{self.directory.name}:{identity}", family_id=raw["family_id"],
                                  id=canonical_hash({"round": identity, "pair": key})[:24])
                    writers[(candidate, anchor)].write(record)
                    scores[candidate][anchor].append(record["imp"])
                    for error in record.get("audit_result", {}).get("errors", []):
                        failures[candidate].append({"board": raw["index"] + 1, **error})
                    completed += 1
                    saved["completed_pairs"] = completed
                    if completed % 16 == 0:
                        self.checkpoint()
                    fill()
            for name in logs.values():
                hashes[name] = hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            status = {}
            for candidate in systems:
                blocked = any(error.get("system_hash") in anchors or error.get("system_hash") is None
                              or error["stage"] in ("dds", "score", "execution") for error in failures[candidate])
                complete = all(len(values) == count and all(type(value) is int for value in values)
                               for values in scores[candidate].values())
                status[candidate] = "blocked" if blocked else "invalid" if failures[candidate] or not complete else "checked"
            if engine_identity() != self.state["engine_hash"]:
                raise ValueError("Trusted evaluator changed before finalizing a round.")
            means = {candidate: {anchor: statistics.mean(values) for anchor, values in outcomes.items()}
                     for candidate, outcomes in scores.items() if status[candidate] == "checked"}
            result = {"schema": "bridge-evolution-round/v1", "id": identity, "boards": count, "seed": saved["seed"],
                      "systems": systems, "anchors": anchors, "scores": scores, "status": status, "errors": failures,
                      "means": means, "objective": {candidate: statistics.mean(values.values()) for candidate, values in means.items()},
                      "logs": logs, "log_sha256": hashes, "oracle_hash": self.oracle_hash}
            save_json(summary_path, result)
            saved.update(status="blocked" if "blocked" in status.values() else "checked",
                         summary_sha256=hashlib.sha256(summary_path.read_bytes()).hexdigest())
            self.checkpoint()
            return result
        finally:
            for _, _, future in pending:
                if not isinstance(future, dict):
                    future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def records(self, result: dict, candidate: str):
        for anchor in result["anchors"]:
            yield from iter_records(self.directory / result["logs"][f"{candidate}/{anchor}"])


def comparison_changes(directory: Path, result: dict, candidate: str, parent: str) -> dict:
    families, auctions, scores = set(), 0, 0
    for anchor in result["anchors"]:
        newer = iter_records(directory / result["logs"][f"{candidate}/{anchor}"])
        older = iter_records(directory / result["logs"][f"{parent}/{anchor}"])
        for new, old in zip(newer, older, strict=True):
            if new["family_id"] != old["family_id"]:
                raise ValueError("Research comparison logs do not share the same family order.")
            changed = any([step["call"] for step in a["auction"]] != [step["call"] for step in b["auction"]]
                          for a, b in zip(new["tables"], old["tables"], strict=True))
            auctions += changed
            scores += new["imp"] != old["imp"]
            if changed:
                families.add(new["family_id"])
    return {"changed_families": len(families), "changed_pair_auctions": auctions, "changed_pair_scores": scores}
