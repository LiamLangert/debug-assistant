"""Deep (dense-embedding) retrieval from a failing test's traceback.

    uv run scripts/baseline/run_deep.py --split validation --mode direct
    uv run scripts/baseline/run_deep.py --split validation --mode with_posts

Same bugs, same query, same source lines, same metric as the BM25 baseline,
so the deep numbers line up cell-for-cell with run_baseline.py. The only
change is the ranker: cosine similarity of sentence embeddings instead of BM25.

Modes:
  direct      rank code lines by similarity to the traceback (no posts).
  with_posts  retrieve 5 StaQC posts with BM25 (exactly as the baseline does),
              then rank code lines by similarity to (traceback + the 5 posts).

Model: all-MiniLM-L6-v2 (frozen). Deterministic, so seed spread is ~0.
"""

import argparse
import json
import platform
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from sentence_transformers import SentenceTransformer
from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "baseline"))
from run_baseline import (  # noqa: E402
    SPLIT_FILE, BUGS_FILE, CORPUS_FILE, RUNS, RESULTS,
    traceback_query, source_lines, evaluate, summarize,
    tokens, git_head, load_jsonl, post_parts,
    K1, B, N_POSTS,
)

MODEL_NAME = "all-MiniLM-L6-v2"
K = 5


def embed_rank(model, query_text: str, line_texts: list[str]) -> list[int]:
    """Rank line indices by cosine similarity to query_text, best first."""
    line_emb = model.encode(line_texts, normalize_embeddings=True,
                            batch_size=256, show_progress_bar=False)
    q_emb = model.encode([query_text], normalize_embeddings=True, show_progress_bar=False)[0]
    scores = line_emb @ q_emb
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [i for i in order if scores[i] > 0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=["train", "validation", "test", "all"], default="validation")
    parser.add_argument("--project", help="only this project")
    parser.add_argument("--bug", help="only this bug id (with --project)")
    parser.add_argument("--out", type=Path, help="log file (default results/deep_<split>_<mode>.json)")
    parser.add_argument("--mode", choices=["direct", "with_posts", "via_posts"], default="direct")
    args = parser.parse_args()
    use_posts = args.mode in ("with_posts", "via_posts")

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

    posts = post_index = None
    if use_posts:
        posts = load_jsonl(CORPUS_FILE)
        post_index = BM25Okapi([tokens(post_parts(p)[2]) or ["blank"] for p in posts], k1=K1, b=B)
        print(f"{len(bugs)} bugs in {args.split}, {len(posts)} StaQC posts (mode=with_posts)")
    else:
        print(f"{len(bugs)} bugs in {args.split} (mode=direct)")

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

        lines = source_lines(bug["project"], bug["buggy_commit"])
        truth = {(c["file"], n) for c in bug["changes"] for n in c["lines"]}
        line_texts = [text for _, _, text in lines]

        if use_posts:
            scores = post_index.get_scores(tokens(query))
            order = sorted(range(len(posts)), key=lambda i: scores[i], reverse=True)
            top = [posts[i] for i in order[:N_POSTS]]
            post_text = "\n".join(post_parts(p)[2] for p in top)
            if args.mode == "via_posts":
                query_text = post_text                 
            else:  
                query_text = f"{query}\n{post_text}"   
        else:
            query_text = query

        row[args.mode] = evaluate(embed_rank(model, query_text, line_texts), lines, truth)
        counts["scored"] += 1

        e = row[args.mode]
        print(f"{name}: {len(lines)} lines | {args.mode}: line {'hit ' if e['line_hit'] else 'miss'}"
              f" file {'hit ' if e['file_hit'] else 'miss'}, first fix line at {e['first_fix_line_rank']}")

    summary = {args.mode: summarize(rows, args.mode)}
    log = {
        "split": args.split, "method": f"dense-embedding ({args.mode})",
        "coverage": {k: counts[k] for k in ("bugs", "run", "reproduced", "scored")},
        "summary": summary, "commit": git_head(ROOT),
        "config": {"model": MODEL_NAME, "k": K, "similarity": "cosine", "mode": args.mode,
                   "posts": "5 StaQC posts via BM25" if use_posts else None,
                   "seeds": "deterministic, spread 0"},
        "python": platform.python_version(),
        "sentence_transformers": version("sentence-transformers"),
        "bugs": rows,
    }
    out = args.out or RESULTS / f"deep_{args.split}_{args.mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, indent=2) + "\n")

    c = log["coverage"]
    print(f"\n{c['scored']} scored of {c['bugs']} bugs")
    s = summary[args.mode]
    if s["n"]:
        print(f"  {args.mode}  line Recall@5 {s['line_hits']}/{s['n']}  file Recall@5 {s['file_hits']}/{s['n']}  MRR {s['mrr']:.3f}")
    print(f"log -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()