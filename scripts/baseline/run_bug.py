"""Run one bug's failing tests on its buggy and fixed checkouts, in one environment.

checkout_bug.py makes the two checkouts. This script starts one
python:<python_version> container (the exact version in bug.info) and runs
in_container.sh in it, which does the actual work: bugsinpy-compile builds the
environment in the buggy tree, the buggy tests run, the fixed tree reinstalls
only the project itself, and the fixed tests run. Sharing one environment
means a test that fails on buggy and passes on fixed differs only in the code.
in_container.sh also says which gaps in BugsInPy's environment it fills.

What is left here is reading the container's files back: each test command's
outcome and traceback (the traceback is run_baseline.py's query), what was
repaired, and how the two environments differ. That goes to
runs/<project>/<bug_id>.json, which holds no host paths, so runs/ can be
committed. The logs stay in data/work/<project>/<bug_id>/:
    constraints.txt          the pins every pip call was constrained to
    buggy/compile.log        bugsinpy-compile's output
    fixed/setup.log          the fixed tree's setup.sh and requirements reinstall
    <version>/test_<i>.log   each test command's output
    <version>/freeze.txt     pip freeze just before that version's tests

Outcomes, per command and per version (the version's first command that did
not pass):
    passed      exit 0
    failed      a test failed (pytest "failed", unittest "failures=")
    error       a test errored: setup, import, collection, or in unittest any
                uncaught exception (which can still be the bug itself)
    no_tests    pytest collected nothing (exit 5)
    timeout     hit --test-timeout
    no_env      compile made no venv, so nothing ran

"reproduced" means buggy did not pass and fixed passed. Whether buggy fails
*because of the bug* is still for the reader to judge, which is why each
non-passing command's exception line and traceback are printed.

Usage:
    uv run scripts/baseline/run_bug.py --project PySnooper --bug 2
"""

import argparse
import itertools
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "data" / "work"
RUNS = ROOT / "runs"
FRAMEWORK = ROOT / "data" / "cache" / "bugsinpy" / "framework"
BUGSINPY_PROJECTS = ROOT / "data" / "cache" / "bugsinpy" / "projects"
PIP_CACHE = ROOT / "data" / "cache" / "pip"
HERE = Path(__file__).resolve().parent
DRIVER = HERE / "in_container.sh"
EXTRAS_FILE = HERE / "extra_requirements.json"
FALLBACK_PYTEST = "pytest==5.4.3"
# Parallel C-extension builds; more than 16 buys little and costs memory.
BUILD_JOBS = min(16, os.cpu_count() or 1)
VERSIONS = ("buggy", "fixed")
# What checkout_bug.py leaves in data/work/<project>/<bug_id>/<version>/ besides
# the tree. Anything else there is the last run's output.
FROM_CHECKOUT = {"status.json", "checkout.log"}


def read(path: Path) -> str:
    """The file's text, or "" when it is missing."""
    return path.read_text(errors="replace") if path.exists() else ""


# ---- Reading test output --------------------------------------------------
# The logs only say what happened in text, so the outcome, traceback and
# exception line are parsed out of them here, once per command.

PYTEST_HEADER = re.compile(r"^=+ (FAILURES|ERRORS) =+$")
PYTEST_ANY_HEADER = re.compile(r"^=+ .* =+$")
PYTEST_SUMMARY = re.compile(r"^\d+ (failed|passed|error|errors|skipped)\b")
UNITTEST_HEADER = re.compile(r"^(ERROR|FAIL): ")
TRACEBACK = "Traceback (most recent call last):"


def outcome_of(cmd: str, code: int, output: str) -> str:
    if code == 0:
        return "passed"
    if code == 124:
        return "timeout"
    last = (output.strip().splitlines() or [""])[-1]
    if "pytest" in cmd or "py.test" in cmd:
        if code == 5:
            return "no_tests"
        return "failed" if " failed" in last or last.startswith("FAILED") else "error"
    # unittest prints FAILED (failures=N, errors=M) on its last line.
    return "failed" if "failures=" in last else "error"


def traceback_of(output: str) -> str | None:
    """The failure report: pytest's, else unittest's, else any bare tracebacks."""
    lines = output.splitlines()
    return pytest_traceback(lines) or unittest_traceback(lines) or bare_traceback(lines)


def pytest_traceback(lines: list[str]) -> str | None:
    """The FAILURES and ERRORS sections, each up to the next ==== header."""
    out, inside = [], False
    for line in lines:
        if PYTEST_HEADER.match(line):
            inside = True
        elif inside and PYTEST_ANY_HEADER.match(line):
            inside = False
        if inside:
            out.append(line)
    # pytest -q ends with a bare "1 failed in 0.1s" line, not a header.
    while out and PYTEST_SUMMARY.match(out[-1]):
        out.pop()
    return "\n".join(out).strip() or None


def unittest_traceback(lines: list[str]) -> str | None:
    """The ERROR: / FAIL: blocks, each up to the ==== rule that opens the next
    block or the ---- rule above "Ran N tests"."""
    out, inside = [], False
    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if UNITTEST_HEADER.match(line):
            inside = True
        elif inside and (line.startswith("=" * 20)
                         or (line.startswith("-" * 20) and next_line.startswith("Ran "))):
            inside = False
        if inside:
            out.append(line)
    return "\n".join(out).strip() or None


def bare_traceback(lines: list[str]) -> str | None:
    """Every "Traceback (most recent call last):" block, through its exception line."""
    blocks, current = [], None
    for line in lines:
        if line.startswith(TRACEBACK):
            current = [line]
        elif current is not None:
            current.append(line)
            if line and not line[0].isspace():  # the exception line ends the block
                blocks.append("\n".join(current))
                current = None
    return "\n\n".join(blocks) or None


def exception_of(traceback: str | None) -> str | None:
    """The first exception: pytest's first "E " line, else the line that ends
    the first traceback."""
    lines = (traceback or "").splitlines()
    for line in lines:
        if line.startswith("E "):
            return line[1:].strip()
    in_traceback = False
    for line in lines:
        if line.startswith(TRACEBACK):
            in_traceback = True
        elif in_traceback and line.strip() and not line[0].isspace():
            return line.strip()
    return None


def compile_errors(log: str) -> list[str]:
    """pip and build errors from the compile log. Compile itself always exits 0."""
    keep = re.compile(r"^(ERROR:|error:|Failed building|Could not find a version|"
                      r"No matching distribution|ModuleNotFoundError|.*Error: )")
    return [line.strip() for line in log.splitlines() if keep.match(line.strip())]


def read_commands(out: Path) -> list[dict]:
    """Each test command in_container.sh ran: test_<i>.cmd, .exit and .log."""
    commands = []
    for i in itertools.count(1):
        if not (out / f"test_{i}.cmd").exists():
            return commands
        cmd = (out / f"test_{i}.cmd").read_text().strip()
        code = int((out / f"test_{i}.exit").read_text())
        output = read(out / f"test_{i}.log")
        outcome = outcome_of(cmd, code, output)
        tb = traceback_of(output) if outcome != "passed" else None
        commands.append({"cmd": cmd, "exit": code, "outcome": outcome,
                         "exception": exception_of(tb), "traceback": tb,
                         "log_tail": "\n".join(output.splitlines()[-15:])})


def read_version(out: Path) -> dict:
    """One version's results, from the files in_container.sh left in out."""
    commands = read_commands(out)
    log = out / ("compile.log" if out.name == "buggy" else "setup.log")
    return {
        "outcome": next((c["outcome"] for c in commands if c["outcome"] != "passed"),
                        "passed" if commands else "no_env"),
        "repairs": read(out / "repairs.txt").splitlines(),
        "compile_errors": compile_errors(read(log)),
        "pythonpath": read(out / "pythonpath").strip(),
        "commands": commands,
    }


# ---- Running ---------------------------------------------------------------

def python_version(checkout: Path) -> str:
    for line in (checkout / "bugsinpy_bug.info").read_text().splitlines():
        if line.startswith("python_version="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"no python_version in {checkout / 'bugsinpy_bug.info'}")


def prepare(project: str, bug_id: str) -> Path:
    """Check both checkouts are good and clear the last run's output from them."""
    bug_dir = WORK / project / bug_id
    for version in VERSIONS:
        out = bug_dir / version
        status = out / "status.json"
        if not (status.exists() and json.loads(status.read_text())["ok"] and (out / project).is_dir()):
            raise SystemExit(f"{out} is not a good checkout. Run checkout_bug.py first.")
        for old in out.iterdir():
            if old.is_file() and old.name not in FROM_CHECKOUT:
                old.unlink()
        # bugsinpy-compile and in_container.sh edit bugsinpy_requirements.txt
        # in place (encoding, the self-install line), so start every run from
        # BugsInPy's original. tornado 16 has none; checkout_bug.py wrote it empty.
        original = BUGSINPY_PROJECTS / project / "bugs" / bug_id / "requirements.txt"
        (out / project / "bugsinpy_requirements.txt").write_bytes(
            original.read_bytes() if original.exists() else b"")
    (bug_dir / "constraints.txt").unlink(missing_ok=True)
    return bug_dir


def run_container(project: str, bug_id: str, bug_dir: Path, image: str, compile_timeout: int,
                  test_timeout: int, build_jobs: int) -> None:
    """Run in_container.sh in image, printing its progress as it comes."""
    entry = json.loads(EXTRAS_FILE.read_text())["bugs"].get(f"{project}/{bug_id}")
    extras = entry["packages"] if entry else []
    PIP_CACHE.mkdir(parents=True, exist_ok=True)
    name = f"bugsinpy-{project}-{bug_id}".lower()
    cmd = [
        "docker", "run", "--rm", "--name", name,
        # Run as you, so nothing in data/work ends up owned by root.
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp/home",
        "-e", "PIP_CACHE_DIR=/pipcache",
        "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "-e", f"COMPILE_TIMEOUT={compile_timeout}",
        "-e", f"TEST_TIMEOUT={test_timeout}",
        "-e", f"BUILD_JOBS={build_jobs}",
        "-e", f"FALLBACK_PYTEST={FALLBACK_PYTEST}",
        "-e", f"EXTRA_REQUIREMENTS={' '.join(extras)}",
        "-v", f"{bug_dir}:/bug",
        "-v", f"{FRAMEWORK}:/bugsinpy/framework:ro",
        "-v", f"{DRIVER}:/driver.sh:ro",
        "-v", f"{PIP_CACHE}:/pipcache",
        image, "bash", "/driver.sh", project,
    ]
    tail = []
    # Stopping docker run does not stop the container, so on Ctrl-C (which can
    # land anywhere in here) kill it by name.
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as docker:
            for line in docker.stdout:
                print(line, end="", flush=True)
                tail = (tail + [line])[-20:]
    except KeyboardInterrupt:
        killed = subprocess.run(["docker", "kill", name], capture_output=True).returncode == 0
        print(f"\ninterrupted; {'killed container' if killed else 'no container left named'} {name}", flush=True)
        raise
    if docker.returncode != 0:
        raise SystemExit(f"docker run failed ({docker.returncode}):\n{''.join(tail)}")


def run_bug(project: str, bug_id: str, compile_timeout: int = 3600,
            test_timeout: int = 1800, build_jobs: int = BUILD_JOBS) -> dict:
    start = time.monotonic()
    bug_dir = prepare(project, bug_id)
    image = f"python:{python_version(bug_dir / 'buggy' / project)}"
    print(f"{project} {bug_id}: one env in {image}, testing buggy then fixed", flush=True)
    run_container(project, bug_id, bug_dir, image, compile_timeout, test_timeout, build_jobs)

    versions = {version: read_version(bug_dir / version) for version in VERSIONS}
    buggy_env, fixed_env = (set(read(bug_dir / v / "freeze.txt").splitlines()) for v in VERSIONS)
    result = {
        "project": project, "bug_id": bug_id, "image": image,
        "constraints": len(read(bug_dir / "constraints.txt").splitlines()),
        "reproduced": versions["buggy"]["outcome"] not in ("passed", "no_env")
                      and versions["fixed"]["outcome"] == "passed",
        "env_diff": {"only_buggy": sorted(buggy_env - fixed_env),
                     "only_fixed": sorted(fixed_env - buggy_env)},
        "versions": versions,
        "seconds": round(time.monotonic() - start),
    }
    write_run(result)
    return result


# ---- Saving and reporting ----------------------------------------------------

def code_commit() -> str:
    """This repo's commit, with -dirty when scripts/baseline has uncommitted changes."""
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    head = git("rev-parse", "HEAD")
    return head + ("-dirty" if git("status", "--porcelain", "--", "scripts/baseline") else "")


def run_file(project: str, bug_id: str) -> Path:
    return RUNS / project / f"{bug_id}.json"


def write_run(result: dict) -> Path:
    """Write runs/<project>/<bug_id>.json, replacing any earlier one only once complete."""
    result = {**result, "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "code_commit": code_commit()}
    path = run_file(result["project"], result["bug_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    tmp.replace(path)
    return path


def indented(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def print_summary(r: dict) -> None:
    for version, v in r["versions"].items():
        print(f"\n== {version}: {v['outcome']}")
        for note in v["repairs"]:
            print(f"repaired: {note}")
        if v["compile_errors"]:
            print(f"compile errors ({len(v['compile_errors'])}, see the log):")
            print(indented("\n".join(v["compile_errors"][:10])))
        for c in v["commands"]:
            print(f"$ {c['cmd']}\n  exit {c['exit']} -> {c['outcome']}")
            if c["outcome"] == "passed":
                continue
            print(f"  exception: {c['exception']}")
            if c["traceback"]:
                print("  traceback:\n" + indented(c["traceback"]))
            else:
                print("  no traceback found; last lines of output:\n" + indented(c["log_tail"]))
    diff = r["env_diff"]
    print("\nenv: " + ("identical for both versions" if not (diff["only_buggy"] or diff["only_fixed"])
                       else f"differs: buggy only {diff['only_buggy']}, fixed only {diff['only_fixed']}"))
    print(f"pip constrained to {r['constraints']} pins from requirements.txt")
    print(f"reproduced: {r['reproduced']}")
    print(f"result: {run_file(r['project'], r['bug_id']).relative_to(ROOT)}")
    print(f"logs: data/work/{r['project']}/{r['bug_id']}/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--bug", required=True, help="bug id")
    ap.add_argument("--compile-timeout", type=int, default=3600, help="seconds, per version")
    ap.add_argument("--test-timeout", type=int, default=1800, help="seconds, per command")
    ap.add_argument("--build-jobs", type=int, default=BUILD_JOBS,
                    help=f"parallel C-extension builds (default {BUILD_JOBS})")
    args = ap.parse_args()
    try:
        r = run_bug(args.project, args.bug, args.compile_timeout, args.test_timeout, args.build_jobs)
    except KeyboardInterrupt:
        raise SystemExit(130)
    print_summary(r)


if __name__ == "__main__":
    main()
