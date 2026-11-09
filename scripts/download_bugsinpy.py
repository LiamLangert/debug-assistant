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
# Pinned so a fresh clone does not follow master if it moves.
PINNED_COMMIT = "11c5f1eea954a42132cfd06bf257766a7963e0fd"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "BugsInPy"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not (RAW_DIR / ".git").exists():
        print(f"Cloning {REPO_URL} -> {RAW_DIR} ...")
        git("init", cwd=RAW_DIR)
        git("remote", "add", "origin", REPO_URL, cwd=RAW_DIR)
    try:
        head = git("rev-parse", "HEAD", cwd=RAW_DIR)
    except subprocess.CalledProcessError:
        head = ""
    if head == PINNED_COMMIT:
        print(f"BugsInPy already at {PINNED_COMMIT}")
        return
    print(f"Fetching BugsInPy {PINNED_COMMIT} ...")
    git("fetch", "--depth", "1", "origin", PINNED_COMMIT, cwd=RAW_DIR)
    git("checkout", "--detach", "FETCH_HEAD", cwd=RAW_DIR)
    print(f"BugsInPy at commit {git('rev-parse', 'HEAD', cwd=RAW_DIR)}")


if __name__ == "__main__":
    main()
