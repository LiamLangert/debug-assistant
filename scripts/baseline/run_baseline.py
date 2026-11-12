"""BM25 baseline from a failing test's traceback.

    uv run scripts/baseline/run_baseline.py --split validation

For each bug that run_bug.py reproduced (runs/<project>/<bug_id>.json says
reproduced: true):

  1. The query is the buggy version's traceback, from every failing command,
     with the container's /bug/buggy/<project>/ prefix cut off the paths.
  2. BM25 picks the top 5 Stack Overflow posts for that query.
  3. The 5 posts' text, joined into one query, ranks every line of the
     project's source at the buggy commit: all tracked .py files except tests
     (parse_bugsinpy.is_test_path).

The same lines are also ranked by the traceback itself, with no posts
("direct"). If the posts do not beat that, they are not adding anything.

Metrics, over the scored bugs:
  line Recall@5   a line the fix touched is in the top 5 lines
  file Recall@5   a file the fix touched is among the first 5 distinct files
                  in that ranking
  MRR             1 / rank of the first fix line (0 when no fix line scores
                  above 0)

Source comes from the cached bare repo (data/cache/repos/<project>.git) at
the buggy commit, so nothing is needed from data/work: run_bugs.py deletes the
checkout trees once a bug has run.
"""

import argparse
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parents[2]
SPLIT_FILE = ROOT / "splits" / "projects.json"
BUGS_FILE = ROOT / "data" / "processed" / "bugs.jsonl"
CORPUS_FILE = ROOT / "data" / "processed" / "corpus.jsonl"
REPOS = ROOT / "data" / "cache" / "repos"
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"

sys.path.insert(0, str(ROOT / "scripts" / "data"))
sys.path.insert(0, str(ROOT / "scripts" / "baseline"))
from checkout_bug import file_at  # noqa: E402
from parse_bugsinpy import is_test_path  # noqa: E402

K1 = 1.5
B = 0.75
N_POSTS = 5
K = 5  # the @5 in Recall@5
FAILED = ("failed", "error", "timeout")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text or "")]


def ranked(scores) -> list[int]:
    """Indices with a positive score, best first. Ties keep index order (path, line)."""
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [i for i in order if scores[i] > 0]


def source_lines(project: str, commit: str) -> list[tuple[str, int, str]]:
    """(path, 1-based line, text) for every non-test .py file tracked at commit."""
    repo = REPOS / f"{project}.git"
    if not repo.is_dir():
        raise SystemExit(f"{repo} is missing. Run scripts/baseline/checkout_bug.py for this bug first.")
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    rows = []
    for name in names:
        if name.endswith(".py") and not is_test_path(name):
            text = file_at(repo, commit, name).decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), start=1):
                rows.append((name, i, line))
    return rows


def traceback_query(run: dict) -> str:
    """The buggy version's tracebacks, from every failing command, as repo paths."""
    parts = []
    for cmd in run["versions"]["buggy"]["commands"]:
        if cmd["outcome"] in FAILED and cmd.get("traceback"):
            parts.append(cmd["traceback"])
    text = "\n".join(parts)
    # /bug/buggy/<project>/lib/x.py -> lib/x.py. Otherwise every frame adds
    # "bug" and "buggy", which are only the container's mount point.
    return text.replace(f"/bug/buggy/{run['project']}/", "").replace("/bug/buggy/", "")


def evaluate(ranking: list[int], lines: list[tuple[str, int, str]], truth: set[tuple[str, int]]) -> dict:
    truth_files = {f for f, _ in truth}
    top_lines = [lines[i][:2] for i in ranking[:K]]
    files: list[str] = []
    for i in ranking:
        if lines[i][0] not in files:
            files.append(lines[i][0])
            if len(files) == K:
                break
    first = next((r for r, i in enumerate(ranking, start=1) if lines[i][:2] in truth), None)
    return {
        "line_hit": any(pl in truth for pl in top_lines),
        "file_hit": any(f in truth_files for f in files),
        "first_fix_line_rank": first,
        "top_lines": [f"{f}:{n}" for f, n in top_lines],
        "top_files": files,
    }


def summarize(rows: list[dict], key: str) -> dict:
    scored = [r[key] for r in rows if key in r]
    n = len(scored)
    if not n:
        return {"n": 0}
    line_hits = sum(s["line_hit"] for s in scored)
    file_hits = sum(s["file_hit"] for s in scored)
    mrr = sum(1 / s["first_fix_line_rank"] for s in scored if s["first_fix_line_rank"]) / n
    return {
        "n": n,
        "line_recall@5": round(line_hits / n, 4), "line_hits": line_hits,
        "file_recall@5": round(file_hits / n, 4), "file_hits": file_hits,
        "mrr": round(mrr, 4),
    }


def git_head(cwd: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip()


def load_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=["train", "validation", "test", "all"], default="validation")
    parser.add_argument("--project", help="only this project")
    parser.add_argument("--bug", help="only this bug id (with --project)")
    parser.add_argument("--out", type=Path, help="log file (default results/baseline_<split>.json)")
    args = parser.parse_args()

    split = json.loads(SPLIT_FILE.read_text())
    projects = (split["train"] + split["validation"] + split["test"]) if args.split == "all" else split[args.split]
    if args.project:
        if args.project not in projects:
            raise SystemExit(f"{args.project} is not in the {args.split} split")
        projects = [args.project]
    bugs = [b for b in load_jsonl(BUGS_FILE)
            if b["project"] in projects and (not args.bug or b["bug_id"] == args.bug)]
    bugs.sort(key=lambda b: (b["project"].lower(), int(b["bug_id"])))

    posts = load_jsonl(CORPUS_FILE)
    post_index = BM25Okapi([tokens(p["text"]) for p in posts], k1=K1, b=B)
    print(f"{len(bugs)} bugs in {args.split}, {len(posts)} posts")

    rows = []
    counts = Counter(bugs=len(bugs))
    for bug in bugs:
        name = f"{bug['project']} {bug['bug_id']}"
        row = {"project": bug["project"], "bug_id": bug["bug_id"]}
        rows.append(row)
        run_file = RUNS / bug["project"] / f"{bug['bug_id']}.json"
        if not run_file.exists():
            row["skipped"] = "not run"
            continue
        counts["run"] += 1
        run = json.loads(run_file.read_text())
        if not run.get("reproduced"):
            row["skipped"] = f"{run['stage']} failed" if run.get("error") else "not reproduced"
            print(f"{name}: {row['skipped']}, skipping")
            continue
        counts["reproduced"] += 1
        query = traceback_query(run)
        if not tokens(query):
            row["skipped"] = "no traceback"
            print(f"{name}: no traceback in its run file, skipping")
            continue

        lines = source_lines(bug["project"], bug["buggy_commit"])
        truth = {(c["file"], n) for c in bug["changes"] for n in c["lines"]}
        present = {f for f, _, _ in lines}
        row["fix_files_in_search"] = sum(f in present for f in {f for f, _ in truth})
        line_index = BM25Okapi([tokens(text) for _, _, text in lines], k1=K1, b=B)

        top = ranked(post_index.get_scores(tokens(query)))[:N_POSTS]
        row["posts"] = [posts[i]["post_id"] for i in top]
        post_query = "\n".join(posts[i]["text"] for i in top)
        row["via_posts"] = evaluate(ranked(line_index.get_scores(tokens(post_query))), lines, truth)
        row["direct"] = evaluate(ranked(line_index.get_scores(tokens(query))), lines, truth)
        counts["scored"] += 1

        mark = lambda e: f"line {'hit ' if e['line_hit'] else 'miss'} file {'hit ' if e['file_hit'] else 'miss'}"
        print(f"{name}: {len(lines)} lines | via posts: {mark(row['via_posts'])}"
              f" | direct: {mark(row['direct'])}, first fix line at {row['direct']['first_fix_line_rank']}")

    summary = {"via_posts": summarize(rows, "via_posts"), "direct": summarize(rows, "direct")}
    log = {
        "split": args.split,
        "filter": {"project": args.project, "bug": args.bug},
        "coverage": {k: counts[k] for k in ("bugs", "run", "reproduced", "scored")},
        "summary": summary,
        "commit": git_head(ROOT),
        "config": {
            "baseline": "okapi-bm25", "k1": K1, "b": B,
            "posts": N_POSTS, "k": K, "post_query": "top posts joined",
            "query": "buggy traceback", "search": "non-test .py lines at the buggy commit",
        },
        "data": {
            "bugsinpy_commit": git_head(ROOT / "data" / "raw" / "BugsInPy"),
            "corpus_n": len(posts),
        },
        "python": platform.python_version(),
        "rank_bm25": version("rank-bm25"),
        "bugs": rows,
    }
    out = args.out or RESULTS / f"baseline_{args.split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, indent=2) + "\n")

    c = log["coverage"]
    print(f"\n{c['scored']} scored of {c['bugs']} bugs ({c['run']} run, {c['reproduced']} reproduced)")
    for key, s in summary.items():
        if s["n"]:
            print(f"  {key:9} line Recall@5 {s['line_hits']}/{s['n']}  file Recall@5 {s['file_hits']}/{s['n']}"
                  f"  MRR {s['mrr']:.3f}")
    print(f"log -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()
