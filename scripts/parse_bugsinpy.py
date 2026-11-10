"""Parse BugsInPy bug folders into structured records.

For each bug under data/raw/BugsInPy/projects/<project>/bugs/<id>/ we read:
  bug.info       -> buggy/fixed commit ids, the failing test file(s)
  bug_patch.txt  -> the fix diff; we extract the changed file(s) and the
                    line numbers the fix touched

"changes" is the ground-truth label: source files only. Edits the fix made to
test files go in "test_changes" so they are kept but never scored.

Output: data/processed/bugs.jsonl  (one JSON record per bug)

Usage:
    uv run scripts/parse_bugsinpy.py
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "data" / "raw" / "BugsInPy" / "projects"
OUT_FILE = ROOT / "data" / "processed" / "bugs.jsonl"


def parse_bug_info(path: Path) -> dict:
    """bug.info is key="value" lines. Return a dict of those fields."""
    info = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line:
            key, _, val = line.partition("=")
            info[key.strip()] = val.strip().strip('"')
    return info


def is_test_path(path: str) -> bool:
    """Test code: under a test/ or tests/ dir, or a test_*.py, *_test.py, conftest.py file."""
    *dirs, name = path.split("/")
    if any(d in ("test", "tests") for d in dirs):
        return True
    return re.fullmatch(r"(test_.*|.*_test|conftest)\.py", name) is not None


def parse_patch(path: Path) -> list[dict]:
    """Parse a git diff. Lines are numbers in the buggy file, which is what we search."""
    changes = []
    current = None
    old_no = None

    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("+++ b/"):
            current = {"file": line[len("+++ b/"):].strip(), "lines": []}
            changes.append(current)
            old_no = None
        elif line.startswith("@@"):
            m = re.search(r"-(\d+)", line)
            old_no = int(m.group(1)) if m else None
        elif current is None or old_no is None:
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            # Pure insert: the buggy file has no new line, so mark the line it landed next to.
            anchor = old_no - 1 if old_no > 1 else old_no
            if anchor not in current["lines"]:
                current["lines"].append(anchor)
        elif line.startswith("-") and not line.startswith("---"):
            current["lines"].append(old_no)
            old_no += 1
        else:
            old_no += 1

    cleaned = []
    for c in changes:
        if c["lines"]:
            c["lines"] = sorted(set(c["lines"]))
            cleaned.append(c)
    return cleaned

def main() -> None:
    records = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        bugs_dir = project_dir / "bugs"
        if not bugs_dir.exists():
            continue
        for bug_dir in sorted(bugs_dir.iterdir(), key=lambda p: p.name):
            info_path = bug_dir / "bug.info"
            patch_path = bug_dir / "bug_patch.txt"
            if not (info_path.exists() and patch_path.exists()):
                continue
            info = parse_bug_info(info_path)
            changes = parse_patch(patch_path)
            # bug.info joins several failing test files with ";"
            test_files = [t.strip() for t in info.get("test_file", "").split(";") if t.strip()]
            records.append({
                "project": project_dir.name,
                "bug_id": bug_dir.name,
                "buggy_commit": info.get("buggy_commit_id"),
                "fixed_commit": info.get("fixed_commit_id"),
                "test_files": test_files,
                "changes": [c for c in changes if not is_test_path(c["file"])],
                "test_changes": [c for c in changes if is_test_path(c["file"])],
            })

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    print(f"Parsed {len(records)} bugs -> {OUT_FILE}")
    if records:
        print("Example:", json.dumps(records[0], indent=2))


if __name__ == "__main__":
    main()