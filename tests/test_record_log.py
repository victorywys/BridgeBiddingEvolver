import copy
import io
import json
import contextlib
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bridge_evolve.evaluation.record_log import (
    HEADER, RecordDecoder, RecordEncoder, RecordReader, RecordWriter, agent_board,
    agent_decision, decode_records, encode_records, iter_records, record_statistics, write_single_record,
)


def fixture():
    return {"number": 1, "index": 0, "dealer": "N", "vulnerability": "none",
            "hands": {seat: "AKQ.JT9.KQ32.876" for seat in "NESW"},
            "tables": [{"id": "table1", "auction": [
                {"seat": "N", "call": "1C",
                 "public_context": {"state": "idle", "minimum": 0, "quality": [True], "old": "remove"},
                 "public_context_after": {"state": "natural", "minimum": 11, "quality": [1]},
                 "private_trace": {"rule_id": "long.repeated.rule.identifier",
                                   "features": [{"expression": {"op": "hcp"}, "value": 15}],
                                   "inference": {"hcp_min": 11}},
                 "public_explanation": {"summary": "北家1C开叫，承诺十一点以上大牌点。",
                                        "notes": ["重复说明内容"], "warnings": []}},
                {"seat": "E", "call": "P", "public_context": {"state": "idle", "minimum": 0},
                 "public_context_after": {"state": "idle", "minimum": 0}},
                {"seat": "S", "call": "1H",
                 "public_context": {"state": "natural", "minimum": 11, "quality": [1]},
                 "public_context_after": {"state": "fit", "minimum": 11, "quality": [1], "support": 4},
                 "private_trace": {"rule_id": "long.repeated.rule.identifier",
                                   "features": [{"expression": {"op": "hcp"}, "value": 10}]},
                 "public_explanation": {"summary": "南家1H应叫，承诺四张以上红桃。", "notes": [], "warnings": []}},
            ], "contract": {"level": 4, "strain": "H", "declarer": "S", "doubling": 1},
            "tricks": 10, "score_ns": 420}],
            "audit_result": {"status": "checked", "errors": [], "checks": [{"id": "check", "status": "passed"}]},
            "imp": 0, "nested": [None, False, 0, 1, 1.0, "", [], {}, [0, "not a tag"], {"空": "值"}]}


class CompactRecordTests(unittest.TestCase):
    def test_lossless_roundtrip_uses_per_side_deltas_and_keeps_number_types(self):
        record = fixture()
        encoder, decoder = RecordEncoder(), RecordDecoder()
        row = encoder.encode(record)
        decoded = decoder.decode(json.loads(json.dumps(row)))
        self.assertEqual(decoded, record)
        steps = decoded["tables"][0]["auction"]
        self.assertIs(type(steps[0]["public_context"]["quality"][0]), bool)
        self.assertIs(type(steps[0]["public_context_after"]["quality"][0]), int)
        self.assertNotIn("old", steps[0]["public_context_after"])
        self.assertEqual(steps[2]["public_context"]["minimum"], 11)
        self.assertEqual(len(encoder.expressions), 1)
        second = copy.deepcopy(record)
        second["number"], second["index"] = 2, 1
        self.assertEqual(decoder.decode(encoder.encode(second)), second)

    def test_streaming_reader_legacy_pretty_json_and_random_access_are_compatible(self):
        records = [fixture(), {**fixture(), "number": 2, "index": 1}]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.jsonl"
            with RecordWriter(path) as writer:
                for record in records:
                    writer.write(record)
                    writer.flush()
            self.assertEqual(writer.metadata()["records"], 2)
            self.assertEqual(writer.metadata()["stored_bytes"], path.stat().st_size)
            self.assertEqual(list(iter_records(path)), records)
            with path.open("rb") as source:
                reader = RecordReader(source)
                entries = list(reader.entries(projection="index"))
                for entry, expected in zip(entries, records, strict=True):
                    source.seek(entry.offset)
                    self.assertEqual(reader.selected(source.read(entry.length)), expected)
            legacy = Path(temporary) / "old.jsonl"
            legacy.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.assertEqual(list(iter_records(legacy)), records)
            pretty = Path(temporary) / "old.json"
            pretty.write_text(json.dumps(records[0], indent=2))
            self.assertEqual(list(iter_records(pretty)), records[:1])
            with self.assertRaises(FileExistsError):
                with RecordWriter(path):
                    pass

    def test_projection_omits_bulk_context_but_keeps_search_rules_and_all_calls(self):
        record = fixture()
        reader = RecordReader(io.BytesIO(encode_records(iter([record]))))
        projected = next(reader.entries(projection="index")).record
        steps = projected["tables"][0]["auction"]
        self.assertEqual([step["call"] for step in steps], ["1C", "P", "1H"])
        self.assertNotIn("public_context", steps[0])
        self.assertNotIn("public_context_after", steps[0])
        self.assertEqual(steps[0]["private_trace"], {"rule_id": "long.repeated.rule.identifier"})
        self.assertEqual(steps[0]["public_explanation"], {"summary": record["tables"][0]["auction"][0]["public_explanation"]["summary"]})
        self.assertEqual(projected["audit_result"], {"status": "checked", "errors": []})

    def test_concurrent_selected_expansions_do_not_share_mutable_context(self):
        records = [fixture(), {**fixture(), "number": 2}]
        source = io.BytesIO(encode_records(iter(records)))
        reader = RecordReader(source)
        entries = list(reader.entries())
        lines = [source.getvalue()[entry.offset:entry.offset + entry.length] for entry in entries]
        with ThreadPoolExecutor(max_workers=4) as executor:
            actual = list(executor.map(reader.selected, lines * 10))
        self.assertEqual(actual, records * 10)
        actual[0]["tables"][0]["auction"][0]["private_trace"]["features"][0]["expression"]["op"] = "changed"
        self.assertEqual(reader.selected(lines[0]), records[0])

    def test_missing_null_empty_false_and_removed_keys_are_distinct(self):
        record = {"tables": [{"auction": [
            {"seat": "N", "public_context": {}, "public_context_after": {"value": None}},
            {"seat": "S", "public_context": {"value": False}, "public_context_after": {"value": []}},
            {"seat": "N", "public_context": {"value": {}}, "public_context_after": {}},
        ]}]}
        self.assertEqual(RecordDecoder().decode(RecordEncoder().encode(record)), record)
        runtime = {"shape": (4, 3, 3, 3), "public_context": {"typed": (True,)},
                   "public_context_after": {"typed": (1,)}}
        encoded = json.loads(json.dumps(RecordEncoder().encode(runtime)))
        self.assertEqual(RecordDecoder().decode(encoded), json.loads(json.dumps(runtime)))

    def test_bad_references_tags_headers_and_truncation_fail_explicitly(self):
        bad = [
            [[], [], [], [], [2, -1]], [[], [], [], [], [99]], [[], [], [], [], [0, 0, 1]],
            [["key"], [], [], [[0, 0]], [0, 0, 1, 2]],
            [["key"], [], [], [[0], []], [0, 0, [3, 0, [0, 1], [0]]]],
            [[], [], [], [], [3, True, [], []]], [[], [], [], [], [1]],
        ]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                RecordDecoder().decode(row)
        for contents in (b'{"schema":"bridge-record-log/v99"}\n',
                         json.dumps({**HEADER, "unknown": True}).encode() + b"\n",
                         json.dumps(HEADER).encode() + b"\n[["):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                list(RecordReader(io.BytesIO(contents)).entries())
        decoder = RecordDecoder()
        decoder.decode([["key"], [], [], [[0]], [0, 0, 1]])
        with self.assertRaisesRegex(ValueError, "repeated"):
            decoder.decode([["key"], [], [], [], [0, 0, 1]])

    def test_invalid_non_json_and_nonfinite_values_are_not_silently_encoded(self):
        for record in ([], {"bad": object()}, {"bad": float("nan")}, {"bad": float("inf")}, {1: "not a key"}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                RecordEncoder().encode(record)

    def test_agent_views_and_statistics_do_not_dump_every_context(self):
        record = fixture()
        summary = agent_board(record)
        self.assertEqual(summary["hands"], [record["hands"][seat] for seat in "NESW"])
        self.assertEqual(summary["tables"][0]["auction"][0], ["N", "1C", "long.repeated.rule.identifier"])
        self.assertNotIn("public_context", json.dumps(summary))
        stats = record_statistics(iter([record]))
        self.assertEqual((stats["records"], stats["tables"], stats["decisions"]), (1, 1, 3))
        self.assertEqual(stats["rules"]["long.repeated.rule.identifier"], 2)
        self.assertEqual(stats["contracts"], {"4H": 1})
        selected = agent_decision(record, 1, 3)
        self.assertEqual(selected["hand"], record["hands"]["S"])
        self.assertEqual(selected["prefix"], [["N", "1C"], ["E", "P"]])
        self.assertEqual(selected["decision"], record["tables"][0]["auction"][2])
        self.assertNotIn("dds", selected)
        self.assertNotIn("hands", selected)

    def test_single_compact_json_roundtrip_and_command_line_views(self):
        from bridge_evolve.cli import main
        record = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board.json"
            metadata = write_single_record(path, record)
            self.assertEqual(metadata["stored_bytes"], path.stat().st_size)
            self.assertEqual(list(iter_records(path)), [record])
            self.assertEqual(decode_records(path.read_bytes()), [record])
            output = Path(temporary) / "converted.jsonl"
            cases = [
                (["records", "convert", "--input", str(path), "--output", str(output)], "schema"),
                (["records", "show", "--input", str(output), "--board", "1"], "tables"),
                (["records", "show", "--input", str(output), "--index", "0", "--view", "full"], "nested"),
                (["records", "show", "--input", str(output), "--board", "1", "--view", "decision", "--turn", "3"], "decision"),
                (["records", "stats", "--input", str(output), "--top", "1"], "rules"),
            ]
            for arguments, field in cases:
                target = io.StringIO()
                with self.subTest(arguments=arguments), contextlib.redirect_stdout(target):
                    self.assertEqual(main(arguments), 0)
                self.assertIn(field, json.loads(target.getvalue()))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["records", "convert", "--input", str(path), "--output", str(output)]), 1)
                self.assertEqual(main(["records", "show", "--input", str(output), "--board", "999"]), 1)
                self.assertEqual(main(["records", "show", "--input", str(output), "--board", "1", "--view", "decision"]), 1)

    def test_real_formal_run_compact_scores_auctions_csv_viewer_and_zip_are_consistent(self):
        from bridge_evolve.evaluation.data import generate_dataset
        from bridge_evolve.evaluation.experiment import evaluate
        from test_board_viewer import ROOT, viewer
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = generate_dataset(root / "dataset", count=2, seed=20261103, split="train")
            output = root / "run"
            config = {"experiment_id": "codec-smoke", "dataset": str(dataset),
                      "system_a": str(ROOT / "systems/general-v2.json"), "system_b": str(ROOT / "systems/pass-v2.json"),
                      "dds_cache": str(root / "dds.sqlite"), "output": str(output), "bootstrap_seed": 1, "bootstrap_resamples": 100}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            with contextlib.redirect_stderr(io.StringIO()):
                evaluate(config_path)
            manifest = json.loads((output / "run.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["record_encoding"]["boards.jsonl"]["records"], 2)
            self.assertEqual(manifest["record_encoding"]["auctions.jsonl"]["records"], 4)
            self.assertEqual(len(list(iter_records(output / "boards.jsonl"))), 2)
            self.assertEqual(len(list(iter_records(output / "auctions.jsonl"))), 4)
            bundle = viewer.load_run(output)
            self.assertEqual(bundle["statistics"]["boards"], 2)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as target:
                for path in output.rglob("*"):
                    if path.is_file():
                        target.write(path, "run/" + path.relative_to(output).as_posix())
            restored = viewer.import_file(archive.getvalue(), "formal.zip")
            for before, after in zip(bundle["boards"], restored["boards"], strict=True):
                for key in ("hands", "dealer", "vulnerability", "tables", "dds", "imp", "point_difference"):
                    self.assertEqual(before[key], after[key])
