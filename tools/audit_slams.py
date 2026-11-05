"""Verify a frozen paired run and publish explicit called/made/missed slam evidence."""

import argparse
import csv
import hashlib
import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path

from bridge_evolve.evaluation.cache import DDSCache
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import source_snapshot
from bridge_evolve.evaluation.slams import summarize_games, summarize_slams
from bridge_evolve.oracle import DDSAdapter, DDTable
from bridge_evolve.systems import load_system
from bridge_evolve.systems.inputs import parse_board
from board_viewer import assemble_run, explain_public_auction, read_rows, verify_audit_evidence
from verify_classics import audit as core_audit


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def save(path, value):
    if path.exists():
        if load(path) != value:
            raise ValueError(f"Existing audit artifact differs: {path.name}.")
        return
    with path.open("x", encoding="utf8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write("\n")


def independent_checker(project):
    directory = project / "results/ccba-prec-v5.2-compact-audit-1000-20261028"
    path = directory / "supplemental-check.py"
    expected = load(directory / "audit-report.json")["checker_sha256"]
    if digest(path) != expected:
        raise ValueError("The frozen independent scoring checker changed.")
    spec = importlib.util.spec_from_file_location("slam_independent_scoring", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the independently implemented scoring formulas.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path, expected


def verify(directory, validation):
    project = directory.parent.parent
    run, summary = load(directory / "run.json"), load(directory / "summary.json")
    if run["status"] != "completed" or source_snapshot() != run["source_files"]:
        raise ValueError("Audit requires a completed run with its exact runtime source.")
    verify_audit_evidence(directory, run["artifacts_sha256"], (run["system_a"]["hash"], run["system_b"]["hash"]))
    contracts, development = load(validation / "semantic-contracts.json"), load(validation / "development-audit.json")
    precomputed = load(validation / "oracle-precompute.json")
    if (precomputed["status"] != "completed" or precomputed["completed"] != run["boards"]
            or precomputed["dataset_hash"] != run["dataset_hash"] or precomputed["oracle"] != run["oracle"]):
        raise ValueError("The real-DDS precomputation does not identify this dataset/oracle.")
    for report in (contracts, development):
        if report["status"] != "passed" or report["source_hash"] != run["source_hash"]:
            raise ValueError("Current candidate contracts/development checks did not pass.")
    systems = {"ccba": run["system_a"]["hash"], "prec": run["system_b"]["hash"]}
    if contracts["systems"] != systems or development["systems"] != systems or development["dataset_hash"] != run["dataset_hash"]:
        raise ValueError("Development checks identify a different candidate or dataset.")
    strict = contracts["probe_counts"].get("consistent", 0)
    if not strict or contracts["probe_counts"] != {"consistent": strict}:
        raise ValueError("Some strict semantic contracts did not pass.")
    core = {"schema_version": 1, "mode": "audit", "status": "running", "errors": []}
    core_audit(directory, core)
    core["status"] = "passed"
    save(directory / "audit.json", core)
    deals, scores, auctions = (read_rows(directory / name) for name in ("dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl"))
    bundle = assemble_run(run, deals, scores, auctions, directory.name, explain=False)
    definitions = {identity: load_system(directory / "systems" / f"{identity}.json") for identity in systems.values()}
    shared, shared_path, shared_hash = independent_checker(project)
    with (directory / "boards.csv").open(newline="", encoding="utf8") as source:
        reader = csv.DictReader(source)
        csv_rows = list(reader)
    if len(csv_rows) != len(scores):
        raise ValueError("CSV count does not match the paired boards.")
    adapter = DDSAdapter()
    if adapter.provenance.to_dict() != run["oracle"]:
        raise ValueError("DDS provenance differs from the frozen run.")
    calls = Counter()
    flags = {}
    with DDSCache(project / ".runtime/dds/tables.sqlite", adapter) as cache:
        for record, row, csv_row in zip(bundle["boards"], scores, csv_rows, strict=True):
            board = parse_board({key: record[key] for key in ("dealer", "vulnerability", "hands")})
            saved_table = DDTable.from_dict(record["dds"]["table"])
            if cache.solve(board.deal) != saved_table or adapter.par(saved_table, board.dealer, board.vulnerability) != record["dds"]["par"]:
                raise ValueError(f"DDS/par mismatch on board{record['number']}.")
            expected_csv = {key: str(row[key]) for key in ("index", "family_id", "dealer", "vulnerability", "point_difference_a", "imp_a")}
            for table in record["tables"]:
                name = table["id"]
                if shared.independent_score(table["contract"], table["tricks"], record["vulnerability"]) != table["score_ns"]:
                    raise ValueError("Independent duplicate scoring mismatch.")
                expected_csv[name + "_contract"] = shared.contract_text(table["contract"])
                expected_csv[name + "_tricks"] = "" if table["tricks"] is None else str(table["tricks"])
                expected_csv[name + "_score_ns"] = str(table["score_ns"])
                meanings = explain_public_auction(table["auction"], record["dealer"], record["vulnerability"],
                    {side: definitions[table["systems"][side]["hash"]] for side in ("NS", "EW")})
                if any(meaning["basis"] != "frozen-public-protocol" or meaning["warnings"] for meaning in meanings):
                    raise ValueError(f"Unverified public explanation on board{record['number']}, {name}.")
                calls.update(step["private_trace"]["rule_id"] for step in table["auction"])
                bids = [(turn, step) for turn, step in enumerate(table["auction"]) if step["call"] not in ("P", "X", "XX")]
                if bids:
                    turn, step = bids[-1]
                    rule = step["private_trace"]["rule_id"]
                    category = ("artificial-splinter-terminal-3nt" if ".fit.splinter_" in rule and step["call"] == "3NT"
                                else "precision-shortness-terminal-suit" if any(".precision.club_short_" + suit + "." in rule for suit in ("H", "S"))
                                else None)
                    if category and [item["call"] for item in table["auction"][turn + 1:]] == ["P", "P", "P"]:
                        flags.setdefault(category, []).append({
                            "index": record["index"], "board_number": record["number"], "family_id": record["family_id"],
                            "board": {key: record[key] for key in ("dealer", "vulnerability", "hands")}, "table": name,
                            "history": [item["call"] for item in table["auction"]],
                            **{key: table[key] for key in ("contract", "tricks", "score_ns")},
                        })
            if csv_row != expected_csv or row["imp_a"] != shared.independent_imp(row["point_difference_a"]):
                raise ValueError("Independent IMP or CSV mismatch.")
    if sum(calls.values()) != development["bound_checked_decisions"] or dict(calls) != development["rule_hits"]:
        raise ValueError("Frozen policies differ from the all-board development replay.")
    slam, cases = summarize_slams(bundle["boards"], {systems["ccba"]: "A", systems["prec"]: "B"})
    games, game_cases = summarize_games(bundle["boards"], {systems["ccba"]: "A", systems["prec"]: "B"})
    save(directory / "slam-summary.json", slam)
    save(directory / "game-summary.json", games)
    game_case_text = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in game_cases)
    game_cases_path = directory / "game-cases.jsonl"
    if game_cases_path.exists():
        if game_cases_path.read_text(encoding="utf8") != game_case_text:
            raise ValueError("Existing game cases differ from the frozen run.")
    else:
        with game_cases_path.open("x", encoding="utf8") as target:
            target.write(game_case_text)
    case_text = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    cases_path = directory / "slam-cases.jsonl"
    if cases_path.exists():
        if cases_path.read_text(encoding="utf8") != case_text:
            raise ValueError("Existing slam cases differ from the frozen run.")
    else:
        with cases_path.open("x", encoding="utf8") as target:
            target.write(case_text)
    findings = [{"id": name, "severity": "high", "description": "已编码人工短套答叫被停成终局，需进一步修复协议后续。",
                 "cases": items, "affected_tables": len(items), "affected_unique_families": len({item["family_id"] for item in items})}
                for name, items in flags.items()]
    for filename in ("semantic-contracts.json", "development-audit.json", "overlap-audit.json", "frozen-overlap-audit.json", "oracle-precompute.json"):
        shutil.copyfile(validation / filename, directory / filename)
    checker = Path(__file__)
    shutil.copyfile(checker, directory / "supplemental-check.py")
    for name in ("board_viewer.py", "verify_classics.py"):
        shutil.copyfile(project / "tools" / name, directory / name)
    report = {
        "schema_version": 1, "status": "completed-with-findings" if findings else "passed",
        "engineering_checks_status": "passed", "semantic_review_status": "completed-with-findings" if findings else "no-targeted-findings",
        "release_acceptance": "blocked_by_semantic_findings" if findings else "not_established",
        "scope": "Fresh unconditional validation families; developer diagnosis, not an independent held-out strength ranking.",
        "source_hash": run["source_hash"], "dataset_hash": run["dataset_hash"], "systems": systems,
        "summary": {key: summary[key] for key in ("boards", "tables", "total_imp", "mean_imp", "ci95", "a_wins", "b_wins", "ties", "invalid_boards", "deleted_or_replaced_boards")},
        "metrics": {"boards": len(deals), "tables": 2 * len(deals), "strict_semantic_contracts": strict,
                    "bound_checked_decisions": sum(calls.values()), "csv_rows_verified": len(csv_rows),
                    "independently_recomputed_scores": 2 * len(deals), "independently_recomputed_imps": len(deals),
                    "dds_tables_verified": len(deals), "dealer_par_recomputed": len(deals),
                    "precomputed_native_solves": precomputed["solves"], "precomputed_cache_hits": precomputed["hits"],
                    "fresh_dds_table_crosschecks": core["fresh_dds_table_crosschecks"], "policy_replayed_tables": core["policy_replayed_tables"]},
        "slam": slam, "games": games, "findings": findings, "checker_sha256": digest(checker),
        "checker_dependency": {"path": shared_path.relative_to(project).as_posix(), "sha256": shared_hash},
        "caveats": ["DDS可成贯是全知牌后机会，不表示不完全信息下必应叫到。",
                    "未叫机会以桌次×定约方统计；两桌与大小贯不能相加成为独立样本。",
                    "未逐条证明原手册等价；未命中两类已知人工终局模式不等于全面语义正确。"],
    }
    if source_snapshot() != run["source_files"] or digest(shared_path) != shared_hash:
        raise ValueError("Audit sources changed during verification.")
    evidence = [path for path in directory.iterdir() if path.is_file() and path.name not in ("audit-report.json", "audit-report.md")]
    evidence.extend((directory / "systems").glob("*"))
    report["evidence_sha256"] = {path.relative_to(directory).as_posix(): digest(path) for path in evidence if path.is_file()}
    save(directory / "audit-report.json", report)
    lines = [f"# {run['experiment_id']}：满贯与进局专项审计", "", f"状态：{report['status']}；开发诊断，不是正式验收或独立强度确认。",
             "", "## 叫贯与打成（桌次）", "", "| 类型 | 叫到 | DDS打成 | 宕约 |", "| --- | --- | --- | --- |",
             *[f"| {label} | {slam['called'][kind]} | {slam['made'][kind]} | {slam['failed'][kind]} |"
               for kind, label in (("small", "小满贯"), ("grand", "大满贯"), ("total", "合计"))],
             "", "## 可成而未叫", "", f"可成小满贯的基础牌族：{slam['unique_families_with_small_slam']}；可成大满贯：{slam['unique_families_with_grand_slam']}。",
             f"按桌次×定约方计，未叫小满贯：{slam['opportunities']['small_not_bid']}；未叫大满贯：{slam['opportunities']['grand_not_bid']}。",
             f"已叫贯但选错可成花色/庄位等的小满贯机会：{slam['opportunities']['small_wrong_contract']}。",
             "", "两桌/大小贯不重复当作独立样本；以上是全知DDS机会，不是每副必应叫到的标签。", "",
             "全部漏叫/选错案例见[slam-cases.jsonl](slam-cases.jsonl)，原输入、20项可成阶数、实际发牌人/局况par和两桌完整叫牌均已冻结。",
             "", "## 分体系", "", "```json", json.dumps(slam["by_system"], ensure_ascii=False, indent=2), "```",
             "", "## 进局与邀请诊断", "", f"叫到成局或满贯{games['called']}桌，DDS打成{games['made']}桌、宕约{games['failed']}桌。",
             f"可成局基础牌族{games['unique_families_with_game']}副；按桌次×定约方，未叫成局{games['opportunities']['not_bid']}次，已到局但定约不对{games['opportunities']['wrong_contract']}次。",
             "信号次数：" + json.dumps(games["signals"], ensure_ascii=False) + "。成局机会是牌后全知诊断，不意味着当时都应进局。",
             "", "## 机械检查与边界", "", json.dumps(report["metrics"], ensure_ascii=False, indent=2),
             "", *["- " + caveat for caveat in report["caveats"]],
             *[f"- 仍有语义发现：{item['id']}，{item['affected_tables']}桌。" for item in findings], ""]
    with (directory / "audit-report.md").open("x", encoding="utf8") as target:
        target.write("\n".join(lines))
    print(json.dumps({"status": report["status"], "slam": slam, "report": str(directory / "audit-report.md")}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    args = parser.parse_args()
    if (args.run / "audit-report.json").exists():
        parser.error("Audit artifacts already exist; do not overwrite frozen evidence.")
    verify(args.run.resolve(), args.validation.resolve())


if __name__ == "__main__":
    main()
