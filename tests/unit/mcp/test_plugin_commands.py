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

import argparse
import contextlib
import functools
import io
import re
import shlex
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


# ==========================================================================
# What a grant ALLOWS -- matched the way Claude Code matches it
# ==========================================================================
#
# THE DEFECT THIS REPLACED (review of PR #61, 2026-09-25). The test that stood
# here read each `Bash(...)` rule, dropped `uv run`, and failed only when the
# SECOND word was `login`, `logout` or `context`. It caught
# `Bash(uv run sc login:*)` and was blind to the rule that was actually there:
# `Bash(uv run sc:*)`, a prefix rule that allows EVERY `sc` subcommand. Those
# grants were safe while every `sc` subcommand was a read-only view. The day
# `sc login`, `sc logout` and `sc context use` were added under the same
# prefix, the `sc` skill and `/sc` granted a model all three, and the suite
# stayed green. It checked how a rule was WRITTEN, not what it ALLOWS.
#
# So a rule is now compiled the way Claude Code matches it, the commands it
# could allow are generated from the parsers themselves, and each one is RUN
# through the real entry points -- with every handler replaced by a stand-in
# -- to see which handler it reaches. `sc --json login` is `sc login` to
# argparse, and `swarm login` is `sc login` to `cli.main`'s routing; neither
# is visible to a test that looks at words.


def _rule_pattern(rule: str) -> "re.Pattern[str]":
    """A `Bash(...)` specifier, compiled to what Claude Code matches.

    code.claude.com/docs/en/permissions, "Wildcard patterns":
      * a `*` matches any text, including spaces, and a rule with no `*`
        matches one exact command;
      * a trailing ` *` also matches the bare command, but only when it is the
        rule's only wildcard (`Bash(ls *)` matches `ls`, not `lsof`);
      * `:*` at the end is an equivalent spelling of a trailing ` *`.
    `test_a_rule_matches_what_the_documentation_says_it_matches` holds this to
    the documentation's own table, row for row.
    """
    spec = rule[:-2] + " *" if rule.endswith(":*") else rule
    if spec.count("*") == 1 and spec.endswith(" *"):
        return re.compile(re.escape(spec[:-2]) + r"(?: .*)?", re.DOTALL)
    return re.compile(".*".join(re.escape(part) for part in spec.split("*")), re.DOTALL)


@pytest.mark.parametrize(
    "rule,command,matches",
    [
        # The table under "Wildcard patterns", plus the `:*` equivalence.
        ("npm run build", "npm run build", True),
        ("npm run build", "npm run build --watch", False),
        ("npm run *", "npm run build", True),
        ("npm run *", "npm run test --watch", True),
        ("npm run *", "npm run", True),
        ("npm run *", "npm install", False),
        ("git log * main", "git log --oneline main", True),
        ("git log * main", "git log main", False),
        ("git log * main", "git push origin main", False),
        ("git * main", "git push origin main", True),
        ("git * main", "git log", False),
        ("* --version", "node --version", True),
        ("* --version", "node -v", False),
        ("ls *", "ls -la", True),
        ("ls *", "ls", True),
        ("ls *", "lsof", False),
        ("ls*", "lsof", True),
        ("* --help *", "npm --help x", True),
        ("* --help *", "npm --help", False),
        ("ls:*", "ls", True),
        ("ls:*", "ls -la", True),
        ("ls:*", "lsof", False),
    ],
)
def test_a_rule_matches_what_the_documentation_says_it_matches(rule, command, matches):
    """The matcher is the premise of every grant test below, so it is held to
    Claude Code's own examples. A matcher that matched nothing would make the
    no-grant tests pass on any skill at all."""
    assert bool(_rule_pattern(rule).fullmatch(command)) is matches


class _Reached(BaseException):
    """Raised by a stand-in handler, carrying which handler a line would RUN.

    A BaseException so that neither CLI's `except SwarmError` nor anything
    catching `Exception` can swallow it on the way out.
    """


class _NoClient:
    """SwarmClient, for a line that is parsed and never sent anywhere."""

    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self) -> "_NoClient":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


@functools.lru_cache(maxsize=None)
def _reached(command: str) -> str | None:
    """The handler a shell command would run -- e.g. `sc.cmd_login` -- or None.

    The command goes through `sc.main` or `cli.main` exactly as a shell would
    hand it over, with every `cmd_*` handler replaced by a stand-in that
    reports its own name. `cli.cmd_sc` is left alone: it is routing, not a
    command -- it hands the line to `sc`. None means argparse refused the line.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    start = next((i for i, word in enumerate(words) if word in _PARSERS), None)
    if start is None:
        return None
    program, argv = words[start], words[start + 1:]

    def _stand_in(name: str):
        def _handler(*_a, **_k):
            raise _Reached(name)

        return _handler

    with pytest.MonkeyPatch.context() as patch:
        for module in (sc, cli):
            short = module.__name__.rsplit(".", 1)[-1]
            for name, value in list(vars(module).items()):
                if name.startswith("cmd_") and callable(value) and (module, name) != (cli, "cmd_sc"):
                    patch.setattr(module, name, _stand_in(f"{short}.{name}"))
        patch.setattr(sc, "SwarmClient", _NoClient)
        patch.setattr(cli, "SwarmClient", _NoClient)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                (sc.main if program == "sc" else cli.main)(argv)
        except _Reached as reached:
            return str(reached)
        except SystemExit:
            return None
    return None


def _leaves(parser: argparse.ArgumentParser) -> list[list[str]]:
    """One minimal argv per command a parser can reach, read off the parser.

    Positionals get a placeholder, required options get a flag and a
    placeholder, and every subparser is walked. Derived rather than listed:
    a subcommand added later is probed the day it is added.
    """
    required: list[str] = []
    branches: list[list[str]] = []
    optional_branch = True
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API for this
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            optional_branch = not action.required
            for name, child in action.choices.items():
                branches += [[name] + rest for rest in _leaves(child)]
        elif not action.option_strings:
            if action.nargs in (None, "+"):
                required.append("x")
            elif isinstance(action.nargs, int):
                required += ["x"] * action.nargs
        elif action.required:
            required += [action.option_strings[0], "x"]
    if not branches:
        return [required]
    return [required + branch for branch in branches] + ([required] if optional_branch else [])


#: What a model must never be GRANTED, named by the handler that would run.
#: `sc login` opens a browser and blocks for up to five minutes; `sc logout`
#: revokes the developer's sign-in; `sc context use prod` moves every later
#: dispatch to another cluster, and `sc context remove` deletes a context's
#: refresh token and client secret. The read-only skill may still TELL the
#: developer to run them -- it may not be allowed to run them itself.
_DEVELOPER_STATE = frozenset(
    {
        "sc.cmd_login",
        "sc.cmd_logout",
        "sc.cmd_context_add",
        "sc.cmd_context_use",
        "sc.cmd_context_remove",
    }
)

#: The surfaces that promise to be read-only: the `sc` skill and `/sc`.
_READ_ONLY_SURFACES = (
    _PLUGIN / "skills" / "sc" / "SKILL.md",
    _PLUGIN / "commands" / "sc.md",
)


def _writing_handlers() -> frozenset[str]:
    """The handlers behind `_WRITING_SUBCOMMANDS`, found by running them."""
    found: dict[str, str | None] = {}
    for leaf in _leaves(_PARSERS["swarm"]):
        if leaf and leaf[0] in _WRITING_SUBCOMMANDS:
            found[leaf[0]] = _reached(" ".join(["swarm"] + leaf))
    missing = sorted(set(_WRITING_SUBCOMMANDS) - {k for k, v in found.items() if v})
    assert not missing, f"no runnable probe for {missing}; the write check would be vacuous"
    return frozenset(v for v in found.values() if v)


def _probes(forbidden: frozenset[str]) -> list[str]:
    """Every argv tail, from either parser, that reaches a forbidden handler."""
    tails: list[str] = []
    for program, parser in _PARSERS.items():
        for leaf in _leaves(parser):
            if _reached(" ".join([program] + leaf)) in forbidden:
                tails.append(" ".join(leaf))
    return tails


def _granted_rules(path: Path) -> list[str]:
    """The `Bash(...)` rules in a file's `allowed-tools`: the grant, and only it.

    Prose may say anything; only the frontmatter grants. Both spellings of the
    list are read -- one rule per `- ` line, or several on the key's own line.
    """
    text = path.read_text()
    if not text.startswith("---"):
        return []
    front = text.split("---", 2)[1]
    block = re.search(r"^allowed-tools:(.*?)(?=^\S|\Z)", front, re.MULTILINE | re.DOTALL)
    return _BASH_RULE.findall(block.group(1)) if block else []


def _allowed(rule: str, forbidden: frozenset[str]) -> list[str]:
    """Commands this rule lets a model run that reach a forbidden handler.

    Two sources of candidates, because a rule can be wrong two ways:
      * the canonical spellings of every forbidden command -- catches
        `Bash(uv run sc:*)`, `Bash(sc:*)` and `Bash(uv run:*)`;
      * the rule's own literal text, extended by each forbidden tail, with and
        without a placeholder value in between -- catches a grant that is
        narrow in words but not in effect, such as `Bash(uv run sc --json:*)`
        (`sc --json login` is `sc login`) or `Bash(uv run sc --context *)`.
    """
    pattern = _rule_pattern(rule)
    spec = rule[:-2] + " *" if rule.endswith(":*") else rule
    tails = _probes(forbidden)
    candidates: set[str] = set()
    if "*" in spec:
        literal = spec.split("*", 1)[0]
        for tail in tails:
            for filler in ("", "x "):
                candidates.add(literal + filler + tail)
                candidates.add(literal.rstrip() + " " + filler + tail)
    else:
        candidates.add(spec)
    for program in _PARSERS:
        for launcher in (f"uv run {program}", program):
            candidates.update(f"{launcher} {tail}" for tail in tails)
    return sorted(c for c in candidates if pattern.fullmatch(c) and _reached(c) in forbidden)


def test_the_probes_reach_every_handler_they_stand_for():
    """A probe that parsed to nothing would make every grant test below pass on
    any rule at all -- the empty sweep reported as a clean one."""
    reached = {_reached(f"sc {tail}") for tail in _probes(_DEVELOPER_STATE)}
    assert reached >= _DEVELOPER_STATE, sorted(_DEVELOPER_STATE - reached)
    # And the routing half: `swarm login` is `sc login`.
    assert _reached("uv run swarm login") == "sc.cmd_login"
    assert _reached("uv run sc --json login") == "sc.cmd_login"
    assert _reached("uv run sc accounts --json") == "sc.cmd_accounts"


@pytest.mark.parametrize("path", _MARKDOWN, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_grant_lets_a_model_change_the_developers_sign_in_or_cluster(path):
    """THE DEFECT: `Bash(uv run sc:*)` in the `sc` skill and in `/sc` allowed
    `uv run sc login`, `uv run sc logout` and `uv run sc context use prod`.

    THE MUTATIONS THIS CATCHES: put `Bash(uv run sc:*)` or `Bash(sc:*)` back,
    or grant a view with a flag before it (`Bash(uv run sc --json:*)`), or add
    `Bash(uv run swarm login:*)`. Each allows a handler in `_DEVELOPER_STATE`.
    """
    offences = [
        f"Bash({rule}) allows `{command}`"
        for rule in _granted_rules(path)
        for command in _allowed(rule, _DEVELOPER_STATE)
    ]
    assert not offences, (
        f"{path} lets a model change the developer's own sign-in or which cluster "
        f"every later call reaches: {offences}"
    )


@pytest.mark.parametrize("path", _READ_ONLY_SURFACES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_the_read_only_surfaces_grant_nothing_that_writes(path):
    """The same blindness, one test up: `test_the_read_only_skill_never_gains_a_command_that_writes`
    reads the SECOND word too, so `Bash(uv run swarm:*)` in the `sc` skill
    would grant `swarm dispatch` and pass it. This asks what each rule allows."""
    forbidden = _writing_handlers() | _DEVELOPER_STATE
    offences = [
        f"Bash({rule}) allows `{command}`"
        for rule in _granted_rules(path)
        for command in _allowed(rule, forbidden)
    ]
    assert not offences, f"{path} promises to be read-only and grants: {offences}"


@pytest.mark.parametrize("path", _READ_ONLY_SURFACES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_read_only_command_the_surface_tells_a_model_to_run_is_granted(path):
    """The other half. Without it, the tests above pass on a skill that grants
    nothing -- and a model told to run `uv run sc accounts` then stops at a
    permission prompt for a view that is always safe."""
    rules = [_rule_pattern(rule) for rule in _granted_rules(path)]
    assert rules, f"{path} grants no Bash rule at all"
    checked, ungranted = 0, []
    for command in sorted({m for m in _BACKTICKED_COMMAND.findall(path.read_text()) if m.startswith("uv run ")}):
        if "$" in command:
            continue  # `$ARGUMENTS` is the operator's, not a command
        runnable = re.sub(r"<[^>]*>", "x", command)
        handler = _reached(runnable)
        if handler is None or handler in _DEVELOPER_STATE:
            continue  # a sign-in command is told, never granted
        checked += 1
        if not any(rule.fullmatch(runnable) for rule in rules):
            ungranted.append(command)
    assert checked, f"found no read-only command in {path}; the scan ran over nothing"
    assert not ungranted, f"{path} tells a model to run these and does not grant them: {ungranted}"


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
