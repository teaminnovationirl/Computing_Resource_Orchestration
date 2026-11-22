"""PostToolUse hook for Edit and Write (slice T-1, D-109): ruff check on the edited .py file. Standard
library only. Findings go to stderr with exit 2, which Claude Code shows to Claude. The hook never fixes,
formats or writes anything (--no-fix, --no-cache). Files outside the project, other file types and files
that the ruff settings exclude pass silently."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def ruff_path(project: Path) -> Path:
    """The venv's ruff, by run.sh's platform rule: Scripts/ruff.exe on Windows, bin/ruff elsewhere."""
    return project / ".venv" / ("Scripts/ruff.exe" if os.name == "nt" else "bin/ruff")


def report(text: str) -> int:
    sys.stderr.buffer.write((text + "\n").encode("utf-8", "replace"))
    sys.stderr.buffer.flush()
    return 2


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        file_path = payload["tool_input"]["file_path"]
    except (ValueError, KeyError, TypeError):
        return 0
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not isinstance(file_path, str) or not file_path.endswith(".py") or not project_dir:
        return 0
    project, target = Path(project_dir).resolve(), Path(file_path).resolve()
    if not target.is_relative_to(project) or not target.is_file():
        return 0
    ruff = ruff_path(project)
    if not ruff.is_file():
        return report(f"ruff_check.py: no ruff at {ruff}; run pip install -r requirements-dev.txt")
    try:
        done = subprocess.run([str(ruff), "check", "--no-fix", "--no-cache", "--force-exclude", "--quiet",
                               "--output-format", "concise", str(target)],
                              cwd=project, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return report(f"ruff_check.py: ruff did not run ({exc})")
    if done.returncode == 0:
        return 0
    return report((done.stdout + done.stderr).decode("utf-8", "replace").strip())


if __name__ == "__main__":
    sys.exit(main())
