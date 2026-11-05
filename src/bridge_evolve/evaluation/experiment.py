"""Real-DDS two-table, fixed-system comparison with a complete run manifest."""

from __future__ import annotations

import hashlib
import csv
import json
import shutil
import sqlite3
import time
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bridge_evolve.core import Contract, Seat, Strain, score_ns, signed_imp
from bridge_evolve.evaluation.cache import DDSCache
from bridge_evolve.evaluation.data import board_data, canonical_hash, deal_hash, load_dataset
from bridge_evolve.evaluation.statistics import paired_summary
from bridge_evolve.evaluation.record_log import RecordWriter, iter_records
from bridge_evolve.oracle import DDTable, DDSAdapter
from bridge_evolve.systems import load_system, render_system
from bridge_evolve.systems.inputs import Board, run_auction
from bridge_evolve.systems.language import integer, load_json, object_fields


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_snapshot() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


def table_score(result: dict, board: Board, table: DDTable) -> tuple[int, int | None]:
    contract = result["contract"]
    if contract == {"passed_out": True}:
        return 0, None
    fields = object_fields(contract, {"level", "strain", "declarer", "doubling"})
    final = Contract(integer(fields["level"]), Strain(fields["strain"]), Seat(fields["declarer"]), integer(fields["doubling"]))
    tricks = table.tricks(final.declarer, final.strain)
    return score_ns(final, tricks, board.vulnerability), tricks


def paired_board(a, b, board: Board, table: DDTable) -> dict:
    first = run_auction(a, b, board, compact=True, trace=True)
    second = run_auction(b, a, board, compact=True, trace=True)
    s1, tricks1 = table_score(first, board, table)
    s2, tricks2 = table_score(second, board, table)
    return {
        "table1": {"contract": first["contract"], "tricks": tricks1, "score_ns": s1, "auction": first["auction"],
               "partnership_contexts": first.get("partnership_contexts", {})},
        "table2": {"contract": second["contract"], "tricks": tricks2, "score_ns": s2, "auction": second["auction"],
               "partnership_contexts": second.get("partnership_contexts", {})},
        "point_difference_a": s1 - s2,
        "imp_a": signed_imp(s1 - s2),
    }


def _save(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf8")
    temporary.replace(path)


def evaluate(config_path: Path) -> Path:
    config = object_fields(load_json(config_path), {
        "experiment_id", "dataset", "system_a", "system_b", "dds_cache",
        "output", "bootstrap_seed", "bootstrap_resamples",
    }, {"migration_manifest"})
    for field in ("experiment_id", "dataset", "system_a", "system_b", "dds_cache", "output",
                  *(("migration_manifest",) if "migration_manifest" in config else ())):
        if not isinstance(config[field], str) or not config[field]:
            raise ValueError(f"{field} must be a nonempty string.")
    seed, resamples = integer(config["bootstrap_seed"]), integer(config["bootstrap_resamples"])
    if resamples < 100:
        raise ValueError("Use at least 100 frozen bootstrap replicates.")
    base = config_path.resolve().parent
    paths = {name: (base / config[name]).resolve() for name in ("dataset", "system_a", "system_b", "dds_cache", "output")}
    manifest, records = load_dataset(paths["dataset"])
    if manifest["split"] == "test" and (paths["dataset"].parent / "consumed.json").exists():
        raise ValueError("This test dataset has been consumed for debugging/selection; use a fresh held-out dataset.")
    a, b = load_system(paths["system_a"]), load_system(paths["system_b"])
    if a.scope != "general" or b.scope != "general":
        raise ValueError("A full competitive comparison requires two general systems.")
    migration = None
    translation_contents = None
    if "migration_manifest" in config:
        migration_path = (base / config["migration_manifest"]).resolve()
        migration = load_json(migration_path)
        if not isinstance(migration, dict) or migration.get("systems") != {"ccba": a.content_hash, "prec": b.content_hash}:
            raise ValueError("Migration manifest does not identify the actual A/B system hashes.")
        if "translation_manifest" in migration:
            name = migration["translation_manifest"]
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("The translation manifest must be a sibling file.")
            translation_contents = (migration_path.parent / name).read_bytes()
            if hashlib.sha256(translation_contents).hexdigest() != migration.get("translation_sha256"):
                raise ValueError("Translation manifest content hash mismatch.")
            if not isinstance(json.loads(translation_contents), dict):
                raise ValueError("The translation manifest must be a JSON object.")
    adapter = DDSAdapter()
    oracle = adapter.provenance.to_dict()
    if oracle.get("oracle_kind") != "dds":
        raise ValueError("A formal comparison requires a real DDS oracle.")
    output = paths["output"]
    output.mkdir(parents=True, exist_ok=False)
    systems = output / "systems"
    systems.mkdir()
    for system in (a, b):
        _save(systems / f"{system.content_hash}.json", system.to_data())
        (systems / f"{system.content_hash}.md").write_text(render_system(system), encoding="utf8")
    shutil.copyfile(paths["dataset"].parent / manifest["boards"], output / "dataset-boards.jsonl")
    if hashlib.sha256((output / "dataset-boards.jsonl").read_bytes()).hexdigest() != manifest["boards_sha256"]:
        raise ValueError("Dataset changed while copying the frozen run inputs.")
    _save(output / "dataset-manifest.json", manifest)
    source = source_snapshot()
    run = {
        "schema_version": 1, "experiment_id": config["experiment_id"], "status": "running",
        "started_at": timestamp(), "finished_at": None, "mode": "competitive-full",
        "score": "A IMP = signed_imp(table1_NS - table2_NS); one base family per pair",
        "dataset_hash": manifest["boards_sha256"], "dataset_split": manifest["split"],
        "boards": len(records), "tables": 2 * len(records), "completed_boards": 0,
        "system_a": {"name": a.name, "hash": a.content_hash},
        "system_b": {"name": b.name, "hash": b.content_hash},
        "source_hash": canonical_hash(source), "source_files": source,
        "config": config, "config_hash": canonical_hash(config), "oracle": oracle,
        "errors": [], "sample_unit": "unique base deal family; two tables combined",
    }
    if migration is not None:
        _save(output / "migration.json", migration)
        run["migration"] = migration
    if translation_contents is not None:
        (output / "translation.json").write_bytes(translation_contents)
        run["translation_sha256"] = hashlib.sha256(translation_contents).hexdigest()
    _save(output / "run.json", run)
    values = []
    contracts = {"A": Counter(), "B": Counter()}
    rules = {"A": Counter(), "B": Counter()}
    start = time.monotonic()
    try:
        with DDSCache(paths["dds_cache"], adapter) as cache, RecordWriter(output / "boards.jsonl") as rows, \
                RecordWriter(output / "auctions.jsonl") as auctions:
            for record in records:
                run["active_board"] = record.index
                table = cache.solve(record.board.deal)
                result = paired_board(a, b, record.board, table)
                values.append(result["imp_a"])
                row = {
                    "index": record.index, "family_id": record.family_id,
                    "deal_hash": deal_hash(record.board.deal),
                    "dealer": record.board.dealer.value, "vulnerability": record.board.vulnerability.value,
                    "point_difference_a": result["point_difference_a"], "imp_a": result["imp_a"],
                    "dds": adapter.diagnostics(table, record.board.dealer, record.board.vulnerability),
                }
                for table_name in ("table1", "table2"):
                    played = result[table_name]
                    row[table_name] = {key: value for key, value in played.items() if key != "auction"}
                    auctions.write({
                        "index": record.index, "table": table_name,
                        "systems": {"NS": a.content_hash if table_name == "table1" else b.content_hash,
                                    "EW": b.content_hash if table_name == "table1" else a.content_hash},
                        **played,
                    })
                    for step in played["auction"]:
                        label = "A" if (
                            (step["seat"] in ("N", "S")) == (table_name == "table1")
                        ) else "B"
                        rules[label][step["private_trace"]["rule_id"]] += 1
                    final = played["contract"]
                    if "passed_out" not in final:
                        label = "A" if ((final["declarer"] in ("N", "S")) == (table_name == "table1")) else "B"
                        contracts[label][str(final["level"]) + final["strain"]] += 1
                rows.write(row)
                rows.flush()
                auctions.flush()
                run["completed_boards"] += 1
                if run["completed_boards"] % 100 == 0:
                    _save(output / "run.json", run)
                    print(json.dumps({"completed_boards": run["completed_boards"], "seconds": round(time.monotonic() - start, 2)}), file=sys.stderr, flush=True)
            if source_snapshot() != source:
                raise RuntimeError("Source changed during the frozen comparison; results cannot be finalized.")
            summary = paired_summary(values, seed=seed, resamples=resamples)
            summary.update({
                "system_a": run["system_a"], "system_b": run["system_b"],
                "dataset_split": manifest["split"], "distribution": manifest["distribution"],
                "contracts": {name: dict(sorted(counts.items())) for name, counts in contracts.items()},
                "rule_hits": {name: dict(sorted(counts.items())) for name, counts in rules.items()},
                "oracle": oracle, "elapsed_seconds": time.monotonic() - start,
                "dds_cache_hits": cache.hits, "dds_solves": cache.solves,
                "invalid_boards": 0, "deleted_or_replaced_boards": 0,
            })
            _save(output / "summary.json", summary)
            _csv(output / "boards.jsonl", output / "boards.csv")
            _report(output / "report.md", summary, run)
            run["status"] = "completed"
            run["record_encoding"] = {"boards.jsonl": rows.metadata(), "auctions.jsonl": auctions.metadata()}
            run["active_board"] = None
            run["finished_at"] = timestamp()
            run["artifacts_sha256"] = {
                name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                for name in ("boards.jsonl", "boards.csv", "auctions.jsonl", "summary.json", "report.md", "dataset-boards.jsonl",
                             *(("migration.json",) if migration is not None else ()),
                             *(("translation.json",) if translation_contents is not None else ()))
            }
            _save(output / "run.json", run)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        run["status"] = "failed"
        run["finished_at"] = timestamp()
        run["errors"].append({"board": run.get("active_board"), "type": type(error).__name__, "message": str(error)})
        _save(output / "run.json", run)
        raise
    finally:
        if run["status"] == "running":
            run["status"] = "failed"
            run["finished_at"] = timestamp()
            run["errors"].append({"board": run.get("active_board"), "type": "unexpected_interruption",
                                  "message": "No successful result was finalized; inspect the original exception."})
            _save(output / "run.json", run)
    return output / "report.md"


def _csv(source: Path, target: Path) -> None:
    fields = ["index", "family_id", "dealer", "vulnerability", "table1_contract", "table1_tricks",
              "table1_score_ns", "table2_contract", "table2_tricks", "table2_score_ns", "point_difference_a", "imp_a"]
    with target.open("x", encoding="utf8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for record in iter_records(source):
            row = {key: record[key] for key in ("index", "family_id", "dealer", "vulnerability", "point_difference_a", "imp_a")}
            for table in ("table1", "table2"):
                contract = record[table]["contract"]
                row[table + "_contract"] = "P" if "passed_out" in contract else (
                    str(contract["level"]) + contract["strain"] + {1: "", 2: "X", 4: "XX"}[contract["doubling"]] + " by " + contract["declarer"]
                )
                row[table + "_tricks"] = record[table]["tricks"]
                row[table + "_score_ns"] = record[table]["score_ns"]
            writer.writerow(row)


def _report(path: Path, summary: dict, run: dict) -> None:
    mean = summary["mean_imp"]
    low, high = summary["ci95"]
    conclusion = (
        "A在本次设置下的均值较高，区间完全在0以上。" if low > 0 else
        "B在本次设置下的均值较高，A收益区间完全在0以下。" if high < 0 else
        "区间跨0，本次样本不足以确认哪一版本更强。"
    )
    lines = [
        "# CCBA / Precision 工程移植版本：真实DDS同牌比较", "",
        f"- 实验：{run['experiment_id']}；状态：completed。",
        f"- A：{summary['system_a']['name']}；哈希 `{summary['system_a']['hash']}`。",
        f"- B：{summary['system_b']['name']}；哈希 `{summary['system_b']['hash']}`。",
        f"- {summary['boards']}副完整基础牌例、{summary['tables']}桌；split={summary['dataset_split']}。",
        "- 两桌保持N/E/S/W手牌、发牌人及局况不变，只交换体系位置。",
        f"- 真实DDS {summary['oracle']['version']}；commit `{summary['oracle']['source_commit']}`；单线程求解。",
        "", "## 主结果", "",
        "| 指标 | A相对B |", "| --- | ---: |",
        f"| 平均IMP/副 | {mean:+.4f} |",
        f"| 总IMP | {summary['total_imp']:+d} |",
        f"| 95%配对牌族bootstrap区间 | [{low:+.4f}, {high:+.4f}] |",
        f"| 赢 / 输 / 平副数 | {summary['a_wins']} / {summary['b_wins']} / {summary['ties']} |",
        f"| 无效 / 删除或替换牌例 | {summary['invalid_boards']} / {summary['deleted_or_replaced_boards']} |",
        "", conclusion,
        "", "## 解释边界", "",
        "- 这不是两份手册全部约定的逐条完整复现。工程移植保留主要开叫与协议，但部分精细后续按明示自然策略补全；详见同目录migration.json。结果不能直接冠名为原版体系的实力差距。",
        "- 比较对象是从用户文档迁移并补充了确定性判断的工程版本，不是人类搭档的唯一标准CCBA/精确实现。",
        "- 正文优先；模糊评估与未展开的自然续叫使用明示工程口径。请连同迁移清单、实际IR和规则命中统计阅读，不能省略这些限制。",
        "- 对手固定为另一版本，公开协议可见不表示会主动学会适应；不声称博弈均衡或普适最优。",
        "- 双明手墩数近似公开四手的最优打牌，不是实战打牌，也没有在叫牌时泄露给策略。",
        "- 每副先转换IMP再平均；两桌组成一个样本。该区间仅处理牌例抽样不确定性，不处理迁移定义差异。",
        "- 本次没有对1000副结果选择候选或调参。若继续据此修改体系，必须把这批test标为已消耗。",
        "", "## 可复核产物", "",
        "- `run.json`：配置、源码哈希、DDS来源、运行状态和产物校验。",
        "- `dataset-boards.jsonl`：完整52张牌、发牌人及局况；`dataset-manifest.json`记录生成方式。",
        "- `boards.jsonl`：两桌真实庄位、定约、DDS墩数、NS分数和逐副IMP。",
        "- `boards.csv`：同一逐副结果的表格版本。",
        "- `auctions.jsonl`：完整公开叫牌、命中规则和实际求值的私有原子特征，仅供牌后研究。",
        "- `systems/`：实际执行的不可变IR与中文规范。",
        "- `summary.json`：bootstrap设置、定约分布、全部规则命中数。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf8")
