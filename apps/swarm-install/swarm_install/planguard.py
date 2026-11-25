"""The wrong-project check, run by the engine over every plan it makes (issue 344).

The rule is not written here. It is `scripts/lib/wrong-project.jq`, the same
file the operator scripts' guard includes (`scripts/lib/destroy-guard.jq`, read
by `scripts/lib/plan-guard.sh`): one rule, two readers. The wheel carries it as
`swarm_install/_guard/wrong-project.jq` (apps/swarm-install/pyproject.toml);
a checkout reads it from `scripts/lib/`.

This module runs that file with jq over a plan's `resource_changes` and turns
the verdict into a refusal that names each resource and the project it names,
beside the install's own. It fails closed: no jq, a jq that exits non-zero or
prints something that is not the verdict, and the plan is refused, because a
check that did not run is not evidence the plan stays in the project.

Callers (`terraform.run_pass`, `uninstall.destroy_root` and its unprotect
pass, `upgrade.Upgrade.plan` and its infra re-plan) call `refusal` right after
planning and raise their own refusal before any apply.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The rule file's name, in the wheel's `_guard/` and in the checkout's `scripts/lib/`.
RULE_FILE = "wrong-project.jq"
PACKAGED_DIR = "_guard"
#: The checkout's copy (development and the unit tests).
CHECKOUT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "lib"
#: The program jq runs: the shared rule over the plan's changes, nothing else.
PROGRAM = 'include "wrong-project"; plan_wrong_project($project; [.resource_changes[]?])'
JQ_TIMEOUT_SECONDS = 120


def rule_dir() -> Path:
    """The directory holding `wrong-project.jq`: the wheel's copy, else the checkout's."""
    packaged = importlib.resources.files(__package__ or "swarm_install") / PACKAGED_DIR
    if packaged.joinpath(RULE_FILE).is_file():
        return Path(str(packaged))
    return CHECKOUT_DIR


def jq_binary() -> str:
    """The jq to run: the one on PATH (the engine puts its pinned jq's directory
    first, `tools.use_bin_dir`), else the pinned jq, fetched and checked."""
    found = shutil.which("jq")
    if found:
        return found
    from . import tools

    return str(tools.ensure("jq"))


class Unchecked(RuntimeError):
    """The rule could not be run, so the plan is not known to stay in the project."""


def wrong_project(plan: Mapping[str, Any], project: str) -> list[dict[str, Any]]:
    """`[{address, type, project}]`: each change of `plan` that names a project
    other than `project`, by the shared rule. Raises `Unchecked` when jq could
    not give a verdict."""
    directory = rule_dir()
    if not directory.joinpath(RULE_FILE).is_file():
        raise Unchecked(f"the rule file {RULE_FILE} is not in {directory}")
    try:
        binary = jq_binary()
        done = subprocess.run([binary, "-c", "-L", str(directory), "--arg", "project", project, PROGRAM],
                              input=json.dumps(dict(plan)), capture_output=True, text=True,
                              timeout=JQ_TIMEOUT_SECONDS, check=False)
    except Exception as exc:
        raise Unchecked(f"jq could not be run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        raise Unchecked(f"jq exited {done.returncode}: {done.stderr.strip()[-300:]}")
    try:
        verdict = json.loads(done.stdout)
    except ValueError as exc:
        raise Unchecked("jq printed no verdict") from exc
    if not isinstance(verdict, list) or not all(isinstance(v, dict) for v in verdict):
        raise Unchecked("jq printed something that is not the verdict")
    return verdict


def refusal(plan: Mapping[str, Any], project: str, what: str) -> str | None:
    """The refusal for `plan`, or None when it names no project but `project`.

    `what` says which plan it is (a pass, a root's destroy). The message names
    every offending resource, the project it names and the install's project."""
    try:
        found = wrong_project(plan, project)
    except Unchecked as exc:
        return (f"{what}: the plan could not be checked for resources in a project other than {project} "
                f"({exc}); nothing was applied")
    if not found:
        return None
    named = "; ".join(f"{f.get('address')} names project {f.get('project')}" for f in found)
    return (f"{what} would change resources outside the install's project {project}: {named}. "
            "The install touches only its own project; nothing was applied")
