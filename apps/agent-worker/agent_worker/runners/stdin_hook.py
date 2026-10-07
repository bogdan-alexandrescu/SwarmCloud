"""PreToolUse hook for claude-code's Bash tool: the command's stdin is /dev/null (#750).

Run by the claude-code runner's interpreter as `python -I <this file>`, from
the `--settings` file `claude_code.write_headless_settings` writes. Standard
library only, and nothing imported from the worker: `-I` drops the package's
path, and a hook must not depend on it.

WHY. Headless, nothing is ever attached to a Bash command's stdin, so a
command that reads it -- a stray `cat`, `read x` -- waits until the Bash
timeout kills it: two minutes in C3E's implement step (2026-10-06), ten since
#736 raised the timeout. The CLI's Bash tool attaches `< /dev/null` itself,
but not to a command that carries a heredoc or any `<` redirect (2.1.283),
which is exactly the shape of a PR-text command.

HOW. The CLI's documented PreToolUse output, `hookSpecificOutput.updatedInput`
with no `permissionDecision`, replaces the call's input. The hook hands back
the same input with the command wrapped as

    { <command>
    } < /dev/null

A brace group runs in the current shell, so `cd`, `export` and `exit` behave
as they did, and its exit status is the command's. Only the OUTER stdin is
replaced: a pipe between commands, a heredoc or a redirect inside the group is
the inner command's own and wins over the group's. The newline before `}`
ends a trailing comment or heredoc terminator.

LEFT ALONE, passed through with no output:
  * a command that already redirects stdin (`cmd < file`, `0< f`) -- matched
    as the CLI matches it, so a `<` inside quotes also counts; such a command
    is left exactly as it would have run without the hook. Heredoc BODIES are
    removed before that test: a `<` in PR text (`<!-- -->`, `<details>`,
    `a < b`) is data, not a redirect, and counting it would skip exactly the
    heredoc-then-`cat` shape this hook exists for;
  * a command with a heredoc that has no terminator line: bash runs it with
    a warning, but wrapped, the body would swallow the closing `}`;
  * a command ending in a backslash, which would continue onto the `}`;
  * an empty command, and any input that is not a Bash call with a string
    command.

FAIL OPEN. Any error here exits 0 with nothing written, so the CLI runs the
command unchanged: a hook that broke every Bash call would cost the step,
where a missed rewrite costs at most the timeout it was meant to save.
"""

import json
import re
import sys

#: A stdin redirect: `<`, optionally after a file-descriptor number, not part
#: of `<<` (heredoc, herestring) or `<(` (process substitution). The CLI uses
#: the same test to decide not to add its own `< /dev/null`.
STDIN_REDIRECT = re.compile(r"(?:^|[\s;&|(])\d*<(?![<(])")

#: A heredoc opener, `<<EOF`, `<<-EOF`, `<< 'EOF'`, `<<"EOF"`: the CLI's own
#: heredoc pattern, with `<<<` (a herestring, which has no body) excluded.
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)(\w+)\2")


def strip_heredoc_bodies(command):
    """The command without its heredoc bodies, or None if one is unterminated."""
    lines = command.split("\n")
    kept = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        # Several heredocs on one line take their bodies one after another.
        for dash, _quote, delimiter in HEREDOC.findall(line):
            while True:
                if i >= len(lines):
                    return None
                body = lines[i]
                i += 1
                if (body.lstrip("\t") if dash else body) == delimiter:
                    kept.append(body)
                    break
    return "\n".join(kept)


def rewrite(command):
    """The command with its outer stdin from /dev/null, or None to leave it alone."""
    if not isinstance(command, str) or not command.strip():
        return None
    outer = strip_heredoc_bodies(command)
    if outer is None or STDIN_REDIRECT.search(outer):
        return None
    trailing = len(command.rstrip("\n")) - len(command.rstrip("\n").rstrip("\\"))
    if trailing % 2 == 1:
        return None
    return "{ " + command + "\n} < /dev/null"


def main():
    try:
        call = json.load(sys.stdin)
        if not isinstance(call, dict) or call.get("tool_name", "Bash") != "Bash":
            return 0
        tool_input = call.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        command = rewrite(tool_input.get("command"))
        if command is None:
            return 0
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": dict(tool_input, command=command),
            }
        }
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
