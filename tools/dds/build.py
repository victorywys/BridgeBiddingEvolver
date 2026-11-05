#!/usr/bin/env python3
"""Build pinned DDS 2.9.0 locally: python tools/dds/build.py [--output DIR].

Only public https://github.com/dds-bridge/dds source is downloaded. No Python
binding, Boost, OpenMP, system installation, sudo, or GPL ``ddd`` driver is used.
Upstream DDS at this commit is Apache-2.0 licensed (source/LICENSE); retain that
license if redistributing the library. The separate ddd driver is GPL and is
not part of this build.

The original Linux probe parses the obsolete third line of ``free -k``.
Modern procps returns Swap there, giving zero threads on swapless machines.
Build patch linux-available-memory-sysconf-v1 replaces ONLY that probe with
sysconf(_SC_AVPHYS_PAGES/_SC_PAGESIZE); unmodified upstream source stays in
output/source. The patched translation unit, patch digest, exact flags,
compiler version, source commit, binary digest and build ID are recorded.
There are no DDS threading macros. The constructor-only dds.cpp unit is
excluded: the legacy ELF hooks run in an unsafe order against C++ globals on
this toolchain. Initialization and atexit cleanup are explicit in the adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

SOURCE_URL = "https://github.com/dds-bridge/dds.git"
SOURCE_COMMIT = "8d75755c8df81999557758c9757514edb94017bc"
DDS_VERSION = "2.9.0"
FLAGS = ("-O3", "-DNDEBUG", "-fPIC", "-std=c++11", "-shared")
PATCH_ID = "linux-available-memory-sysconf-v1"
OLD_PROBE = '''  FILE * fifo = popen(
    "free -k | tail -n+3 | head -n1 | awk '{print $NF}'", "r");
  int ignore = fscanf(fifo, "%llu", &kilobytesFree);
  fclose(fifo);'''
NEW_PROBE = '''  const long pages = sysconf(_SC_AVPHYS_PAGES);
  const long pageSize = sysconf(_SC_PAGESIZE);
  if (pages > 0 && pageSize > 0)
    kilobytesFree = static_cast<unsigned long long>(pages) *
      static_cast<unsigned long long>(pageSize) / 1024;'''


def run(*command: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/dds"))
    parser.add_argument("--compiler", default="c++")
    arguments = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("this pinned build recipe currently supports Linux only")
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / "source"
    if not source.exists():
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", "v2.9.0",
             SOURCE_URL, str(source)],
            check=True,
        )
    if run("git", "rev-parse", "HEAD", cwd=source) != SOURCE_COMMIT:
        raise RuntimeError("DDS source is not the pinned official commit")
    if run("git", "status", "--porcelain", "--untracked-files=no", cwd=source):
        raise RuntimeError("DDS source has tracked modifications; refusing an unpinned build")
    build = output / "build"
    build.mkdir(exist_ok=True)
    original = (source / "src/System.cpp").read_text()
    if original.count(OLD_PROBE) != 1:
        raise RuntimeError("DDS Linux memory patch context does not match exactly")
    patched = build / "System.cpp"
    patched.write_text(original.replace(OLD_PROBE, NEW_PROBE))
    sources = re.findall(r"\b\w+\.cpp\b", (source / "src/Makefiles/sources.txt").read_text())
    if len(sources) != 27 or len(set(sources)) != 27:
        raise RuntimeError("unexpected DDS source-file list")
    units = [
        patched if name == "System.cpp" else source / "src" / name
        for name in sources if name != "dds.cpp"
    ]
    compiler = run(arguments.compiler, "--version").splitlines()[0]
    library = output / "libdds.so"
    subprocess.run(
        [arguments.compiler, *FLAGS, "-I", str(source / "src"),
         *(str(path) for path in units), "-o", str(library)],
        check=True,
    )
    metadata = {
        "schema_version": 1,
        "version": DDS_VERSION,
        "source_url": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "license": "Apache-2.0",
        "compiler": compiler,
        "compile_flags": list(FLAGS),
        "threading": "single",
        "constructor": False,
        "excluded_source_units": ["dds.cpp"],
        "patch_id": PATCH_ID,
        "patch_sha256": hashlib.sha256((OLD_PROBE + "\n" + NEW_PROBE).encode()).hexdigest(),
        "patched_system_sha256": hashlib.sha256(patched.read_bytes()).hexdigest(),
        "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
    }
    metadata["build_id"] = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = library.with_suffix(".build.json")
    manifest.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(f"DDS {DDS_VERSION} {SOURCE_COMMIT}\n{library}\n{manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
