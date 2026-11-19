"""Isolated engineering workspaces for framework incidents, not diagnosis-only tickets."""

import copy
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from bridge_evolve.evolution.config import ROOT, engine_identity
from bridge_evolve.evolution.director import structured_request
from bridge_evolve.evolution.delivery import DeliveryDeadline, SolutionReturned
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evolution.feedback import bounded
from bridge_evolve.evolution.models import MaintenanceBudgetExhausted, ModelRequestError
from bridge_evolve.evolution.research_tools import STRING, paged_value, tool
from bridge_evolve.systems.navigation import at_pointer

LOGGER = logging.getLogger(__name__)
LIST = tool("list_engine_files", "List actual isolated source/test/documentation paths by glob; page before reading. Never guess paths.",
    {"glob": STRING, "offset": {"type": "integer", "minimum": 0},
     "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ())
SEARCH = tool("search_engine_files", "Literal text search in actual isolated files; returns paths, line numbers and matching source.",
    {"query": STRING, "glob": STRING, "offset": {"type": "integer", "minimum": 0},
     "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("query",))
READ = tool("read_engine_file", "Read isolated source/tests or read-only documentation, with line numbers.",
    {"path": STRING, "start": {"type": "integer", "minimum": 1},
     "count": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("path",))
EDIT = tool("edit_engine_file", "Replace one exact source/test fragment in the isolated revision; old='' creates a NEW file. Never edits the active engine.",
    {"path": STRING, "old": STRING, "new": STRING}, ("path", "old", "new"))
TEST = tool("run_engine_tests", "Execute unittest selectors against the isolated source. Actual output and return code are saved.",
    {"selectors": {"type": "array", "minItems": 1, "items": STRING}}, ("selectors",))
RESULT = tool("read_engine_result", "Read a complete saved engineering result or context using its ID and JSON pointer.",
    {"result_id": STRING, "pointer": STRING, "offset": {"type": "integer", "minimum": 0},
     "limit": {"type": "integer", "minimum": 1, "maximum": 100},
     "text_offset": {"type": "integer", "minimum": 0},
     "text_limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ("result_id",))
SUBMIT = tool("submit_engine_revision", "Deliver an actual source revision after successful focused tests; diagnosis alone is not delivery.",
    {"diagnosis": STRING, "summary": STRING, "reproduction_test": STRING},
    ("diagnosis", "summary", "reproduction_test"))
REVIEW = tool("review_engine_revision", "Review the actual isolated source changes, regression reproduction and test output.",
    {"approve": {"type": "boolean"}, "reason": STRING}, ("approve", "reason"))


class EngineWorkspace:
    def __init__(self, controller, job):
        self.controller, self.job = controller, job
        self.directory = controller.directory / "repairs" / job["id"]
        self.path = self.directory / "engineering.json"
        self.root = self.directory / "engine"
        self.original = self.directory / "original-engine"
        self.data = controller.read_artifact(self.path)
        expected_base = controller.state.get("repair_engine", {}).get("hash", controller.state["engine_hash"])
        carried = None
        if self.data and self.data.get("base_engine_hash", expected_base) != expected_base:
            carried = self.data
            self.directory = self.directory / ("rebased-" + expected_base[:12])
            self.path = self.directory / "engineering.json"
            self.root, self.original = self.directory / "engine", self.directory / "original-engine"
            self.directory.mkdir(parents=True, exist_ok=True)
            self.data = controller.read_artifact(self.path)
        if self.data is None:
            project = Path(controller.config["project"])
            base_revision = copy.deepcopy(controller.state.get("repair_engine"))
            base = Path(base_revision["path"]) if base_revision else ROOT
            if base_revision and engine_identity(base) != base_revision["hash"]:
                raise ValueError("The accepted base engine changed before the isolated repair was created.")
            for root in (self.root, self.original):
                root.mkdir(parents=True, exist_ok=True)
                for name in ("src", "tests", "tools"):
                    origin = base / name if (base / name).is_dir() else project / name
                    if name == "tools" and origin != project / name:
                        shutil.copytree(project / name, root / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc"))
                    shutil.copytree(origin, root / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc"))
                from bridge_evolve.evolution.runtime import asset_hashes
                for asset, digest in asset_hashes(base).items():
                    if digest is not None and asset.startswith("tools/board-viewer/"):
                        target = root / asset
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(base / asset, target)
                if (base / "docs").is_dir() or (project / "docs").is_dir():
                    origin = base / "docs" if (base / "docs").is_dir() else project / "docs"
                    shutil.copytree(origin, root / "docs", dirs_exist_ok=True)
                if (base / "systems/general-v2.json").is_file():
                    (root / "systems").mkdir(exist_ok=True)
                    shutil.copyfile(base / "systems/general-v2.json", root / "systems/general-v2.json")
                    if (project / "systems").is_dir():
                        for item in (project / "systems").iterdir():
                            target = root / "systems" / item.name
                            if not target.exists():
                                target.symlink_to(item, target_is_directory=item.is_dir())
                for name in ("systems", "examples", "experiments", ".runtime", ".venv"):
                    if (project / name).exists() and not (root / name).exists():
                        (root / name).symlink_to(project / name, target_is_directory=True)
                shutil.copyfile(project / "pyproject.toml", root / "pyproject.toml")
            self.data = {"changes": {}, "operations": {}, "tests": [], "base_revision": base_revision,
                         "base_engine_hash": expected_base}
            if carried:
                job.setdefault("base_changes", []).append({
                    "from": carried["base_engine_hash"], "to": expected_base,
                    "iteration": job.get("iteration", 1), "created_at": timestamp()})
                job["iteration"] = 1
                self.data["prior_branch"] = {
                    "changes_to_port": list(carried["changes"]),
                    "evidence": {"details_ref": self.full_result(carried)},
                    "instruction": "The accepted base changed. Port retained edits onto this fresh base, then rerun tests; old results are not current validation."}
            self.save()
        job["workspace"] = self.path.relative_to(controller.directory).as_posix()

    def save(self):
        self.controller.artifact(self.path, self.data)
        self.job.update(tool_calls=len(self.data["operations"]), updated_at=timestamp())

    def full_result(self, value):
        from bridge_evolve.evaluation.data import canonical_hash
        identity = canonical_hash(value)
        path = self.directory / f"result-{identity}.json"
        self.controller.artifact(path, value)
        return {"result_id": identity, "instruction": "Use read_engine_result with this result_id and a narrow JSON pointer."}

    def feedback(self, value, *, limit):
        if len(json.dumps(value, ensure_ascii=False).encode()) <= limit:
            return value
        return bounded(value, limit=limit, reference=self.full_result(value))

    def source(self, name, *, reading=False):
        path = (self.root / name).resolve()
        suffixes = (".py", ".mjs", ".css", ".html", ".md", ".json", ".toml") if reading else (".py", ".mjs", ".css", ".html")
        if not path.is_relative_to(self.root.resolve()) or path.suffix not in suffixes:
            raise ValueError("Choose a source/test file inside this engineering revision.")
        if reading and (name == "pyproject.toml" or name.split("/")[0] == "docs"):
            return path
        if name.split("/")[0] not in ("src", "tests", "tools"):
            raise ValueError("Engineering edits belong to src, tests or tools.")
        return path

    def files(self, pattern="*"):
        result = []
        for directory in ("src", "tests", "tools", "docs"):
            for parent, directories, names in os.walk(self.root / directory, followlinks=False):
                directories[:] = sorted(name for name in directories
                                        if name not in ("node_modules", "__pycache__", "dist") and not name.startswith("."))
                for name in sorted(names):
                    path = Path(parent) / name
                    relative = path.relative_to(self.root).as_posix()
                    if (path.is_file() and path.suffix in (".py", ".mjs", ".css", ".html", ".md", ".json", ".toml")
                            and path.resolve().is_relative_to(self.root.resolve())
                            and fnmatch.fnmatchcase(relative, pattern)):
                        result.append(relative)
        if (self.root / "pyproject.toml").is_file() and fnmatch.fnmatchcase("pyproject.toml", pattern):
            result.append("pyproject.toml")
        return sorted(result)

    def execute(self, name, args, call_id):
        from bridge_evolve.evaluation.data import canonical_hash
        fingerprint = canonical_hash({"name": name, "args": args})
        saved = self.data["operations"].get(call_id)
        if saved is not None:
            if saved["fingerprint"] != fingerprint:
                raise ValueError("An engineering call ID was reused with different input.")
            if "result" in saved:
                return saved["result"]
            if saved.get("expected_sha256"):
                path = self.source(args["path"])
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == saved["expected_sha256"]:
                    result = {"ok": True, "written": args["path"], "sha256": saved["expected_sha256"]}
                    saved.update(status="completed", result=result, finished_at=timestamp())
                    self.save()
                    return result
        else:
            saved = {"fingerprint": fingerprint, "name": name, "arguments": args, "status": "started",
                     "started_at": timestamp()}
            self.data["operations"][call_id] = saved
            self.save()
        self.controller.maintenance_stop()
        try:
            if name == LIST["name"]:
                result = paged_value(self.files(args.get("glob", "*")), args.get("offset", 0), args.get("limit", 40))
            elif name == SEARCH["name"]:
                query = args["query"].casefold()
                offset, limit = args.get("offset", 0), args.get("limit", 20)
                result = paged_value([], offset, limit)
                for relative in self.files(args.get("glob", "*.py")):
                    for number, line in enumerate(self.source(relative, reading=True).read_text().splitlines(), 1):
                        if query in line.casefold():
                            if offset <= result["total"] < offset + limit:
                                result["value"].append({"path": relative, "line": number, "text": line[:1000]})
                            result["total"] += 1
                result["next_offset"] = offset + limit if offset + limit < result["total"] else None
            elif name == READ["name"]:
                path = self.source(args["path"], reading=True)
                lines = path.read_text().splitlines()
                start = args.get("start", 1) - 1
                result = {"total_lines": len(lines), "lines": [
                    {"line": index + 1, "text": value}
                    for index, value in enumerate(lines[start:start + args.get("count", 150)], start)]}
            elif name == RESULT["name"]:
                identity = args["result_id"]
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    raise ValueError("Choose a saved engineering result ID.")
                value = self.controller.read_artifact(self.directory / f"result-{identity}.json")
                if value is None:
                    raise ValueError("Unknown engineering result ID.")
                value = at_pointer(value, args.get("pointer", ""))
                if isinstance(value, str):
                    start, count = args.get("text_offset", 0), args.get("text_limit", 8000)
                    result = {"total_characters": len(value), "offset": start, "text": value[start:start + count]}
                else:
                    result = paged_value(value, args.get("offset", 0), args.get("limit", 20))
            elif name == EDIT["name"]:
                path = self.source(args["path"])
                old = path.read_text() if path.exists() else ""
                if args["old"] == "":
                    if path.exists():
                        raise ValueError("An empty old fragment creates a new file, not a blind overwrite.")
                    new = args["new"]
                else:
                    if old.count(args["old"]) != 1:
                        raise ValueError("The exact old fragment must occur once; read the current file.")
                    new = old.replace(args["old"], args["new"], 1)
                self.data["changes"].setdefault(args["path"], {"before": old})
                self.data["changes"][args["path"]]["after"] = new
                saved["expected_sha256"] = hashlib.sha256(new.encode()).hexdigest()
                self.save()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new)
                result = {"written": args["path"], "sha256": hashlib.sha256(new.encode()).hexdigest()}
            else:
                selectors = args["selectors"]
                if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", item) for item in selectors):
                    raise ValueError("Use unittest module/class/test selectors, not shell arguments.")
                result = self.run_tests(selectors)
                self.data["tests"].append(result)
            result = {"ok": True, **result}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            LOGGER.warning("Engineering tool %s failed: %s", name, error)
            result = {"ok": False, "error": str(error), "type": type(error).__name__}
        saved.update(status="completed", result=result, finished_at=timestamp())
        self.save()
        return result

    def run_tests(self, selectors, *, original=False):
        from bridge_evolve.evaluation.data import canonical_hash
        root = self.original if original else self.root
        environment = {key: value for key, value in os.environ.items()
                       if key in ("PATH", "HOME", "LANG", "BRIDGE_DDS_REQUIRED", "BRIDGE_DDS_LIBRARY")}
        environment.update(PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join((str(root / "src"), str(self.root / "tests"))))
        completed = subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", *selectors],
            cwd=root, env=environment, capture_output=True, text=True,
            timeout=max(1, min(1800, self.controller.maintenance_time_window())), check=False)
        return {"selectors": selectors, "returncode": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr, "original": original,
                "source_hash": canonical_hash(self.data["changes"])}


def repair_engine(controller, job):
    try:
        workspace = EngineWorkspace(controller, job)
        job.update(status="engineering", permissions="isolated-engine-repair-and-tests",
                   next_action="工程维护者正在隔离副本复现、改代码并运行回归。")
        controller.checkpoint()
        attempt = job.setdefault("iteration", 1)
        while True:
            def validate(proposal):
                from bridge_evolve.evaluation.data import canonical_hash
                tests = workspace.data["tests"]
                if not workspace.data["changes"] or not tests or tests[-1]["returncode"] != 0:
                    raise ValueError("Deliver actual code and a passing test run, not only a repair plan.")
                if tests[-1]["source_hash"] != canonical_hash(workspace.data["changes"]):
                    raise ValueError("Source changed after testing; rerun tests.")
                if not any(proposal["reproduction_test"] in row["selectors"] and row["returncode"] == 0 for row in tests):
                    raise ValueError("Run the named reproduction regression before delivery.")
                try:
                    reproduction = workspace.run_tests([proposal["reproduction_test"]], original=True)
                except subprocess.TimeoutExpired as error:
                    raise ValueError("Original-engine reproduction timed out; inspect and narrow the regression instead of claiming it passed.") from error
                workspace.data["original_reproduction"] = reproduction
                workspace.save()
                if not reproduction["returncode"] or not re.search(r"Ran [1-9]\d* tests?", reproduction["stderr"]):
                    raise ValueError("The new reproduction test must actually fail against the original engine; otherwise the defect is not reproduced.")

            tools = [(item, lambda args, call_id, name=item["name"]: workspace.execute(name, args, call_id))
                     for item in (READ, EDIT, TEST, RESULT, LIST, SEARCH)]
            proposal = structured_request(controller, workspace.directory / f"engineering-{attempt}.json",
                "You are the framework maintenance engineer. Deliver a working isolated source revision, not a "
                "diagnosis-only ticket. Reproduce the reported defect in a focused test, edit src/tools/tests, run it "
                "and directly related regressions, iterate on failures, then submit. Existing engine and scores remain "
                "unchanged. Do not mask failures or weaken bidding/evaluation to obtain passing tests. First use the "
                "actual source index or list_engine_files/search_engine_files; never guess file paths or use "
                "read_engine_file for a directory. Read only missing details. Model calls use the maintenance pool. "
                "Report actual results in Chinese.",
                {"incident": job["error"], "evidence": [controller.read_artifact(controller.directory / name)
                    for name in job["evidence"][:2]], "saved_changes": list(workspace.data["changes"]),
                    "source_index": workspace.files("src/bridge_evolve/evolution/*")[:40],
                    "retained_branch": workspace.data.get("prior_branch"),
                    "previous_review": job.get("review"), "last_period_solution": job.get("latest_solution")},
                SUBMIT, f"repair:{job['id']}:engine:{attempt}", validator=validate,
                tools=tools, feedback=workspace.feedback,
                solution_evidence=lambda: {"engineering": copy.deepcopy(workspace.data),
                                           "acceptance": "not_yet_independently_reviewed"})
            def validate_review(review):
                if not review["reason"].strip():
                    raise ValueError("An independent engineering review needs a concrete reason or next action.")

            review = structured_request(controller, workspace.directory / f"engineering-review-{attempt}.json",
                "Review this actual isolated engineering revision and tests. Accept necessary root-cause fixes, "
                "reject masked failures or score/opponent weakening. Give a concrete correction on rejection.",
                {"incident": job["error"], "proposal": proposal, "changes": workspace.data["changes"],
                 "tests": workspace.data["tests"][-1], "original_reproduction": workspace.data["original_reproduction"]},
                REVIEW, f"repair:{job['id']}:engine-review:{attempt}", validator=validate_review,
                tools=[tools[0], tools[3], tools[4], tools[5]], feedback=workspace.feedback)
            job["review"] = review
            if not review["approve"]:
                attempt += 1
                job.update(iteration=attempt, next_action=review["reason"])
                controller.checkpoint()
                continue
            base = workspace.data.get("base_revision")
            current = controller.state.get("repair_engine")
            if current and (not base or current["hash"] != base["hash"]
                            or engine_identity(workspace.original) != base["hash"]):
                raise ValueError("This repair branch is not based on the current accepted engine; merge it before acceptance.")
            covers = [*base.get("covers", [base["repair"]]), job["id"]] if base else [job["id"]]
            job.update(status="accepted_engine_revision", engine_path=str(workspace.root),
                       engine_hash=engine_identity(workspace.root),
                       diagnosis=proposal, next_action="使用此隔离引擎启动续接；旧引擎与旧账本不变。",
                       validation={"status": "passed", "tests": workspace.data["tests"][-1]["selectors"]})
            controller.state["repair_engine"] = {"path": str(workspace.root), "repair": job["id"],
                                               "hash": job["engine_hash"], "covers": list(dict.fromkeys(covers))}
            controller.checkpoint()
            return
    except SolutionReturned as error:
        job.setdefault("solution_returns", []).append(error.value["id"])
        job.update(status="solution_returned", latest_solution=error.value["solution"],
                   iteration=job.get("iteration", 1) + 1,
                   next_action=error.value["solution"]["next_action"])
        job.pop("repair_error", None)
        controller.checkpoint()
    except DeliveryDeadline as error:
        LOGGER.warning("Engineering %s could not return a solution: %s", job["id"], error)
        job.update(status="delivery_failed", repair_error=str(error), iteration=job.get("iteration", 1) + 1,
                   next_action="本周期方案返回失败；原始响应、隔离代码及测试保留，下周期继续，不伪装成验收。")
        controller.checkpoint()
    except (MaintenanceBudgetExhausted, EvolutionStopped, ModelRequestError, OSError, ValueError, RuntimeError) as error:
        if (controller.directory / "cancel-requested").exists():
            raise
        LOGGER.exception("Engineering maintenance %s retained for continuation", job["id"])
        job.update(status="deferred", repair_error=str(error), next_action="恢复已保存的隔离工程副本与测试，不从头诊断。")
        controller.checkpoint()
