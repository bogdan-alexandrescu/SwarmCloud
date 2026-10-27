"""PostToolUse hook for claude-code: the budget's 80% note, told once (BUDGET-CAP, part of #2459).

Run by the claude-code runner's interpreter as `python -I <this file>
<note>`, from the `--settings` file `claude_code.write_headless_settings`
writes, where `<note>` is the attempt's `$SWARM_BUDGET_NOTE`. Standard
library only, and nothing imported from the worker: `-I` drops the package's
path, and a hook must not depend on it.

WHY (BGD, 2026-10-26). At 80% of its budget the agent is told to finish and
publish. A headless claude-code run takes no new message while it runs; the
one place text reaches it mid-run is a hook's
`hookSpecificOutput.additionalContext`, shown with the next tool result. The
worker writes the note (`agent_worker.budget.BudgetGuard`) and this hook
hands it on.

ONCE. The note is renamed to `<note>.told` before it is read: a rename is
atomic, so of two tool calls finishing together exactly one tells it, and
every later call finds no note and prints nothing.

FAIL OPEN. No note, a note that is not a regular file or is too large, JSON
that is not a note, or any error: exit 0, nothing written. A missed note
costs only the warning; the stop at 100% does not depend on it.
"""

import json
import os
import stat
import sys

MAX_NOTE_BYTES = 8 * 1024


def main():
    try:
        if len(sys.argv) < 2:
            return 0
        note = sys.argv[1]
        told = note + ".told"
        try:
            os.rename(note, told)
        except OSError:
            return 0
        fd = os.open(told, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return 0
            data = os.read(fd, MAX_NOTE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_NOTE_BYTES:
            return 0
        message = json.loads(data.decode("utf-8")).get("message")
        if not isinstance(message, str) or not message.strip():
            return 0
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }))
        sys.stdout.flush()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
