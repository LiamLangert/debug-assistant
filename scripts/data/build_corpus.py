"""Build a small sample Stack Overflow corpus for the smoke-test baseline.

Uses the StaQC dataset (Python Stack Overflow question-code pairs, CC-licensed,
redistributed on Hugging Face). For the walkthrough smoke test we only need a
small sample of posts to retrieve against, not the full Stack Exchange dump.

Each corpus doc is one Stack Overflow question plus one code block taken from an
answer. StaQC does not include comments or the other answers on the thread, so
those are not stored.

Output: data/processed/corpus.jsonl  (one JSON post per line)

Usage:
    uv run scripts/data/build_corpus.py --n 2000
"""

import argparse
import json
from pathlib import Path

from datasets import load_dataset

ROOT = Path(__file__).resolve().parents[2]
OUT_FILE = ROOT / "data" / "processed" / "corpus.jsonl"
DATASET = "koutch/staqc"
CONFIG = "sca_python"
# Pinned dataset revision. Do not follow the moving "main" branch.
REVISION = "5851ae43677d73ad00af3f2ab117b8bf3bb51cca"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000,
                    help="number of posts to sample for the smoke-test corpus")
    args = ap.parse_args()

    # single-code answer posts, Python. Streaming so we do not pull the whole set.
    ds = load_dataset(
        DATASET,
        CONFIG,
        split="train",
        revision=REVISION,
        streaming=True,
        trust_remote_code=True,
    )

    corpus = []
    for i, row in enumerate(ds):
        if i >= args.n:
            break
        # question text, and the one code block StaQC pulled out of an answer
        snippet = row["snippet"]
        if isinstance(snippet, dict):
            snippet = " ".join(snippet.get("text") or [])
        elif isinstance(snippet, list):
            snippet = " ".join(str(part) for part in snippet)
        question = (row.get("question") or "").strip()
        snippet = (snippet or "").strip()
        corpus.append({
            "post_id": str(row["question_id"]),
            "question": question,
            "snippet": snippet,
            "text": (question + "\n" + snippet).strip(),
        })

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("w") as f:
        for p in corpus:
            f.write(json.dumps(p) + "\n")

    print(f"Wrote {len(corpus)} posts -> {OUT_FILE}")
    if corpus:
        print("Example:", json.dumps(corpus[0], indent=2)[:400])


if __name__ == "__main__":
    main()