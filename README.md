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
running each project's old tests. Like BugsInPy's own checkout, the test files
come from the fixed commit, since the fix usually adds the failing test. The
buggy source files come from the buggy commit. The score is BM25 from the test
text to posts, then BM25 from each post onto the buggy file lines. The smoke
bugs are the three PySnooper ones, and PySnooper is in the test split. The
ground truth is the source lines the fix touched. Edits the fix made to test
files are kept in `test_changes` in `bugs.jsonl` and are not scored.
