# debug-assistant

CS6170 Capstone Project - Locates and provides hints for Python bugs

## Smoke test

One command. No GPU and no API key. It downloads the pinned BugsInPy commit and a
small Stack Overflow sample, then prints Recall@5.

```bash
uv sync && uv run scripts/run_baseline_smoke.py
```

`data/` is not in git. That command rebuilds it. Same commit and same corpus
revision every time, hashes in `manifests/smoke.json`. The project split is
`splits/projects.json`. The number, the git commit, and those hashes are written
to `results/baseline_smoke.json`.

The smoke query is the failing test file, not a traceback. A traceback means
running each project's old tests. The score is still BM25 from that text to
posts, then BM25 from each post onto the buggy file lines. The smoke bugs are
the PySnooper ones, and PySnooper is in the test split. Bug 1's test file is
not in the buggy commit, so that bug is logged and left out of the score.
