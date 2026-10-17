import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checkin.runtime import BrowserUnavailable, browser_path, launch_options
from checkin.vault import Store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_standard_browser_paths_and_channels(self):
        cases = (
            ("msedge", "Microsoft/Edge/Application/msedge.exe", "msedge"),
            ("chrome", "Google/Chrome/Application/chrome.exe", "chrome"),
            ("brave", "BraveSoftware/Brave-Browser/Application/brave.exe", "chromium"),
        )
        for name, relative, channel in cases:
            expected = self.executable(relative)
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=True),
                patch("checkin.runtime.registry_paths", return_value=[]),
            ):
                self.store.set_setting("browser", name)
                self.assertEqual(
                    launch_options(self.store),
                    {"channel": channel, "executable_path": str(expected)},
                )

    def test_registry_and_custom_paths_without_silent_fallback(self):
        registered = self.executable("registered/brave.exe")
        custom = self.executable("portable/my browser.exe")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("checkin.runtime.registry_paths", return_value=[registered]),
        ):
            self.assertEqual(browser_path("brave"), registered)
            self.assertEqual(browser_path("brave", str(custom)), custom)
            with self.assertRaises(BrowserUnavailable):
                browser_path("brave", str(self.root / "missing.exe"))
        self.store.set_setting("browser", "brave")
        self.store.set_setting(
            "browser_paths", {"brave": str(custom), "chrome": "unused.exe"}
        )
        self.assertEqual(launch_options(self.store)["executable_path"], str(custom))

    def test_missing_browser_and_chromium_resolution(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("checkin.runtime.registry_paths", return_value=[]),
        ):
            for name in ("msedge", "chrome", "brave", "unknown"):
                with self.subTest(name=name), self.assertRaises(BrowserUnavailable):
                    browser_path(name)
        self.store.set_setting("browser", "chromium")
        self.assertEqual(launch_options(self.store), {"channel": None})
