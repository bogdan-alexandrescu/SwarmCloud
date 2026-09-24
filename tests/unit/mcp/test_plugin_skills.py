"""The plugin's skills, checked against the bridge they describe.

A skill is prose, so nothing at runtime refuses a sentence that has stopped
being true. The two ways that happens here are both mechanical, and both are
checked below rather than proof-read:

* **A skill names a tool that does not exist.** `allowed-tools` is a permission
  rule, not a lookup: a typo or a wished-for name is accepted silently and the
  model simply never gets the tool, which reads as "delegation does not work"
  rather than as a broken manifest.
* **A skill keeps saying a tool does not exist after it lands.** The delegation
  skill tells a session to hand-roll a join because `swarm_workflow` is not
  there. The day that tool ships, that paragraph starts costing the platform
  the exact feature it was built for -- and nothing about shipping a tool would
  otherwise make anyone reopen a markdown file two directories away. So the
  "does not exist yet" list is asserted to still be true, and goes red on the
  commit that makes it false.

Offline and dependency-free by construction: the frontmatter is parsed here by
hand rather than with PyYAML, which is present only as a transitive dependency
of uvicorn and the kubernetes client and could leave without notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_mcp import server

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_SKILLS = sorted(_PLUGIN.glob("skills/*/SKILL.md"))

#: The bridge's own list is the only statement of what exists.
_REAL = {tool["name"] for tool in server.TOOLS}

#: The MCP server id in `.mcp.json`; a permission rule is `mcp__<server>__<tool>`.
_PREFIX = "mcp__swarmcloud__"


def _plugin_manifest() -> dict:
    """`plugin/.claude-plugin/plugin.json`, parsed."""
    import json

    return json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())


def _scoped_prefix() -> str:
    """The prefix a PLUGIN-bundled MCP server's tools arrive under.

    DERIVED FROM THE MANIFEST, never written out, because both halves of it are
    editable: the plugin's `name` and the key under `mcpServers`. A plugin's own
    server is scoped -- `mcp__plugin_<plugin>_<server>__<tool>` -- and the
    plugin name's hyphens become underscores. Renaming either would silently
    ungrant every scoped permission in `delegate`, and "the model never got the
    tool" looks exactly like "delegation does not work".
    """
    manifest = _plugin_manifest()
    servers = manifest.get("mcpServers") or {}
    assert isinstance(servers, dict) and servers, (
        "plugin.json declares no mcpServers, so the `delegate` skill's tools "
        "exist only when a session's own .mcp.json happens to register them -- "
        "which is to say only inside this repository"
    )
    assert list(servers) == ["swarmcloud"], (
        f"expected one server named `swarmcloud`, found {sorted(servers)}"
    )
    plugin_name = str(manifest["name"]).replace("-", "_")
    return f"mcp__plugin_{plugin_name}_swarmcloud__"


#: BOTH spellings of the same bridge. The project server and the plugin-bundled
#: server are different registrations of one process, and a skill has to be
#: granted whichever one the session actually has -- so every check below reads
#: an entry under either prefix rather than under one.
_PREFIXES = (_PREFIX, _scoped_prefix())


def _granted(entries) -> set[str]:
    """The bare tool names an `allowed-tools` list grants, both prefixes."""
    names: set[str] = set()
    for entry in entries:
        for prefix in _PREFIXES:
            if entry.startswith(prefix):
                names.add(entry[len(prefix):])
    return names


def _granted_per_prefix(entries) -> dict[str, set[str]]:
    return {
        prefix: {e[len(prefix):] for e in entries if e.startswith(prefix)}
        for prefix in _PREFIXES
    }

#: A fully backticked bare identifier -- `swarm_dispatch`, but NOT the
#: `swarm_mcp` inside a backticked module path, which is not a tool name.
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"^swarm_[a-z_]+$")

#: The heading the "not yet" list lives under, and the shape of one entry:
#: a top-level bullet whose lead is a bolded, backticked tool name.
_NOT_YET_HEADING = "## Tools that do not exist yet"
_NOT_YET_ENTRY = re.compile(r"^\s*\*\s+\*\*`(swarm_[a-z_]+)`\*\*")

#: Anything that dispatches, cancels, or writes to the operator's tree.
_WRITE_TOOLS = {"swarm_dispatch", "swarm_apply", "swarm_integrate", "swarm_cancel"}


def _split(text: str) -> tuple[list[str], str]:
    """Frontmatter lines and body, or a failure naming what is wrong.

    Deliberately strict. A skill whose frontmatter does not open and close with
    `---` is not loaded by the host at all, and the symptom -- a skill that
    never triggers -- looks nothing like the cause.
    """
    lines = text.split("\n")
    assert lines and lines[0].strip() == "---", "frontmatter must open with ---"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], "\n".join(lines[index + 1 :])
    raise AssertionError("frontmatter opened with --- and never closed")


def _frontmatter(text: str) -> dict[str, object]:
    """The subset of YAML a skill header may use: scalars and `- ` lists.

    Anything else raises rather than being ignored, because a silently dropped
    key here is a permission that was never granted.
    """
    fields: dict[str, object] = {}
    key: str | None = None
    for raw in _split(text)[0]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):
            item = raw.strip()
            assert item.startswith("- "), f"not a list item: {raw!r}"
            assert key is not None, f"list item before any key: {raw!r}"
            value = fields[key]
            assert isinstance(value, list), f"{key} has both a value and items"
            value.append(item[2:].strip())
            continue
        assert ":" in raw, f"not `key: value`: {raw!r}"
        key, _, value_text = raw.partition(":")
        key = key.strip()
        assert key, f"empty key: {raw!r}"
        value_text = value_text.strip()
        # `: ` inside an unquoted scalar makes the line a mapping to a real
        # YAML parser, which is the host's, not this one's. Rejected here so
        # the header that loads in this test is the header that loads there.
        assert ": " not in value_text or value_text[0] in "\"'", (
            f"{key} needs quoting -- an unquoted scalar may not contain ': ': {raw!r}"
        )
        fields[key] = value_text if value_text else []
    return fields


def _load(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    return _frontmatter(text), _split(text)[1]


def _identifiers(body: str) -> set[str]:
    return {
        span
        for span in _BACKTICKED.findall(body)
        if _IDENTIFIER.match(span)
    }


def test_there_are_skills_to_check():
    """`glob` returning nothing is how this file would pass hardest at the
    moment the plugin directory moved out from under it."""
    assert _SKILLS, f"no SKILL.md under {_PLUGIN}/skills"


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_the_frontmatter_parses_and_carries_a_name_and_description(path):
    fields, _ = _load(path)
    assert fields.get("name") == path.parent.name, "name must match the directory"
    description = fields.get("description")
    assert isinstance(description, str) and description.strip()
    assert isinstance(fields.get("allowed-tools"), list)
    assert fields["allowed-tools"], "a skill with no allowed-tools can do nothing"


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_every_tool_a_skill_is_allowed_is_a_tool_the_bridge_serves(path):
    """An `allowed-tools` entry is matched, not resolved. A name that is wrong
    grants nothing, silently, and the model behaves as if the feature is
    missing rather than as if the manifest is."""
    fields, _ = _load(path)
    entries = [
        entry
        for entry in fields["allowed-tools"]
        if any(entry.startswith(prefix) for prefix in _PREFIXES)
    ]
    unknown = sorted(
        entry
        for entry in entries
        if not any(
            entry.startswith(prefix) and entry[len(prefix):] in _REAL
            for prefix in _PREFIXES
        )
    )
    assert not unknown, f"{path.parent.name} allows tools that do not exist: {unknown}"

    # A rule that begins `mcp__` and matches NEITHER registration is a rule that
    # grants nothing at all, and it is the shape a typo takes -- `mcp__swarm__`,
    # `mcp__plugin_swarmcloud__`. Caught here rather than left to be discovered
    # as "the model never called the tool".
    stray = sorted(
        entry
        for entry in fields["allowed-tools"]
        if entry.startswith("mcp__")
        and not any(entry.startswith(prefix) for prefix in _PREFIXES)
    )
    assert not stray, (
        f"{path.parent.name} has MCP rules under neither registration, so they "
        f"grant nothing: {stray}. The two are {list(_PREFIXES)}"
    )


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_every_tool_named_in_the_prose_exists_or_is_marked_as_not_existing(path):
    fields, body = _load(path)
    cited = _identifiers(body) | _identifiers(str(fields.get("description", "")))
    unexplained = sorted(cited - _REAL - _not_yet(body))
    assert not unexplained, (
        f"{path.parent.name} names {unexplained}, which the bridge does not serve. "
        "Either it exists and this list is stale, or say plainly that it does not."
    )


def _not_yet(body: str) -> set[str]:
    """The tools a skill declares as not-yet-built, from its own section."""
    if _NOT_YET_HEADING not in body:
        return set()
    section = body.split(_NOT_YET_HEADING, 1)[1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    return {m.group(1) for line in section.split("\n") if (m := _NOT_YET_ENTRY.match(line))}


def test_the_delegation_skill_claims_nothing_real_is_missing():
    """This replaces a scaffold that did its job and fired exactly once.

    The old test asserted the skill still had a "Tools that do not exist yet"
    section listing `swarm_workflow` and a follow tool, and went red the moment
    either became real -- which is what it was for. Both landed on 2026-09-22,
    it fired, the section was deleted and its substance moved into the sections
    that own the behaviour.

    Keeping the scaffold after that would have required the skill to keep a
    heading that was a lie in order to satisfy a test. So the guarantee is
    inverted and made permanent instead: the skill must never tell a session
    that a tool which DOES exist is missing. That was the expensive failure --
    it made sessions hand-join a fan-out and lose artifact staging -- and it
    stays guarded whether or not a "not yet" section ever exists again.
    """
    path = _PLUGIN / "skills" / "delegate" / "SKILL.md"
    _, body = _load(path)

    declared_missing = _not_yet(body)
    wrongly_missing = sorted(declared_missing & _REAL)
    assert not wrongly_missing, (
        f"{wrongly_missing} exist in swarm_mcp.server.TOOLS but the skill "
        "still tells sessions to work around their absence"
    )

    # The other direction: a tool the skill leans on must actually be there.
    named = {m.group(1) for m in re.finditer(r"`(swarm_[a-z_]+)`", body)}
    phantom = sorted(named - _REAL)
    assert not phantom, (
        f"the skill names {phantom}, which do not exist in "
        "swarm_mcp.server.TOOLS -- a session told to call one gets an error "
        "at the moment it is trying to be useful"
    )


def test_the_read_only_skill_never_gains_a_tool_that_writes():
    """`sc`'s text promises it "never writes, never refreshes a credential and
    never cancels anything, so it is always safe to run". That promise is a
    permission rule, so it is checked as one."""
    fields, _ = _load(_PLUGIN / "skills" / "sc" / "SKILL.md")
    granted = _granted(fields["allowed-tools"])
    assert not granted & _WRITE_TOOLS, "sc is documented as read-only"


def test_the_delegation_skill_can_reach_every_tool_it_tells_a_session_to_call():
    """The opposite failure to the one above: prose that names a real tool the
    skill was never granted. The model reads the instruction, calls the tool,
    and is refused."""
    fields, body = _load(_PLUGIN / "skills" / "delegate" / "SKILL.md")
    granted = _granted(fields["allowed-tools"])
    needed = _identifiers(body) & _REAL
    assert needed <= granted, f"named but not allowed: {sorted(needed - granted)}"


def test_both_registrations_grant_exactly_the_same_tools():
    """THE MUTATION THIS CATCHES, and it is the one that will actually happen:
    a nineteenth tool ships, somebody adds `mcp__swarmcloud__swarm_whatever` to
    `delegate`, tests it in this repository where the project `.mcp.json` is what
    registers the bridge, and it works. Installed anywhere else the session gets
    the plugin's SCOPED server instead, the new permission does not match, and
    the tool is refused -- with no error anyone will connect to the manifest.

    So the two lists are one list, asserted symmetric. Drop either spelling of
    any tool and this goes red naming which side is short.
    """
    fields, _ = _load(_PLUGIN / "skills" / "delegate" / "SKILL.md")
    per_prefix = _granted_per_prefix(fields["allowed-tools"])
    project, scoped = _PREFIXES
    assert per_prefix[project], "no project-server permissions at all"
    assert per_prefix[scoped], (
        "no plugin-scoped permissions, so this skill works only in a session "
        "whose own .mcp.json registers the bridge"
    )
    assert per_prefix[project] == per_prefix[scoped], (
        "only under the project server: "
        f"{sorted(per_prefix[project] - per_prefix[scoped])}; "
        "only under the plugin server: "
        f"{sorted(per_prefix[scoped] - per_prefix[project])}"
    )


def test_the_plugin_bundles_the_bridge_it_tells_a_session_to_call():
    """`delegate` calls TOOLS, not commands, so the server has to arrive with
    the plugin. It did not: `mcpServers` was absent from `plugin.json` and the
    only registration was `.mcp.json` at the repository root -- which a session
    in any other directory does not have. Eighteen permissions, a skill written
    against them, and no server.

    The command is checked too, because a server that cannot start is the same
    outcome as one that is not declared: `${CLAUDE_PLUGIN_ROOT}` is the only
    path the host expands here (it is NOT exported to Bash-tool commands, which
    is why the shell half of this plugin stays repository-bound), and the bridge
    is a console script of the workspace one directory above `plugin/`.
    """
    servers = _plugin_manifest()["mcpServers"]
    swarmcloud = servers["swarmcloud"]
    assert swarmcloud.get("command") == "uv", swarmcloud
    args = swarmcloud.get("args") or []
    assert args[-1] == "swarm-mcp", (
        f"the server must run the `swarm-mcp` console script, not {args[-1]!r}"
    )
    assert "--directory" in args, (
        "without --directory, `uv run` resolves against the session's working "
        "directory, which is the thing this declaration exists to stop mattering"
    )
    directory = args[args.index("--directory") + 1]
    assert directory.startswith("${CLAUDE_PLUGIN_ROOT}"), (
        f"{directory!r} is not plugin-relative; a literal path works on one "
        "machine and an unexpanded variable works on none"
    )
    # plugin/ -> the workspace root, which is where pyproject.toml lives.
    assert directory == "${CLAUDE_PLUGIN_ROOT}/..", directory
    assert (_PLUGIN.parent / "pyproject.toml").exists(), (
        "the directory the plugin points `uv run` at holds no pyproject.toml"
    )
