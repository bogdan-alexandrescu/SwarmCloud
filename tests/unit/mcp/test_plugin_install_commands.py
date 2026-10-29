"""`/sc:install`, `/sc:doctor` and `/sc:team` are a face over the engine's
`--json-events`, and never see a value (docs/design/day-one-install.md §2.5;
plan lane DAYONE-SC-INSTALL-1).

The three commands drive the installer's verbs on the `swarm` command
(`apps/swarm-mcp/swarm_mcp/cli.py`, `INSTALL_VERBS`), which the release home's
`install.sh` puts on the admin's PATH. What a model may run is the
`allowed-tools` grant; what it is told to run is the prose. Both are read here:

  * THE MACHINE FORM ONLY. A run of the engine is `--json-events` (one JSON
    object per line, `swarm_install.events`) and a status is `--status --json`.
    The human view is for a terminal; a model that parses it guesses.
  * NO VALUE IN ANY PROMPT OR TOOL SCHEMA. The grant holds no MCP tool (so no
    tool schema at all), nothing that reads a file, stdin or Secret Manager,
    and the argument hint asks for nothing secret-shaped.
  * A REFUSAL IS HANDED ON, NEVER WORKED AROUND (lesson 47). When Claude Code's
    auto mode refuses the command, the text says to hand the person the exact
    `!` line or ask for a narrow allow rule, and offers no other route.
  * A SECRET STEP IS THE PERSON'S (review finding 11). On a `step_paused`
    event whose reason is `needs_terminal`, the text hands the person the
    engine's own resume line and never asks for, reads or pipes the value.

Offline: the markdown is parsed by hand, and `swarm_install` is imported from
its source directory the way `tests/unit/install/__init__.py` does, because
apps/swarm-install is not yet a member of the root uv workspace.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SOURCE = str(_REPO / "apps" / "swarm-install")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)

from swarm_install import secret_entry  # noqa: E402

from swarm_mcp import cli  # noqa: E402

_COMMANDS = _REPO / "plugin" / "commands"

#: The three faces this lane adds, by the slash command each one is.
_FACES = {
    "install": _COMMANDS / "install.md",
    "doctor": _COMMANDS / "doctor.md",
    "team": _COMMANDS / "team.md",
}

_BASH_RULE = re.compile(r"Bash\(([^)]*)\)")

#: A backticked `swarm ...` command in the prose (the `!` line is matched
#: separately: it is the person's to type, not the model's to run).
_BACKTICKED_SWARM = re.compile(r"`(swarm(?: [^`\n]*)?)`")

#: Words that, in a grant or an argument hint, mean a value could pass through.
_VALUE_WORDS = re.compile(
    r"secret|token|password|passphrase|private.?key|api.?key|credential|--stdin|"
    r"GOCSPX|PAT\b",
    re.IGNORECASE,
)

#: Spellings that are a way round a refused command, rather than a hand-off.
_WORKAROUNDS = (
    "dangerously",
    "skip-permissions",
    "bypassPermissions",
    "SWARM_ASSUME_YES",
    "bash -c",
    "sh -c",
    "python -c",
    "eval ",
    "sudo ",
    "--yes",
    "nohup",
    "| sh",
    "| bash",
)


def _read(face: str) -> str:
    path = _FACES[face]
    assert path.is_file(), f"{path} does not exist: /sc:{face} has no command file"
    return path.read_text()


def _split(text: str) -> tuple[str, str]:
    """(frontmatter, body). Strict: the file opens with `---` and closes it."""
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "frontmatter must open with ---"
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise AssertionError("frontmatter opened with --- and never closed")


def _allowed_tools(front: str) -> list[str]:
    block = re.search(r"^allowed-tools:(.*?)(?=^\S|\Z)", front, re.MULTILINE | re.DOTALL)
    assert block, "no allowed-tools in the frontmatter"
    return re.findall(r"^\s*-\s*(\S.*?)\s*$", block.group(1), re.MULTILINE)


def _field(front: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.*)$", front, re.MULTILINE)
    return match.group(1).strip().strip('"') if match else ""


def _grants(face: str) -> list[str]:
    front, _ = _split(_read(face))
    return _BASH_RULE.findall("\n".join(_allowed_tools(front)))


def _rule_pattern(rule: str) -> re.Pattern[str]:
    """A `Bash(...)` rule compiled the way Claude Code matches it (the same
    reading as `test_plugin_commands._rule_pattern`)."""
    spec = rule[:-2] + " *" if rule.endswith(":*") else rule
    if spec.count("*") == 1 and spec.endswith(" *"):
        return re.compile(re.escape(spec[:-2]) + r"(?: .*)?", re.DOTALL)
    return re.compile(".*".join(re.escape(part) for part in spec.split("*")), re.DOTALL)


def _is_machine_form(words: list[str]) -> bool:
    """An installer invocation that speaks JSON: `--json-events`, or a status
    read as `--status --json`. `swarm doctor`'s client tier takes no flags and
    prints no value, so it is the one non-installer verb allowed bare."""
    if words[:2] == ["swarm", "doctor"]:
        return words == ["swarm", "doctor"]
    if len(words) < 2 or words[1] not in cli.INSTALL_VERBS:
        return False
    if "--json-events" in words:
        return True
    return words[1] == "install" and "--status" in words and "--json" in words


def test_commands_run_json_events_only():
    """Every `swarm` command a face grants or tells a model to run is the
    engine's machine form, and every one it tells a model to run is granted.

    THE MUTATIONS THIS CATCHES: grant `Bash(swarm install:*)` (the human view,
    and a grant that does not pin the machine form), spell `swarm install
    --project <P>` in the prose without `--json-events`, or grant a tool other
    than `swarm` (`Bash(gcloud:*)`) for the model to do a step by hand.
    """
    checked = 0
    for face in _FACES:
        text = _read(face)
        grants = _grants(face)
        assert grants, f"/sc:{face} grants no Bash rule: it cannot run the engine"
        for rule in grants:
            literal = rule[:-2] if rule.endswith(":*") else rule
            words = literal.split()
            assert words[:1] == ["swarm"], f"/sc:{face} grants Bash({rule}), which is not `swarm`"
            assert "*" not in literal, f"/sc:{face} grants Bash({rule}), a wildcard mid-rule"
            assert _is_machine_form(words), (
                f"/sc:{face} grants Bash({rule}), which does not pin --json-events "
                "(or --status --json)"
            )
        patterns = [_rule_pattern(rule) for rule in grants]
        for command in sorted(set(_BACKTICKED_SWARM.findall(text))):
            words = command.split()
            assert _is_machine_form(words), (
                f"/sc:{face} tells a model to run `{command}`, which is not the engine's "
                "machine form"
            )
            runnable = re.sub(r"<[^>]*>", "x", command)
            assert any(p.fullmatch(runnable) for p in patterns), (
                f"/sc:{face} tells a model to run `{command}` and does not grant it"
            )
            checked += 1
    assert checked >= len(_FACES), f"found only {checked} commands across the faces; the scan ran over nothing"


def test_no_value_in_any_prompt_or_tool_schema():
    """No face can carry a value: no MCP tool (no schema), no file or stdin
    reader, nothing secret-shaped in a grant or the argument hint, and the
    body says the value is never Claude's.

    THE MUTATIONS THIS CATCHES: add `Read` or an `mcp__...` tool to a grant,
    grant `Bash(swarm install --json-events --stdin:*)`, or put `<token>` in the
    argument hint.
    """
    for face in _FACES:
        front, body = _split(_read(face))
        tools = _allowed_tools(front)
        others = [t for t in tools if not t.startswith("Bash(")]
        assert not others, f"/sc:{face} grants tools other than Bash rules: {others}"
        for rule in _BASH_RULE.findall("\n".join(tools)):
            assert not _VALUE_WORDS.search(rule), f"/sc:{face} grants Bash({rule}), which can carry a value"
            assert not re.search(r"[|<>;&`$]", rule), f"/sc:{face} grants Bash({rule}), a pipe or redirect"
        hint = _field(front, "argument-hint")
        assert not _VALUE_WORDS.search(hint), f"/sc:{face}'s argument hint asks for a value: {hint!r}"
        assert _field(front, "description"), f"/sc:{face} has no description"
        assert "never" in body and "value" in body, (
            f"/sc:{face} does not say that a value is never Claude's to see"
        )
        for bad in ("--stdin", "create-secrets", "gcloud secrets", "versions access"):
            assert bad not in body, f"/sc:{face} names {bad!r}, a route a value takes"


@pytest.mark.parametrize("face", sorted(_FACES))
def test_refusal_hands_exact_command(face):
    """Lesson 47: auto mode refused IAM and deletion commands, despite verbal
    pre-approval. On a refusal the face hands the person the exact `!` line or
    asks for a narrow allow rule, and offers no workaround.

    THE MUTATIONS THIS CATCHES: drop the refusal paragraph, suggest a broad rule
    (`Bash(swarm:*)` or `Bash(*)`), or add any way round (`--dangerously-skip-
    permissions`, `SWARM_ASSUME_YES`, `bash -c`, running the steps by hand).
    """
    text = _read(face)
    _, body = _split(text)
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if re.search(r"refus", p, re.IGNORECASE)]
    refusal = [p for p in paragraphs if "auto mode" in p.lower() or "permission" in p.lower()]
    assert refusal, f"/sc:{face} says nothing about a refused permission"
    said = "\n".join(refusal)
    assert re.search(r"`! swarm [^`]+`", said), f"/sc:{face} does not hand the person the `!` line on a refusal"
    suggested = _BASH_RULE.findall(said)
    assert suggested, f"/sc:{face} does not name the narrow allow rule to ask for"
    grants = set(_grants(face))
    for rule in suggested:
        assert rule in grants, f"/sc:{face} suggests Bash({rule}), which is not one of its own narrow grants"
    assert re.search(r"never .*(work around|retry|another spelling)", said, re.IGNORECASE | re.DOTALL), (
        f"/sc:{face} does not forbid working around the refusal"
    )
    for bad in _WORKAROUNDS:
        assert bad not in text, f"/sc:{face} holds a workaround: {bad!r}"
    for rule in grants:
        assert rule.split()[0] == "swarm" and rule not in ("swarm:*", "swarm *", "*"), rule


def test_secret_step_hands_bang_command():
    """Review finding 11: under `/sc:install` the engine's stdin is a pipe Claude
    writes to, so the engine pauses a secret step with `step_paused`,
    `reason: needs_terminal` and `command: ! swarm install --resume`
    (`swarm_install.secret_entry`). The faces that run the engine hand that line
    to the person verbatim and never ask for, read or pipe the value.

    THE MUTATION THIS CATCHES: tell Claude to "ask the person for the value and
    pass it in", or drop the `needs_terminal` paragraph, or hand a different
    line from the one the engine emits.
    """
    for face in ("install", "team"):
        _, body = _split(_read(face))
        paragraphs = [p for p in re.split(r"\n\s*\n", body) if secret_entry.NEEDS_TERMINAL in p]
        assert paragraphs, f"/sc:{face} says nothing about a `{secret_entry.NEEDS_TERMINAL}` pause"
        said = "\n".join(paragraphs)
        assert f"`{secret_entry.RESUME_LINE}`" in said, (
            f"/sc:{face} does not hand the person the engine's line, {secret_entry.RESUME_LINE!r}"
        )
        assert "`command`" in said, f"/sc:{face} does not say the line is the event's `command`"
        for verb in ("ask for", "read", "pipe"):
            assert re.search(rf"never[^.]*\b{verb}\b", said, re.IGNORECASE), (
                f"/sc:{face} does not forbid Claude to {verb} the value"
            )
        assert not re.search(r"(ask|prompt) (the person|them|the admin) for (the|a|its) (value|secret)", body, re.IGNORECASE)
