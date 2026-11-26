#!/usr/bin/env python3
"""One-time setup for the ARK console: creates the .venv it runs in."""
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python() -> Path:
    if sys.platform.startswith("win"):
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def main() -> int:
    print("Setting up the ARK console (one time only)...")
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 or newer is needed. Install it from python.org")
        print("and tick 'Add python.exe to PATH', then run RUN-ARK-MAP.bat again.")
        return 1
    if not venv_python().exists():
        print("Creating the app environment...")
        try:
            venv.create(VENV, with_pip=True)
        except Exception as exc:
            print(f"ERROR: the app environment could not be created ({exc}).")
            print("The usual cause is a Python install without venv support -")
            print("reinstall Python from python.org with the default options,")
            print("then run RUN-ARK-MAP.bat again.")
            return 1
    # Completion marker: the launcher skips setup only when this
    # file exists. Keying the skip on pythonw.exe alone was a trap -
    # the venv creates that file BEFORE the marker is written, so a
    # first setup that died midway was never retried.
    (VENV / ".setup-complete").write_text(
        "setup finished\n", encoding="utf-8")
    print()
    print("Setup complete. From now on, just double-click RUN-ARK-MAP.bat")
    print("The first start will ask for your Nitrado API token and service ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
