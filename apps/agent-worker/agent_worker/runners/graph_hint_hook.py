"""PreToolUse hook for claude-code: the code graph's nudge (SKILL-ON-DEMAND, part of #2688).

Run by the claude-code runner's interpreter as `python -I <this file>
<state>`, from the `--settings` file `claude_code.write_headless_settings`
writes, only on a step whose graph is staged or served in the `skill` shape
(`graphstage.SHAPE_SKILL`). `<state>` is a file beside that settings file,
outside the checkout. Standard library only, and nothing imported from the
worker: `-I` drops the package's path, and a hook must not depend on it.

WHY (the owner's pick of 3 November 2026, "Skill on demand, with a nudge").
Batch one deferred the graph's ten tools behind tool search and no agent
fetched them, 0 calls in 45 steps; batch two loaded them on every turn, about
1,900 tokens, for 2 calls in 12 steps; and subagents, a third to two thirds
of all calls, were never told about the graph. The skill costs about 100
tokens until it is invoked. This hook does the two things a description
cannot:

1. THE NUDGE. When the agent greps (the Grep tool, or `grep`, `rg`, `git
   grep` or `ag` in Bash) for an identifier-shaped name -- `cancel_workflow`,
   `GraphServer`, `def handle_row(` -- the result carries one line saying the
   graph answers who calls it, and the exact call. At most `MAX_HINTS` a
   step, never twice for one name. A plain word, a regex, an all-caps
   constant or a phrase is not a symbol the graph would answer better, and
   gets nothing.
2. THE SUBAGENT LINE. A prompt the agent writes for a subagent (the Task or
   Agent tool) gets `SUBAGENT_LINE` appended, once, through `updatedInput`:
   a subagent shares the session's skills and tools, but nothing told it the
   graph was there.

WARN, NEVER BLOCK. `hookSpecificOutput.additionalContext` or
`updatedInput`, exit 0, no permission decision: the call runs as asked.

FAIL OPEN. Input it cannot read, a state file it cannot write: exit 0,
nothing written, and the call runs unchanged.
"""

import json
import os
import re
import shlex
import sys

#: The most hints one step gets: a nudge, not a tax on every search.
MAX_HINTS = 3
#: The MCP name of the skill's one tool (server `swarm-graph`, tool `slice`).
SLICE_TOOL = "mcp__swarm-graph__slice"
SKILL = "code-graph"
SUBAGENT_LINE = (
    "Code graph: this repository's code graph is available. Invoke the "
    f"`{SKILL}` skill (or call `{SLICE_TOOL}`) to see who calls a function, "
    "which tests cover a file and what a change touches, before grepping for "
    "a symbol's callers."
)
HINT = (
    "Code graph: `{name}` looks like a symbol. The `" + SKILL + "` skill answers "
    "who calls it, through imports and methods that grep misses, in one call: "
    + SLICE_TOOL + " {{\"symbols\": [\"{name}\"], \"include\": [\"callers\", \"tests\"]}}."
)
SEARCH_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "ag"})
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n()]")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: What may stand around a name in a pattern that still asks for that name.
_PREFIXES = ("async def ", "def ", "class ", "function ", "func ", "fn ")
_WRAPPERS = (r"\b", r"\<", r"\>", "^", "$", r"\(", "(", ":", r"\.")


def symbol_in(pattern):
    """The identifier `pattern` searches for, or None when it is not one.

    Identifier-shaped: a whole identifier of four or more characters with an
    underscore inside it or a lower-to-upper case change (snake_case,
    camelCase, CamelCase), and not all capitals. A definition prefix and
    word-boundary or call markers around it are allowed."""
    if not isinstance(pattern, str):
        return None
    text = pattern.strip()
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    changed = True
    while changed:
        changed = False
        for mark in _WRAPPERS:
            if text.startswith(mark):
                text, changed = text[len(mark):], True
            if text.endswith(mark):
                text, changed = text[:-len(mark)], True
    if len(text) < 4 or not _IDENTIFIER.fullmatch(text) or text.upper() == text:
        return None
    inner = text.strip("_")
    if "_" in inner or re.search(r"[a-z][A-Z]", text):
        return text
    return None


def _pattern_of(words):
    """The pattern argument of one grep-like command's words, or None."""
    args = words[1:]
    if words and words[0] == "git":
        if not args or args[0] != "grep":
            return None
        args = args[1:]
    index = 0
    while index < len(args):
        word = args[index]
        if word in ("-e", "--regexp") and index + 1 < len(args):
            return args[index + 1]
        if word.startswith("--regexp="):
            return word.split("=", 1)[1]
        if word == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if word.startswith("-"):
            index += 1
            continue
        return word
    return None


def searched_symbol(tool_name, tool_input):
    """The identifier a Grep call or a grep-like Bash command searches for."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Grep":
        return symbol_in(tool_input.get("pattern"))
    command = tool_input.get("command")
    if tool_name != "Bash" or not isinstance(command, str):
        return None
    for part in _SEPARATORS.split(command):
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        while words and words[0] in ("sudo", "command", "time", "env"):
            words.pop(0)
        if not words:
            continue
        head = os.path.basename(words[0])
        if head in SEARCH_COMMANDS or head == "git":
            found = symbol_in(_pattern_of([head, *words[1:]]))
            if found:
                return found
    return None


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as handle:
            names = json.load(handle).get("hinted")
    except Exception:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _write_state(path, names):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"hinted": names}, handle)
    os.replace(tmp, path)


def _emit(output):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                        **output}}))
    sys.stdout.flush()


def main():
    try:
        if len(sys.argv) < 2:
            return 0
        state = sys.argv[1]
        call = json.load(sys.stdin)
        if not isinstance(call, dict):
            return 0
        tool_name = call.get("tool_name")
        tool_input = call.get("tool_input")
        if tool_name in SUBAGENT_TOOLS and isinstance(tool_input, dict):
            prompt = tool_input.get("prompt")
            if isinstance(prompt, str) and prompt.strip() and SUBAGENT_LINE not in prompt:
                _emit({"updatedInput": dict(tool_input,
                                            prompt=f"{prompt.rstrip()}\n\n{SUBAGENT_LINE}")})
            return 0
        name = searched_symbol(tool_name, tool_input)
        if name is None:
            return 0
        hinted = _read_state(state)
        if name in hinted or len(hinted) >= MAX_HINTS:
            return 0
        _write_state(state, [*hinted, name])
        _emit({"additionalContext": HINT.format(name=name)})
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
