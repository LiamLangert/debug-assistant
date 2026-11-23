"""Deep retrieval, function-level units (plus module-level fallback).

    uv run scripts/baseline/run_deep_func.py --split validation

Instead of ranking individual source lines (thin, and millions of them), we
parse each file into units -- every function/method is a unit, and the leftover
module-level code is grouped into fallback chunks so every line is covered.
Each unit is embedded whole, ranked against the traceback by cosine similarity,
and a fix is "found" if it falls inside a top-K unit.

Reported:
  file Recall@5   a fix file is among the first 5 distinct files in the ranking
  unit Recall@5   a fix line falls inside one of the top 5 ranked units
  coverage        fraction of fix lines that live inside SOME unit

Model: all-MiniLM-L6-v2 (frozen). Deterministic.
"""

import argparse
import ast
import json
import platform
import subprocess
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
REPOS = ROOT / "data" / "cache" / "repos"
sys.path.insert(0, str(ROOT / "scripts" / "baseline"))
sys.path.insert(0, str(ROOT / "scripts" / "data"))
from run_baseline import (  # noqa: E402
    SPLIT_FILE, BUGS_FILE, RUNS, RESULTS,
    traceback_query, tokens, git_head, load_jsonl,
)
from checkout_bug import file_at  # noqa: E402
from parse_bugsinpy import is_test_path  # noqa: E402

MODEL_NAME = "all-MiniLM-L6-v2"
K = 5


def chunk_file(name: str, text: str) -> list[tuple[str, int, int, str]]:
    """(file, start_line, end_line, unit_text). Functions are units; leftover
    module-level code is grouped into fallback chunks so every line is covered."""
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [(name, 1, n, text)]

    covered = [False] * (n + 1)
    units = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            units.append((name, start, end, "\n".join(lines[start - 1:end])))
            for i in range(start, min(end, n) + 1):
                covered[i] = True

    i = 1
    while i <= n:
        if covered[i]:
            i += 1
            continue
        start = i
        while i <= n and not covered[i]:
            i += 1
        end = i - 1
        chunk = "\n".join(lines[start - 1:end])
        if chunk.strip():
            units.append((name, start, end, chunk))

    units.sort(key=lambda u: u[1])
    return units


def source_units(project: str, commit: str) -> list[tuple[str, int, int, str]]:
    repo = REPOS / f"{project}.git"
    if not repo.is_dir():
        raise SystemExit(f"{repo} is missing. Run scripts/baseline/checkout_bug.py first.")
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    units = []
    for name in names:
        if name.endswith(".py") and not is_test_path(name):
            text = file_at(repo, commit, name).decode("utf-8", errors="replace")
            units.extend(chunk_file(name, text))
    return units


def evaluate_units(ranking, units, truth):
    truth_files = {f for f, _ in truth}

    def unit_hits(i):
        f, s, e, _ = units[i]
        return any(tf == f and s <= tl <= e for tf, tl in truth)

    top = ranking[:K]
    files = []
    for i in ranking:
        if units[i][0] not in files:
            files.append(units[i][0])
            if len(files) == K:
                break
    first = next((r for r, i in enumerate(ranking, start=1) if unit_hits(i)), None)
    return {
        "unit_hit": any(unit_hits(i) for i in top),
        "file_hit": any(f in truth_files for f in files),
        "first_hit_rank": first,
        "top_units": [f"{units[i][0]}:{units[i][1]}-{units[i][2]}" for i in top],
        "top_files": files,
    }


def summarize_units(rows):
    scored = [r["func"] for r in rows if "func" in r]
    n = len(scored)
    if not n:
        return {"n": 0}
    unit_hits = sum(s["unit_hit"] for s in scored)
    file_hits = sum(s["file_hit"] for s in scored)
    mrr = sum(1 / s["first_hit_rank"] for s in scored if s["first_hit_rank"]) / n
    return {"n": n,
            "unit_recall@5": round(unit_hits / n, 4), "unit_hits": unit_hits,
            "file_recall@5": round(file_hits / n, 4), "file_hits": file_hits,
            "mrr": round(mrr, 4)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--split", choices=["train", "validation", "test", "all"], default="validation")
    ap.add_argument("--project")
    ap.add_argument("--bug")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    split = json.loads(SPLIT_FILE.read_text())
    projects = (split["train"] + split["validation"] + split["test"]) if args.split == "all" else split[args.split]
    if args.project:
        if args.project not in projects:
            raise SystemExit(f"{args.project} is not in the {args.split} split")
        projects = [args.project]
    bugs = [b for b in load_jsonl(BUGS_FILE)
            if b["project"] in projects and (not args.bug or b["bug_id"] == args.bug)]
    bugs.sort(key=lambda b: (b["project"].lower(), int(b["bug_id"])))

    print(f"loading {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME)
    print(f"{len(bugs)} bugs in {args.split}")

    rows = []
    counts = Counter(bugs=len(bugs))
    for bug in bugs:
        name = f"{bug['project']} {bug['bug_id']}"
        row = {"project": bug["project"], "bug_id": bug["bug_id"]}
        rows.append(row)
        run_file = RUNS / bug["project"] / f"{bug['bug_id']}.json"
        if not run_file.exists():
            row["skipped"] = "not run"; continue
        counts["run"] += 1
        run = json.loads(run_file.read_text())
        if not run.get("reproduced"):
            row["skipped"] = "not reproduced"; continue
        counts["reproduced"] += 1
        query = traceback_query(run)
        if not tokens(query):
            row["skipped"] = "no traceback"; continue

        units = source_units(bug["project"], bug["buggy_commit"])
        truth = {(c["file"], n) for c in bug["changes"] for n in c["lines"]}
        covered = sum(any(f == uf and us <= ln <= ue for uf, us, ue, _ in units)
                      for f, ln in truth)
        row["fix_lines"] = len(truth)
        row["fix_lines_covered"] = covered

        texts = [u[3] for u in units]
        unit_emb = model.encode(texts, normalize_embeddings=True, batch_size=128, show_progress_bar=False)
        q_emb = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        scores = unit_emb @ q_emb
        ranking = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        ranking = [i for i in ranking if scores[i] > 0]

        row["func"] = evaluate_units(ranking, units, truth)
        counts["scored"] += 1
        e = row["func"]
        print(f"{name}: {len(units)} units | func: unit {'hit ' if e['unit_hit'] else 'miss'}"
              f" file {'hit ' if e['file_hit'] else 'miss'}, first hit at {e['first_hit_rank']}")

    summary = {"func": summarize_units(rows)}
    log = {
        "split": args.split, "method": "dense-embedding (function-level + module fallback)",
        "coverage": {k: counts[k] for k in ("bugs", "run", "reproduced", "scored")},
        "summary": summary, "commit": git_head(ROOT),
        "config": {"model": MODEL_NAME, "k": K, "similarity": "cosine",
                   "unit": "function/method, module-level leftovers as fallback chunks",
                   "query": "buggy traceback", "search": "non-test .py at the buggy commit"},
        "python": platform.python_version(),
        "sentence_transformers": version("sentence-transformers"),
        "bugs": rows,
    }
    out = args.out or RESULTS / f"deep_{args.split}_func.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, indent=2) + "\n")

    c = log["coverage"]
    print(f"\n{c['scored']} scored of {c['bugs']} bugs")
    s = summary["func"]
    if s["n"]:
        print(f"  func  unit Recall@5 {s['unit_hits']}/{s['n']}  file Recall@5 {s['file_hits']}/{s['n']}  MRR {s['mrr']:.3f}")
    print(f"log -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()