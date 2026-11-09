"""Download the BugsInPy dataset into data/raw/BugsInPy.

BugsInPy is a git repo of real bugs from 17 Python projects.
Each bug lives at projects/<project>/bugs/<id>/ with:
  bug.info          buggy/fixed commit ids and the failing test file
  bug_patch.txt     the fixing diff
  run_test.sh       command(s) that run the failing test
  requirements.txt  pinned deps for the buggy version

Usage:
    uv run scripts/download_bugsinpy.py   # clone, or update if already present
"""

import subprocess
from pathlib import Path

REPO_URL = "https://github.com/soarsmu/BugsInPy.git"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "BugsInPy"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    if (RAW_DIR / ".git").exists():
        print(f"Updating {RAW_DIR} ...")
        git("fetch", "--depth", "1", "origin", "HEAD", cwd=RAW_DIR)
        git("checkout", "--detach", "FETCH_HEAD", cwd=RAW_DIR)
    else:
        print(f"Cloning {REPO_URL} -> {RAW_DIR} ...")
        RAW_DIR.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--depth", "1", REPO_URL, str(RAW_DIR))
    print(f"BugsInPy at commit {git('rev-parse', 'HEAD', cwd=RAW_DIR)}")


if __name__ == "__main__":
    main()
