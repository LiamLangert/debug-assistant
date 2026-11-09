# debug-assistant

CS6170 Capstone Project - Locates and provides hints for Python bugs

## Setup

```bash
uv sync
uv run scripts/download_bugsinpy.py
```

This clones the BugsInPy repo (shallow) into `data/raw/BugsInPy/`, or updates it if it's
already there. `data/` is git-ignored; re-run the script to rebuild it.
