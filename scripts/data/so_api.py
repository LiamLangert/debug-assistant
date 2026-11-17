"""Stack Overflow search, answers, and comments from the Stack Exchange API, cached.

    similar_posts(text, todate)   python questions similar to a text (e.g. a
                                  traceback), each with its best answer and
                                  its comments

todate (unix seconds) leaves out anything created later.

Every response is cached under data/cache/so_api/, so a query or post is only
fetched once and reruns give the same results even though the live site changes.
Delete that folder to refetch.

Quota: about 300 requests/day per IP with no key. Put SE_API_KEY=<key> (a free
key from stackapps.com) in .env at the repo root to get 10,000/day.

    uv run scripts/data/so_api.py "TypeError: to_unicode must receive a bytes, str or unicode object, got bool"
"""

import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # SE_API_KEY=...; .env is gitignored
CACHE = ROOT / "data" / "cache" / "so_api"
API = "https://api.stackexchange.com/2.3"
BATCH = 100  # the API's limit on ids per request
MAX_TEXT = 2000  # characters of search text sent
TAG = re.compile(r"<[^>]+>")
CODE = re.compile(r"<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>", re.S)


def text_of(body: str) -> str:
    """The API's rendered HTML as plain text."""
    return html.unescape(TAG.sub("", body or "")).strip()


def code_of(body: str) -> str:
    """The <pre><code> blocks in a rendered post, joined."""
    return "\n".join(html.unescape(c).strip() for c in CODE.findall(body or ""))


def _get(path: str, params: dict) -> dict:
    """GET every page of one API call and return the items together.

    The API always gzips its responses, error responses too.
    """
    params = {"site": "stackoverflow", "filter": "withbody", **params}
    if os.environ.get("SE_API_KEY"):
        params["key"] = os.environ["SE_API_KEY"]
    items, page = [], 1
    while True:
        url = f"{API}{path}?" + urllib.parse.urlencode({**params, "page": page})
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                raw, encoding = r.read(), r.headers.get("Content-Encoding")
        except urllib.error.HTTPError as e:  # message only: the URL holds the key
            raw, encoding = e.read(), e.headers.get("Content-Encoding")
            body = json.loads(gzip.decompress(raw) if encoding == "gzip" else raw)
            raise SystemExit(f"Stack Exchange API error {e.code}: "
                             f"{body.get('error_name')}: {body.get('error_message')}") from None
        data = json.loads(gzip.decompress(raw) if encoding == "gzip" else raw)
        items += data.get("items", [])
        if data.get("backoff"):  # the API wants us to wait before the next call
            time.sleep(data["backoff"])
        if not data.get("has_more") or params.get("pagesize", 30) < 100:
            break  # a small pagesize means we only wanted the first page
        page += 1
    if data.get("quota_remaining", 1000) < 20:
        print(f"warning: Stack Exchange API quota nearly used up "
              f"({data['quota_remaining']} requests left today)", file=sys.stderr)
    return {"items": items}


def _per_question(kind: str, question_ids: list[str], row,
                  todate: int | None = None) -> dict[str, list[dict]]:
    """{question_id: [item, ...]} from /questions/{ids}/<kind>, highest score
    first, cached per question.

    The cache keeps everything; todate (unix seconds) drops items created later.
    """
    folder = CACHE / kind
    folder.mkdir(parents=True, exist_ok=True)
    ids = list(dict.fromkeys(str(i) for i in question_ids if i))
    missing = [i for i in ids if not (folder / f"{i}.json").exists()]
    for start in range(0, len(missing), BATCH):
        batch = missing[start:start + BATCH]
        found: dict[str, list[dict]] = {i: [] for i in batch}
        params = {"sort": "votes", "order": "desc", "pagesize": 100}
        for item in _get(f"/questions/{';'.join(batch)}/{kind}", params)["items"]:
            qid = str(item.get("question_id") or item.get("post_id"))
            if qid in found:
                found[qid].append(row(item))
        for qid, items in found.items():  # [] for no items or a deleted question
            (folder / f"{qid}.json").write_text(json.dumps(items, indent=1) + "\n")
    return {i: [x for x in json.loads((folder / f"{i}.json").read_text())
                if not todate or x["creation_date"] <= todate] for i in ids}


def similar_posts(text: str, todate: int | None = None, n: int = 5) -> list[dict]:
    """Up to n python questions similar to text, each with its best answer and
    its comments.

    Questions come from /similar, Stack Overflow's "questions that may already
    have your answer". Only the last MAX_TEXT characters of text are sent (the
    error is at the end of a traceback, and the text goes in the URL). The best
    answer is the accepted one, else the top-voted one. A question with no
    answer is dropped, so there can be fewer than n posts. todate (unix
    seconds) leaves out questions, answers and comments created later.

    Each post has post_id, question (the title), snippet (the answer's code, or
    its text if it has no code), text (question + snippet, like a StaQC corpus
    row), and comments (the question's comments, highest score first, each
    with comment_id, score, creation_date and text).
    """
    params = {"title": text[-MAX_TEXT:], "tagged": "python",
              "sort": "relevance", "order": "desc", "pagesize": n}
    if todate:
        params["todate"] = todate
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    path = CACHE / "similar" / f"{key}.json"
    if not path.exists():
        found = _get("/similar", params)["items"]
        keep = [{"question_id": str(q["question_id"]), "title": html.unescape(q["title"]),
                 "body": q.get("body", ""), "tags": q["tags"], "score": q["score"],
                 "creation_date": q["creation_date"]} for q in found]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"params": params, "items": keep}, indent=1) + "\n")
    hits = json.loads(path.read_text())["items"]

    ids = [q["question_id"] for q in hits]
    answers = _per_question("answers", ids, lambda a: {
        "answer_id": a["answer_id"], "score": a["score"], "is_accepted": a["is_accepted"],
        "creation_date": a["creation_date"], "body": a.get("body", ""),
    }, todate)
    answered = [q for q in hits if answers[q["question_id"]]]
    comments = _per_question("comments", [q["question_id"] for q in answered], lambda c: {
        "comment_id": c["comment_id"], "score": c["score"],
        "creation_date": c["creation_date"], "text": text_of(c.get("body", "")),
    }, todate)

    posts = []
    for q in answered:
        best = max(answers[q["question_id"]], key=lambda a: (a["is_accepted"], a["score"]))
        snippet = code_of(best["body"]) or text_of(best["body"])
        posts.append({"post_id": q["question_id"], "question": q["title"],
                      "snippet": snippet, "text": f"{q['title']}\n{snippet}",
                      "comments": comments[q["question_id"]]})
    return posts


if __name__ == "__main__":
    for p in similar_posts(sys.argv[1]):
        print(f"{p['post_id']} {p['question']}\n    {p['snippet'][:100]!r}"
              f"\n    {len(p['comments'])} comments")
