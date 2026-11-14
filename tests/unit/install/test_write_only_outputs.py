"""No installer step keeps an output nobody reads.

`state.outputs` is the install state's memory between steps and runs: a key
a step writes there that no code of the installer ever reads back is dead
weight in every saved state, and reads as if something depended on it. This
walks the steps that wrote such keys (edge, preflight, identity, verify,
bootstrap, foundations) and the upgrade, collects every literal key they put
in `state.outputs` (an `outputs=` mapping, an Observation's outputs, a
`findings` pair's outputs, an `outputs[...] =` assignment), and requires a
read of each somewhere in `swarm_install` (`.get(key)` or `[key]`).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "apps" / "swarm-install" / "swarm_install"
WRITERS = (
    "steps/edge.py",
    "steps/preflight.py",
    "steps/identity.py",
    "steps/verify.py",
    "steps/bootstrap.py",
    "steps/foundations.py",
    "upgrade.py",
    "verbs/upgrade.py",
    "verbs/install.py",
)

#: Written for a reader outside these steps that does not read it yet; an
#: entry is dropped as soon as something reads the key.
NOT_READ_YET: set[str] = set()


def _trees() -> dict[str, ast.Module]:
    return {str(path.relative_to(PACKAGE)): ast.parse(path.read_text()) for path in sorted(PACKAGE.rglob("*.py"))}


def _constants(trees: dict[str, ast.Module]) -> dict[str, str]:
    """Every module-level `NAME = "literal"` of the package, by name."""
    found: dict[str, str] = {}
    for tree in trees.values():
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                found[node.targets[0].id] = node.value.value
    return found


def _key(node: ast.AST | None, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute):
        return constants.get(node.attr)
    return None


def _dict_keys(node: ast.AST, constants: dict[str, str]) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    return [key for key in (_key(k, constants) for k in node.keys) if key is not None]


def written(trees: dict[str, ast.Module], constants: dict[str, str]) -> dict[str, str]:
    """key -> the first place a writer puts it in `state.outputs`."""
    found: dict[str, str] = {}

    def note(keys: list[str], where: str) -> None:
        for key in keys:
            found.setdefault(key, where)

    for name in WRITERS:
        for node in ast.walk(trees[name]):
            where = f"{name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.keyword) and node.arg == "outputs":
                note(_dict_keys(node.value, constants), where)
            elif isinstance(node, ast.Call) and ast.unparse(node.func).endswith("Observation"):
                for arg in node.args[2:]:
                    note(_dict_keys(arg, constants), where)
            elif isinstance(node, ast.FunctionDef) and node.name in ("findings", "binding"):
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Tuple) and len(ret.value.elts) == 2:
                        note(_dict_keys(ret.value.elts[1], constants), f"{name}:{ret.lineno}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript) and "outputs" in ast.unparse(target.value)):
                        note([k for k in [_key(target.slice, constants)] if k], where)
    return found


def read(trees: dict[str, ast.Module], constants: dict[str, str]) -> set[str]:
    """Every key the package reads: `x.get(key)`, `x[key]` in a load, `key in x`."""
    found: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                found.update(k for k in [_key(node.args[0] if node.args else None, constants)] if k)
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                found.update(k for k in [_key(node.slice, constants)] if k)
            elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                found.update(k for k in [_key(node.left, constants)] if k)
            elif isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                # `for key in (A, B): outputs.get(key)` reads each of them.
                found.update(k for elt in node.iter.elts for k in [_key(elt, constants)] if k)
    return found


def test_the_walk_finds_the_writers_keys():
    trees = _trees()
    constants = _constants(trees)
    keys = written(trees, constants)
    # A sample of keys each kind of write puts there, so the walk cannot pass by finding nothing.
    assert {"verify_runs", "global_ip", "sizing", "workspace_groups", "budget_amount", "upgrade"} <= set(keys)
    assert len(keys) >= 30


@pytest.mark.parametrize("key", sorted(written(_trees(), _constants(_trees()))))
def test_every_output_a_step_writes_is_read(key):
    trees = _trees()
    constants = _constants(trees)
    where = written(trees, constants)[key]
    if key in NOT_READ_YET:
        # MUTATION: a reader appears and the exemption stays, hiding the next write-only key under it.
        assert key not in read(trees, constants), f"{key!r} is read now: drop it from NOT_READ_YET"
        return
    assert key in read(trees, constants), f"{where} writes state.outputs[{key!r}] and nothing reads it"
