"""How a `swarm` or `sc` command is spelled, for the install that is running (#189).

THE DEFECT THIS REPLACES. Every command this package hands back -- the
`follow_live_with` a dispatch or workflow reply carries, the `follow:` line
after `swarm workflow`, `sign-in required for <context>: run ... sc login`,
doctor's remedies -- began `uv run`. That spelling resolves a uv PROJECT from
the reader's current directory, so it runs in a checkout of this repository
and nowhere else. On a machine that has only the `sc` plugin it answers

    error: Failed to spawn: `swarm`
      Caused by: No such file or directory (os error 2)

(measured 2026-09-25 against sc-v0.5.1), on exactly the install whose manifest
says the delegate path "needs only uv", while the delegate skill told the model
the prefix "is not optional". It looked fine in the re-test only because `uv
tool install` had put a `swarm` on that machine's PATH and `uv run` falls back
to PATH -- a copy installed on its own, which can be a different version from
the bridge that printed the command.

ONE FUNCTION DECIDES, and it asks the running install rather than assuming
one. In this order:

  1. A CHECKOUT. This package was imported from `<root>/apps/swarm-mcp` of a
     directory that is a checkout (the repository's own `.mcp.json`, `uv run
     swarm ...`, the test suite). `uv run` resolves that workspace from
     anywhere inside it and needs nothing installed. Asked FIRST because `uv
     run` also puts the workspace's `bin` on its child's PATH, so the PATH
     test below would otherwise find `swarm` there and hand back a bare word
     the reader's own shell -- which `uv run` did not start -- cannot find.
  2. AN INSTALLED TOOL. `uv tool install` builds the environment under uv's
     tool directory, writes `uv-receipt.toml` at its root, and links its
     console scripts onto PATH. When `swarm` on PATH resolves to THIS
     environment's own script, the bare word runs the same code. A `swarm` on
     PATH from any other environment does not count, for the version reason
     above.
  3. `SWARM_MCP_FROM`. The plugin starts its server with `--from
     ${SWARM_MCP_FROM:-<pinned requirement>}`, so when the variable is set it
     IS the source this bridge was built from, verbatim.
  4. The install's PEP 610 record, `direct_url.json`. uv writes it for a git
     requirement as the repository URL, the revision asked for and the
     subdirectory -- the pieces of the requirement plugin.json passed to
     `--from` -- and for a directory as its `file://` URL. Rebuilt into `uv
     tool run --from '<requirement>'`, the command runs the SAME version as
     the bridge that printed it. CI's install step reads what a real `uv tool
     run` install answers here (tests/unit/mcp/test_plugin_bridge_install.py).
  5. Nothing known -- a pip install, an import off PYTHONPATH -- and the bare
     word, which is at least the program's real name.

A CREDENTIAL IN THE SOURCE IS NEVER PRINTED. PEP 610 says a recorded URL
carries no authentication, but `SWARM_MCP_FROM` is whatever someone typed, and
`https://user:token@host/...` is a way to type it. The command is printed to a
terminal and handed to a model, so the URL's userinfo is dropped; a private
repository needs git credentials of its own either way.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .client import _BRIDGE_DIR, _is_checkout

#: The console scripts this package installs for a reader to type
#: (`[project.scripts]` in apps/swarm-mcp/pyproject.toml). `swarm-mcp` is the
#: server itself and is never handed to anyone.
PROGRAMS = ("swarm", "sc")

#: The distribution whose install record is read, and the name a rebuilt
#: requirement binds: `swarm-mcp @ git+...`. A bare name would be a PyPI lookup,
#: and PyPI's `swarm-mcp` is somebody else's project (plugin/README.md).
DISTRIBUTION = "swarm-mcp"

#: What `uv tool install` writes at the root of every environment it creates,
#: and `uv tool run` never writes into the cache environment it builds. The one
#: mark that tells "installed as a tool" from "run once from the cache".
TOOL_RECEIPT = "uv-receipt.toml"

#: The checkout's spelling. Decided here and nowhere else.
CHECKOUT_LAUNCHER = "uv run "

#: `scheme://userinfo@` in a URL, for `_without_credentials`.
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _package_dir() -> Path:
    import swarm_mcp

    return Path(swarm_mcp.__file__).resolve().parent


def running_from_checkout() -> bool:
    """Was this package imported from `<root>/apps/swarm-mcp` of a checkout?"""
    package = _package_dir()
    project = package.parent
    root = project.parent.parent
    return project == root.joinpath(*_BRIDGE_DIR) and _is_checkout(root)


def _scripts_dir() -> Path:
    return Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")


def installed_as_tool(program: str) -> bool:
    """`uv tool install`ed, with THIS environment's `program` first on PATH."""
    if not (Path(sys.prefix) / TOOL_RECEIPT).is_file():
        return False
    found = shutil.which(program)
    if not found:
        return False
    try:
        return Path(found).resolve().parent == _scripts_dir().resolve()
    except OSError:
        return False


def _record() -> dict[str, Any] | None:
    """This install's `direct_url.json`, or None when it has none or it is unreadable."""
    from importlib import metadata

    try:
        raw = metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    try:
        record = json.loads(raw or "")
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _without_credentials(source: str) -> str:
    return _USERINFO.sub(lambda match: match.group("scheme"), source)


def source_requirement() -> str | None:
    """What `uv tool run --from` needs to rebuild THIS install, or None."""
    override = os.environ.get("SWARM_MCP_FROM", "").strip()
    if override:
        return _without_credentials(override)
    record = _record()
    if record is None:
        return None
    url = str(record.get("url") or "").strip()
    vcs = record.get("vcs_info")
    if isinstance(vcs, dict) and url:
        kind = str(vcs.get("vcs") or "git")
        spec = url if url.startswith(f"{kind}+") else f"{kind}+{url}"
        revision = vcs.get("requested_revision") or vcs.get("commit_id")
        if revision:
            spec += f"@{revision}"
        subdirectory = record.get("subdirectory")
        if subdirectory:
            spec += f"#subdirectory={subdirectory}"
        return _without_credentials(f"{DISTRIBUTION} @ {spec}")
    if isinstance(record.get("dir_info"), dict):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "file":
            return urllib.request.url2pathname(parts.path)
    return None


def launcher(program: str) -> str:
    """The words that go in front of `program`, with a trailing space, or ""."""
    if running_from_checkout():
        return CHECKOUT_LAUNCHER
    if installed_as_tool(program):
        return ""
    requirement = source_requirement()
    if requirement:
        return f"uv tool run --from {shlex.quote(requirement)} "
    return ""


def terminal_command(words: str) -> str:
    """A `swarm ...` or `sc ...` command, spelled so it runs where this bridge runs.

    Anything that does not start with one of this package's programs is
    returned as it came -- including a command already spelled by this
    function, so passing one through twice cannot prefix it twice.
    """
    words = words.strip()
    if not words:
        return words
    program = words.split(None, 1)[0]
    if program not in PROGRAMS:
        return words
    return f"{launcher(program)}{words}"


def help_command(words: str) -> str:
    """`terminal_command`, for an argparse `help=` string.

    argparse %-formats every argument's help when it prints it, so a `%` in
    the spelled source -- a percent-encoded character in a `SWARM_MCP_FROM`
    path, say -- would end `--help` in a ValueError. Doubled here, once, rather
    than at each call site. The SPELLING is still decided above.
    """
    return terminal_command(words).replace("%", "%%")
