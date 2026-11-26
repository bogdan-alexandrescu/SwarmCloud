"""Constants groups: the API's codes and enums, written for the console as JSON.

`scripts/gen_api_types.py` writes, beside the console's generated API types,
one file per group here: `apps/swarm-ui/src/generated/constants/<group>.json`.
A value the console must agree with -- a state, a reason code, a tier name --
is read from the live Python object that defines it, so a member added in
Python reaches the console on the next regeneration, and `--check` (CI, and
the worker's pre-publish gate) fails until it does. A hand-copied list drifts
silently instead.

A GROUP is a module `scripts/api_constants/<group>.py` with:

  * `INPUTS`: a tuple of repository paths its output is a function of (a
    directory ends in "/"). `--inputs` prints them, the pre-publish gate's
    trigger, with the standard library only, so
  * the module imports only the standard library at module level; application
    code is imported INSIDE `build()`;
  * `build()`: returns a JSON-serialisable dict, read from live objects (enum
    members, constants, a function's return value), never from source text.

Groups are discovered here, in sorted order; adding a module adds a group.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

PACKAGE_DIR = Path(__file__).resolve().parent


def group_names() -> list[str]:
    """Every group module's name, sorted. A module starting with `_` is not one."""
    return sorted(p.stem for p in PACKAGE_DIR.glob("*.py") if not p.name.startswith("_"))


def groups() -> list[tuple[str, ModuleType]]:
    """(name, module) for every group, sorted by name. Importing a group runs
    nothing but its module level, which imports the standard library alone."""
    return [(name, importlib.import_module(f"{__name__}.{name}")) for name in group_names()]
