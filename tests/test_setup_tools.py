import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync.models import SyncError
from calendar_busy_sync.setup_tools import _install_gcloud, temporary_cli


class SetupToolTests(unittest.TestCase):
    def test_decline_does_not_fetch(self):
        with (
            patch("calendar_busy_sync.setup_tools.shutil.which", return_value=None),
            patch("calendar_busy_sync.setup_tools._download") as download,
        ):
            with temporary_cli("gcloud", lambda _prompt: False) as tool:
                self.assertIsNone(tool)
            download.assert_not_called()

    def test_global_binary_is_reused_with_private_config_and_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "gcloud"
            binary.write_text("unchanged")
            with (
                patch("calendar_busy_sync.setup_tools.shutil.which", return_value=str(binary)),
                temporary_cli(
                    "gcloud", lambda _prompt: self.fail("confirmation should not be asked")
                ) as tool,
            ):
                self.assertEqual(tool.executable, str(binary))
                config = Path(tool.environment["CLOUDSDK_CONFIG"])
                self.assertTrue(config.is_dir())
                self.assertNotEqual(config, Path.home() / ".config" / "gcloud")
                self.assertTrue(binary.exists())
            self.assertFalse(config.parent.exists())
            self.assertEqual(binary.read_text(), "unchanged")

    def test_temporary_payload_is_removed_after_success_error_and_keyboard_interrupt(self):
        failures = (None, RuntimeError("operation failed"), KeyboardInterrupt())
        for failure in failures:
            with self.subTest(failure=type(failure).__name__ if failure else "success"):
                captured = {}
                with (
                    patch("calendar_busy_sync.setup_tools.shutil.which", return_value=None),
                    patch("calendar_busy_sync.setup_tools._install_az", side_effect=self._fake_az_install),
                ):
                    try:
                        with temporary_cli("az", lambda _prompt: True) as tool:
                            captured["root"] = Path(tool.environment["AZURE_CONFIG_DIR"]).parent
                            self.assertTrue(Path(tool.executable).is_file())
                            if failure:
                                raise failure
                    except (RuntimeError, KeyboardInterrupt):
                        if failure is None:
                            raise
                self.assertFalse(captured["root"].exists())

    @staticmethod
    def _fake_az_install(root, environment):
        executable = root / "fake-az"
        executable.write_text("temporary")
        return str(executable)

    def test_gcloud_integrity_failure_refuses_to_extract_or_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_bytes = b"not the published archive"
            with (
                patch("calendar_busy_sync.setup_tools.platform.machine", return_value="x86_64"),
                patch(
                    "calendar_busy_sync.setup_tools._download",
                    side_effect=lambda _url, path, **_kw: path.write_bytes(archive_bytes),
                ),
                patch("calendar_busy_sync.setup_tools.tarfile.open") as extract,
            ):
                with self.assertRaises(SyncError):
                    _install_gcloud(root)
                extract.assert_not_called()

    def test_gcloud_archive_extraction_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("../escaped")
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
            with (
                patch("calendar_busy_sync.setup_tools.platform.machine", return_value="x86_64"),
                patch(
                    "calendar_busy_sync.setup_tools._GCLOUD_ARCHIVES",
                    {"x86_64": ("x86_64", hashlib.sha256(archive.read_bytes()).hexdigest())},
                ),
                patch(
                    "calendar_busy_sync.setup_tools._download",
                    side_effect=lambda _url, path, **_kw: path.write_bytes(archive.read_bytes()),
                ),
            ):
                with self.assertRaises(SyncError):
                    _install_gcloud(root)
            self.assertFalse((root.parent / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
