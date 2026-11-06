"""PreToolUse hook for claude-code: warn a second guard run on an unchanged tree (TOKEN-A, part of #2524).

Run by the claude-code runner's interpreter as `python -I <this file>
<state>`, from the `--settings` file `claude_code.write_headless_settings`
writes, where `<state>` is a file beside that settings file, outside the
checkout. Standard library only, and nothing imported from the worker: `-I`
drops the package's path, and a hook must not depend on it.

WHY (BGD, 2026-10-27; #2524 item 6). `scripts/changed-guards.sh` is the one
pre-finish run, about a minute and a long transcript every time. The week's
transcripts showed lanes running it again with nothing changed in between,
re-reading the same result and paying for it on every later turn. Its answer
depends only on the tree, so a second run on the same tree is told so.
Since 2026-11-01 (#2981) the claude-code runner, the one that registers this
hook, no longer tells the agent to run the script at all (its pre-publish
gate runs it after the turn), so the warning says that too.

WARN, NEVER BLOCK. The hook writes `hookSpecificOutput.additionalContext`
and exits 0 with no permission decision: the command runs as asked. A rerun
can be right (a flaky timeout), and refusing it would cost more than the
tokens it saves.

THE TREE, NOT THE TOOL CALLS. "No edit since the last run" is read from git
rather than from Edit/Write calls, since a `sed -i` or a commit in Bash
changes the tree as much: HEAD, `git diff HEAD` and each untracked file's
name, size and mtime, hashed. Outside a git work tree it says nothing.

FAIL OPEN. Input it cannot read, a command that does not run the script, a
git that errors or a state file it cannot write: exit 0, nothing written.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

SCRIPT = "changed-guards.sh"
#: Words that may stand before the script in one command of a pipeline.
WRAPPERS = frozenset({"bash", "sh", "env", "time", "nice", "nohup", "command", "exec"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n()]")
_GIT_SECONDS = 10

MESSAGE = (
    "scripts/changed-guards.sh already ran on this exact tree: no file has "
    "changed since its last run, so it will report the same result. It runs "
    "anyway; next time read the earlier run's output instead. You need not run "
    "it at all: the worker's pre-publish gate runs it after your turn and hands "
    "you back what fails."
)


def runs_guards(command):
    """True when one command of `command` runs scripts/changed-guards.sh."""
    for part in _SEPARATORS.split(command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
        while words:
            head = words[0]
            if head in WRAPPERS:
                words.pop(0)
            elif head == "timeout":
                words.pop(0)
                while words and words[0].startswith("-"):
                    words.pop(0)
                if words:
                    words.pop(0)
            elif head.startswith("-") and len(words) > 1:
                words.pop(0)
            else:
                break
        if words and (words[0] == SCRIPT or words[0].endswith("/" + SCRIPT)):
            return True
    return False


def _git(top, *args):
    return subprocess.run(
        ["git", "-C", top, *args], capture_output=True, check=True, timeout=_GIT_SECONDS,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout


def fingerprint(cwd):
    top = _git(cwd, "rev-parse", "--show-toplevel").decode().strip()
    digest = hashlib.sha256()
    digest.update(_git(top, "rev-parse", "HEAD"))
    digest.update(_git(top, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"))
    for name in _git(top, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not name:
            continue
        st = os.lstat(os.path.join(os.fsencode(top), name))
        digest.update(b"\0%s\0%d\0%d" % (name, st.st_size, st.st_mtime_ns))
    return digest.hexdigest()


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle).get("fingerprint")
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _write_state(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"fingerprint": value}, handle)
    os.replace(tmp, path)


def main():
    try:
        if len(sys.argv) < 2:
            return 0
        state = sys.argv[1]
        call = json.load(sys.stdin)
        tool_input = call.get("tool_input") if isinstance(call, dict) else None
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str) or not runs_guards(command):
            return 0
        cwd = call.get("cwd")
        current = fingerprint(cwd if isinstance(cwd, str) and cwd else os.getcwd())
        repeated = _read_state(state) == current
        _write_state(state, current)
        if repeated:
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": MESSAGE,
                }
            }))
            sys.stdout.flush()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
