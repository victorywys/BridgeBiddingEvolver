"""YAML loader / dumper for bidding systems.

Layout of a system on disk:

```
<root>/
  meta.yaml                  -> name, version, description, parents
  principles.md              -> markdown, parsed into Principle blocks
  opening_table.yaml         -> list of OpeningRule blocks
  conventions/<file>.yaml    -> one Convention per file (preferred)
  reflections/*.md           -> reflection log (read by reflection module)
  patches/*.yaml             -> proposed/applied patches
```

A new system can also be loaded from a single multi-document YAML file
for convenience.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .schema import (
    BiddingSystem,
    Convention,
    Followup,
    Meaning,
    OpeningRule,
    Principle,
    Requirements,
    Trigger,
)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_system(path: str | os.PathLike) -> BiddingSystem:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(root)
    meta = _read_yaml_safe(root / "meta.yaml") or {}
    name = meta.get("Name", root.name)
    version = meta.get("Version", "0")
    system = BiddingSystem(name=name, version=str(version), path=str(root), meta=meta)

    # opening table
    opening_path = root / "opening_table.yaml"
    if opening_path.exists():
        data = _read_yaml_safe(opening_path) or {}
        for entry in data.get("OpeningTable", []):
            system.opening_table.append(_parse_opening(entry))

    # conventions
    conv_dir = root / "conventions"
    if conv_dir.exists():
        for f in sorted(conv_dir.glob("*.yaml")):
            data = _read_yaml_safe(f)
            if not data:
                continue
            conv = _parse_convention(data, source=str(f.relative_to(root)))
            system.conventions.append(conv)

    # principles
    p_path = root / "principles.md"
    if p_path.exists():
        system.principles = _parse_principles(p_path.read_text(encoding="utf-8"))

    # stable ordering by priority desc, then name
    system.opening_table.sort(key=lambda r: (-r.priority, r.name))
    system.conventions.sort(key=lambda c: (-c.priority, c.name))
    return system


def _read_yaml_safe(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_opening(entry: Dict[str, Any]) -> OpeningRule:
    body = entry.get("OpeningRule", entry)
    return OpeningRule(
        name=body["Name"],
        priority=int(body.get("Priority", 50)),
        description=body.get("Description", ""),
        requirements=Requirements(constraints=dict(body.get("Requirements", {}) or {})),
        action=(body.get("Action", {}) or {}).get("Bid", body.get("Bid", "")),
    )


def _parse_convention(data: Dict[str, Any], source: str | None = None) -> Convention:
    body = data.get("Convention", data)
    trig_d = body.get("Trigger", {}) or {}
    trigger = Trigger(
        auction_pattern=list(trig_d.get("AuctionPattern", [])),
        seat=trig_d.get("Seat"),
        extras={k: v for k, v in trig_d.items() if k not in ("AuctionPattern", "Seat")},
    )
    followups: List[Followup] = []
    for fu in body.get("Followups", []) or []:
        followups.append(
            Followup(
                pattern=list(fu.get("Pattern", [])),
                meaning=Meaning(fields=dict(fu.get("Meaning", {}) or {})),
                requirements=Requirements(constraints=dict(fu.get("Requirements", {}) or {})),
                action=(fu.get("Action", {}) or {}).get("Bid"),
            )
        )
    return Convention(
        name=body["Name"],
        description=body.get("Description", ""),
        priority=int(body.get("Priority", 50)),
        trigger=trigger,
        requirements=Requirements(constraints=dict(body.get("Requirements", {}) or {})),
        meaning=Meaning(fields=dict(body.get("Meaning", {}) or {})),
        action=(body.get("Action", {}) or {}).get("Bid"),
        followups=followups,
        tags=list(body.get("Tags", []) or []),
        source=source,
    )


_PRINCIPLE_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def _parse_principles(text: str) -> List[Principle]:
    out: List[Principle] = []
    parts = _PRINCIPLE_RE.split(text)
    # parts: [preamble, name1, body1, name2, body2, ...]
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        out.append(Principle(name=name, text=body))
    return out


# ---------------------------------------------------------------------------
# Dumpers
# ---------------------------------------------------------------------------


def dump_convention(conv: Convention) -> str:
    return yaml.safe_dump(conv.to_yaml(), sort_keys=False, allow_unicode=True)


def dump_system(system: BiddingSystem) -> Dict[str, str]:
    """Return path -> file content for the entire system, without writing."""
    out: Dict[str, str] = {}
    out["meta.yaml"] = yaml.safe_dump(
        {"Name": system.name, "Version": system.version, **system.meta},
        sort_keys=False, allow_unicode=True,
    )
    out["opening_table.yaml"] = yaml.safe_dump(
        {"OpeningTable": [r.to_yaml() for r in system.opening_table]},
        sort_keys=False, allow_unicode=True,
    )
    for conv in system.conventions:
        fname = f"conventions/{_slug(conv.name)}.yaml"
        out[fname] = dump_convention(conv)
    if system.principles:
        chunks = ["# Principles\n"]
        for p in system.principles:
            chunks.append(f"## {p.name}\n\n{p.text}\n")
        out["principles.md"] = "\n".join(chunks)
    return out


def save_system(system: BiddingSystem, target_path: str | os.PathLike) -> None:
    root = Path(target_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "conventions").mkdir(exist_ok=True)
    for rel, content in dump_system(system).items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unnamed"
