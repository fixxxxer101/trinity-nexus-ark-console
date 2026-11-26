#!/usr/bin/env python3
"""One-time setup for the ARK map: creates .venv and installs arkparser."""
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
    print("Setting up the ARK map (one time only)...")
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 or newer is needed. Install it from python.org")
        print("and tick 'Add python.exe to PATH', then run RUN-ARK-MAP.bat again.")
        return 1
    if not venv_python().exists():
        print("Creating the app environment...")
        venv.create(VENV, with_pip=True)
    py = str(venv_python())
    print("Installing the ARK save reader (arkparser)...")
    r = subprocess.run([py, "-m", "pip", "install", "--upgrade", "arkparser"])
    if r.returncode != 0:
        print("ERROR: pip install failed - check the internet connection and run RUN-ARK-MAP.bat again.")
        return 1
    r = subprocess.run([py, "-c", "import arkparser; print('arkparser ready')"])
    if r.returncode != 0:
        print("ERROR: arkparser installed but would not import. Run setup again;")
        print("if it keeps failing, note what this window says and check")
        print("the troubleshooting section of the README.")
        return 1
    print()
    print("Setup complete. From now on, just double-click RUN-ARK-MAP.bat")
    print("The first start will ask for your Nitrado API token and service ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
