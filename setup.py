"""Create the local runtime; use --build to include packaging tools."""

import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Install Fluent Check-in locally")
    parser.add_argument("--build", action="store_true", help="Also install PyInstaller")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or later is required")
    python = (
        ROOT
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    requirements = "requirements-build.txt" if args.build else "requirements.txt"
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(ROOT / requirements)],
        check=True,
        cwd=ROOT,
    )
    print("Ready. Run .venv/Scripts/python.exe main.py (or double-click start.cmd).")


if __name__ == "__main__":
    main()
