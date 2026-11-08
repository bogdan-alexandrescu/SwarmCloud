"""A fake `terraform` that holds the line the real one holds on inputs.

It reads the root's own `variables.tf` (the repository's, copied into the
release directory the pass runs in) and, per pass:

* refuses a key in `swarm-install.auto.tfvars.json` that the root does not
  declare (real Terraform only warns about one in a file, then applies
  without it: the switch the install meant to turn on stays off, seam A1);
* refuses a run that leaves a variable with no default unset, as
  `TF_INPUT=0` does.

`apply` hands the pass's inputs to `World.applied`, where the fake services
make what the pass makes; `output -json` answers from `World.tf_outputs`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .world import World

_VARIABLE = re.compile(r'^variable\s+"([^"]+)"\s*\{', re.MULTILINE)
INPUTS_FILE = "swarm-install.auto.tfvars.json"


def _block(text: str, start: int) -> str:
    """The body of the block opened at `start`: `terraform fmt` (which `make
    lint` holds every root to) closes a top-level block with `}` at column 0."""
    end = text.find("\n}", start)
    if end < 0:
        raise ValueError(f"an unterminated block at offset {start}")
    return text[text.index("{", start) + 1:end]


def declared(root_dir: Path) -> dict[str, bool]:
    """Every variable a root declares, and whether it has a default."""
    found: dict[str, bool] = {}
    for tf in sorted(root_dir.glob("*.tf")):
        text = tf.read_text()
        for match in _VARIABLE.finditer(text):
            body = _block(text, match.start())
            found[match.group(1)] = re.search(r"^\s*default\s*=", body, re.MULTILINE) is not None
    return found


def defaults(root_dir: Path) -> dict[str, str]:
    """Each variable's `default = <literal>` as written, where it is one line."""
    found: dict[str, str] = {}
    for tf in sorted(root_dir.glob("*.tf")):
        text = tf.read_text()
        for match in _VARIABLE.finditer(text):
            default = re.search(r"^  default\s*=\s*(.+)$", _block(text, match.start()), re.MULTILINE)
            if default:
                found[match.group(1)] = default.group(1).strip()
    return found


def check_inputs(root_dir: Path) -> list[str]:
    """What real Terraform would refuse (or silently drop) about the inputs file."""
    inputs_path = root_dir / INPUTS_FILE
    inputs = json.loads(inputs_path.read_text()) if inputs_path.is_file() else {}
    variables = declared(root_dir)
    problems = [f"{root_dir.name}: {key} is not a declared variable" for key in sorted(inputs)
                if key not in variables]
    problems += [f"{root_dir.name}: No value for required variable {name}" for name, has_default
                 in sorted(variables.items()) if not has_default and name not in inputs]
    return problems


def attach(world: World) -> None:
    world.tf_state = {}  # type: ignore[attr-defined]  # root -> resource addresses
    world.tf_outputs = {"bootstrap": {}, "infra": {}}  # type: ignore[attr-defined]
    world.tf_refused = []  # type: ignore[attr-defined]
    world.tf_applies = []  # type: ignore[attr-defined]  # (root, inputs) in order
    if not hasattr(world, "applied"):
        world.applied = lambda root, inputs, env: None  # type: ignore[attr-defined]

    def terraform(words: list[str], env: dict[str, str], cwd: Path | None) -> tuple[int, str, str]:
        assert cwd is not None, "terraform runs in its root"
        root = cwd.name
        verb = words[0] if words else ""
        if verb == "init":
            return 0, "Terraform has been successfully initialized!\n", ""
        if verb == "state" and words[1:2] == ["list"]:
            found = world.tf_state.get(root)  # type: ignore[attr-defined]
            if not found:
                return 1, "", "No state file was found!\n"
            return 0, "\n".join(found) + "\n", ""
        if verb == "plan":
            problems = check_inputs(cwd)
            if problems:
                world.tf_refused.extend(problems)  # type: ignore[attr-defined]
                return 1, "", "Error: " + "\nError: ".join(problems) + "\n"
            (cwd / "swarm-install.tfplan").write_text(json.dumps({"targets": [w for w in words if
                                                                              w.startswith("-target=")]}))
            return 0, "Plan: 0 to add, 0 to change, 0 to destroy.\n", ""
        if verb == "show":
            return 0, json.dumps({"format_version": "1.2", "resource_changes": []}), ""
        if verb == "apply":
            inputs = json.loads((cwd / INPUTS_FILE).read_text())
            targets = json.loads((cwd / "swarm-install.tfplan").read_text()).get("targets", [])
            world.tf_applies.append((root, inputs, targets))  # type: ignore[attr-defined]
            state = world.tf_state.setdefault(root, [])  # type: ignore[attr-defined]
            made = [t.removeprefix("-target=") for t in targets] or [f"{root}.applied"]
            state.extend(a for a in made if a not in state)
            world.applied(root, inputs, env, targets)  # type: ignore[attr-defined]
            return 0, "Apply complete!\n", ""
        if verb == "import":
            address, resource_id = [w for w in words[1:] if not w.startswith("-")][:2]
            if not world.remote_exists(resource_id):  # type: ignore[attr-defined]
                return 1, "", f"Error: Cannot import non-existent remote object ({resource_id})\n"
            state = world.tf_state.setdefault(root, [])  # type: ignore[attr-defined]
            if address in state:
                return 1, "", f"Error: Resource already managed by Terraform ({address})\n"
            state.append(address)
            return 0, "Import successful!\n", ""
        if verb == "output":
            outputs: dict[str, Any] = world.tf_outputs.get(root, {})  # type: ignore[attr-defined]
            return 0, json.dumps({k: {"value": v} for k, v in outputs.items()}), ""
        world.unhandled.append("terraform " + " ".join(words[:3]))
        return 1, "", f"the fake terraform has no `{verb}`"

    world.terraform = terraform  # type: ignore[attr-defined]
