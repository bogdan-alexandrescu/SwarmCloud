"""Every command the bridge hands back runs where the bridge itself runs (#189).

MEASURED 2026-09-25 on sc-v0.5.1 (ec49edf). Every command the swarm-mcp bridge
hands back began `uv run`: `follow_live_with` from `swarm_dispatch` and
`swarm_workflow`, the `follow:` line after `swarm workflow`, the sign-in hint
`sign-in required for <context>: run uv run sc login`, doctor's remedies. `uv
run` resolves a uv PROJECT from the reader's directory, so outside a checkout
it answers

    error: Failed to spawn: `swarm`
      Caused by: No such file or directory (os error 2)

-- on exactly the install the plugin manifest says "needs only uv", while the
delegate skill told the model the prefix "is not optional". It looked fine in
the re-test only because `uv tool install` had put a `swarm` on that machine's
PATH, and `uv run` falls back to PATH: the control that shows it could have
gone the other way.

THE PROPERTY, held here for each kind of install the bridge can be running
from: a command the bridge prints, run in the reader's shell, reaches the same
code as the bridge that printed it.

  * a checkout (the repository's own `.mcp.json`, `uv run swarm ...`): `uv run`;
  * `uv tool install`, with this environment's `swarm` on PATH: the bare word;
  * the plugin's `uv tool run --from '<requirement>' swarm-mcp`, with nothing
    installed: `uv tool run --from '<that requirement>' swarm ...`, rebuilt
    from the install's own PEP 610 record, so it names the same version;
  * the developer escape hatch, `SWARM_MCP_FROM`: that value, verbatim.

Offline. The installed layout is simulated the way
test_bridge_outside_a_checkout.py simulates it -- the package's `__file__`
moved into a uv cache under `tmp_path`, the install record stood in for -- and
CI's install step asks the REAL `uv tool run` install the same question
(test_plugin_bridge_install.py, `_PROBE`).
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest

import swarm_mcp
from swarm_mcp import auth, cli, client, follow, server, signin
from swarm_mcp.auth import Tier
from swarm_mcp.config import Deployment

_REPO = Path(__file__).resolve().parents[3]
_PACKAGE = _REPO / "apps" / "swarm-mcp" / "swarm_mcp"
_MANIFEST = _REPO / "plugin" / ".claude-plugin" / "plugin.json"
_SKILL = _REPO / "plugin" / "skills" / "delegate" / "SKILL.md"

#: A ref no release carries, so nothing here can pass by matching the pinned one.
_REF = "sc-v9.8.7"

#: `--from ${SWARM_MCP_FROM:-<requirement>}` in the plugin manifest.
_EXPANSION = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*):-(?P<default>[^}]*)\}$")
_GIT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*) @ git\+(?P<url>https://[^@\s]+)"
    r"@(?P<ref>[^#@\s]+)#subdirectory=(?P<subdirectory>[^&\s]+)$"
)


def _pinned() -> re.Match:
    """The requirement the plugin starts the bridge with, as plugin.json says it."""
    args = json.loads(_MANIFEST.read_text())["mcpServers"]["swarmcloud"]["args"]
    expansion = _EXPANSION.match(args[args.index("--from") + 1])
    assert expansion, args
    spec = _GIT_REQUIREMENT.match(expansion["default"])
    assert spec, expansion["default"]
    return spec


def _requirement(ref: str = _REF) -> str:
    """plugin.json's requirement, at `ref` instead of the pinned tag."""
    spec = _pinned()
    return spec.group(0).replace(f"@{spec['ref']}#", f"@{ref}#", 1)


def _git_record(ref: str = _REF) -> dict:
    """What uv writes into `direct_url.json` for that requirement (PEP 610's VCS
    form): the URL without `git+`, the revision asked for, the subdirectory."""
    spec = _pinned()
    return {
        "url": spec["url"],
        "vcs_info": {"vcs": "git", "requested_revision": ref, "commit_id": "0" * 40},
        "subdirectory": spec["subdirectory"],
    }


class _Installed:
    """The one method of `importlib.metadata.Distribution` that is read."""

    def __init__(self, record: dict | None) -> None:
        self._record = record

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._record is not None:
            return json.dumps(self._record)
        return None


def _install(
    monkeypatch,
    tmp_path: Path,
    record: dict | None,
    *,
    tool: bool = False,
    on_path: tuple[str, ...] = (),
    elsewhere_on_path: tuple[str, ...] = (),
) -> Path:
    """Make the running bridge look installed rather than imported from this checkout.

    `tool=False`: the cache environment `uv tool run` builds, which carries no
    receipt. `tool=True`: an environment `uv tool install` made, with the
    `uv-receipt.toml` uv writes at its root. `on_path`: the programs whose PATH
    entry is THIS environment's script; `elsewhere_on_path`: programs on PATH
    that belong to some other environment.
    """
    if tool:
        root = tmp_path / "uv-tools" / "swarm-mcp"
    else:
        root = tmp_path / "uv-cache" / "archive-v0" / "Xq3bVf0"
    site = root / "lib" / "python3.11" / "site-packages"
    (site / "swarm_mcp").mkdir(parents=True)
    scripts = root / "bin"
    scripts.mkdir()
    for program in ("swarm", "sc", "swarm-mcp"):
        (scripts / program).write_text("#!/bin/sh\n")
    if tool:
        (root / "uv-receipt.toml").write_text("[tool]\nrequirements = []\n")
    other = tmp_path / "somewhere-else" / "bin"
    other.mkdir(parents=True)

    monkeypatch.setattr(swarm_mcp, "__file__", str(site / "swarm_mcp" / "__init__.py"))
    monkeypatch.setattr(client, "__file__", str(site / "swarm_mcp" / "client.py"))
    monkeypatch.setattr(sys, "prefix", str(root))

    def _distribution(name: str) -> _Installed:
        assert name.replace("_", "-").lower() == "swarm-mcp", name
        return _Installed(record)

    monkeypatch.setattr(importlib.metadata, "distribution", _distribution)

    def _which(program, *args, **kwargs):  # noqa: ARG001
        if program in on_path:
            return str(scripts / program)
        if program in elsewhere_on_path:
            (other / program).write_text("#!/bin/sh\n")
            return str(other / program)
        return None

    monkeypatch.setattr(shutil, "which", _which)
    monkeypatch.delenv("SWARM_MCP_FROM", raising=False)
    return root


def _tool_run(requirement: str, words: str) -> str:
    return f"uv tool run --from {shlex.quote(requirement)} {words}"


# -- a plugin-only install ------------------------------------------------------


def test_a_plugin_only_install_is_handed_the_uv_tool_run_spelling(monkeypatch, tmp_path):
    """THE DEFECT. The plugin's server runs from uv's cache with nothing
    installed; `uv run swarm tail ...` fails there with `Failed to spawn`."""
    _install(monkeypatch, tmp_path, _git_record())

    command = follow.follow_command(["task_a", "task_b"])

    assert command == _tool_run(_requirement(), "swarm tail task_a task_b"), command
    assert not command.startswith("uv run "), command


def test_the_requirement_is_the_one_the_plugin_starts_the_bridge_with(monkeypatch, tmp_path):
    """Rebuilt from the install's own record, the printed command names the
    SAME repository, subdirectory and revision the bridge was built from -- so
    it cannot run a different version from the bridge that printed it, which a
    PATH copy can (#189's control)."""
    _install(monkeypatch, tmp_path, _git_record())

    argv = shlex.split(follow.follow_command(["task_a"]))

    assert argv[:4] == ["uv", "tool", "run", "--from"], argv
    assert argv[4] == _requirement(), (
        f"the printed --from is {argv[4]!r}; plugin.json's requirement at {_REF} is "
        f"{_requirement()!r}"
    )
    assert argv[5:] == ["swarm", "tail", "task_a"], argv


def test_every_hint_the_bridge_prints_uses_the_one_spelling(monkeypatch, tmp_path):
    """One helper decides. The dispatch reply, the sign-in hint, the doctor's
    remedies, the no-log line and the CLI's own labels all went through a
    `uv run` constant; each is checked here, on the install where it was wrong."""
    _install(monkeypatch, tmp_path, _git_record())
    spelled = _tool_run(_requirement(), "")

    class _Dispatching:
        def dispatch(self, **kwargs):  # noqa: ARG002
            return {"id": "task_1", "state": "QUEUED"}

    reply = json.loads(server._call(_Dispatching(), "swarm_dispatch", {"prompt": "x", "profile": "mock"}))
    deployment = Deployment(
        context="acme", url="https://swarm.example.com", client_id="id.apps.googleusercontent.com",
        source="test", front_door=True, current=True,
    )
    said = {
        "swarm_dispatch follow_live_with": reply["follow_live_with"],
        "the sign-in hint": signin.sign_in_hint(deployment),
        "the IAP refusal's login command": client._login_command(),
        "the no-log line": follow.no_log_line("task_a", "att_1"),
        "doctor's proxy-tier remedy": auth.WHY_NOT[(Tier.PROXY, "team")],
        "the dispatch refusal label": cli.terminal_command("swarm dispatch"),
    }
    for where, text in said.items():
        assert "uv run " not in text, f"{where} still says `uv run`: {text}"
        assert spelled in text, f"{where} is not spelled for this install: {text}"
    assert f"{spelled}swarm tail task_1" == reply["follow_live_with"]
    assert f"`{spelled}sc login`" in said["the sign-in hint"]
    assert f"{spelled}sc login" in said["doctor's proxy-tier remedy"]


def test_the_escape_hatch_is_handed_back_verbatim(monkeypatch, tmp_path):
    """`SWARM_MCP_FROM=<checkout>/apps/swarm-mcp` is what the manifest passed to
    `--from`, so it is what a command must pass too -- the working copy, not
    the pinned tag."""
    working_copy = tmp_path / "my checkout" / "apps" / "swarm-mcp"
    _install(monkeypatch, tmp_path, {"url": working_copy.as_uri(), "dir_info": {}})
    monkeypatch.setenv("SWARM_MCP_FROM", str(working_copy))

    argv = shlex.split(follow.follow_command(["task_a"]))

    assert argv == ["uv", "tool", "run", "--from", str(working_copy), "swarm", "tail", "task_a"], argv


def test_a_directory_install_names_its_directory_when_the_variable_is_not_visible(
    monkeypatch, tmp_path
):
    """The escape hatch's record is `dir_info` with a file URL; Claude Code does
    not have to pass SWARM_MCP_FROM through to the server for the command to
    name the same source."""
    working_copy = tmp_path / "work" / "apps" / "swarm-mcp"
    _install(monkeypatch, tmp_path, {"url": working_copy.as_uri(), "dir_info": {}})

    argv = shlex.split(follow.follow_command(["task_a"]))

    assert argv[:4] == ["uv", "tool", "run", "--from"], argv
    assert Path(argv[4]) == working_copy, argv


def test_a_credential_in_the_source_url_is_never_printed(monkeypatch, tmp_path):
    """A requirement can carry a token in its URL's userinfo. The command is
    printed to a terminal and handed to a model, so the token is dropped."""
    record = _git_record()
    record["url"] = record["url"].replace("https://", "https://x-access-token:ghs_secretsecret@")
    _install(monkeypatch, tmp_path, record)

    command = follow.follow_command(["task_a"])

    assert "ghs_secretsecret" not in command, command
    assert "x-access-token" not in command, command


# -- an installed tool --------------------------------------------------------


def test_an_installed_tool_is_spelled_bare(monkeypatch, tmp_path):
    """`uv tool install` links this environment's `swarm` and `sc` onto PATH, so
    the bare word runs the same code as the bridge."""
    _install(monkeypatch, tmp_path, _git_record(), tool=True, on_path=("swarm", "sc"))

    assert follow.follow_command(["task_a"]) == "swarm tail task_a"
    assert client._login_command() == "sc login"


def test_an_installed_tool_whose_script_is_not_on_path_is_run_through_uv(monkeypatch, tmp_path):
    """Installed, but the tool directory is not on this PATH: the bare word
    would be `command not found`, so the install is named instead."""
    _install(monkeypatch, tmp_path, _git_record(), tool=True, on_path=())

    assert follow.follow_command(["task_a"]) == _tool_run(_requirement(), "swarm tail task_a")


def test_a_swarm_on_path_from_another_environment_is_not_trusted(monkeypatch, tmp_path):
    """#189's control: a `swarm` somebody `uv tool install`ed separately can be
    another version. The plugin's cache environment is not it, so the command
    still names the bridge's own source."""
    _install(monkeypatch, tmp_path, _git_record(), elsewhere_on_path=("swarm", "sc"))

    assert follow.follow_command(["task_a"]) == _tool_run(_requirement(), "swarm tail task_a")


# -- a checkout -----------------------------------------------------------------


def test_a_checkout_keeps_uv_run(monkeypatch):
    """Imported from this repository, as the test suite and the repository's own
    `.mcp.json` import it: `uv run` resolves the workspace from anywhere inside
    the checkout, and is the one spelling that needs nothing installed there."""
    monkeypatch.delenv("SWARM_MCP_FROM", raising=False)
    assert Path(swarm_mcp.__file__).resolve().is_relative_to(_REPO.resolve())

    assert follow.follow_command(["task_a"]) == "uv run swarm tail task_a"
    assert client._login_command() == "uv run sc login"


def test_nothing_to_follow_is_still_nothing():
    """`swarm tail ` with the ids missing still looks runnable, which is worse
    than absent -- whatever the install."""
    assert follow.follow_command([]) == ""
    assert follow.follow_command([None, ""]) == ""


# -- one place ------------------------------------------------------------------


def _string_literals(path: Path) -> list[str]:
    """Every string constant in a module that is not a docstring."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_only_one_module_spells_a_launcher():
    """`follow.RUN_PREFIX` was one constant, and one constant was still the
    defect: it was right for one install of three. The spelling is DECIDED in
    `swarm_mcp.invocation`, and no other module writes `uv run ` or `uv tool
    run` into a string it prints."""
    modules = sorted(_PACKAGE.glob("*.py"))
    assert len(modules) > 5, f"found nothing to read under {_PACKAGE}"
    offenders = {
        path.name: [s for s in _string_literals(path) if "uv run " in s or "uv tool run" in s]
        for path in modules
        if path.name != "invocation.py"
    }
    offenders = {name: found for name, found in offenders.items() if found}
    assert not offenders, f"a launcher is spelled outside swarm_mcp.invocation: {offenders}"


def test_the_delegate_skill_says_to_run_the_command_as_handed_back():
    """`plugin/skills/delegate/SKILL.md` told the model "The `uv run` prefix is
    not optional" -- a second statement of the spelling, and the wrong one on a
    plugin-only install. The skill now says the bridge spells it and names the
    three spellings a reader may see."""
    text = _SKILL.read_text()
    assert "prefix is not optional" not in text, "the skill still insists on one prefix"
    assert "follow_live_with" in text
    assert "uv tool run --from" in text, "the plugin-only spelling is not described"
