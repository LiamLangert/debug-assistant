"""Check out and run every bug in a split, one at a time, for run_baseline.py.

    uv run scripts/baseline/run_bugs.py --split validation
    uv run scripts/baseline/run_bugs.py --split validation --project tornado

For each bug: checkout_bug.py's checkout, then run_bug.py's run, then the two
checkout trees are deleted (pandas is ~2 GB a bug; --keep-trees keeps them).
The logs stay in data/work/<project>/<bug_id>/.

Every bug ends up with a file in runs/<project>/<bug_id>.json, also when the
checkout or the container fails: then it holds "error" and "stage" instead of
results. Bugs that already have one are skipped, so a stopped batch picks up
where it left off. To run some again:
    --redo errors        checkout or container failures
    --redo unreproduced  those, plus runs where the bug did not reproduce
    --redo all           everything

Two bugs run at once by default (--jobs 1 for one at a time). The cores are
split between them for C builds (--build-jobs). Checkouts take turns, since
bugs of one project share one bare clone; the containers run side by side,
and their output lines are tagged with the bug.

Ctrl-C stops the batch after killing the running bugs' containers. Those bugs
get no run file, so the same command runs them again.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from checkout_bug import WORK, checkout_bug  # noqa: E402
from run_bug import BUILD_JOBS, ROOT, VERSIONS, container_name, run_bug, run_file, write_run  # noqa: E402

SPLIT_FILE = ROOT / "splits" / "projects.json"
BUGS_FILE = ROOT / "data" / "processed" / "bugs.jsonl"
MIN_FREE_GB = 10  # stop before a bug when the disk under data/work has less than this
JOBS = 2  # bugs at once

CHECKOUT_LOCK = threading.Lock()  # one checkout at a time: they share data/cache/repos
STOP = threading.Event()  # Ctrl-C: start no more bugs; a killed container is not a failure
LOW_DISK = threading.Event()  # start no more bugs; the running ones finish


def clock(seconds: float) -> str:
    m = round(seconds) // 60
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m{round(seconds) % 60:02d}s"


def wanted(project: str, bug_id: str, redo: str | None) -> bool:
    path = run_file(project, bug_id)
    if redo == "all" or not path.exists():
        return True
    run = json.loads(path.read_text())
    if redo == "errors":
        return bool(run.get("error"))
    if redo == "unreproduced":
        return not run.get("reproduced")
    return False


def delete_trees(project: str, bug_id: str) -> None:
    for version in VERSIONS:
        tree = WORK / project / bug_id / version / project
        if tree.is_dir():
            shutil.rmtree(tree)


def scrub(text: str) -> str:
    """Host paths out of an error, so runs/ stays shareable: repo-relative, ~ for home."""
    return text.replace(f"{ROOT}/", "").replace(str(Path.home()), "~")


def one_bug(project: str, bug_id: str, args: argparse.Namespace) -> dict:
    """Check out and run one bug. Writes its run file, error or not, unless the
    batch was stopped (Ctrl-C), which raises KeyboardInterrupt instead."""
    try:
        with CHECKOUT_LOCK:
            status = checkout_bug(project, bug_id, force=args.force_checkout)
    except (Exception, SystemExit) as e:
        status = {"exception": {"ok": False, "problems": [scrub(str(e) or type(e).__name__)]}}
    if STOP.is_set():
        raise KeyboardInterrupt
    bad = {v: [scrub(p) for p in s["problems"]] for v, s in status.items() if not s["ok"]}
    if bad:
        result = {"project": project, "bug_id": bug_id, "reproduced": False,
                  "stage": "checkout", "error": "checkout failed", "problems": bad}
        write_run(result)
        return result
    try:
        return run_bug(project, bug_id, args.compile_timeout, args.test_timeout, args.build_jobs)
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit) as e:
        if STOP.is_set():  # its container was killed by Ctrl-C; not a real failure
            raise KeyboardInterrupt from e
        detail = str(e) if isinstance(e, SystemExit) else traceback.format_exc()
        result = {"project": project, "bug_id": bug_id, "reproduced": False,
                  "stage": "run", "error": scrub(detail.strip())[-3000:]}
        write_run(result)
        return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--split", choices=["train", "validation", "test", "all"], default="validation")
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--bug", help="only this bug id (with one --project)")
    ap.add_argument("--redo", choices=["errors", "unreproduced", "all"],
                    help="run bugs again that already have a run file")
    ap.add_argument("--limit", type=int, help="stop after this many bugs")
    ap.add_argument("--keep-trees", action="store_true", help="keep the checkouts after each run")
    ap.add_argument("--force-checkout", action="store_true", help="redo checkouts that are already OK")
    ap.add_argument("--compile-timeout", type=int, default=3600, help="seconds, per version")
    ap.add_argument("--test-timeout", type=int, default=1800, help="seconds, per command")
    ap.add_argument("--jobs", type=int, default=JOBS, help=f"bugs at once (default {JOBS})")
    ap.add_argument("--build-jobs", type=int,
                    help="C build jobs per bug (default: the cores split between --jobs, at most 16)")
    args = ap.parse_args()
    args.jobs = max(1, args.jobs)
    if args.build_jobs is None:
        args.build_jobs = max(1, min(BUILD_JOBS, (os.cpu_count() or 1) // args.jobs))

    split = json.loads(SPLIT_FILE.read_text())
    projects = (split["train"] + split["validation"] + split["test"]) if args.split == "all" else split[args.split]
    if args.project:
        unknown = [p for p in args.project if p not in projects]
        if unknown:
            raise SystemExit(f"not in the {args.split} split: {', '.join(unknown)}")
        projects = args.project
    if args.bug and len(projects) != 1:
        raise SystemExit("--bug needs exactly one --project")

    with BUGS_FILE.open() as f:
        bugs = [json.loads(line) for line in f]
    bugs = [(b["project"], b["bug_id"]) for b in bugs
            if b["project"] in projects and (not args.bug or b["bug_id"] == args.bug)]
    bugs.sort(key=lambda pb: (pb[0].lower(), int(pb[1])))
    todo = [pb for pb in bugs if wanted(*pb, args.redo)]
    skipped = len(bugs) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(bugs)} bugs in {args.split}, {skipped} skipped (run file exists), {len(todo)} to run")
    if not todo:
        return
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise SystemExit("docker is not running")

    jobs = min(args.jobs, len(todo))
    print(f"{jobs} at a time, {args.build_jobs} build jobs each")
    done: list[tuple[str, str, dict, float]] = []
    running: set[tuple[str, str]] = set()
    batch_start = time.monotonic()

    def run_one(n: int, project: str, bug_id: str) -> tuple[str, str, dict, float] | None:
        """One bug from start to verdict; None when the batch stopped before it."""
        if STOP.is_set() or LOW_DISK.is_set():
            return None
        WORK.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(WORK).free / 1e9
        if free_gb < MIN_FREE_GB:
            if not LOW_DISK.is_set():
                LOW_DISK.set()
                print(f"\nstopping: {free_gb:.1f} GB free under data/work (need {MIN_FREE_GB}); "
                      f"letting running bugs finish", flush=True)
            return None
        print(f"\n===== [{n}/{len(todo)}] {project} {bug_id} =====", flush=True)
        running.add((project, bug_id))
        start = time.monotonic()
        try:
            result = one_bug(project, bug_id, args)
        finally:
            running.discard((project, bug_id))
            if not args.keep_trees:
                delete_trees(project, bug_id)
        took = time.monotonic() - start
        done.append((project, bug_id, result, took))

        if result.get("error"):
            verdict = f"{result['stage']} FAILED: {result['error'].splitlines()[-1][:120]}"
        else:
            v = result["versions"]
            verdict = (f"{'reproduced' if result['reproduced'] else 'NOT reproduced'}"
                       f" (buggy {v['buggy']['outcome']}, fixed {v['fixed']['outcome']})")
        finished = len(done)
        left = (time.monotonic() - batch_start) / finished * (len(todo) - finished)
        print(f"===== {project} {bug_id}: {verdict} in {clock(took)}"
              f"{f'; ~{clock(left)} left' if finished < len(todo) else ''}", flush=True)
        return done[-1]

    pool = ThreadPoolExecutor(max_workers=jobs)
    futures = [pool.submit(run_one, n, project, bug_id)
               for n, (project, bug_id) in enumerate(todo, start=1)]
    try:
        for future in as_completed(futures):
            future.result()
    except KeyboardInterrupt:
        STOP.set()
        for project, bug_id in list(running):
            killed = subprocess.run(["docker", "kill", container_name(project, bug_id)],
                                    capture_output=True).returncode == 0
            print(f"\ninterrupted {project} {bug_id}"
                  f"{'; killed its container' if killed else ''}", flush=True)
        print("\nstopped (Ctrl-C). Waiting for the running bugs to clean up; "
              "run the same command to carry on.", flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if not done:
        return
    reproduced = [d for d in done if d[2].get("reproduced")]
    errors = [d for d in done if d[2].get("error")]
    print(f"\n{len(done)} run in {clock(time.monotonic() - batch_start)}: {len(reproduced)} reproduced, "
          f"{len(done) - len(reproduced) - len(errors)} not reproduced, {len(errors)} failed")
    for project, bug_id, result, _ in done:
        if not result.get("reproduced"):
            why = (f"{result['stage']} failed" if result.get("error") else
                   f"buggy {result['versions']['buggy']['outcome']}, fixed {result['versions']['fixed']['outcome']}")
            print(f"  {project} {bug_id}: {why}")
    print("results in runs/, logs in data/work/")


if __name__ == "__main__":
    main()
