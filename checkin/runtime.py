"""Locate installed browsers without reading their user profiles."""

import os
from pathlib import Path
import sys

BROWSERS = {
    "msedge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "brave": "Brave",
    "chromium": "Playwright Chromium",
}
INSTALLATIONS = {
    "msedge": ("Microsoft/Edge/Application/msedge.exe", "msedge.exe"),
    "chrome": ("Google/Chrome/Application/chrome.exe", "chrome.exe"),
    "brave": ("BraveSoftware/Brave-Browser/Application/brave.exe", "brave.exe"),
}


class BrowserUnavailable(Exception):
    pass


def registry_paths(executable):
    if sys.platform != "win32":
        return []
    import winreg

    paths = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive,
                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}",
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    if isinstance(value, str):
                        paths.append(Path(os.path.expandvars(value.strip('"'))))
            except OSError:
                continue
    return paths


def browser_path(name, custom=""):
    if name not in BROWSERS:
        raise BrowserUnavailable("未知浏览器，请在设置中重新选择。")
    if custom:
        path = Path(custom)
        if path.is_absolute() and path.suffix.lower() == ".exe" and path.is_file():
            return path
        raise BrowserUnavailable("所选浏览器程序不存在或不是 EXE，请在设置中重新选择。")
    if name == "chromium":
        return None  # Playwright resolves its own versioned installation.
    relative, executable = INSTALLATIONS[name]
    candidates = [
        Path(os.environ[key]) / relative
        for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
        if os.environ.get(key)
    ]
    candidates.extend(registry_paths(executable))
    for path in candidates:
        if path.is_file():
            return path
    raise BrowserUnavailable(
        f"未找到 {BROWSERS[name]}。请安装浏览器，或在设置中选择其 EXE。"
    )


def launch_options(store):
    name = store.setting("browser", "msedge")
    custom = store.setting("browser_paths", {}).get(name, "")
    path = browser_path(name, custom)
    if name == "chromium":
        return {"executable_path": str(path)} if path else {"channel": None}
    # Brave exposes Chromium's automation protocol but is not a named PW channel.
    return {
        "channel": "chromium" if name == "brave" else name,
        "executable_path": str(path),
    }
