"""Check out source files for a small smoke-test subset of BugsInPy bugs.

BugsInPy ships only metadata; the real project code appears only when you
check out a bug's commits. For the walkthrough we do NOT need all 501
bugs -- just a handful from one small project. This script clones a
project once, then for each selected bug copies the files we actually need:
  - the failing test files, from the FIXED commit (used to build the query)
  - each buggy source file the fix touched, from the BUGGY commit
    (used by the second BM25 stage)

The tests come from the fixed commit because that is where the failing test
usually lives: the fix often adds it, or adds a file for it. BugsInPy's own
bugsinpy-checkout does the same, copying the fixed version's test files into
the buggy checkout.

Output: data/processed/checkouts/<project>/<bug_id>/
            tests/<repo path>       each failing test file, from the fixed commit
            buggy/<repo path>       each buggy source file the fix touched

Files keep their repo paths under tests/ and buggy/, so a file's path relative
to that folder is its path in the repo, e.g. buggy/tqdm/contrib/__init__.py.

Usage:
    uv run scripts/smoketest/checkout_bugs_smoke.py --project PySnooper --n 5
"""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUGS_FILE = ROOT / "data" / "processed" / "bugs.jsonl"
PROJECTS_DIR = ROOT / "data" / "raw" / "BugsInPy" / "projects"
OUT_DIR = ROOT / "data" / "processed" / "checkouts"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def project_github_url(project: str) -> str:
    """Read the project's github_url from its project.info."""
    info = (PROJECTS_DIR / project / "project.info").read_text()
    for line in info.splitlines():
        if line.startswith("github_url="):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError(f"no github_url for {project}")


def load_bugs(project: str, n: int) -> list[dict]:
    """Load the first n bugs for the given project from bugs.jsonl."""
    bugs = []
    with BUGS_FILE.open() as f:
        for line in f:
            r = json.loads(line)
            if r["project"] == project:
                bugs.append(r)
            if len(bugs) >= n:
                break
    return bugs


def copy_into(clone_dir: Path, rel: str, dest_root: Path) -> bool:
    """Copy repo file `rel` to dest_root/rel, keeping its directories."""
    src = clone_dir / rel
    if not src.exists():
        return False
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return True


def checkout(clone_dir: Path, bug: dict, which: str) -> bool:
    """Check out the bug's buggy or fixed commit. Return False if it fails."""
    commit = bug[f"{which}_commit"]
    print(f"  bug {bug['bug_id']}: checkout {which} {commit[:10]}")
    try:
        git("checkout", "--detach", commit, cwd=clone_dir)
        return True
    except subprocess.CalledProcessError:
        print(f"    (could not checkout {commit}, skipping)")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="PySnooper")
    ap.add_argument("--n", type=int, default=5, help="number of bugs")
    args = ap.parse_args()

    bugs = load_bugs(args.project, args.n)
    if not bugs:
        print(f"No bugs found for project {args.project}")
        return

    url = project_github_url(args.project)
    print(f"Cloning {url} (once) ...")

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / args.project
        git("clone", url, str(clone_dir))   

        for bug in bugs:
            bug_id = bug["bug_id"]
            out = OUT_DIR / args.project / bug_id
            # Start clean so files from an older layout do not linger.
            if out.exists():
                shutil.rmtree(out)
            (out / "tests").mkdir(parents=True)
            (out / "buggy").mkdir()

            # The failing test files, from the fixed commit -> the query source
            if not checkout(clone_dir, bug, "fixed"):
                continue
            for test_file in bug.get("test_files", []):
                if not copy_into(clone_dir, test_file, out / "tests"):
                    print(f"    test file missing at the fixed commit: {test_file}")

            # Each buggy source file the fix touched -> second BM25 stage
            if not checkout(clone_dir, bug, "buggy"):
                continue
            for change in bug.get("changes", []):
                if not copy_into(clone_dir, change["file"], out / "buggy"):
                    print(f"    buggy file missing at the buggy commit: {change['file']}")

    print(f"\nDone. Checkouts saved under {OUT_DIR / args.project}")


if __name__ == "__main__":
    main()