"""PostToolUse hook for a claude-code browser agent: the gate's CAPTCHA hand-off, told (CAPTCHA-HANDOFF-2).

Run by the claude-code runner's interpreter as `python -I <this file>
<spool>`, from the `--settings` file `claude_code.write_headless_settings`
writes, where `<spool>` is the attempt's `$SWARM_HANDOFF`. Standard library
only, and nothing imported from the worker: `-I` drops the package's path,
and a hook must not depend on it.

WHY (BGD, 2026-10-23; part of #2058; docs/design/live-browser.md §4.1). When
the gate sees a CAPTCHA on the agent's page it raises the hand-off itself
(`HandoffPath._raise_for_gate`, `agent_worker/handoff.py`) and pauses the
agent's browser. The agent's next tool result must then say that a person has
been asked to solve it and that it should wait and not retry -- and
claude-code puts text into a tool result only through a hook. So after every
tool call this reads `<spool>/.last` and the answer it names,
`<spool>/responses/<id>.json`, and while that answer is `origin: "gate"` with
`outcome: "waiting"` it returns the answer's `message` (the worker's
`GATE_WAITING` sentence) as `hookSpecificOutput.additionalContext`. An agent
request that joined the gate's open hand-off is answered the same way, so it
is told too. Any other answer -- the agent's own request, a returned, expired
or refused one -- prints nothing.

BOUNDED. Two small reads, no wait and no network: `.last` and the answer are
each opened without following a symlink, must be regular files, and are read
up to a fixed size; `.last` must be a request id (the worker's `REQUEST_ID`),
so it cannot name a path outside the spool.

FAIL OPEN. A missing spool, a missing or unreadable file, JSON that is not an
answer, or any error here exits 0 with nothing written: the tool call's result
is the CLI's, unchanged. A hook that broke every tool call would cost the
step; a missed note costs only what `swarm-handoff wait` still tells the agent.
"""

import json
import os
import re
import stat
import sys

#: The worker's request ids (`handoff.REQUEST_ID`): no `/`, no `..`.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: The most read of `.last` and of an answer. The worker's answers are a few
#: hundred bytes; anything far larger is not one.
MAX_LAST_BYTES = 256
MAX_ANSWER_BYTES = 64 * 1024
#: The most passed on: GATE_WAITING with a 253-character host fits easily.
MAX_MESSAGE_CHARS = 2000
#: The values the worker writes for a gate-raised hand-off still open
#: (`handoff.GATE_ORIGIN`, the `waiting` outcome).
GATE_ORIGIN = "gate"
WAITING = "waiting"


def read_small(path, limit):
    """The file's text, up to `limit` bytes; None if it is not a regular file or is larger."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        return None
    return data.decode("utf-8")


def gate_message(spool):
    """The open gate hand-off's message for the agent, or None."""
    if not spool:
        return None
    last = read_small(os.path.join(spool, ".last"), MAX_LAST_BYTES)
    request_id = (last or "").strip()
    if not REQUEST_ID.fullmatch(request_id):
        return None
    text = read_small(os.path.join(spool, "responses", request_id + ".json"), MAX_ANSWER_BYTES)
    if text is None:
        return None
    answer = json.loads(text)
    if not isinstance(answer, dict):
        return None
    if answer.get("origin") != GATE_ORIGIN or answer.get("outcome") != WAITING:
        return None
    message = answer.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()[:MAX_MESSAGE_CHARS]


def main():
    try:
        # The CLI writes the call to stdin; nothing in it is needed.
        sys.stdin.buffer.read(1 << 20)
    except Exception:
        pass
    try:
        message = gate_message(sys.argv[1] if len(sys.argv) > 1 else "")
        if message is None:
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
