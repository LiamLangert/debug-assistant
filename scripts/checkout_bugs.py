"""Check out source files for a small smoke-test subset of BugsInPy bugs.

BugsInPy ships only metadata; the real project code appears only when you
check out a bug's buggy commit. For the walkthrough we do NOT need all 501
bugs -- just a handful from one small project. This script shallow-clones a
project once, then for each selected bug checks out its buggy commit and
copies the files we actually need:
  - the failing test file  (used to build the query)
  - each buggy file the fix touched (used by the second BM25 stage)

Output: data/processed/checkouts/<project>/<bug_id>/
            test__<testfile>        the failing test's source
            buggy__<path>           each buggy file the fix touched

Usage:
    uv run scripts/checkout_bugs.py --project PySnooper --n 5
"""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def safe_name(path: str) -> str:
    """Flatten a path into a single filename."""
    return path.replace("/", "__")


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
            commit = bug["buggy_commit"]
            print(f"  bug {bug_id}: checkout {commit[:10]}")
            try:
                git("checkout", "--detach", commit, cwd=clone_dir)
            except subprocess.CalledProcessError:
                print(f"    (could not checkout {commit}, skipping)")
                continue

            out = OUT_DIR / args.project / bug_id
            out.mkdir(parents=True, exist_ok=True)

            # The failing test file -> the query source
            test_file = bug.get("test_file")
            if test_file:
                src = clone_dir / test_file
                if src.exists():
                    shutil.copy(src, out / f"test__{safe_name(test_file)}")
                else:
                    print(f"    test file missing at this commit: {test_file}")

            # Each buggy file the fix touched -> second BM25 stage
            for change in bug.get("changes", []):
                fpath = change["file"]
                src = clone_dir / fpath
                if src.exists():
                    shutil.copy(src, out / f"buggy__{safe_name(fpath)}")
                else:
                    print(f"    buggy file missing at this commit: {fpath}")

    print(f"\nDone. Checkouts saved under {OUT_DIR / args.project}")


if __name__ == "__main__":
    main()