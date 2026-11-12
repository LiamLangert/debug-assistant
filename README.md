# debug-assistant

CS6170 Capstone Project - Locates and provides hints for Python bugs

## Smoke test

One command. No GPU and no API key. It downloads the pinned BugsInPy commit and a
small Stack Overflow sample, then prints Recall@5.

```bash
uv sync && uv run scripts/smoketest/run_baseline_smoke.py
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

## Full baseline

The query is the real traceback of each bug's failing test. Getting it means
running the project's old tests, so this needs Docker and takes hours per split.

```bash
uv run scripts/baseline/run_bugs.py --split validation      # check out and run every bug
uv run scripts/baseline/run_baseline.py --split validation  # score them
```

`run_bugs.py` runs each bug's buggy and fixed versions in one `python:<version>`
container (`checkout_bug.py`, then `run_bug.py`), deletes the checkouts, and
skips bugs it has already run, so it can be stopped and started again. Each bug's
outcome and tracebacks go to `runs/<project>/<bug_id>.json`, which holds no host
paths and is small enough to commit. A bug counts only when its test fails on
the buggy version and passes on the fixed one (`"reproduced": true`). Build logs
stay in `data/work/`.

`run_baseline.py` ranks every non-test source line at the buggy commit. It
reports line and file Recall@5 and MRR, once through the Stack Overflow posts and
once with the traceback alone, to `results/baseline_<split>.json`.
