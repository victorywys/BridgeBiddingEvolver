"""Environment inspection with an actual pinned DDS self-test and no secrets."""

import platform
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from importlib.util import find_spec
from typing import Literal

from bridge_evolve import __version__
from bridge_evolve.oracle import DDSAdapter, DDSError, DDSUnavailableError
from bridge_evolve.oracle.verification import verify_adapter

MINIMUM_PYTHON = (3, 11)
DDS_MODULE = "dds3"


@dataclass(frozen=True)
class DDSStatus:
    available: bool
    adapter_status: Literal["verified", "unavailable", "failed"]
    binding_module: str
    binding_detected: bool
    binding_version: str | None
    detail: str
    library_path: str | None = None
    solver_version: str | None = None
    source_commit: str | None = None
    build_id: str | None = None
    library_sha256: str | None = None


@dataclass(frozen=True)
class EnvironmentReport:
    schema_version: int
    package_version: str
    python_version: str
    python_executable: str
    python_supported: bool
    platform: str
    dds: DDSStatus


def inspect_dds() -> DDSStatus:
    detected = find_spec(DDS_MODULE) is not None
    binding_version = None
    if detected:
        try:
            binding_version = distribution_version(DDS_MODULE)
        except PackageNotFoundError:
            detail = "dds3 is discoverable, but its distribution version is unavailable."
        else:
            detail = "dds3 is discoverable; importing and solving have not been verified."
    else:
        detail = "No dds3 Python module was found."

    try:
        adapter = DDSAdapter()
    except DDSError as exc:
        return DDSStatus(
            available=False,
            adapter_status="unavailable" if isinstance(exc, DDSUnavailableError) else "failed",
            binding_module=DDS_MODULE,
            binding_detected=detected,
            binding_version=binding_version,
            detail=f"{detail} ctypes DDS adapter unavailable: {exc}",
        )
    try:
        verify_adapter(adapter)
    except DDSError as exc:
        return DDSStatus(
            available=False,
            adapter_status="failed",
            binding_module=DDS_MODULE,
            binding_detected=detected,
            binding_version=binding_version,
            detail=f"{detail} DDS library loaded but real solve verification failed: {exc}",
            library_path=adapter.provenance.library_path,
            solver_version=adapter.provenance.version,
            source_commit=adapter.provenance.source_commit,
            build_id=adapter.provenance.build_id,
            library_sha256=adapter.provenance.library_sha256,
        )
    return DDSStatus(
        available=True,
        adapter_status="verified",
        binding_module=DDS_MODULE,
        binding_detected=detected,
        binding_version=binding_version,
        detail=(
            f"{detail} Independent of Python module detection, ctypes DDS "
            f"{adapter.provenance.version} passed a real 20-entry published-fixture solve."
        ),
        library_path=adapter.provenance.library_path,
        solver_version=adapter.provenance.version,
        source_commit=adapter.provenance.source_commit,
        build_id=adapter.provenance.build_id,
        library_sha256=adapter.provenance.library_sha256,
    )


def collect_diagnostics() -> EnvironmentReport:
    return EnvironmentReport(
        schema_version=1,
        package_version=__version__,
        python_version=platform.python_version(),
        python_executable=sys.executable,
        python_supported=sys.version_info[:2] >= MINIMUM_PYTHON,
        platform=platform.platform(),
        dds=inspect_dds(),
    )
