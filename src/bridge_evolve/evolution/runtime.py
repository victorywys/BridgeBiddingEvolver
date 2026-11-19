"""Versioned runtime references and complete immutable engine bundles."""

import hashlib
import json
import shutil
from pathlib import Path

REFERENCES = {
    "navigation": "docs/13-system-navigation.md",
    "forcing_pass": "docs/18-blank-forcing-pass.md",
    "module": "docs/17-controlled-evolution.md",
    "language": "docs/07-executable-systems.md",
    "protocol": "docs/08-general-protocols-v2.md",
    "operators": "src/bridge_evolve/systems/language.py",
    "protocol_operators": "src/bridge_evolve/systems/v2_language.py",
    "example": "systems/general-v2.json",
}
VIEWER_ASSETS = tuple("tools/board-viewer/" + name for name in (
    "index.html", "styles.css", "app.mjs", "protocol-review.html", "protocol-review.mjs",
    "node_modules/lucide/dist/umd/lucide.min.js",
    "node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-400-normal.woff2",
    "node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-500-normal.woff2",
    "node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-600-normal.woff2",
    "node_modules/@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2",
))


def asset_hashes(root):
    names = {name for name in REFERENCES.values() if not name.startswith("src/")} | set(VIEWER_ASSETS)
    names.update(path.relative_to(root).as_posix() for path in (root / "tools/board-viewer").glob("*")
                 if path.is_file() and path.suffix in (".mjs", ".css", ".html") and not path.name.endswith(".test.mjs"))
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() if (root / name).is_file() else None
            for name in sorted(names)}


def validate_runtime(root):
    from bridge_evolve.evolution.config import engine_identity
    for name in (*REFERENCES.values(), *VIEWER_ASSETS):
        if not (root / name).is_file():
            raise FileNotFoundError(f"Required engine runtime asset is missing: {root / name}")
    manifest = root / "runtime-manifest.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())
        if value["engine"] != engine_identity(root) or value["assets"] != asset_hashes(root):
            raise ValueError("Frozen runtime assets differ from their verified manifest.")


def freeze_engine(source, destination):
    from bridge_evolve.evolution.config import engine_identity, save_json
    source, destination = Path(source), Path(destination)
    validate_runtime(source)
    identity = engine_identity(source)
    if destination.exists():
        if not (destination / "runtime-manifest.json").is_file():
            raise ValueError("An incomplete or unversioned bundle cannot be reused as a frozen runtime.")
        validate_runtime(destination)
        if engine_identity(destination) != identity:
            raise ValueError("An existing frozen engine cannot be overwritten with a different revision.")
        return identity
    shutil.copytree(source / "src", destination / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / "tools").mkdir()
    for name in ("board_viewer.py", "verify_classics.py"):
        shutil.copy2(source / "tools" / name, destination / "tools" / name)
    for name in asset_hashes(source):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    save_json(destination / "runtime-manifest.json", {
        "schema": "bridge-engine-runtime/v1", "engine": identity, "assets": asset_hashes(destination)})
    validate_runtime(destination)
    if engine_identity(destination) != identity:
        raise ValueError("The complete frozen engine differs from its source.")
    return identity
