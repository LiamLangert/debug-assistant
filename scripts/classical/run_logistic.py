"""Logistic regression reranks the BM25 top 50 posts.

    uv run scripts/classical/run_logistic.py

Fit on train projects only. A post counts as positive when its own text puts
a fix line in the top 5 source lines. Features are the BM25 score and how
much of the question and the snippet show up in the traceback. Post age and
tags are not on a StaQC row, so they are not features. An accepted answer and
the question's comments are features when the post has them (the API rows do)
and stay 0 on StaQC.

After the fit, the model picks the five posts. Lines are ranked from the
traceback only, so the post text does not get mixed into that query. The
test split is not read. Nothing here rewrites the baseline logs.

The fit needs a handful of train projects with a reproduced run and a cached
repo. One bug is not a training set, so the script stops rather than write
a model from it.
"""

import json
import sys
from pathlib import Path

from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "baseline"))
sys.path.insert(0, str(ROOT / "scripts" / "data"))

import run_baseline as base  # noqa: E402

SPLIT_FILE = ROOT / "splits" / "projects.json"
RESULTS = ROOT / "results"
TOP_N = 50  # posts the reranker sees
KEEP = 5  # posts it hands to the line ranker
MIN_TRAIN_BUGS = 8
FEATURES = (
    "bm25",
    "question_overlap",
    "snippet_overlap",
    "accepted",
    "comment_overlap",
)


def share(text: str, query_tokens: set[str]) -> float:
    """Share of this text's words that also appear in the traceback."""
    words = base.tokens(text)
    if not words:
        return 0.0
    return sum(word in query_tokens for word in words) / len(words)


def comment_text(post: dict) -> str:
    comments = post.get("comments") or []
    return " ".join((c.get("text") or "") for c in comments[: base.N_COMMENTS])


def features_of(post: dict, bm25_score: float, query_tokens: set[str]) -> list[float]:
    question, snippet, _ = base.post_parts(post)
    return [
        float(bm25_score),
        share(question, query_tokens),
        share(snippet, query_tokens),
        1.0 if post.get("accepted") else 0.0,
        share(comment_text(post), query_tokens),
    ]


def post_finds_fix(line_index, lines, post_text: str, truth: set[tuple[str, int]]) -> bool:
    """True when this post's text alone puts a fix line in the top 5."""
    scores = line_index.get_scores(base.tokens(post_text) or ["blank"])
    top = [lines[i][:2] for i in base.ranked(scores)[: base.K]]
    return any(line in truth for line in top)


def load_bugs(names: list[str]) -> list[dict]:
    bugs = [b for b in base.load_jsonl(base.BUGS_FILE) if b["project"] in names]
    bugs.sort(key=lambda b: (b["project"].lower(), int(b["bug_id"])))
    return bugs


def prepare(bug: dict, posts: list[dict], post_index):
    """Traceback, source lines, gold lines, and the BM25 top 50 for one bug.

    None when the run, the traceback, or the cached repo is missing.
    """
    run_file = base.RUNS / bug["project"] / f"{bug['bug_id']}.json"
    if not run_file.exists():
        return None
    run = json.loads(run_file.read_text())
    if not run.get("reproduced"):
        return None
    query = base.traceback_query(run)
    if not base.tokens(query):
        return None
    repo = base.REPOS / f"{bug['project']}.git"
    if not repo.is_dir():
        return None
    lines = base.source_lines(bug["project"], bug["buggy_commit"])
    truth = {(c["file"], n) for c in bug["changes"] for n in c["lines"]}
    scores = post_index.get_scores(base.tokens(query))
    order = base.ranked(scores)[:TOP_N]
    return {
        "bug": bug,
        "query": query,
        "lines": lines,
        "truth": truth,
        "line_index": base.BM25Okapi(
            [base.tokens(text) or ["blank"] for _, _, text in lines], k1=base.K1, b=base.B
        ),
        "chosen": [(posts[i], float(scores[i])) for i in order],
    }


def rows_for(ready: dict) -> list[dict]:
    query_tokens = set(base.tokens(ready["query"]))
    rows = []
    for post, score in ready["chosen"]:
        _, _, text = base.post_parts(post)
        rows.append({
            "x": features_of(post, score, query_tokens),
            "y": int(post_finds_fix(ready["line_index"], ready["lines"], text, ready["truth"])),
            "post": post,
        })
    return rows


def rerank(ready: dict, scaler, model) -> dict:
    """Pick the top 5 posts. Rank lines from the traceback only."""
    query_tokens = set(base.tokens(ready["query"]))
    scored = []
    for post, bm25_score in ready["chosen"]:
        x = scaler.transform([features_of(post, bm25_score, query_tokens)])
        scored.append((float(model.predict_proba(x)[0, 1]), post))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked = [post for _, post in scored[:KEEP]]
    ranking = base.ranked(ready["line_index"].get_scores(base.tokens(ready["query"])))
    judged = base.evaluate(ranking, ready["lines"], ready["truth"])
    judged["posts"] = [post.get("post_id") for post in picked]
    return judged


def coverage(bugs: list[dict]) -> dict:
    """How many bugs in this split can actually be used."""
    counts = {"bugs": len(bugs), "reproduced": 0, "repo": 0}
    for bug in bugs:
        run_file = base.RUNS / bug["project"] / f"{bug['bug_id']}.json"
        if not run_file.exists():
            continue
        run = json.loads(run_file.read_text())
        if not run.get("reproduced"):
            continue
        counts["reproduced"] += 1
        if (base.REPOS / f"{bug['project']}.git").is_dir():
            counts["repo"] += 1
    return counts


def main() -> None:
    split = json.loads(SPLIT_FILE.read_text())
    posts = base.load_jsonl(base.CORPUS_FILE)
    post_index = base.BM25Okapi(
        [base.tokens(base.post_parts(p)[2]) or ["blank"] for p in posts], k1=base.K1, b=base.B
    )
    train_bugs = load_bugs(split["train"])
    train_ready = []
    for bug in train_bugs:
        ready = prepare(bug, posts, post_index)
        if ready is not None:
            train_ready.append(ready)

    have = coverage(train_bugs)
    print(f"train: {have['bugs']} bugs, {have['reproduced']} reproduced, {have['repo']} with a repo")
    if len(train_ready) < MIN_TRAIN_BUGS:
        raise SystemExit(
            f"Not fitting. {len(train_ready)} train bugs can be used and this wants "
            f"at least {MIN_TRAIN_BUGS}. The train tracebacks are mostly not in runs/ yet."
        )

    examples = [row for ready in train_ready for row in rows_for(ready)]
    positives = sum(row["y"] for row in examples)
    if positives == 0 or positives == len(examples):
        raise SystemExit(
            f"Not fitting. {positives} of {len(examples)} train posts are positive, so there is no split to learn."
        )

    scaler = StandardScaler()
    x_train = scaler.fit_transform([row["x"] for row in examples])
    y_train = [row["y"] for row in examples]
    model = LogisticRegression(random_state=0, max_iter=1000, class_weight="balanced")
    model.fit(x_train, y_train)

    val_bugs = load_bugs(split["validation"])
    judged = []
    for bug in val_bugs:
        ready = prepare(bug, posts, post_index)
        if ready is None:
            continue
        result = rerank(ready, scaler, model)
        result["project"] = bug["project"]
        result["bug_id"] = bug["bug_id"]
        judged.append(result)

    # base.summarize reads rows that contain the metric dict under a key
    wrapped = [{"logistic": row} for row in judged]
    summary = base.summarize(wrapped, "logistic")
    log = {
        "model": "logistic regression",
        "features": list(FEATURES),
        "coef": [round(float(c), 4) for c in model.coef_[0]],
        "intercept": round(float(model.intercept_[0]), 4),
        "class_weight": "balanced",
        "scaler_mean": [round(float(v), 4) for v in scaler.mean_],
        "scaler_scale": [round(float(v), 4) for v in scaler.scale_],
        "seed": 0,
        "top_n": TOP_N,
        "keep": KEEP,
        "train": {
            "bugs": len(train_ready),
            "posts": len(examples),
            "positives": positives,
        },
        "validation": summary,
        "commit": base.git_head(ROOT),
        "corpus_n": len(posts),
        "bugs": judged,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "classical_validation.json"
    out.write_text(json.dumps(log, indent=2) + "\n")
    print(f"fit on {len(train_ready)} train bugs, {positives}/{len(examples)} positive posts")
    if summary["n"]:
        print(f"validation line Recall@5 {summary['line_hits']}/{summary['n']}  "
              f"file Recall@5 {summary['file_hits']}/{summary['n']}  MRR {summary['mrr']:.3f}")
    print(f"log -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
