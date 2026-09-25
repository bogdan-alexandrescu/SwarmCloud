"""The SHELL half of the plugin surface, which nothing checked.

`test_plugin_skills.py` checks every MCP TOOL a skill names against
`server.TOOLS`, and it is thorough. It has no opinion at all about the other
kind of thing a skill tells a model to do, which is run a command --
`Bash(uv run sc:*)`, `Bash(uv run swarm doctor:*)`, `uv run swarm tail <id>`.
That half fails in exactly the way the tool half was protected from:

  * A SUBCOMMAND THAT DOES NOT EXIST. argparse answers "invalid choice" on
    stderr and exits 2. The model reads a non-zero exit and an error it did not
    expect, and reports the platform as broken rather than the skill as wrong.
  * A BINARY THAT IS NOT ON PATH. This is the one that was actually live.
    `swarm` and `sc` are console scripts of `swarm-mcp`, installed into the
    uv-managed environment and never onto the shell's, so a fresh checkout
    answers `command not found: swarm`. Three places in the bridge handed a
    model the string `swarm tail <id>` as `follow_live_with` -- a field whose
    entire purpose is to be runnable -- and one of them was the reply to
    `swarm_dispatch`, so the failure landed at the exact moment a session was
    trying to show progress on work it had just started.

    `test_follow_cursor.py::test_the_tool_is_registered_and_its_schema_names_the_cursor`
    already asserts that no tool DESCRIPTION says `swarm tail`. It stopped one
    field short of the place the model actually reads, which is the response
    body.

Offline and dependency-free: the parsers are built, never run, and the
frontmatter is parsed by hand for the reason `test_plugin_skills` gives -- PyYAML
is here only as a transitive dependency and could leave without notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_mcp import cli, sc

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"

#: Every markdown file in the plugin that can tell someone to run something:
#: the skills and the slash commands both carry `allowed-tools` and prose, and
#: the README is included because a command that does not exist costs a human
#: reader the same half hour it costs a model.
_MARKDOWN = (
    sorted(_PLUGIN.glob("skills/*/SKILL.md"))
    + sorted(_PLUGIN.glob("commands/*.md"))
    + [_PLUGIN / "README.md"]
)

#: `Bash(uv run swarm doctor:*)` -> the command words inside the parentheses,
#: with the trailing argument pattern dropped.
_BASH_RULE = re.compile(r"Bash\(([^)]*)\)")

#: A backticked command in the prose that starts with `uv run sc`, `uv run
#: swarm`, `sc` or `swarm`. Captured whole so the subcommand can be read off it.
_BACKTICKED_COMMAND = re.compile(r"`((?:uv run )?(?:swarm|sc)(?: [^`\n]*)?)`")

#: The two argparse surfaces the plugin is allowed to reach for.
_PARSERS = {"swarm": cli.build_parser(), "sc": sc.build_parser()}


def _subcommands(parser) -> set[str]:
    """Every subcommand name a parser accepts, aliases included.

    Read off the parser rather than off a list kept here: a list would be the
    third statement of what `swarm` can do, and the one nobody updates.
    """
    names: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API for this
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            names |= set(choices)
    return names


def _commands_named(text: str) -> set[tuple[str, ...]]:
    """Command word-tuples this text tells a model to run, `uv run` stripped."""
    found: set[tuple[str, ...]] = set()
    for rule in _BASH_RULE.findall(text):
        # `Bash(uv run sc:*)` -- the `:*` is the argument pattern, not a word.
        words = rule.split(":")[0].split()
        if words:
            found.add(tuple(words))
    for span in _BACKTICKED_COMMAND.findall(text):
        found.add(tuple(span.split()))
    return {
        words[2:] if words[:2] == ("uv", "run") else words
        for words in found
    }


def test_there_are_markdown_files_to_check():
    """A glob that matched nothing is how this file would pass hardest at the
    moment the plugin directory moved out from under it -- the same trap
    `test_plugin_skills` guards, for the same reason."""
    assert _MARKDOWN, f"no skill or command markdown under {_PLUGIN}"


@pytest.mark.parametrize("path", _MARKDOWN, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_command_the_plugin_names_is_one_that_exists(path):
    """A subcommand named in `allowed-tools` or in the prose must be real.

    The failure this prevents is not a refused permission -- `allowed-tools` is
    matched, not resolved, so a wrong name grants nothing silently. It is the
    prose: a model that reads `uv run swarm logs <id>` runs it, argparse exits 2
    with "invalid choice: 'logs'", and the session reports SwarmCloud as broken.
    """
    text = path.read_text()
    problems: list[str] = []
    for words in sorted(_commands_named(text)):
        if not words or words[0] not in _PARSERS:
            continue
        if len(words) < 2:
            continue  # bare `sc` is the overview; bare `swarm` prints help
        subcommand = words[1]
        # `$ARGUMENTS`, `<id>` and the like are placeholders, not subcommands.
        if subcommand.startswith(("$", "<", "-", "[")):
            continue
        real = _subcommands(_PARSERS[words[0]])
        if subcommand not in real:
            problems.append(
                f"`{' '.join(words)}` -- {words[0]} has no {subcommand!r} "
                f"subcommand; it has {sorted(real)}"
            )
    assert not problems, f"{path} names commands that do not exist: {problems}"


def test_nothing_the_bridge_hands_back_tells_a_model_to_run_a_bare_swarm():
    """The live defect, guarded at the place it actually lived.

    `swarm` and `sc` are not on anyone's PATH -- they are console scripts of
    this package, installed into the uv environment. Any command string the
    bridge RETURNS is a string a model will run verbatim, so it has to carry the
    `uv run` prefix that works in a fresh checkout.

    THE MUTATION THIS CATCHES: change `follow.TAIL_COMMAND` back to
    `"swarm tail"`, or rebuild the string inline in `workflows.report` the way
    that function used to, and this goes red. Reverting it in only one of the
    three original places goes red too, because every producer is checked.
    """
    from swarm_mcp import follow, server, workflows

    assert follow.TAIL_COMMAND.startswith("uv run "), (
        "the tailer is spelled for a shell that has `swarm` on its PATH, and no "
        "shell does -- it is a console script in the uv environment"
    )

    command = follow.follow_command(["task_a", "task_b"])
    assert command == "uv run swarm tail task_a task_b"

    # Nothing to follow must produce NOTHING, not a bare command. `swarm tail `
    # with the ids missing still looks runnable, which is worse than absent.
    assert follow.follow_command([]) == ""
    assert follow.follow_command([None, ""]) == ""

    # Every module that builds one of these strings goes through that function.
    # Checked by source inspection because the alternative is a live client.
    for module in (server, workflows):
        source = Path(module.__file__).read_text()
        offenders = [
            line.strip()
            for line in source.split("\n")
            if '"swarm tail' in line or "'swarm tail" in line
        ]
        assert not offenders, (
            f"{module.__name__} builds a tail command itself instead of calling "
            f"follow.follow_command: {offenders}"
        )


def test_the_dispatch_reply_points_at_the_tool_before_the_terminal():
    """A model cannot run a background shell; it can call `swarm_follow`.

    The dispatch reply used to offer only the terminal command, which is how a
    session holding a perfectly good cursor tool went and ran a binary that does
    not exist. Both are offered now, and the tool is named.
    """
    from swarm_mcp import server

    tool = next(t for t in server.TOOLS if t["name"] == "swarm_follow")
    assert tool, "swarm_follow must exist for the dispatch reply to point at it"

    source = Path(server.__file__).read_text()
    dispatch = source.split('if name == "swarm_dispatch":', 1)[1].split("if name ==", 1)[0]
    assert '"follow_with": "swarm_follow"' in dispatch, (
        "the dispatch reply must name the tool a model can actually call"
    )


#: `swarm` subcommands that change something -- the control plane, or the
#: operator's own files. Named rather than derived because "does this write" is
#: not a property argparse knows, and a guess in either direction here is worse
#: than a list somebody has to update: too narrow and the read-only skill gains
#: a write, too broad and a harmless view gets refused.
_WRITING_SUBCOMMANDS = frozenset(
    {
        "dispatch",        # spends the shared pool
        "apply",           # writes into the operator's working tree
        "integrate",       # moves the operator's branch
        "cancel",          # stops running work
        "workflow",        # spends the shared pool, N steps at a time
        "workflow-cancel",
        "init",            # writes .env
    }
)


def test_the_read_only_skill_never_gains_a_command_that_writes():
    """`sc`'s own text promises it "never writes, never refreshes a credential
    and never cancels anything, so it is always safe to run".

    `test_plugin_skills` already checks that promise for MCP tools. It cannot
    see the Bash rules, and the `sc` skill carries three of them -- so the
    cheapest way to break the promise is to add a fourth. A permission is the
    right place to enforce a safety claim, so both halves are checked.

    THE MUTATION THIS CATCHES: add `Bash(uv run swarm dispatch:*)` to
    `plugin/skills/sc/SKILL.md` and this goes red, while every other test in the
    suite stays green.
    """
    text = (_PLUGIN / "skills" / "sc" / "SKILL.md").read_text()
    granted = _commands_named(text)
    writes = sorted(
        " ".join(words)
        for words in granted
        if words and words[0] in _PARSERS and len(words) > 1
        and words[1] in _WRITING_SUBCOMMANDS
    )
    assert not writes, (
        f"the read-only skill is allowed to run commands that write: {writes}"
    )


#: `sc` subcommands that write the DEVELOPER's local state rather than the
#: cluster: the credential store (login, logout) and the config file that
#: decides which deployment every later call reaches (context). They do not
#: belong in `_WRITING_SUBCOMMANDS` -- the read-only skill should still be able
#: to TELL the developer to run `sc login` -- but a model must never be GRANTED
#: them: `sc login` opens a browser and blocks for up to five minutes, and
#: `sc context use` silently moves every later dispatch to another cluster.
_LOCAL_STATE_SUBCOMMANDS = frozenset({"login", "logout", "context"})


@pytest.mark.parametrize("path", _MARKDOWN, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_skill_or_command_grants_a_model_the_developers_sign_in(path):
    """THE MUTATION THIS CATCHES: add `Bash(uv run sc login:*)` to any skill's
    `allowed-tools`. Naming the command in prose stays allowed; granting it
    does not. The grant is the `Bash(...)` rule, so only those are read."""
    text = path.read_text()
    granted = []
    for rule in _BASH_RULE.findall(text):
        words = rule.split(":")[0].split()
        words = words[2:] if words[:2] == ["uv", "run"] else words
        if words and words[0] in _PARSERS and len(words) > 1 and words[1] in _LOCAL_STATE_SUBCOMMANDS:
            granted.append(rule)
    assert not granted, f"{path} grants a model the developer's own sign-in: {granted}"


def test_the_sign_in_commands_exist_on_both_spellings():
    """`sc login` is what a tool tells a developer to run when they are not
    signed in, so it has to be real on `sc` -- and `swarm login` has to mean
    the same thing, the way `swarm accounts` means `sc accounts`."""
    for name in ("login", "logout", "whoami", "context"):
        assert name in _subcommands(_PARSERS["sc"]), f"sc has no {name!r}"
        assert name in _subcommands(_PARSERS["swarm"]), f"swarm has no {name!r}"


def test_the_plugin_is_installable_at_all():
    """WITHOUT THIS FILE NONE OF THE REST OF THE PLUGIN IS REACHABLE.

    A Claude Code plugin is installed from a marketplace, and a marketplace is
    `.claude-plugin/marketplace.json` at the root of the repository being used
    as one. It did not exist. Every skill, every tool permission and the whole
    of `/sc` was therefore correct, tested, and impossible to install -- the
    plugin README even said so, in a paragraph explaining that the file belonged
    to another track and had not landed.

    This asserts the manifest exists, is valid JSON, and points at a directory
    that really holds a plugin manifest -- a `source` naming a path with no
    `plugin.json` in it installs nothing and says little.
    """
    import json

    manifest_path = _REPO / ".claude-plugin" / "marketplace.json"
    assert manifest_path.exists(), (
        "there is no .claude-plugin/marketplace.json, so `sc` cannot be "
        "installed into a session however correct the skills are"
    )
    manifest = json.loads(manifest_path.read_text())

    assert manifest.get("name"), "a marketplace needs a name"
    assert manifest.get("owner"), "a marketplace needs an owner"
    entries = manifest.get("plugins") or []
    assert entries, "a marketplace with no plugins installs nothing"

    by_name = {entry.get("name"): entry for entry in entries}
    assert "sc" in by_name, f"the `sc` plugin is not listed: {sorted(by_name)}"

    source = by_name["sc"].get("source")
    assert source, "the `sc` entry names no source directory"
    target = (_REPO / source).resolve()
    assert target == _PLUGIN.resolve(), (
        f"the marketplace points at {target}, which is not the plugin directory"
    )
    assert (target / ".claude-plugin" / "plugin.json").exists(), (
        "the source directory holds no plugin.json, so installing it yields "
        "nothing and says little about why"
    )


def test_the_marketplace_and_the_plugin_agree_on_the_name():
    """Two manifests naming one plugin, so they are checked against each other.

    A marketplace entry named `sc` pointing at a plugin whose own manifest says
    `swarmcloud` installs under one name and is referred to in the skills under
    the other, and the mismatch surfaces as a slash command that does not exist.
    """
    import json

    marketplace = json.loads(
        (_REPO / ".claude-plugin" / "marketplace.json").read_text()
    )
    plugin = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    listed = {entry.get("name") for entry in marketplace.get("plugins") or []}
    assert plugin["name"] in listed, (
        f"plugin.json calls this {plugin['name']!r}; the marketplace lists "
        f"{sorted(listed)}"
    )
