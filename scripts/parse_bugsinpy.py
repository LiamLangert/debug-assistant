"""Parse BugsInPy bug folders into structured records.

For each bug under data/raw/BugsInPy/projects/<project>/bugs/<id>/ we read:
  bug.info       -> buggy/fixed commit ids, the failing test file
  bug_patch.txt  -> the fix diff; we extract the changed file(s) and the
                    line numbers the fix touched (our ground-truth label)

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


def parse_patch(path: Path) -> list[dict]:
    """Parse a git diff. Return a list of {file, lines} the fix changed."""
    changes = []
    current_file = None
    new_line_no = None

    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            changes.append({"file": current_file, "lines": []})
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            if m:
                new_line_no = int(m.group(1))
        elif current_file is not None and new_line_no is not None:
            if line.startswith("+") and not line.startswith("+++"):
                changes[-1]["lines"].append(new_line_no)
                new_line_no += 1
            elif line.startswith("-") and not line.startswith("---"):
                if new_line_no not in changes[-1]["lines"]:
                    changes[-1]["lines"].append(new_line_no)
            else:
                new_line_no += 1

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
            records.append({
                "project": project_dir.name,
                "bug_id": bug_dir.name,
                "buggy_commit": info.get("buggy_commit_id"),
                "fixed_commit": info.get("fixed_commit_id"),
                "test_file": info.get("test_file"),
                "changes": changes,
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