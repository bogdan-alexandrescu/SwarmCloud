"""The `swarm` verbs the installer adds (design §2.1), discovered, never listed.

Every module in this package that defines `NAME` is a verb (`install`,
`uninstall`, `upgrade`, `team`, `addon`); each lane adds its own module, so no
two lanes edit a shared registry (plan §1 conventions). A verb module defines:

* `NAME: str` -- the subcommand, e.g. `"install"`;
* `HELP: str` -- one line for `swarm --help`;
* `add_arguments(parser)` -- its flags on an `argparse` subparser;
* `run(args) -> int` -- an `swarm_install.exitcodes.ExitCode`.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

REQUIRED = ("NAME", "HELP", "add_arguments", "run")


def discover(package: str = __name__) -> dict[str, ModuleType]:
    """Every verb module of `package`, by verb name."""
    root = importlib.import_module(package)
    verbs: dict[str, ModuleType] = {}
    for info in sorted(pkgutil.iter_modules(root.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{package}.{info.name}")
        if not hasattr(module, "NAME"):
            continue
        missing = [name for name in REQUIRED if not hasattr(module, name)]
        if missing:
            raise ValueError(f"verb module {module.__name__} lacks {missing}")
        if module.NAME in verbs:
            raise ValueError(f"two verb modules are named {module.NAME!r}")
        verbs[module.NAME] = module
    return verbs
