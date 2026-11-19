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
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.oracle import DDSAdapter
from bridge_evolve.systems import load_system
from bridge_evolve.systems.inputs import parse_board
from bridge_evolve.evolution.config import ROOT, engine_identity, save_json
from bridge_evolve.evolution.faults import adjudicate_baseline

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


def evaluate_pair(task: tuple, baseline_fault_policy="strict") -> dict:
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
    return adjudicate_baseline(record, candidate, opponent, baseline_fault_policy)


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
        self.incident_callback = None
        self.connection = sqlite3.connect(directory / "cache.sqlite")
        self.connection.execute("CREATE TABLE IF NOT EXISTS pairs (key TEXT PRIMARY KEY, body BLOB NOT NULL, sha256 TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS families (family TEXT PRIMARY KEY, round_id TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS usage (name TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS pair_reservations (key TEXT PRIMARY KEY, evaluation TEXT)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS evaluation_holds (id TEXT PRIMARY KEY, total INTEGER NOT NULL, remaining INTEGER NOT NULL)")
        self.connection.execute("INSERT OR IGNORE INTO usage VALUES ('pairs',?)", (state["evaluated_pairs"],))
        self.connection.commit()
        state["evaluated_pairs"] = self.connection.execute("SELECT count FROM usage WHERE name='pairs'").fetchone()[0]
        oracle = DDSAdapter().provenance.to_dict()
        if "oracle" in state and state["oracle"] != oracle:
            self.connection.close()
            raise ValueError("The frozen DDS provenance changed; do not mix solver versions.")
        state["oracle"] = oracle
        self.oracle_hash = canonical_hash(oracle)
        self.active_reservation = None

    def close(self):
        self.connection.close()

    def reserve_evaluation(self, identity, pairs):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            known = self.connection.execute("SELECT total FROM evaluation_holds WHERE id=?", (identity,)).fetchone()
            if known is not None:
                if known[0] != pairs:
                    raise ValueError("Evaluation reservation differs from its frozen allocation.")
                return
            used = self.connection.execute("SELECT count FROM usage WHERE name='pairs'").fetchone()[0]
            held = self.connection.execute("SELECT COALESCE(SUM(remaining),0) FROM evaluation_holds").fetchone()[0]
            if used + held + pairs > self.config["max_pairs"]:
                raise EvolutionStopped("Insufficient unreserved paired budget for candidate evaluation.")
            self.connection.execute("INSERT INTO evaluation_holds VALUES (?,?,?)", (identity, pairs, pairs))

    def release_evaluation(self, identity):
        with self.connection:
            self.connection.execute("UPDATE evaluation_holds SET remaining=0 WHERE id=?", (identity,))

    def remaining_evaluation_pairs(self, identity):
        row = self.connection.execute("SELECT remaining FROM evaluation_holds WHERE id=?", (identity,)).fetchone()
        return row[0] if row is not None else 0

    def reserve_pair(self, key=None):
        key = key or "diagnostic-" + str(random.SystemRandom().getrandbits(128))
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute("SELECT 1 FROM pair_reservations WHERE key=?", (key,)).fetchone() is not None:
                return False
            used = self.connection.execute("SELECT count FROM usage WHERE name='pairs'").fetchone()[0]
            held = self.connection.execute("SELECT COALESCE(SUM(remaining),0) FROM evaluation_holds").fetchone()[0]
            if self.active_reservation is not None:
                released = self.connection.execute(
                    "UPDATE evaluation_holds SET remaining=remaining-1 WHERE id=? AND remaining>0",
                    (self.active_reservation,))
                if released.rowcount != 1:
                    raise EvolutionStopped("The candidate's reserved paired evaluation budget is exhausted.")
            elif used + held >= self.config["max_pairs"]:
                raise EvolutionStopped("Paired evaluation budget exhausted; queued candidate reservations are protected.")
            if used >= self.config["max_pairs"]:
                raise EvolutionStopped("Paired evaluation budget exhausted.")
            self.connection.execute("INSERT INTO pair_reservations VALUES (?,?)", (key, self.active_reservation))
            self.connection.execute("UPDATE usage SET count=count+1 WHERE name='pairs'")
        self.state["evaluated_pairs"] = used + 1
        return True

    def _seed(self, identity: str) -> int:
        known = {round["seed"] for round in self.state["rounds"].values()}
        if self.config["random_seed"] is None:
            value = random.SystemRandom().randrange(2**53)
        else:
            value = int(canonical_hash({"seed": self.config["random_seed"], "round": identity})[:13], 16)
        while value in known:
            value = (value + 1) % 2**53
        return value

    def _round_data(self, identity: str, systems: list[str], count: int, *, sampling=None) -> tuple[dict, list]:
        saved = self.state["rounds"].get(identity)
        if saved is None:
            saved = {"id": identity, "systems": systems, "boards": count, "seed": self._seed(identity), "status": "preparing",
                     "baseline_fault_policy": self.config.get("baseline_fault_policy", "strict")}
            self.state["rounds"][identity] = saved
            self.checkpoint()
        if saved["systems"] != systems or saved["boards"] != count:
            raise ValueError("Resume round differs from its frozen candidates or board count.")
        if saved.get("baseline_fault_policy", "strict") != self.config.get("baseline_fault_policy", "strict"):
            raise ValueError("Resume round differs from its frozen baseline scoring policy.")
        sampling = self.config.get("development_sampling") if sampling is None else sampling
        if (saved.get("sampling_hash") is not None) != (sampling is not None) and "dataset" in saved:
            raise ValueError("Resume round differs from its frozen sampling distribution.")
        if sampling is not None:
            sampling_hash = canonical_hash(sampling)
            if saved.get("sampling_hash", sampling_hash) != sampling_hash:
                raise ValueError("Resume round differs from its frozen conditional cohort.")
            saved.update(sampling_hash=sampling_hash, evaluation_scope="conditional")
        if "dataset" not in saved:
            parent = self.directory / "rounds" / identity
            parent.mkdir(parents=True, exist_ok=True)
            attempt = 1
            target = parent / f"draw-{attempt}"
            while target.exists() and not (target / "manifest.json").is_file():
                attempt += 1
                target = parent / f"draw-{attempt}"
            if (target / "manifest.json").is_file():
                manifest = target / "manifest.json"
            elif sampling is None:
                manifest = generate_dataset(target, count=count, seed=saved["seed"], split="validation")
            else:
                from bridge_evolve.evolution.sampling import conditional_source, descriptor
                description = {**descriptor(sampling), "draw": {}}
                manifest = generate_dataset(target, count=count, seed=saved["seed"], split="validation",
                    sampling=description, source=conditional_source(sampling, count, saved["seed"], self.stop,
                                                                   description["draw"], self.system))
            saved["dataset"] = str(manifest.relative_to(self.directory))
            self.checkpoint()
        manifest, boards = load_dataset(self.directory / saved["dataset"])
        if manifest["count"] != count or manifest["seed"] != saved["seed"]:
            raise ValueError("Fresh round dataset identity mismatch.")
        if sampling is not None:
            from bridge_evolve.evolution.sampling import check_cohort, descriptor
            expected = descriptor(sampling)
            if not isinstance(manifest["sampling"], dict) or any(
                    manifest["sampling"].get(key) != value for key, value in expected.items()):
                raise ValueError("The dataset does not identify this frozen conditional cohort.")
            for index, board in enumerate(boards):
                check_cohort(sampling, {"board": board_data(board.board)}, index, self.system)
        for board in boards:
            previous = self.connection.execute("SELECT round_id FROM families WHERE family=?", (board.family_id,)).fetchone()
            if previous is not None and previous[0] != identity:
                raise ValueError("A supposedly fresh round overlaps another round's base family.")
            self.connection.execute("INSERT OR IGNORE INTO families VALUES (?,?)", (board.family_id, identity))
        self.connection.commit()
        raws = [{"index": board.index, "family_id": board.family_id, "board": board_data(board.board)} for board in boards]
        return saved, raws

    def system(self, identity):
        if f"systems/{identity}.json" not in self.state["system_files"]:
            raise ValueError("Auction sampling may use only registered frozen systems.")
        return frozen_system(str(self.directory / "systems"), identity)

    def evaluate(self, identity: str, systems: list[str], count: int, *, sampling=None) -> dict:
        self.stop()
        systems = list(dict.fromkeys(systems))
        saved, boards = self._round_data(identity, systems, count, sampling=sampling)
        parent = self.directory / "rounds" / identity
        summary_path = parent / "summary.json"
        if saved.get("status") in ("checked", "blocked"):
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
        components = {candidate: {anchor: {"played_imp_total": 0, "forfeit_imp_total": 0,
                                          "played_pairs": 0, "forfeit_pairs": 0} for anchor in anchors}
                      for candidate in systems}
        coverage = {candidate: {"opening_pass": set(), "partner_response": set()} for candidate in systems}
        from bridge_evolve.evolution.goals import protocol_goal
        goal = protocol_goal(self.config.get("protocol_goal"))
        failures = {candidate: [] for candidate in systems}
        logs, hashes = {}, {}
        pending = deque()
        completed = 0
        resources = {"computed_pairs": 0, "cached_pairs": 0, "failed_pair_records": 0,
                     "dds_cache_hits": 0, "dds_cache_solves": 0, "dds_fresh_crosschecks": 0,
                     "dds_unreported_pair_records": 0}
        original_units = self.state["evaluated_pairs"]
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
                                              "engine": self.state["engine_hash"], "oracle": self.oracle_hash,
                                              **({"baseline_fault_policy": self.config["baseline_fault_policy"]}
                                                 if self.config.get("baseline_fault_policy", "strict") != "strict" else {})})
                        cached = self.connection.execute("SELECT body,sha256 FROM pairs WHERE key=?", (key,)).fetchone()
                        if cached is not None:
                            if hashlib.sha256(cached[0]).hexdigest() != cached[1]:
                                raise ValueError("Evolution score cache hash mismatch.")
                            pending.append((task, key, decode_records(zlib.decompress(cached[0]))[0]))
                        else:
                            self.reserve_pair(key)
                            pending.append((task, key, executor.submit(evaluate_pair, task,
                                            self.config.get("baseline_fault_policy", "strict"))))

                fill()
                while pending:
                    self.stop()
                    task, key, value = pending[0]
                    if not isinstance(value, dict):
                        while not value.done():
                            self.stop()
                            wait((value,), timeout=.2)
                        record = value.result()
                        resources["computed_pairs"] += 1
                        verification = record.get("audit_result", {}).get("dds_verification", {})
                        if isinstance(verification, dict) and all(
                                type(verification.get(key)) is int for key in ("cache_hits", "cache_solves")):
                            resources["dds_cache_hits"] += verification["cache_hits"]
                            resources["dds_cache_solves"] += verification["cache_solves"]
                            resources["dds_fresh_crosschecks"] += bool(verification.get("fresh_crosscheck"))
                        else:
                            resources["dds_unreported_pair_records"] += 1
                        if record.get("dds") and canonical_hash(record["dds"]["oracle"]) != self.oracle_hash:
                            raise RuntimeError("Worker DDS provenance differs from the frozen evaluator.")
                        if record["audit_result"]["status"] != "failed":
                            body = zlib.compress(encode_records(iter([record])), level=1)
                            self.connection.execute("INSERT OR IGNORE INTO pairs VALUES (?,?,?)",
                                                    (key, body, hashlib.sha256(body).hexdigest()))
                            self.connection.commit()
                    else:
                        record = value
                        resources["cached_pairs"] += 1
                    pending.popleft()
                    candidate, anchor, raw, _ = task
                    record.update(source=f"evolution:{self.directory.name}:{identity}", family_id=raw["family_id"],
                                  id=canonical_hash({"round": identity, "pair": key})[:24])
                    if self.incident_callback is not None and (
                            record.get("adjudication") or record.get("audit_result", {}).get("errors")):
                        self.incident_callback(record, candidate, anchor, identity)
                    if goal is not None:
                        for table in record["tables"]:
                            for side, definition in table["systems"].items():
                                if definition["hash"] == candidate:
                                    event = goal.opening_pass_event(table, side)
                                    if event["opening_pass"]:
                                        coverage[candidate]["opening_pass"].add(raw["family_id"])
                                    if goal.natural_witness(table, side):
                                        coverage[candidate]["partner_response"].add(raw["family_id"])
                    writers[(candidate, anchor)].write(record)
                    scores[candidate][anchor].append(record["imp"])
                    if type(record["imp"]) is int:
                        component = "forfeit" if record.get("adjudication") else "played"
                        components[candidate][anchor][component + "_pairs"] += 1
                        components[candidate][anchor][component + "_imp_total"] += record["imp"]
                    resources["failed_pair_records"] += bool(record.get("audit_result", {}).get("errors"))
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
                      "logs": logs, "log_sha256": hashes, "oracle_hash": self.oracle_hash,
                      "resources": {**resources, "new_pair_units": self.state["evaluated_pairs"] - original_units,
                          "scope": "Actual work in the finalized execution, not original DDS counters on reused pair cache records."}}
            result.update(baseline_fault_policy=self.config.get("baseline_fault_policy", "strict"),
                          score_components=components)
            cohort = self.config.get("development_sampling") if sampling is None else sampling
            if cohort is not None:
                result.update(evaluation_scope="conditional", development_sampling=cohort,
                              sampling_draw=load_dataset(self.directory / saved["dataset"])[0]["sampling"]["draw"])
            if goal is not None:
                result["protocol_coverage"] = {
                    candidate: {"opening_pass_families": len(counts["opening_pass"]),
                                "natural_partner_response_families": len(counts["partner_response"])}
                    for candidate, counts in coverage.items()}
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

    def artifact(self, path, value):
        save_json(path, value)
        self.state["artifacts"][path.relative_to(self.directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.checkpoint()

    def milestone(self, identity, kind, **fields):
        self.state.setdefault("milestones", {}).setdefault(identity, {
            "id": identity, "kind": kind, "timestamp": timestamp(), **fields})

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
