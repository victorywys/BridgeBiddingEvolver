"""On-disk versioning for bidding systems.

A *repository* lives at ``systems/<id>/`` and contains:

* ``versions/<version>/`` — immutable snapshot directory (full system).
* ``versions/<version>/patch.yaml`` — the patch that produced it (except
  for the seed).
* ``versions/<version>/benchmark.json`` — optional benchmark snapshot.
* ``HEAD`` — text file pointing at the current version label.
* ``log.md`` — append-only human-readable history.

Each version is a complete, self-contained system directory. A
:class:`SystemRepository` lets the evolution loop snapshot, switch HEAD,
roll back, and ablate (drop one convention).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..dsl import BiddingSystem, load_system, save_system
from ..reflection.patch import Patch, apply_patch


@dataclass
class VersionInfo:
    version: str
    parent: Optional[str]
    patch_name: Optional[str]
    timestamp: str
    metrics: Dict[str, object] = field(default_factory=dict)


class SystemRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "versions").mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------
    @classmethod
    def init_from(cls, source_system_path: str | Path,
                  repo_path: str | Path,
                  version_label: str = "v0") -> "SystemRepository":
        repo = cls(repo_path)
        snapshot_dir = repo._version_dir(version_label)
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        # Copy the source tree.
        shutil.copytree(source_system_path, snapshot_dir)
        # Make sure meta.yaml records the version.
        meta_path = snapshot_dir / "meta.yaml"
        meta = yaml.safe_load(meta_path.read_text()) if meta_path.exists() else {}
        meta = meta or {}
        meta["Version"] = version_label
        meta_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))
        repo._write_head(version_label)
        repo._append_log(f"- init `{version_label}` from `{source_system_path}`")
        repo._write_version_info(VersionInfo(
            version=version_label, parent=None, patch_name=None,
            timestamp=_now(),
        ))
        return repo

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    @property
    def head(self) -> str:
        return (self.root / "HEAD").read_text(encoding="utf-8").strip()

    def list_versions(self) -> List[str]:
        return sorted([p.name for p in (self.root / "versions").iterdir() if p.is_dir()])

    def load(self, version: Optional[str] = None) -> BiddingSystem:
        v = version or self.head
        return load_system(self._version_dir(v))

    def path_for(self, version: Optional[str] = None) -> Path:
        return self._version_dir(version or self.head)

    def version_info(self, version: str) -> VersionInfo:
        f = self._version_dir(version) / "VERSION.yaml"
        if not f.exists():
            return VersionInfo(version=version, parent=None, patch_name=None, timestamp="")
        data = yaml.safe_load(f.read_text()) or {}
        return VersionInfo(
            version=version,
            parent=data.get("Parent"),
            patch_name=data.get("Patch"),
            timestamp=data.get("Timestamp", ""),
            metrics=data.get("Metrics", {}) or {},
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def commit_patch(self, patch: Patch, new_version: str,
                     parent: Optional[str] = None,
                     metrics: Optional[Dict[str, object]] = None) -> BiddingSystem:
        """Apply ``patch`` to ``parent`` (default HEAD), snapshot as
        ``new_version``, and advance HEAD."""
        parent = parent or self.head
        base = self.load(parent)
        new_system = apply_patch(base, patch)
        new_system.version = new_version
        snapshot_dir = self._version_dir(new_version)
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        save_system(new_system, snapshot_dir)
        # write patch + version info
        (snapshot_dir / "patch.yaml").write_text(patch.to_yaml(), encoding="utf-8")
        info = VersionInfo(version=new_version, parent=parent, patch_name=patch.name,
                           timestamp=_now(), metrics=dict(metrics or {}))
        self._write_version_info(info)
        self._write_head(new_version)
        self._append_log(
            f"- commit `{new_version}` (parent `{parent}`) "
            f"via patch `{patch.name}`: {patch.rationale}"
        )
        return new_system

    def rollback(self, version: str) -> None:
        if not self._version_dir(version).exists():
            raise FileNotFoundError(version)
        self._write_head(version)
        self._append_log(f"- rollback HEAD -> `{version}`")

    def ablate(self, convention_name: str, new_version: str,
               parent: Optional[str] = None) -> BiddingSystem:
        """Drop a single convention; useful for ablation studies."""
        from ..reflection.patch import Patch, PatchOp
        patch = Patch(
            name=f"ablate_{convention_name}",
            rationale=f"Ablation: removing convention {convention_name}",
            operations=[PatchOp(op="remove_convention", target=convention_name)],
            parent_version=parent or self.head,
        )
        return self.commit_patch(patch, new_version=new_version, parent=parent)

    def attach_benchmark(self, version: str, metrics: Dict[str, object]) -> None:
        path = self._version_dir(version) / "benchmark.json"
        path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        info = self.version_info(version)
        info.metrics = metrics
        self._write_version_info(info)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _version_dir(self, version: str) -> Path:
        return self.root / "versions" / version

    def _write_head(self, version: str) -> None:
        (self.root / "HEAD").write_text(version + "\n", encoding="utf-8")

    def _append_log(self, line: str) -> None:
        path = self.root / "log.md"
        prior = path.read_text(encoding="utf-8") if path.exists() else "# System log\n"
        path.write_text(prior.rstrip() + "\n" + line + "\n", encoding="utf-8")

    def _write_version_info(self, info: VersionInfo) -> None:
        path = self._version_dir(info.version) / "VERSION.yaml"
        path.write_text(yaml.safe_dump({
            "Version": info.version,
            "Parent": info.parent,
            "Patch": info.patch_name,
            "Timestamp": info.timestamp,
            "Metrics": info.metrics,
        }, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def diff_systems(a: BiddingSystem, b: BiddingSystem) -> Dict[str, object]:
    """Produce a structural diff between two systems."""
    a_conv = {c.name: c for c in a.conventions}
    b_conv = {c.name: c for c in b.conventions}
    added = sorted(set(b_conv) - set(a_conv))
    removed = sorted(set(a_conv) - set(b_conv))
    modified = []
    for name in set(a_conv) & set(b_conv):
        if a_conv[name].to_yaml() != b_conv[name].to_yaml():
            modified.append(name)
    a_open = {r.name for r in a.opening_table}
    b_open = {r.name for r in b.opening_table}
    return {
        "conventions_added": added,
        "conventions_removed": removed,
        "conventions_modified": sorted(modified),
        "openings_added": sorted(b_open - a_open),
        "openings_removed": sorted(a_open - b_open),
    }
