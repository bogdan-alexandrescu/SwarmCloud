"""The plugin's MCP server has to start on a machine that has only the plugin.

MEASURED 2026-09-25 on 0.4.0 (commit ea1355d). `/plugin marketplace add
bogdan-alexandrescu/SwarmCloud` then `/plugin install sc@swarmcloud` copies
ONLY `plugin/` into `~/.claude/plugins/cache/swarmcloud/sc/0.4.0/` -- Claude
Code's plugin loading reference says it in as many words: "Files outside the
plugin directory aren't copied, so when a script inside a copied plugin reads a
path above the plugin root, such as `../shared`, it doesn't find them." The
server was declared `uv run --directory ${CLAUDE_PLUGIN_ROOT}/.. swarm-mcp`. In
the cache that directory is `.../cache/swarmcloud/sc/`, which holds no
`pyproject.toml`, so uv answered `error: Failed to spawn: swarm-mcp -- No such
file or directory` and the session showed `plugin:sc:swarmcloud failed to
connect`. It had only ever worked inside a checkout -- the one place where the
repository's own `.mcp.json` already registers the bridge, so the plugin's
declaration added a working server nowhere.

The test that guarded that declaration asserted the defect:
`directory == "${CLAUDE_PLUGIN_ROOT}/.."`, and that the directory it points at
holds a `pyproject.toml` -- checked against THIS checkout, where it does. It
held the shape and never the property.

THE PROPERTY, checked here, is that the declaration names nothing outside the
plugin directory, and that what it names instead -- a git URL -- is this
repository's bridge, whose in-repository dependency resolves by the same
mechanism from the same commit, at a ref that moves with the plugin's version.

Offline, like everything else in tests/unit, except the last test: it is
skipped unless CI hands it a pushed commit to install from
(`SWARM_BRIDGE_INSTALL_REF`). See its docstring for why that one needs the
network and why it runs in CI rather than here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_README = _PLUGIN / "README.md"

#: The server key the skills' permissions are written against: a plugin's
#: server's tools arrive as `mcp__plugin_sc_swarmcloud__*`.
_SERVER_KEY = "swarmcloud"

#: The developer escape hatch. The manifest is what reads it; this name and the
#: README are what tell a developer it exists.
_ESCAPE_VAR = "SWARM_MCP_FROM"

#: The bridge is pinned to the tag `sc-v<plugin.json version>`.
_TAG_PREFIX = "sc-v"

#: Claude Code's own expansion syntax for a server's command, args and env:
#: `${VAR}` or `${VAR:-default}`, the default running to the first `}`. Read
#: out of Claude Code 2.1.282, where plugin MCP args go through
#: `/\$\{([A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?)\}/g` after the plugin path
#: variables are substituted. ANCHORED: the whole `--from` value must be one
#: expansion, so an unset variable leaves exactly the pinned spec.
_EXPANSION = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*):-(?P<default>[^}]*)\}$")

#: A PEP 508 NAMED direct reference to a subdirectory of a GitHub repository,
#: over HTTPS -- the one transport every user can fetch without a key.
_GIT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*) @ "
    r"git\+(?P<url>https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+))"
    r"@(?P<ref>[^#@\s]+)"
    r"#subdirectory=(?P<subdirectory>[^&\s]+)$"
)

#: A `..` path segment anywhere in a string.
_PARENT_SEGMENT = re.compile(r"(^|[/\\])\.\.([/\\]|$)")


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text())


def _server() -> dict:
    servers = _manifest().get("mcpServers")
    assert isinstance(servers, dict) and _SERVER_KEY in servers, (
        f"plugin.json declares no `{_SERVER_KEY}` server under mcpServers: {servers!r}"
    )
    return servers[_SERVER_KEY]


def _from_value(server: dict) -> str:
    args = list(server.get("args") or [])
    assert "--from" in args and args.index("--from") + 1 < len(args), (
        "the server does not say where the bridge comes from (`--from <requirement>`), "
        f"so it can only run a bridge that is already on disk: {args!r}"
    )
    return args[args.index("--from") + 1]


def _pinned(server: dict) -> re.Match:
    """The requirement the server installs when the escape hatch is unset."""
    value = _from_value(server)
    expansion = _EXPANSION.match(value)
    assert expansion, (
        f"`--from` must be exactly `${{{_ESCAPE_VAR}:-<pinned requirement>}}`, "
        f"found {value!r}"
    )
    spec = _GIT_REQUIREMENT.match(expansion["default"])
    assert spec, (
        "the pinned requirement must be `swarm-mcp @ git+https://github.com/"
        "<owner>/<repo>@<ref>#subdirectory=<dir>`, found "
        f"{expansion['default']!r}"
    )
    return spec


def _pyproject(directory: Path) -> dict:
    return tomllib.loads((directory / "pyproject.toml").read_text())


def _normalise(name: str) -> str:
    """PEP 503: `swarm_common`, `Swarm.Common` and `swarm-common` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, f"unparseable requirement {requirement!r}"
    return _normalise(match.group(1))


def _in_repo_packages() -> dict[str, Path]:
    """Every Python package this repository defines, by normalised name."""
    packages: dict[str, Path] = {}
    for pyproject in sorted(_REPO.glob("apps/*/pyproject.toml")):
        name = tomllib.loads(pyproject.read_text()).get("project", {}).get("name")
        if name:
            packages[_normalise(name)] = pyproject.parent.resolve()
    return packages


def _uv_sources(pyproject: dict) -> dict[str, object]:
    sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    return {_normalise(name): source for name, source in sources.items()}


def test_the_server_names_nothing_outside_the_plugin_directory():
    """The defect itself: the declaration reached above the plugin root.

    THE MUTATION THIS CATCHES is 0.4.0's declaration verbatim --
    `uv run --directory ${CLAUDE_PLUGIN_ROOT}/.. swarm-mcp` -- which fails four
    of the assertions below. `uv run` resolves a PROJECT, and a project is
    exactly what the plugin cache does not have; `uv tool run` builds its own
    environment from a requirement and needs nothing on disk.
    """
    server = _server()
    command = server.get("command")
    args = list(server.get("args") or [])
    env = dict(server.get("env") or {})

    assert command == "uv", (
        f"the server must be started by `uv`, the one prerequisite the README "
        f"names, not {command!r}"
    )
    assert args[:2] == ["tool", "run"], (
        "`uv run` needs a project on disk -- that is what `--directory` pointed "
        "it at, and in the plugin cache there is none. `uv tool run` builds an "
        f"environment from a requirement. Found {args[:2]!r}"
    )
    for flag in ("--directory", "--project"):
        assert flag not in args, (
            f"{flag} points uv at a directory, and the only directories a "
            "marketplace install has are the plugin's own"
        )

    values = [str(command), *map(str, args), *map(str, env.values())]
    for value in values:
        assert not _PARENT_SEGMENT.search(value), (
            f"{value!r} climbs out of a directory; above ${{CLAUDE_PLUGIN_ROOT}} "
            "there is only the plugin cache"
        )
        assert "CLAUDE_PROJECT_DIR" not in value, (
            f"{value!r} makes the server depend on where the SESSION is, which "
            "is the repository-bound defect again under a different name"
        )
        assert not re.match(r"^(/Users/|/home/|[A-Za-z]:\\)", value), (
            f"{value!r} is a path on one machine"
        )


def test_the_bridge_is_fetched_by_name_from_the_public_repository():
    """A NAMED requirement, over HTTPS, and the command is its console script.

    Named because a bare name is a PyPI lookup, and PyPI's `swarm-mcp` is
    somebody else's project (0.5.0, "MCP server for Foursquare Swarm check-in
    data", read 2026-09-24): `uvx swarm-mcp` runs a stranger's code. The name
    in `swarm-mcp @ git+...` binds the URL, and uv refuses a URL whose package
    says it is called anything else.
    """
    server = _server()
    spec = _pinned(server)
    args = list(server["args"])
    assert _normalise(spec["name"]) == "swarm-mcp", spec["name"]
    assert args[-1] == "swarm-mcp", (
        f"the server must run the `swarm-mcp` console script, not {args[-1]!r}"
    )
    assert args.index("--from") == len(args) - 3, (
        "`--from <requirement>` must come immediately before the executable; "
        f"anything between them is passed to uv, not to the bridge: {args!r}"
    )


def test_the_pin_moves_with_the_plugin_version():
    """One version for the skills and the bridge they describe.

    Claude Code keeps every user on the cached copy of a plugin until its
    manifest's `version` changes ("a manifest that pins `version` keeps every
    user on the cached copy until its author changes the string") -- so the
    version decides which SKILLS a user has, and the ref decides which BRIDGE
    those skills talk to. A version bumped without the ref ships new skills
    against the old tools; a ref moved without the version never reaches
    anyone who already installed.

    THE MUTATION THIS CATCHES: bump `version` in plugin.json and leave the ref,
    or the reverse.
    """
    version = _manifest().get("version")
    assert isinstance(version, str) and version.strip(), (
        "plugin.json has no version, so Claude Code versions the plugin by commit "
        "and there is nothing for the bridge's ref to agree with"
    )
    ref = _pinned(_server())["ref"]
    assert ref == f"{_TAG_PREFIX}{version}", (
        f"plugin.json is version {version!r} but its bridge is pinned to {ref!r}; "
        f"the ref must be the tag `{_TAG_PREFIX}{version}`"
    )


def test_the_pin_is_written_down_exactly_once():
    """The mirrored-copy rule: one ref, in plugin.json, and nowhere else.

    swarm-common in particular must NOT carry a git source of its own: a
    second `@<ref>` is a second value to forget, and the day they differ the
    bridge runs against a contract it was not built with.
    """
    ref = _pinned(_server())["ref"]
    assert _MANIFEST.read_text().count(f"@{ref}#") == 1, (
        f"plugin.json states the ref {ref!r} more than once"
    )

    others = [path for path in sorted(_PLUGIN.rglob("*")) if path.is_file() and path != _MANIFEST]
    others += [
        _REPO / ".claude-plugin" / "marketplace.json",
        _REPO / "apps" / "swarm-mcp" / "pyproject.toml",
        _REPO / "apps" / "common" / "pyproject.toml",
    ]
    assert len(others) > 3, f"found nothing to search under {_PLUGIN}"
    offenders = [
        str(path.relative_to(_REPO))
        for path in others
        if path.exists() and ref in path.read_text(errors="replace")
    ]
    assert not offenders, (
        f"the ref {ref!r} is restated in {offenders}; say `{_TAG_PREFIX}<version>` "
        "instead, so there is one value to change"
    )

    for directory in (_REPO / "apps" / "swarm-mcp", _REPO / "apps" / "common"):
        for name, source in _uv_sources(_pyproject(directory)).items():
            for entry in source if isinstance(source, list) else [source]:
                pinned = {"git", "url", "rev", "tag", "branch"} & set(entry)
                assert not pinned, (
                    f"{directory.relative_to(_REPO)} gives {name} a {sorted(pinned)} "
                    "source: a second pin. Use a relative `path`; uv resolves it "
                    "from the same commit as the bridge"
                )


def test_the_subdirectory_is_the_package_that_ships_the_server():
    """`#subdirectory=` must land on a buildable package that declares the script."""
    server = _server()
    spec = _pinned(server)
    package_dir = (_REPO / spec["subdirectory"]).resolve()
    assert package_dir.is_relative_to(_REPO.resolve()), package_dir
    assert (package_dir / "pyproject.toml").exists(), (
        f"#subdirectory={spec['subdirectory']} holds no pyproject.toml, so uv has "
        "nothing to build"
    )
    pyproject = _pyproject(package_dir)
    project = pyproject.get("project", {})
    assert _normalise(project.get("name", "")) == _normalise(spec["name"]), (
        f"the requirement names {spec['name']!r} but {spec['subdirectory']} builds "
        f"{project.get('name')!r}; uv refuses the mismatch"
    )
    assert pyproject.get("build-system", {}).get("build-backend"), (
        f"{spec['subdirectory']} declares no build backend"
    )
    executable = server["args"][-1]
    entry = project.get("scripts", {}).get(executable)
    assert entry, (
        f"{spec['subdirectory']} declares no `{executable}` script; it has "
        f"{sorted(project.get('scripts', {}))}"
    )
    module, _, function = entry.partition(":")
    module_file = package_dir / (module.replace(".", "/") + ".py")
    assert module_file.exists(), f"{entry} names a module that does not exist"
    assert re.search(rf"^def {re.escape(function)}\(", module_file.read_text(), re.MULTILINE), (
        f"{module_file.relative_to(_REPO)} has no top-level `{function}`"
    )


def test_every_in_repo_dependency_resolves_from_the_same_commit():
    """swarm-common arrives the way swarm-mcp does, or not at all.

    A BARE `swarm-common` is a PyPI lookup once the bridge is installed from
    git rather than from a checkout -- the root pyproject's path sources apply
    to the root project only. On PyPI the name is unclaimed (404 on
    2026-09-24): today the install fails, and the day someone registers it,
    every operator's bridge installs their code.

    A RELATIVE `path` source in swarm-mcp's own `[tool.uv.sources]` is what uv
    rewrites, inside a Git dependency, into a Git source at the SAME commit
    with the subdirectory taken relative to the checkout root. That is
    astral-sh/uv#9594 ("Respect path dependencies within Git dependencies",
    shipped in uv 0.5.6), implemented in `path_source` in
    `crates/uv-distribution/src/metadata/lowering.rs`: when the dependent is a
    git member, a directory source becomes `RequirementSource::Git` with
    `subdirectory = relative_to(install_path, fetch_root)`. So the walk below
    asks, for every in-repository dependency, the three things that lowering
    needs: a relative path, inside the repository, to a buildable package.
    """
    packages = _in_repo_packages()
    assert {"swarm-mcp", "swarm-common"} <= set(packages), (
        f"expected both packages under apps/, found {sorted(packages)}"
    )

    root_sources = _uv_sources(tomllib.loads((_REPO / "pyproject.toml").read_text()))
    start = (_REPO / _pinned(_server())["subdirectory"]).resolve()
    queue, seen, problems = [start], [], []
    while queue:
        directory = queue.pop()
        if directory in seen:
            continue
        seen.append(directory)
        where = directory.relative_to(_REPO)
        pyproject = _pyproject(directory)
        sources = _uv_sources(pyproject)
        for requirement in pyproject.get("project", {}).get("dependencies", []):
            name = _requirement_name(requirement)
            if name not in packages:
                continue
            if "@" in requirement:
                problems.append(f"{where}: {requirement!r} is a direct reference -- a second pin")
                continue
            source = sources.get(name)
            if not isinstance(source, dict) or "path" not in source:
                problems.append(
                    f"{where}: {name} has no `path` source, so outside this "
                    "checkout uv looks it up on PyPI"
                )
                continue
            if Path(source["path"]).is_absolute():
                problems.append(f"{where}: {name}'s path {source['path']!r} is absolute")
                continue
            target = (directory / source["path"]).resolve()
            if not target.is_relative_to(_REPO.resolve()):
                problems.append(f"{where}: {name}'s path leaves the repository: {target}")
                continue
            if target != packages[name]:
                problems.append(
                    f"{where}: {name}'s path resolves to {target}, but {name} is "
                    f"{packages[name]}"
                )
                continue
            if not _pyproject(target).get("build-system", {}).get("build-backend"):
                problems.append(f"{target.relative_to(_REPO)} declares no build backend")
            # The development workspace must agree, or `uv lock` meets two
            # different URLs for one package and refuses.
            root = root_sources.get(name)
            if isinstance(root, dict) and "path" in root:
                if (_REPO / root["path"]).resolve() != target:
                    problems.append(
                        f"the root pyproject puts {name} at {root['path']!r}; "
                        f"{where} puts it at {source['path']!r}"
                    )
                if bool(root.get("editable")) != bool(source.get("editable")):
                    problems.append(
                        f"the root pyproject and {where} disagree on whether {name} "
                        "is editable, which uv reads as two requirements"
                    )
            queue.append(target)

    assert not problems, "\n".join(problems)

    # Not vacuous: the bridge imports the contract, so the walk must reach it.
    imports_contract = any(
        re.search(r"^\s*(from|import)\s+swarm_common\b", path.read_text(), re.MULTILINE)
        for path in sorted((start / "swarm_mcp").glob("*.py"))
    )
    assert imports_contract, "swarm_mcp no longer imports swarm_common; revisit this test"
    assert packages["swarm-common"] in seen, (
        "swarm_mcp imports swarm_common, but swarm-common is not among the "
        f"dependencies the walk reached: {[str(p.relative_to(_REPO)) for p in seen]}"
    )


def test_the_escape_hatch_is_the_one_the_readme_documents():
    """A developer runs a working copy by setting SWARM_MCP_FROM, and is told so.

    Not a keyword such as `local`: JSON cannot branch, and `uv tool run --from
    local` reads a bare word as a PyPI name -- the same trap as `swarm-mcp`
    itself. The value is anything `--from` takes: a path to `apps/swarm-mcp`
    in a checkout, or another git URL.
    """
    expansion = _EXPANSION.match(_from_value(_server()))
    assert expansion and expansion["var"] == _ESCAPE_VAR, (
        f"`--from` must be overridable through ${{{_ESCAPE_VAR}}}"
    )
    readme = _README.read_text()
    assert _ESCAPE_VAR in readme, f"plugin/README.md never mentions {_ESCAPE_VAR}"
    assert f"{_TAG_PREFIX}<version>" in readme, (
        "plugin/README.md does not say which tag the bridge is pinned to, so the "
        "release step that creates it is written down nowhere"
    )


def _checkout_bound_surfaces() -> list[str]:
    """Every part of the plugin that works only inside a checkout, as it is spelt.

    `uv run` resolves a PROJECT from the session's own directory, and
    `${CLAUDE_PLUGIN_ROOT}` is not exported to the Bash tool, so a command file
    that runs `uv run`, or a skill whose only tools are shell commands, needs a
    checkout of this repository whatever the MCP server does. Derived from the
    plugin's files rather than listed, so a new command is covered the day it
    is added.
    """
    bound: list[str] = []
    for command in sorted((_PLUGIN / "commands").glob("*.md")):
        if re.search(r"^\s*uv run ", command.read_text(), re.MULTILINE):
            bound.append(f"/{command.stem}")
    for skill in sorted((_PLUGIN / "skills").glob("*/SKILL.md")):
        text = skill.read_text()
        front = text.split("---", 2)[1] if text.startswith("---") else ""
        tools = re.findall(r"^\s*-\s*(\S.*?)\s*$", front, re.MULTILINE)
        if tools and not any(t.startswith("mcp__") for t in tools) and any(
            t.startswith("Bash(uv run") for t in tools
        ):
            name = re.search(r"^name:\s*(\S+)", front, re.MULTILINE)
            bound.append(f"`{name.group(1) if name else skill.parent.name}`")
    return bound


def _descriptions() -> dict[str, str]:
    marketplace = json.loads((_REPO / ".claude-plugin" / "marketplace.json").read_text())
    name = _manifest()["name"]
    entries = [p for p in marketplace.get("plugins", []) if p.get("name") == name]
    assert len(entries) == 1, f"marketplace.json lists {name!r} {len(entries)} times"
    return {
        "plugin/.claude-plugin/plugin.json": _manifest().get("description", ""),
        ".claude-plugin/marketplace.json": entries[0].get("description", ""),
    }


def test_the_descriptions_say_what_still_needs_a_checkout():
    """The install listing is where a user decides whether this plugin works for
    them, so it must not over-claim.

    #62 first fixed the old false claim ("requires the swarm-mcp bridge in this
    repository") with a new one: "it needs uv, not a checkout", said of both
    skills in plugin.json and of the whole plugin in marketplace.json. That is
    true of the MCP server only. `/sc` and the `sc` skill run `uv run sc` in the
    session's directory and fail anywhere but a checkout.

    THE PROPERTY: each description has one sentence that mentions a checkout and
    names every checkout-bound surface, and no sentence denies needing a
    checkout unless it is about the MCP server. THE MUTATION THIS CATCHES: both
    descriptions as #62 first wrote them.
    """
    bound = _checkout_bound_surfaces()
    assert "/sc" in bound and "`sc`" in bound, (
        f"expected /sc and the `sc` skill to be checkout-bound, derived {bound}"
    )
    problems = []
    for where, description in _descriptions().items():
        sentences = re.split(r"(?<=[.!?])\s+", description.strip())
        about_checkouts = [s for s in sentences if re.search(r"\bcheckout\b", s)]
        if not any(all(surface in s for surface in bound) for s in about_checkouts):
            problems.append(
                f"{where}: no sentence says that {', '.join(bound)} need a checkout; "
                f"the ones about a checkout are {about_checkouts}"
            )
        for sentence in sentences:
            denies = re.search(r"\b(not|no|without)\s+(a\s+)?checkout\b", sentence)
            if denies and "MCP server" not in sentence:
                problems.append(
                    f"{where}: {sentence!r} says no checkout is needed without saying "
                    "that only the MCP server is meant"
                )
    assert not problems, "\n".join(problems)


#: A commit that exists on GitHub. Set by CI only; see the test below.
_INSTALL_REF = os.environ.get("SWARM_BRIDGE_INSTALL_REF", "").strip()

#: Run with the installed bridge's own interpreter, to read what the MCP
#: protocol cannot show: which repository the installed copy thinks it is in,
#: and how it classes the front door. One JSON line on stdout.
_PROBE = """
import json, sys
from swarm_mcp import client
front_door = sys.argv[1]
print(json.dumps({
    "file": client.__file__,
    "root": str(client._repo_root()),
    "host": client.front_door_host(),
    "front_door_is_front_door": client.is_front_door("https://" + front_door),
    "run_app_is_front_door": client.is_front_door("https://swarm-api-xyz123-uc.a.run.app"),
}))
"""

#: Variables that would tell the probe where the API is. Removed, so the probe
#: sees what a user with none of them set sees.
_ADDRESS_VARS = {
    "SWARM_REPO_ROOT",
    "SWARM_API_HOST",
    "API_HOST",
    "SWARM_API_URL",
    "API_URL",
    "ENVIRONMENT",
    # Since #61 the bridge also takes its deployment from these, and reads
    # tfvars only in developer mode (SWARM_MCP_CONFIG_FROM=repo), which the
    # escape-hatch probe below turns on for itself and nothing else does.
    "SWARM_URL",
    "SWARM_CONTEXT",
    "SWARM_MCP_CONFIG_FROM",
    "SWARM_PLUGIN_DEPLOYMENT_URL",
}


def _tfvars_front_door() -> str:
    """dev.tfvars' frontend_hostname, parsed here rather than by the bridge."""
    tfvars = (_REPO / "terraform" / "environments" / "dev" / "dev.tfvars").read_text()
    assert not re.search(r"^\s*enable_frontend\s*=\s*false", tfvars, re.MULTILINE), (
        "dev.tfvars disables the front door; the probe below has nothing to find"
    )
    hosts = re.findall(r'^\s*frontend_hostname\s*=\s*"(.+?)"', tfvars, re.MULTILINE)
    assert hosts, "dev.tfvars no longer sets frontend_hostname"
    return hosts[0]


def _talk_mcp(argv: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[dict, float, str]:
    """Start the server the way Claude Code does and ask it two questions."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ci", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    started = time.monotonic()
    done = subprocess.run(
        argv,
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=600,
    )
    elapsed = time.monotonic() - started
    replies: dict = {}
    for line in done.stdout.splitlines():
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(reply, dict) and "id" in reply:
            replies[reply["id"]] = reply
    return replies, elapsed, f"exit {done.returncode}\nstderr:\n{done.stderr[-3000:]}"


@pytest.mark.skipif(
    not _INSTALL_REF,
    reason="network: CI sets SWARM_BRIDGE_INSTALL_REF to a pushed commit to install",
)
def test_the_bridge_installs_from_git_the_way_the_plugin_fetches_it(tmp_path):
    """The live half of everything above, at a commit instead of the tag.

    Everything above is the declaration's SHAPE, which is all an offline test
    can see; whether uv really resolves swarm-common from the same commit is
    uv's behaviour, cited above, not proven by it. This proves it. It runs the
    manifest's own command -- the escape-hatch expansion replaced by its
    default, the ref replaced by the commit under test, because the release
    tag does not exist until the merge is tagged -- from a cold uv cache, in a
    directory that is not a checkout, and talks MCP to what starts.

    Then it runs the escape hatch the README documents: the same command with
    `--from` set to this checkout's `apps/swarm-mcp`.

    Network (github.com, pypi.org), so skipped unless CI sets
    SWARM_BRIDGE_INSTALL_REF. It runs in CI, not on a laptop, for the reason
    CLAUDE.md gives: proven there, it is proven for everyone.
    """
    from swarm_mcp import server as bridge

    uv = shutil.which("uv")
    assert uv, "uv is not on PATH"
    declared = _server()
    spec = _pinned(declared)
    requirement = spec.group(0).replace(f"@{spec['ref']}#", f"@{_INSTALL_REF}#", 1)
    assert _INSTALL_REF in requirement, requirement

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH"}
    }
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    env["UV_TOOL_DIR"] = str(tmp_path / "uv-tools")
    # The session's directory is never the checkout: that is the case that broke.
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    assert not elsewhere.resolve().is_relative_to(_REPO.resolve()), elsewhere

    def argv(from_value: str) -> list[str]:
        args = list(declared["args"])
        args[args.index("--from") + 1] = from_value
        return [uv, *args]

    expected_tools = sorted(tool["name"] for tool in bridge.TOOLS)
    runs = (
        ("pinned", requirement),
        ("escape hatch", str(_REPO / spec["subdirectory"])),
    )
    for label, from_value in runs:
        replies, elapsed, diagnostics = _talk_mcp(argv(from_value), cwd=elsewhere, env=env)
        print(f"\n{label}: `uv tool run --from {from_value!r} swarm-mcp` answered in {elapsed:.1f}s")
        assert 1 in replies and "result" in replies[1], (
            f"{label}: the server did not answer `initialize`\n{diagnostics}"
        )
        assert replies[1]["result"]["serverInfo"]["name"] == "swarmcloud", replies[1]
        assert 2 in replies and "result" in replies[2], (
            f"{label}: the server did not answer `tools/list`\n{diagnostics}"
        )
        served = sorted(tool["name"] for tool in replies[2]["result"]["tools"])
        assert served == expected_tools, (
            f"{label}: the installed bridge serves {served}, this commit's serves "
            f"{expected_tools} -- it is not this commit's bridge"
        )

    # WHERE EACH INSTALL THINKS IT IS, and what it makes of the front door --
    # asked of the installed code, in the environment uv built, because both
    # answers depend on where the installed files landed and no mock can say
    # that. The same two facts are held offline, against a simulated layout,
    # in tests/unit/mcp/test_bridge_outside_a_checkout.py.
    front_door = _tfvars_front_door()
    probe_env = {key: value for key, value in env.items() if key not in _ADDRESS_VARS}
    # Both probes in DEVELOPER MODE, the only mode in which the bridge reads
    # tfvars at all since #61: the escape hatch must then find this checkout's
    # front door, and the git install, asked the same, must find none to read.
    # Outside developer mode no tfvars are read by either; that half is held
    # offline (tests/unit/mcp/test_contexts.py watches every file opened).
    probe_env["SWARM_MCP_CONFIG_FROM"] = "repo"
    readings: dict[str, dict] = {}
    for label, from_value in runs:
        args = list(declared["args"])
        args[args.index("--from") + 1] = from_value
        args[-1:] = ["python", "-c", _PROBE, front_door]
        probed = subprocess.run(
            [uv, *args], capture_output=True, text=True, cwd=elsewhere, env=probe_env, timeout=600
        )
        assert probed.returncode == 0, (
            f"{label}: the probe did not run\nexit {probed.returncode}\n{probed.stderr[-3000:]}"
        )
        readings[label] = json.loads(probed.stdout.strip().splitlines()[-1])
        print(f"{label}: {readings[label]}")

    problems = []
    pinned, local = readings["pinned"], readings["escape hatch"]
    # A git install is in uv's cache and has no repository to read -- the
    # state the reviewer's failure scenario starts from.
    if Path(pinned["file"]).resolve().is_relative_to(_REPO.resolve()):
        problems.append(f"pinned: the bridge ran from this checkout ({pinned['file']}), not an install")
    if pinned["host"]:
        problems.append(f"pinned: a git install found a front door ({pinned['host']!r}) to read")
    # ...so an explicit front-door URL has to be recognised by its shape.
    if pinned["front_door_is_front_door"] is not True:
        problems.append(
            f"pinned: SWARM_API_URL=https://{front_door} is not classed as the front "
            "door outside a checkout, so the bridge would present an ID token that IAP "
            "refuses with `Invalid JWT audience`"
        )
    if pinned["run_app_is_front_door"] is not False:
        problems.append("pinned: a *.run.app address was classed as the front door")
    # The escape hatch's files are in the cache too, but it was built from
    # this checkout and must read this checkout's tfvars, as `uv run` here does.
    if Path(local["file"]).resolve().is_relative_to(_REPO.resolve()):
        problems.append(
            f"escape hatch: ran from the checkout itself ({local['file']}), so this "
            "proves nothing about a non-editable install"
        )
    if Path(local["root"]).resolve() != _REPO.resolve():
        problems.append(
            f"escape hatch: the installed bridge takes {local['root']} for its "
            f"repository, not the checkout it was built from ({_REPO})"
        )
    if local["host"] != front_door:
        problems.append(
            f"escape hatch: found front door {local['host']!r}; this checkout's "
            f"dev.tfvars says {front_door!r}"
        )
    assert not problems, "\n".join(problems)

    # Where swarm-common came from: the same repository, the same commit, its
    # own subdirectory -- never PyPI.
    compiled = subprocess.run(
        [uv, "pip", "compile", "--no-header", "-"],
        input=requirement + "\n",
        capture_output=True,
        text=True,
        cwd=elsewhere,
        env=env,
        timeout=600,
    )
    assert compiled.returncode == 0, compiled.stderr[-3000:]
    common_dir = _in_repo_packages()["swarm-common"].relative_to(_REPO.resolve()).as_posix()
    lines = [line.strip() for line in compiled.stdout.splitlines()]
    common = [line for line in lines if _normalise(line.split(" ", 1)[0]) == "swarm-common"]
    assert len(common) == 1, f"swarm-common is not in the resolution:\n{compiled.stdout}"
    assert common[0].startswith(f"swarm-common @ git+{spec['url']}@"), common[0]
    assert _INSTALL_REF in common[0] and f"subdirectory={common_dir}" in common[0], (
        f"swarm-common did not resolve from the bridge's own commit and directory: {common[0]}"
    )
