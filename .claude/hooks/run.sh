#!/bin/sh
# Hook launcher (slice T-1, D-112): sh run.sh <file in .claude/hooks>, from Git Bash on Windows and
# from Linux or WSL. A .py hook runs on the project's venv Python, chosen by platform, not by which file
# exists: .venv/Scripts/python.exe under Git Bash, .venv/bin/python elsewhere (from WSL, a Windows
# venv's python.exe would be handed Linux paths). A .ps1 hook (the Windows toast) runs on Windows
# PowerShell 5.1 and does nothing elsewhere. A missing piece exits 1, never 2: Claude Code takes exit 2
# from a PreToolUse hook as a block of the tool call. Keep LF line endings (.gitattributes, *.sh).
dir=${CLAUDE_PROJECT_DIR:-}
hook="$dir/.claude/hooks/$1"
if [ -z "$dir" ] || [ ! -f "$hook" ]; then
    echo "run.sh: no hook file $hook (is CLAUDE_PROJECT_DIR set?)" >&2
    exit 1
fi
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) windows=1 ;;
    *) windows= ;;
esac
case "$1" in
    *.py)
        if [ -n "$windows" ]; then
            python="$dir/.venv/Scripts/python.exe"
        else
            python="$dir/.venv/bin/python"
        fi
        if [ ! -x "$python" ]; then
            echo "run.sh: no venv Python at $python (python -m venv .venv; pip install -r requirements-dev.txt)" >&2
            exit 1
        fi
        exec "$python" "$hook"
        ;;
    *.ps1)
        [ -n "$windows" ] || exit 0
        exec powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$hook"
        ;;
esac
echo "run.sh: $1 is neither a .py nor a .ps1 hook" >&2
exit 1
