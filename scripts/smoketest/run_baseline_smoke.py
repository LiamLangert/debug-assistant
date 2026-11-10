"""BM25 smoke baseline. Recall@5 on a few PySnooper bugs.

From a fresh clone:

    uv sync && uv run scripts/smoketest/run_baseline_smoke.py

The query is the failing test file, taken from the fixed commit as BugsInPy
does (the fix usually adds the failing test). A real traceback would mean
installing and running each old project. Posts come from the pinned StaQC sample.
For each of the top 5 posts, BM25 ranks lines in the buggy files.
A bug is a hit when any fix line is in those lines.
"""

import hashlib
import json
import platform
from datetime import date
import random
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parents[2]
SPLIT_FILE = ROOT / "splits" / "projects.json"
BUGS_FILE = ROOT / "data" / "processed" / "bugs.jsonl"
CORPUS_FILE = ROOT / "data" / "processed" / "corpus.jsonl"
CHECKOUT_DIR = ROOT / "data" / "processed" / "checkouts"
MANIFEST = ROOT / "manifests" / "smoke.json"
LOG_FILE = ROOT / "results" / "baseline_smoke.json"

SEED = 0
K1 = 1.5
B = 0.75
N_POSTS = 5
N_LINES = 5
CORPUS_N = 500
PROJECT = "PySnooper"
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text or "")]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def step(script: str, *args: str) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def load_split() -> dict:
    return json.loads(SPLIT_FILE.read_text())


def load_bugs(project: str) -> list[dict]:
    bugs = []
    with BUGS_FILE.open() as f:
        for line in f:
            row = json.loads(line)
            if row["project"] == project:
                bugs.append(row)
    return bugs


def load_corpus() -> list[dict]:
    posts = []
    with CORPUS_FILE.open() as f:
        for line in f:
            posts.append(json.loads(line))
    return posts


def repo_files(root: Path) -> list[tuple[str, Path]]:
    """(repo path, file on disk) for each file under a checkout's tests/ or buggy/."""
    return [(p.relative_to(root).as_posix(), p) for p in sorted(root.rglob("*")) if p.is_file()]


def file_lines(bug_dir: Path) -> list[tuple[str, int, str]]:
    """(repo path, 1-based line number, line text) for each buggy file."""
    rows = []
    for rel, path in repo_files(bug_dir / "buggy"):
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            rows.append((rel, i, line))
    return rows


def top_posts(query: str, post_bm25: BM25Okapi, posts: list[dict]) -> list[dict]:
    scores = post_bm25.get_scores(tokens(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [posts[i] for i in order[:N_POSTS]]


def top_lines(post_text: str, lines: list[tuple[str, int, str]]) -> list[tuple[str, int]]:
    docs = [tokens(text) or ["blank"] for _, _, text in lines]
    if not docs:
        return []
    bm25 = BM25Okapi(docs, k1=K1, b=B)
    scores = bm25.get_scores(tokens(post_text))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    picked = []
    for i in order[:N_LINES]:
        rel, lineno, _ = lines[i]
        picked.append((rel, lineno))
    return picked


def is_hit(bug: dict, picked: set[tuple[str, int]]) -> bool:
    for change in bug.get("changes", []):
        for lineno in change.get("lines", []):
            if (change["file"], lineno) in picked:
                return True
    return False


def write_manifest(bugsinpy_commit: str) -> dict:
    sys.path.insert(0, str(ROOT / "scripts" / "data"))
    from build_corpus import REVISION

    manifest = {
        "retrieved": date.today().isoformat(),
        "bugsinpy": {
            "source": "https://github.com/soarsmu/BugsInPy.git",
            "commit": bugsinpy_commit,
        },
        "corpus": {
            "source": "koutch/staqc",
            "config": "sca_python",
            "revision": REVISION,
            "n": CORPUS_N,
        },
        "files": {
            "bugs.jsonl": {"sha256": sha256(BUGS_FILE)},
            "corpus.jsonl": {"sha256": sha256(CORPUS_FILE)},
        },
        "baseline": {
            "library": "rank-bm25",
            "version": version("rank-bm25"),
            "k1": K1,
            "b": B,
        },
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def bugsinpy_commit() -> str:
    raw = ROOT / "data" / "raw" / "BugsInPy"
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=raw, check=True, capture_output=True, text=True
    ).stdout.strip()


def git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def main() -> None:
    random.seed(SEED)
    split = load_split()
    if PROJECT not in split["test"]:
        raise SystemExit(f"{PROJECT} is not in the test split")

    step("data/download_bugsinpy.py")
    step("data/parse_bugsinpy.py")
    step("data/build_corpus.py", "--n", str(CORPUS_N))
    step("smoketest/checkout_bugs_smoke.py", "--project", PROJECT, "--n", "3")

    bugs = load_bugs(PROJECT)
    posts = load_corpus()
    post_bm25 = BM25Okapi([tokens(p["text"]) or ["blank"] for p in posts], k1=K1, b=B)

    hits = 0
    per_bug = []
    for bug in bugs:
        bug_dir = CHECKOUT_DIR / PROJECT / bug["bug_id"]
        if not (bug_dir / "buggy").is_dir():
            raise SystemExit(f"{bug_dir} uses an old layout. Re-run scripts/smoketest/checkout_bugs_smoke.py.")
        tests = repo_files(bug_dir / "tests")
        if not tests:
            print(f"bug {bug['bug_id']}: no test file, skipping")
            per_bug.append({"bug_id": bug["bug_id"], "hit": None, "skipped": "test file missing"})
            continue
        if not repo_files(bug_dir / "buggy"):
            print(f"bug {bug['bug_id']}: no source files, skipping")
            per_bug.append({"bug_id": bug["bug_id"], "hit": None, "skipped": "source files missing"})
            continue
        # Some bugs have several failing test files. Query with all of them.
        query = "\n".join(path.read_text(errors="replace") for _, path in tests)
        lines = file_lines(bug_dir)
        picked = set()
        for post in top_posts(query, post_bm25, posts):
            picked.update(top_lines(post["text"], lines))
        hit = is_hit(bug, picked)
        hits += int(hit)
        per_bug.append({"bug_id": bug["bug_id"], "hit": hit, "n_lines": len(picked)})
        print(f"bug {bug['bug_id']}: {'hit' if hit else 'miss'}")

    n = sum(1 for row in per_bug if row.get("hit") is not None)
    recall = (hits / n) if n else 0.0
    commit = bugsinpy_commit()
    manifest = write_manifest(commit)
    log = {
        "metric": "Recall@5",
        "value": round(recall, 4),
        "hits": hits,
        "n": n,
        "commit": git_head(),
        "config": {
            "baseline": "okapi-bm25",
            "k1": K1,
            "b": B,
            "posts": N_POSTS,
            "lines_per_post": N_LINES,
            "seed": SEED,
            "query": "failing test file",
            "project": PROJECT,
            "corpus_n": CORPUS_N,
        },
        "data": {
            "bugsinpy_commit": commit,
            "corpus_revision": manifest["corpus"]["revision"],
            "bugs_sha256": manifest["files"]["bugs.jsonl"]["sha256"],
            "corpus_sha256": manifest["files"]["corpus.jsonl"]["sha256"],
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "rank_bm25": version("rank-bm25"),
        "bugs": per_bug,
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(log, indent=2) + "\n")
    print(f"Recall@5 = {hits}/{n} = {recall:.3f}")
    print(f"log -> {LOG_FILE}")


if __name__ == "__main__":
    main()
