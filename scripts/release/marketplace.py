#!/usr/bin/env python3
"""The plugin marketplace and the plugin bundle of a SwarmCloud release
(DAYONE-RELEASE-1; docs/design/day-one-plan.md §6.4-§6.5, D28).

    marketplace.py plugin --dir <copy of plugin/> --version V --compat install/compat.json
    marketplace.py marketplace --version V --source ./plugin --out marketplace.json
    marketplace.py compat --version V --compat install/compat.json --out compat.json
    marketplace.py wheel-version V

A teammate types the D28 line, `claude plugin marketplace add
https://storage.googleapis.com/swarmcloud/marketplace.json`, then installs
`sc@swarmcloud`. So the marketplace is named `swarmcloud` and lists one plugin,
`sc`. The root marketplace (written by `publish.sh --promote`) points at
`./releases/V/plugin`; each release also carries its own pinned marketplace,
`releases/V/marketplace.json`, pointing at `./plugin`, for a team that must
stay on V (the skew message names it, swarm_mcp/discovery.py).

`plugin` turns a copy of `plugin/` into the release's plugin:

* `.claude-plugin/plugin.json` gets the release's version, and its MCP server
  runs the release's OWN bridge wheel from the release home -- never a git ref
  of this repository, which a company's teammate cannot read. The frozen
  contract's wheel from the same release is passed with `--with`, because
  `swarm-common` is not on PyPI: a bare name there is an index lookup today
  (404) and somebody else's code the day the name is registered.
  `SWARM_MCP_FROM` still replaces the bridge, for a developer's working copy.
* `compat.json` is written beside it: the deployment versions this plugin
  supports, which the bridge reads at start (`swarm_mcp.discovery.supported`).

The standard library only: `publish.sh --promote` runs this on the publisher's
laptop with whatever `python3` it has, not inside the uv workspace.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: Where every company reads the release (D25; release-home.sh, preflight.py).
RELEASE_HOME = "https://storage.googleapis.com/swarmcloud"
MARKETPLACE_NAME = "swarmcloud"
PLUGIN_NAME = "sc"
PLUGIN_DESCRIPTION = (
    "SwarmCloud from inside a Claude Code session: read your deployment, dispatch work to it by "
    "runner profile, and follow it as it runs."
)

#: A release name: `YYYY.MM.N`, or a candidate `YYYY.MM.N-rc.N` (plan §7.2-§7.3).
RELEASE_NAME = re.compile(r"\A(\d{4})\.(\d{2})\.(\d+)(?:-rc\.(\d+))?\Z")


def check_version(version: str) -> str:
    if not RELEASE_NAME.match(version or ""):
        raise ValueError(f"{version!r} is not a release name (YYYY.MM.N or YYYY.MM.N-rc.N)")
    return version


def wheel_version(version: str) -> str:
    """The PEP 440 form a wheel's file name carries: `2026.01.2` builds
    `2026.1.2`, `2026.11.3-rc.2` builds `2026.11.3rc2`."""
    match = RELEASE_NAME.match(check_version(version))
    assert match is not None
    year, month, n, rc = match.groups()
    base = f"{int(year)}.{int(month)}.{int(n)}"
    return base if rc is None else f"{base}rc{int(rc)}"


def wheel_url(distribution: str, version: str) -> str:
    """`swarm_mcp` -> the release's `swarm_mcp-<V>-py3-none-any.whl`."""
    return f"{RELEASE_HOME}/releases/{version}/{distribution}-{wheel_version(version)}-py3-none-any.whl"


def _key(version: str) -> tuple[tuple[int, ...], int, int]:
    """Release order: the numbers, then a candidate before its release."""
    match = RELEASE_NAME.match(check_version(version))
    assert match is not None
    year, month, n, rc = match.groups()
    return (int(year), int(month), int(n)), (0 if rc is not None else 1), int(rc or 0)


def compat(version: str, source: Path) -> dict:
    """The plugin's supported deployment range for release `version`, from
    `install/compat.json`. A bound is a release name or "" (open). A range that
    leaves out the release's own deployment is refused: a plugin always talks
    to the deployment of its own release."""
    check_version(version)
    data = json.loads(Path(source).read_text())
    deployments = data.get("deployments") if isinstance(data, dict) else None
    if not isinstance(deployments, dict) or set(deployments) != {"min", "max_exclusive"}:
        raise ValueError(f"{source}: `deployments` must hold exactly `min` and `max_exclusive`")
    low = str(deployments["min"] or "").strip()
    high = str(deployments["max_exclusive"] or "").strip()
    if low and _key(low) > _key(version):
        raise ValueError(f"{source}: min {low} is after release {version}, so its own deployment is out of range")
    if high and _key(high) <= _key(version):
        raise ValueError(f"{source}: max_exclusive {high} is not after release {version}, "
                         "so its own deployment is out of range")
    return {"plugin": version, "deployments": {"min": low, "max_exclusive": high}}


def marketplace(version: str, source: str) -> dict:
    """The marketplace document: one plugin, `sc`, at `version`, from `source`
    (a path relative to the marketplace file)."""
    check_version(version)
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "SwarmCloud"},
        "metadata": {"description": "The SwarmCloud plugin for Claude Code.", "version": version},
        "plugins": [{
            "name": PLUGIN_NAME,
            "source": source,
            "version": version,
            "description": PLUGIN_DESCRIPTION,
        }],
    }


def bridge_args(version: str) -> list[str]:
    """The MCP server's `uv` arguments: the release's bridge wheel, with the
    release's contract wheel beside it."""
    bridge = f"swarm-mcp @ {wheel_url('swarm_mcp', version)}"
    common = f"swarm-common @ {wheel_url('swarm_common', version)}"
    return ["tool", "run", "--from", f"${{SWARM_MCP_FROM:-{bridge}}}", "--with", common, "swarm-mcp"]


def prepare_plugin(directory: Path, version: str, compat_source: Path) -> None:
    """Make a copy of `plugin/` the release's plugin (see the module docstring)."""
    directory = Path(directory)
    manifest_path = directory / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("name") != PLUGIN_NAME:
        raise ValueError(f"{manifest_path}: the plugin is {manifest.get('name')!r}, not {PLUGIN_NAME!r}")
    servers = manifest.get("mcpServers") or {}
    if "swarmcloud" not in servers:
        raise ValueError(f"{manifest_path}: no `swarmcloud` MCP server to point at the release's bridge")
    manifest["version"] = check_version(version)
    servers["swarmcloud"]["args"] = bridge_args(version)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (directory / "compat.json").write_text(json.dumps(compat(version, compat_source), indent=2) + "\n")


def _write(path: str, document: dict) -> None:
    Path(path).write_text(json.dumps(document, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plugin = sub.add_parser("plugin", help="make a copy of plugin/ the release's plugin")
    plugin.add_argument("--dir", required=True)
    plugin.add_argument("--version", required=True)
    plugin.add_argument("--compat", required=True)
    market = sub.add_parser("marketplace", help="write a marketplace document")
    market.add_argument("--version", required=True)
    market.add_argument("--source", required=True)
    market.add_argument("--out", required=True)
    comp = sub.add_parser("compat", help="write the release's compat.json")
    comp.add_argument("--version", required=True)
    comp.add_argument("--compat", required=True)
    comp.add_argument("--out", required=True)
    wheel = sub.add_parser("wheel-version", help="print the wheel file name's form of a release name")
    wheel.add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "plugin":
            prepare_plugin(Path(args.dir), args.version, Path(args.compat))
        elif args.command == "marketplace":
            _write(args.out, marketplace(args.version, args.source))
        elif args.command == "compat":
            _write(args.out, compat(args.version, Path(args.compat)))
        else:
            print(wheel_version(args.version))
    except (OSError, ValueError) as exc:
        print(f"marketplace.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
