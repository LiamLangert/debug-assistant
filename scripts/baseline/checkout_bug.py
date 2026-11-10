"""Check out one BugsInPy bug with BugsInPy's own bugsinpy-checkout.

The bug gets two checkouts, which later steps compile and test:
  buggy  the buggy commit, with the failing test files from the fixed commit
  fixed  the same, plus the files the fix commit changed

Output: data/work/<project>/<bug_id>/<version>/
            <project>/       the checkout: full source tree plus bugsinpy_* files
            checkout.log     bugsinpy-checkout's output
            status.json      {"ok": bool, "problems": [...], "repairs": [...]}

bugsinpy-checkout exits 0 even when it fails, so every checkout is checked
against git instead: HEAD, the test files, and (for fixed) the fixed sources.

Two BugsInPy gaps are repaired after it runs, and listed under "repairs":
  - For the fixed version it copies in the files `git show --name-only <fix>`
    lists. For a merge commit (10 scrapy bugs) git lists only files that differ
    from both parents, so the fix's files are often left out. We copy every
    file that differs between the buggy and fixed commits from the fixed one.
  - tornado bug 16 has no requirements.txt, and bugsinpy-compile refuses to
    run without one. Its setup.sh installs what it needs, so we write an empty one.

Two things keep the pinned dataset untouched and the run fast:
  - bugsinpy-checkout uses projects/<p>/bugs/<id>/ as scratch space, so it
    runs from a copy of BugsInPy in data/cache/bugsinpy/, never the pinned one.
  - Each project is cloned once, bare, into data/cache/repos/. Each checkout
    starts from a local clone of that (hardlinked, near instant). The
    `git clone` inside bugsinpy-checkout then fails because the directory
    exists, and the script carries on with our clone, as BugsInPy intends
    for a reused work directory.

A version already checked out OK is skipped, unless --force is given.
Exits 1 if either version fails its checks.

Usage:
    uv run scripts/baseline/checkout_bug.py --project PySnooper --bug 2 [--force]

From Python: checkout_bug("PySnooper", "2") returns {version: status}.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUGS_FILE = ROOT / "data" / "processed" / "bugs.jsonl"
PINNED = ROOT / "data" / "raw" / "BugsInPy"
SCRATCH = ROOT / "data" / "cache" / "bugsinpy"
REPOS = ROOT / "data" / "cache" / "repos"
WORK = ROOT / "data" / "work"
VERSIONS = {"buggy": "0", "fixed": "1"}
BUGSINPY_FILES = ["bugsinpy_bug.info", "bugsinpy_requirements.txt", "bugsinpy_run_test.sh"]


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def pinned_commit() -> str:
    return git("rev-parse", "HEAD", cwd=PINNED).stdout.strip()


def prepare_scratch() -> None:
    """Copy BugsInPy's framework and projects to SCRATCH, once per pinned commit."""
    marker = SCRATCH / ".commit"
    commit = pinned_commit()
    if marker.exists() and marker.read_text().strip() == commit:
        return
    print(f"Copying BugsInPy {commit[:10]} -> {SCRATCH}")
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    for part in ("framework", "projects"):
        shutil.copytree(PINNED / part, SCRATCH / part)
    marker.write_text(commit + "\n")


def github_url(project: str) -> str:
    for line in (PINNED / "projects" / project / "project.info").read_text().splitlines():
        if line.startswith("github_url="):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError(f"no github_url for {project}")


def has_commit(repo: Path, commit: str) -> bool:
    return git("cat-file", "-e", f"{commit}^{{commit}}", cwd=repo, check=False).returncode == 0


def cache_repo(project: str, commits: list[str]) -> Path:
    """Bare clone of the project, holding the given commits."""
    repo = REPOS / f"{project}.git"
    if not repo.exists():
        print(f"Cloning {github_url(project)} -> {repo} (once)")
        REPOS.mkdir(parents=True, exist_ok=True)
        git("clone", "--bare", github_url(project), str(repo))
    for commit in commits:
        # A commit on no branch is missing from the clone. GitHub serves it by full id.
        if not has_commit(repo, commit) and len(commit) == 40:
            print(f"  fetching {commit[:10]}, which is on no branch")
            if git("fetch", "origin", commit, cwd=repo, check=False).returncode == 0:
                # Keep a ref so it is never garbage-collected.
                git("update-ref", f"refs/bugsinpy/{commit}", commit, cwd=repo)
    return repo


def file_at(repo: Path, commit: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def repair(bug: dict, version: str, checkout: Path) -> list[str]:
    """Fill the BugsInPy gaps described above. Returns what was changed."""
    repairs = []
    if (checkout / ".git").exists() and not (checkout / "bugsinpy_requirements.txt").exists():
        (checkout / "bugsinpy_requirements.txt").write_text("")
        repairs.append("wrote an empty bugsinpy_requirements.txt (BugsInPy has none)")
    if version == "fixed" and (checkout / ".git").exists():
        buggy, fixed = bug["buggy_commit"], bug["fixed_commit"]
        diff = git("diff", "--name-only", "--no-renames", buggy, fixed, cwd=checkout)
        for path in diff.stdout.splitlines():
            want = file_at(checkout, fixed, path)
            local = checkout / path
            if want is None:
                if local.exists():
                    local.unlink()
                    repairs.append(f"deleted {path}, as the fixed commit does")
            elif not local.exists() or local.read_bytes() != want:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(want)
                repairs.append(f"took {path} from the fixed commit")
    return repairs


def verify(bug: dict, version: str, checkout: Path) -> list[str]:
    """What is wrong with a checkout. Empty means it is OK."""
    if not (checkout / ".git").exists():
        return ["no checkout"]
    problems = []
    head = git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    buggy = git("rev-parse", f"{bug['buggy_commit']}^{{commit}}", cwd=checkout, check=False)
    if buggy.returncode != 0 or head != buggy.stdout.strip():
        problems.append(f"HEAD is {head[:10]}, not the buggy commit")

    for name in BUGSINPY_FILES:
        if not (checkout / name).exists():
            problems.append(f"{name} missing")

    # Both versions run the failing tests as they are at the fixed commit.
    fixed = bug["fixed_commit"]
    for path in bug["test_files"]:
        local = checkout / path
        if not local.exists():
            problems.append(f"test file missing: {path}")
        elif local.read_bytes() != file_at(checkout, fixed, path):
            problems.append(f"test file differs from the fixed commit: {path}")

    sources = [c["file"] for c in bug["changes"]]
    if version == "fixed":
        for path in sources:
            local = checkout / path
            if not local.exists() or local.read_bytes() != file_at(checkout, fixed, path):
                problems.append(f"source not at the fixed commit: {path}")
    else:
        # The buggy tree may differ from the buggy commit only in the test files.
        status = git("status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout
        allowed = set(bug["test_files"]) | set(BUGSINPY_FILES) | {"bugsinpy_setup.sh",
                                                                   "bugsinpy_patchfile.info"}
        for line in status.splitlines():
            path = line[3:].strip('"')
            if path not in allowed:
                problems.append(f"unexpected change in buggy tree: {path}")
    return problems


def checkout_version(bug: dict, version: str, repo: Path, force: bool) -> dict:
    project, bug_id = bug["project"], bug["bug_id"]
    out = WORK / project / bug_id / version
    checkout = out / project
    status_file = out / "status.json"

    if not force and status_file.exists() and json.loads(status_file.read_text())["ok"]:
        return {"ok": True, "problems": [], "skipped": True}

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # No checkout here: bugsinpy-checkout resets to the commits itself.
    git("clone", "--local", "--no-checkout", str(repo), str(checkout))

    # The work dir must be absolute: bugsinpy-checkout cds into its own folder first.
    cmd = [str(SCRATCH / "framework" / "bin" / "bugsinpy-checkout"),
           "-p", project, "-i", bug_id, "-v", VERSIONS[version], "-w", str(out)]
    with (out / "checkout.log").open("w") as log:
        subprocess.run(["bash", *cmd], stdout=log, stderr=subprocess.STDOUT)

    repairs = repair(bug, version, checkout)
    problems = verify(bug, version, checkout)
    result = {"ok": not problems, "problems": problems, "repairs": repairs}
    status_file.write_text(json.dumps(result, indent=2) + "\n")
    return result


def load_bug(project: str, bug_id: str) -> dict:
    with BUGS_FILE.open() as f:
        for line in f:
            bug = json.loads(line)
            if bug["project"] == project and bug["bug_id"] == bug_id:
                return bug
    raise SystemExit(f"no bug {project} {bug_id} in {BUGS_FILE}")


def checkout_bug(project: str, bug_id: str, force: bool = False) -> dict[str, dict]:
    """Check out both versions of one bug. Returns {version: status}."""
    bug = load_bug(project, bug_id)
    prepare_scratch()
    repo = cache_repo(project, [bug["buggy_commit"], bug["fixed_commit"]])
    return {version: checkout_version(bug, version, repo, force) for version in VERSIONS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--bug", required=True, help="bug id")
    ap.add_argument("--force", action="store_true", help="redo versions that are already OK")
    args = ap.parse_args()

    results = checkout_bug(args.project, args.bug, args.force)
    for version, result in results.items():
        label = "skip (already OK)" if result.get("skipped") else (
            "ok" if result["ok"] else "FAILED")
        print(f"{args.project} {args.bug} {version}: {label}")
        for note in result.get("repairs", []):
            print(f"    repaired: {note}")
        for problem in result["problems"]:
            print(f"    {problem}")
    if not all(r["ok"] for r in results.values()):
        print(f"see checkout.log and status.json in data/work/{args.project}/{args.bug}/")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
