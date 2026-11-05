import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge_evolve import __version__
from bridge_evolve.diagnostics import (
    PackageNotFoundError,
    collect_diagnostics,
    inspect_dds,
)
from bridge_evolve.oracle import DDSConfigurationError, DDSUnavailableError


class DDSDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter_patch = patch(
            "bridge_evolve.diagnostics.DDSAdapter",
            side_effect=DDSUnavailableError("native library absent"),
        )
        self.adapter = self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)

    def test_missing_binding_is_not_a_working_solver(self) -> None:
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=None) as find,
            patch("bridge_evolve.diagnostics.distribution_version") as version,
        ):
            status = inspect_dds()
        find.assert_called_once_with("dds3")
        version.assert_not_called()
        self.assertFalse(status.available)
        self.assertFalse(status.binding_detected)
        self.assertIsNone(status.binding_version)
        self.assertEqual(status.adapter_status, "unavailable")
        self.assertIn("No dds3 Python module", status.detail)
        self.assertIn("native library absent", status.detail)

    def test_detected_binding_does_not_imply_verified_dds(self) -> None:
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=object()),
            patch(
                "bridge_evolve.diagnostics.distribution_version",
                return_value="test-distribution-version",
            ) as version,
        ):
            status = inspect_dds()
        version.assert_called_once_with("dds3")
        self.assertTrue(status.binding_detected)
        self.assertEqual(status.binding_version, "test-distribution-version")
        self.assertFalse(status.available)
        self.assertIn("not been verified", status.detail)

    def test_missing_distribution_metadata_is_explicit(self) -> None:
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=object()),
            patch(
                "bridge_evolve.diagnostics.distribution_version",
                side_effect=PackageNotFoundError("dds3"),
            ),
        ):
            status = inspect_dds()
        self.assertTrue(status.binding_detected)
        self.assertIsNone(status.binding_version)
        self.assertFalse(status.available)
        self.assertIn("distribution version is unavailable", status.detail)

    def test_loaded_module_is_not_a_successful_solve(self) -> None:
        self.adapter.side_effect = DDSConfigurationError("wrong native version")
        with patch("bridge_evolve.diagnostics.find_spec", return_value=None):
            status = inspect_dds()
        self.assertFalse(status.available)
        self.assertEqual(status.adapter_status, "failed")
        self.assertIn("wrong native version", status.detail)

    def test_failed_real_self_test_reports_loaded_but_unavailable(self) -> None:
        self.adapter.side_effect = None
        self.adapter.return_value.provenance = SimpleNamespace(
            library_path="local/libdds.so", version="2.9.0", source_commit="pin",
            build_id="build", library_sha256="digest",
        )
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=None),
            patch(
                "bridge_evolve.diagnostics.verify_adapter",
                side_effect=DDSConfigurationError("published table mismatch"),
            ) as verify,
        ):
            status = inspect_dds()
        verify.assert_called_once_with(self.adapter.return_value)
        self.assertFalse(status.available)
        self.assertEqual(status.adapter_status, "failed")
        self.assertEqual(status.library_path, "local/libdds.so")
        self.assertIn("published table mismatch", status.detail)

    def test_actual_adapter_does_not_require_a_python_dds_module(self) -> None:
        self.adapter.side_effect = None
        self.adapter.return_value.provenance = SimpleNamespace(
            library_path="local/libdds.so", version="2.9.0", source_commit="pin",
            build_id="build", library_sha256="digest",
        )
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=None),
            patch("bridge_evolve.diagnostics.verify_adapter") as verify,
        ):
            status = inspect_dds()
        verify.assert_called_once_with(self.adapter.return_value)
        self.assertTrue(status.available)
        self.assertFalse(status.binding_detected)
        self.assertEqual(status.adapter_status, "verified")
        self.assertEqual(status.solver_version, "2.9.0")
        self.assertIn("20-entry", status.detail)

    def test_unexpected_metadata_errors_are_not_hidden(self) -> None:
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=object()),
            patch(
                "bridge_evolve.diagnostics.distribution_version",
                side_effect=OSError("unreadable package metadata"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "unreadable package metadata"):
                inspect_dds()


class EnvironmentDiagnosticsTests(unittest.TestCase):
    def test_python_support_boundary(self) -> None:
        with (
            patch("bridge_evolve.diagnostics.find_spec", return_value=None),
            patch(
                "bridge_evolve.diagnostics.DDSAdapter",
                side_effect=DDSUnavailableError("native library absent"),
            ),
        ):
            for version, supported in (((3, 10, 0), False), ((3, 11, 0), True)):
                with self.subTest(version=version):
                    with patch("bridge_evolve.diagnostics.sys.version_info", version):
                        report = collect_diagnostics()
                    self.assertEqual(report.python_supported, supported)
                    self.assertEqual(report.package_version, __version__)


if __name__ == "__main__":
    unittest.main()
