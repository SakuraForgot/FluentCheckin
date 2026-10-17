"""Build a Windows bundle from main.py without collecting workspace data."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Build Fluent Check-in for Windows")
    parser.add_argument(
        "--onefile", action="store_true", help="Build a single EXE instead of a folder"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Build a console variant for CLI use/debugging",
    )
    parser.add_argument(
        "--output",
        default="dist",
        help="Output root inside the project (default: dist)",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run this script on Windows to build a Windows package")
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import PyInstaller, playwright, PySide6, qfluentwidgets, filelock",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode:
        parser.error("Missing build dependencies. Run: python setup.py --build")
    subprocess.run(
        [str(python), str(ROOT / "tools/render_icon.py")], check=True, cwd=ROOT
    )
    name = "FluentCheckin" + ("-CLI" if args.console else "")
    mode = "onefile" if args.onefile else "onedir"
    work = ROOT / "build" / (name + "-" + mode)
    dist = (ROOT / args.output / mode).resolve()
    data = dist / "data" if args.onefile else dist / name / "data"
    if data.exists():
        parser.error(
            "Output contains runtime data. Use --output dist/release-new for a clean build."
        )
    # Reject redirected output directories before PyInstaller can replace files.
    for path in (work, dist):
        if not path.resolve().is_relative_to(ROOT):
            parser.error("Build output must remain inside the project directory")
        path.mkdir(parents=True, exist_ok=True)
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.console else "--windowed",
        "--noupx",
        "--icon",
        str(ROOT / "checkin/assets/app.ico"),
        "--add-data",
        str(ROOT / "checkin/assets") + ":checkin/assets",
        "--workpath",
        str(work / "temp"),
        "--specpath",
        str(work),
        "--distpath",
        str(dist),
        "--collect-data",
        "qfluentwidgets",
        "--collect-data",
        "qframelesswindow",
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "PyQt5",
        "--exclude-module",
        "PyQt6",
        "--exclude-module",
        "PySide2",
        str(ROOT / "main.py"),
    ]
    # Ship the project notices and dependency license files with the executable.
    for document in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md"):
        command.extend(["--add-data", str(ROOT / document) + ":."])
    for distribution in (
        "PySide6",
        "PySide6-Fluent-Widgets",
        "playwright",
        "filelock",
    ):
        command.extend(["--recursive-copy-metadata", distribution])
    environment = os.environ.copy()
    system_root = Path(environment.get("SYSTEMROOT", "C:/Windows"))
    # External tools (e.g. Poppler) may put incompatible ICU/Qt DLLs on PATH.
    # Limit dependency discovery to this interpreter and Windows system folders.
    environment["PATH"] = os.pathsep.join(
        (str(python.parent), str(system_root / "System32"), str(system_root))
    )
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    ):
        environment.pop(key, None)
    subprocess.run(command, check=True, cwd=ROOT, env=environment)
    package = dist / (name + ".exe" if args.onefile else name)
    executable = package if args.onefile else package / (name + ".exe")
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not produce the expected executable")
    if args.onefile:
        artifact = package
    else:
        # Only archive the fresh PyInstaller output, never local user data.
        if (package / "data").exists():
            raise RuntimeError(
                "Close the app and rebuild: release output must not contain data"
            )
        artifact = Path(
            shutil.make_archive(str(dist / name), "zip", root_dir=dist, base_dir=name)
        )
    with artifact.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    artifact.with_suffix(artifact.suffix + ".sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )
    print(f"Executable: {executable}\nPackage: {artifact}\nSHA256: {digest}")
    print(
        "Target machine needs Edge, Chrome or Brave. Local account data is not included."
    )


if __name__ == "__main__":
    main()
