"""Build a small sample Stack Overflow corpus for the smoke-test baseline.

Uses the StaQC dataset (Python Stack Overflow question-code pairs, CC-licensed,
redistributed on Hugging Face). For the walkthrough smoke test we only need a
small sample of posts to retrieve against, not the full Stack Exchange dump.

Each corpus doc = one SO post: the question title + its code snippet.

Output: data/processed/corpus.jsonl  (one JSON post per line)

Usage:
    uv run scripts/build_corpus.py --n 2000
"""

import argparse
import json
from pathlib import Path

from datasets import load_dataset

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "processed" / "corpus.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000,
                    help="number of posts to sample for the smoke-test corpus")
    args = ap.parse_args()

    # single-code answer posts, Python
    ds = load_dataset("koutch/staqc", "sca_python", split="train")

    corpus = []
    for i, row in enumerate(ds):
        if i >= args.n:
            break
        # a 'post' = the question title plus its code snippet
        snippet = row["snippet"]
        if isinstance(snippet, dict):          
            snippet = " ".join(snippet.get("text", []))
        elif isinstance(snippet, list):
            snippet = " ".join(snippet)
        text = (row["question"] or "") + "\n" + (snippet or "")
        corpus.append({
            "post_id": str(row["question_id"]),
            "text": text.strip(),
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