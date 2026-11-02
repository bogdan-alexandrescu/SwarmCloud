"""The installer's face on the `swarm` command (docs/design/day-one-install.md §2.1).

`swarm install`, `swarm uninstall` and the rest are spelled on the existing
`swarm` command (`apps/swarm-mcp/swarm_mcp/cli.py`), which routes them here on
their first word and imports this package only then: a teammate's `swarm`,
which has no engine, never loads the Google clients (plan R6).

The verbs are **discovered, never listed** (`swarm_install.verbs.discover`):
this parser is built from every verb module the package holds, so a lane that
adds a verb adds one module and edits nothing here.

Exit codes are `swarm_install.exitcodes.ExitCode`; argparse's own usage error
is already 2 (`ExitCode.USAGE`).
"""

from __future__ import annotations

import argparse
import sys
from types import ModuleType

from . import verbs as _verbs


def build_parser(found: dict[str, ModuleType]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm",
        description="Install and run a SwarmCloud deployment in your own Google Cloud project.",
    )
    sub = parser.add_subparsers(dest="verb", required=True, metavar="VERB")
    for name, module in found.items():
        verb = sub.add_parser(name, help=module.HELP, description=module.HELP)
        module.add_arguments(verb)
        verb.set_defaults(_run=module.run)
    return parser


def main(argv: list[str] | None = None, *, package: str = _verbs.__name__) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser(_verbs.discover(package)).parse_args(raw)
    try:
        return int(args._run(args))
    except KeyboardInterrupt:
        return 130
