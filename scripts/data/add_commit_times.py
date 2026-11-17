"""Add each bug's buggy commit time to data/processed/bugs.jsonl.

BugsInPy records only commit ids, so the time comes from the project's cached
bare repo (data/cache/repos/<project>.git). run_baseline.py uses it as the
cutoff for Stack Overflow search: nothing posted after the buggy commit.

Adds "buggy_commit_time" (committer time, unix seconds) to each bug whose
project repo is cached. With --clone, missing repos are cloned first.
parse_bugsinpy.py rewrites bugs.jsonl without this field, so rerun this after it.

    uv run scripts/data/add_commit_times.py [--clone]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "baseline"))
from checkout_bug import BUGS_FILE, REPOS, cache_repo, git  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clone", action="store_true", help="clone project repos that are not cached")
    args = parser.parse_args()

    bugs = [json.loads(line) for line in BUGS_FILE.open()]
    missing = set()
    for bug in bugs:
        project, commit = bug["project"], bug["buggy_commit"]
        if not (REPOS / f"{project}.git").exists() and not args.clone:
            missing.add(project)
            continue
        repo = cache_repo(project, [commit])
        r = git("log", "-1", "--format=%ct", commit, cwd=repo, check=False)
        if r.returncode == 0:
            bug["buggy_commit_time"] = int(r.stdout.strip())
        else:
            print(f"{project} {bug['bug_id']}: commit {commit[:10]} not found")

    with BUGS_FILE.open("w") as f:
        for bug in bugs:
            f.write(json.dumps(bug) + "\n")
    done = sum("buggy_commit_time" in b for b in bugs)
    print(f"{done} of {len(bugs)} bugs have a commit time -> {BUGS_FILE.relative_to(ROOT)}")
    if missing:
        print(f"no cached repo (use --clone): {', '.join(sorted(missing))}")


if __name__ == "__main__":
    main()
