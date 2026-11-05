"""ctypes binding to the pinned DDS 2.9.0 legacy C API.

All native operations, including initialization, use one process-wide lock:
CalcDDtablePBN uses shared globals even when DDS has only one worker.
Do not call this library independently through another native binding.
The build manifest and binary digest are checked before loading; an arbitrary
dds3 module or same-named system library is never considered a verified solver.
"""

from __future__ import annotations

import atexit
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

from bridge_evolve.core.cards import Deal
from bridge_evolve.core import Contract, Seat, Vulnerability, score_ns
from bridge_evolve.core.types import Strain
from bridge_evolve.oracle.pbn import deal_to_pbn
from bridge_evolve.oracle.table import DECLARERS, STRAINS, DDTable

DDS_VERSION = "2.9.0"
SOURCE_COMMIT = "8d75755c8df81999557758c9757514edb94017bc"
SOURCE_URL = "https://github.com/dds-bridge/dds.git"
ADAPTER_VERSION = "ctypes-dds-2.9.0-v2-par"
BUILD_FLAGS = ("-O3", "-DNDEBUG", "-fPIC", "-std=c++11", "-shared")
PATCH_ID = "linux-available-memory-sysconf-v1"
PATCH_SHA256 = "90e42cbddb28ac34aa8b789233dfb58e8be3e87b11056ea16163f85e67dabcdc"
PATCHED_SYSTEM_SHA256 = "78fd2dbe8502bfa6819ba5eb7a6ad4da28eb2487a84b8e1583ea5d629f40813a"
DDS_STRAINS = (Strain.SPADES, Strain.HEARTS, Strain.DIAMONDS, Strain.CLUBS, Strain.NOTRUMP)
_NATIVE_LOCK = threading.RLock()
_LIBRARIES: dict[str, tuple[ctypes.CDLL, str]] = {}


class DDSError(RuntimeError):
    """A real solver is unavailable, misconfigured, or failed; never a score."""


class DDSUnavailableError(DDSError):
    """The selected native DDS library does not exist."""


class DDSConfigurationError(DDSError):
    """The native library or build provenance is not the supported pinned build."""


class DDSSolveError(DDSError):
    def __init__(self, code: int, detail: str, pbn: str) -> None:
        self.code = code
        self.pbn = pbn
        super().__init__(f"DDS CalcDDtablePBN failed ({code}): {detail}; deal={pbn}")


class DDSParError(DDSError):
    """The pinned dealer-aware par API failed or returned inconsistent data."""


@dataclass(frozen=True)
class DDSProvenance:
    oracle_kind: str
    solver: str
    version: str
    source_url: str
    source_commit: str
    license: str
    adapter_version: str
    api: str
    build_id: str
    library_sha256: str
    library_path: str
    compiler: str
    compile_flags: tuple[str, ...]
    patch_id: str
    threads: int
    thread_model: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["compile_flags"] = list(self.compile_flags)
        return data


class _TableDealPBN(ctypes.Structure):
    _fields_ = [("cards", ctypes.c_char * 80)]


class _TableResults(ctypes.Structure):
    _fields_ = [("resTable", (ctypes.c_int * 4) * 5)]


class _ParContract(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in ("underTricks", "overTricks", "level", "denom", "seats")]


class _ParResults(ctypes.Structure):
    _fields_ = [("score", ctypes.c_int), ("number", ctypes.c_int), ("contracts", _ParContract * 10)]


class _DDSInfo(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_int),
        ("minor", ctypes.c_int),
        ("patch", ctypes.c_int),
        ("versionString", ctypes.c_char * 10),
        ("system", ctypes.c_int),
        ("numBits", ctypes.c_int),
        ("compiler", ctypes.c_int),
        ("constructor", ctypes.c_int),
        ("numCores", ctypes.c_int),
        ("threading", ctypes.c_int),
        ("noOfThreads", ctypes.c_int),
        ("threadSizes", ctypes.c_char * 128),
        ("systemString", ctypes.c_char * 1024),
    ]


def _library_path(library_path: str | Path | None) -> Path:
    if library_path is not None:
        return Path(library_path).expanduser().resolve()
    configured = os.environ.get("BRIDGE_DDS_LIBRARY")
    if configured:
        return Path(configured).expanduser().resolve()
    project = Path(__file__).resolve().parents[3]
    candidates = [Path.cwd() / ".runtime/dds/libdds.so", project / ".runtime/dds/libdds.so"]
    return next((path.resolve() for path in candidates if path.is_file()), candidates[-1])


def _build_metadata(library: Path) -> dict[str, Any]:
    if not library.is_file():
        raise DDSUnavailableError(
            f"Native DDS library not found at {library}. "
            "Run python tools/dds/build.py from the project root, or set BRIDGE_DDS_LIBRARY."
        )
    manifest = library.with_suffix(".build.json")
    try:
        metadata = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise DDSConfigurationError(f"Missing or invalid pinned DDS build manifest: {manifest}") from exc
    if not isinstance(metadata, dict):
        raise DDSConfigurationError("DDS build manifest must be a JSON object")
    expected = {
        "schema_version": 1,
        "version": DDS_VERSION,
        "source_url": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "license": "Apache-2.0",
        "threading": "single",
        "constructor": False,
        "excluded_source_units": ["dds.cpp"],
        "compile_flags": list(BUILD_FLAGS),
        "patch_id": PATCH_ID,
        "patch_sha256": PATCH_SHA256,
        "patched_system_sha256": PATCHED_SYSTEM_SHA256,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise DDSConfigurationError(f"DDS build manifest has unsupported {key}")
    if not isinstance(metadata.get("compiler"), str) or not metadata["compiler"]:
        raise DDSConfigurationError("DDS build manifest is missing the compiler identity")
    try:
        digest = hashlib.sha256(library.read_bytes()).hexdigest()
    except OSError as exc:
        raise DDSConfigurationError(f"Cannot read the pinned DDS library: {library}") from exc
    if metadata.get("library_sha256") != digest:
        raise DDSConfigurationError("DDS library SHA-256 does not match its pinned build manifest")
    identity = {key: value for key, value in metadata.items() if key != "build_id"}
    build_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if metadata.get("build_id") != build_id:
        raise DDSConfigurationError("DDS build identity does not match its manifest")
    return metadata


def _load_library(path: Path, digest: str) -> ctypes.CDLL:
    key = str(path)
    if key in _LIBRARIES:
        library, previous_digest = _LIBRARIES[key]
        if previous_digest != digest:
            raise DDSConfigurationError("DDS binary changed after loading; restart this process")
        return library
    try:
        library = ctypes.CDLL(key)
        library.SetResources.argtypes = [ctypes.c_int, ctypes.c_int]
        library.SetResources.restype = None
        library.GetDDSInfo.argtypes = [ctypes.POINTER(_DDSInfo)]
        library.GetDDSInfo.restype = None
        library.CalcDDtablePBN.argtypes = [_TableDealPBN, ctypes.POINTER(_TableResults)]
        library.CalcDDtablePBN.restype = ctypes.c_int
        library.DealerParBin.argtypes = [ctypes.POINTER(_TableResults), ctypes.POINTER(_ParResults), ctypes.c_int, ctypes.c_int]
        library.DealerParBin.restype = ctypes.c_int
        library.ErrorMessage.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char)]
        library.ErrorMessage.restype = None
        library.FreeMemory.argtypes = []
        library.FreeMemory.restype = None
    except (OSError, AttributeError) as exc:
        raise DDSConfigurationError(f"Cannot load the pinned DDS legacy C API: {exc}") from exc
    available_bytes = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    if available_bytes < 64 * 1024 * 1024:
        raise DDSConfigurationError("DDS initialization needs at least 64 MiB of available memory")
    # DDS 2.9.0 GetDDSInfo accesses worker memory, so initialization must precede it.
    library.SetResources(128, 1)
    info = _DDSInfo()
    library.GetDDSInfo(ctypes.byref(info))
    if (info.major, info.minor, info.patch) != (2, 9, 0) or info.threading != 0:
        raise DDSConfigurationError("DDS runtime must be version 2.9.0 compiled single-threaded")
    if info.noOfThreads != 1:
        raise DDSConfigurationError("DDS could not allocate its single solver worker")
    _LIBRARIES[key] = (library, digest)
    return library


def _free_libraries() -> None:
    with _NATIVE_LOCK:
        for library, _ in _LIBRARIES.values():
            library.FreeMemory()
        _LIBRARIES.clear()


atexit.register(_free_libraries)


class DDSAdapter:
    """Real, non-caching full-table solver with immutable build provenance."""

    def __init__(self, library_path: str | Path | None = None) -> None:
        path = _library_path(library_path)
        with _NATIVE_LOCK:
            metadata = _build_metadata(path)
            self._library = _load_library(path, metadata["library_sha256"])
        self._provenance = DDSProvenance(
            oracle_kind="dds",
            solver="DDS",
            version=DDS_VERSION,
            source_url=SOURCE_URL,
            source_commit=SOURCE_COMMIT,
            license="Apache-2.0",
            adapter_version=ADAPTER_VERSION,
            api="CalcDDtablePBN(ddTableDealPBN, ddTableResults*)",
            build_id=metadata["build_id"],
            library_sha256=metadata["library_sha256"],
            library_path=str(path),
            compiler=metadata["compiler"],
            compile_flags=tuple(metadata["compile_flags"]),
            patch_id=PATCH_ID,
            threads=1,
            thread_model="single native worker; process-wide serialized calls",
        )

    @property
    def provenance(self) -> DDSProvenance:
        return self._provenance

    def solve(self, deal: Deal) -> DDTable:
        pbn = deal_to_pbn(deal)
        encoded = pbn.encode("ascii")
        if len(encoded) >= 80:
            raise ValueError("complete PBN exceeds the DDS legacy 80-byte buffer")
        output = _TableResults()
        for row in output.resTable:
            for index in range(4):
                row[index] = -1
        with _NATIVE_LOCK:
            code = self._library.CalcDDtablePBN(_TableDealPBN(encoded), ctypes.byref(output))
            if code != 1:
                message = ctypes.create_string_buffer(80)
                self._library.ErrorMessage(code, message)
                raise DDSSolveError(code, message.value.decode("ascii", errors="replace"), pbn)
        values = tuple(
            tuple(output.resTable[DDS_STRAINS.index(strain)][seat_index] for strain in STRAINS)
            for seat_index, _ in enumerate(DECLARERS)
        )
        try:
            return DDTable(values)
        except ValueError as exc:
            raise DDSSolveError(code, f"invalid or incomplete 20-entry table: {exc}", pbn) from exc

    def par(self, table: DDTable, dealer: Seat, vulnerability: Vulnerability) -> dict:
        if not isinstance(table, DDTable) or not isinstance(dealer, Seat) or not isinstance(vulnerability, Vulnerability):
            raise TypeError("Dealer par requires a DDTable, Seat and Vulnerability.")
        native = _TableResults()
        for suit_index, strain in enumerate(DDS_STRAINS):
            for seat_index, seat in enumerate(DECLARERS):
                native.resTable[suit_index][seat_index] = table.tricks(seat, strain)
        output = _ParResults()
        vulnerable = {Vulnerability.NONE: 0, Vulnerability.BOTH: 1, Vulnerability.NS: 2, Vulnerability.EW: 3}[vulnerability]
        with _NATIVE_LOCK:
            code = self._library.DealerParBin(ctypes.byref(native), ctypes.byref(output), DECLARERS.index(dealer), vulnerable)
            if code != 1:
                message = ctypes.create_string_buffer(80)
                self._library.ErrorMessage(code, message)
                raise DDSParError(f"DDS DealerParBin failed ({code}): {message.value.decode('ascii', errors='replace')}")
        if not 1 <= output.number <= 10:
            raise DDSParError("DDS returned an invalid number of par contracts.")
        if output.score == 0 and output.number == 1 and output.contracts[0].level == 0:
            return {"score_ns": 0, "passed_out": True, "contracts": [], "api": "DealerParBin"}
        strains = (Strain.NOTRUMP, Strain.SPADES, Strain.HEARTS, Strain.DIAMONDS, Strain.CLUBS)
        positions = ((Seat.NORTH,), (Seat.EAST,), (Seat.SOUTH,), (Seat.WEST,),
                     (Seat.NORTH, Seat.SOUTH), (Seat.EAST, Seat.WEST))
        contracts = []
        for item in output.contracts[:output.number]:
            if (not 1 <= item.level <= 7 or not 0 <= item.denom <= 4 or not 0 <= item.seats <= 5
                    or not 0 <= item.underTricks <= 13 or not 0 <= item.overTricks <= 6
                    or item.underTricks and item.overTricks):
                raise DDSParError("DDS returned an invalid binary par contract.")
            strain, declarers = strains[item.denom], positions[item.seats]
            doubling = 2 if item.underTricks else 1
            tricks = 6 + item.level + item.overTricks - item.underTricks
            for declarer in declarers:
                contract = Contract(item.level, strain, declarer, doubling)
                if table.tricks(declarer, strain) != tricks or score_ns(contract, tricks, vulnerability) != output.score:
                    raise DDSParError("Dealer par contract disagrees with its DDS table, vulnerability or NS score.")
            contracts.append({"level": item.level, "strain": strain.value, "declarers": [seat.value for seat in declarers],
                              "doubling": doubling, "tricks": tricks})
        return {"score_ns": output.score, "passed_out": False, "contracts": contracts, "api": "DealerParBin"}

    def diagnostics(self, table: DDTable, dealer: Seat, vulnerability: Vulnerability) -> dict:
        return {"schema_version": 1, "dealer": dealer.value, "vulnerability": vulnerability.value,
                "table": table.to_dict(), "makeable_levels": table.makeable_levels(),
                "par": self.par(table, dealer, vulnerability), "oracle": self.provenance.to_dict()}
