from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checkin.vault import Store, default_root


class PortableTests(unittest.TestCase):
    def test_data_follows_source_or_executable_not_working_directory(self):
        with patch("checkin.vault.sys.frozen", False, create=True):
            self.assertEqual(
                default_root(), Path(__file__).resolve().parent.parent / "data"
            )
        with (
            tempfile.TemporaryDirectory() as root,
            patch("checkin.vault.sys.frozen", True, create=True),
            patch(
                "checkin.vault.sys.executable", str(Path(root) / "FluentCheckin.exe")
            ),
            patch("checkin.vault.sys._MEIPASS", "some-extraction-folder", create=True),
        ):
            self.assertEqual(default_root(), Path(root) / "data")
            store = Store()
            self.assertEqual(store.root, Path(root) / "data")
            self.assertEqual(store.accounts(), [])
            self.assertTrue((store.root / "app.sqlite3").is_file())

    def test_explicit_root_still_overrides_portable_default(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            self.assertEqual(store.root, Path(root))
