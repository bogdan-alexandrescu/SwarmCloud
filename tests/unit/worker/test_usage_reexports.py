# source-text: forbidden-pattern - lifecycle.py may keep no `NAME = <mod>_mod.NAME` binding that nothing reads; it stays a test because a binding is read when ANY file in apps/ or tests/ reads it, and a rule sees one file at a time
"""`lifecycle.py` binds only the moved names something still reads (issue 193).

The split of issue 68 moved the heartbeat, checkpointing and usage helpers out
of `lifecycle.py` and left a binding of each behind. The moved modules read
their own copies, so `monkeypatch.setattr(lifecycle, "HEARTBEAT_EVENT_EVERY",
...)` succeeded and changed nothing; two tests had to be re-aimed. A binding
nothing reads is dropped, so a stale patch of it raises `AttributeError`, and
the guard below fails on a new one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agent_worker import checkpointing, heartbeat, lifecycle, usage

REPO = Path(__file__).resolve().parents[3]
LIFECYCLE = REPO / "apps" / "agent-worker" / "agent_worker" / "lifecycle.py"

#: (owning module, name): each lives in its module and is no longer bound in
#: `lifecycle`.
DROPPED = [
    (heartbeat, "HEARTBEAT_EVENT_EVERY"),
    (checkpointing, "_KEY_SEGMENT_RE"),
    (usage, "_USAGE_KEYS"),
    (usage, "_SUMMED_SPEND"),
    (usage, "_SESSION_SPEND"),
    (usage, "_ESTIMATE_READ_LIMIT"),
    (usage, "_has_usage_keys"),
    (usage, "MODEL_USAGE_KEY"),
    (usage, "SUBAGENT_COST_KEY"),
    (usage, "_MODEL_USAGE_FIELDS"),
    (usage, "_TOKEN_WEIGHTS"),
    (usage, "_is_number"),
    (usage, "_model_usage_rows"),
    (usage, "_weighted_tokens"),
    (usage, "_subagent_cost_usd"),
    (usage, "_add_model_usage"),
    # Read only by two tests through `lifecycle`, now through `usage`.
    (usage, "_UNNAMED_MODEL"),
    (usage, "_PROMPT_BUCKET_TOKENS"),
    (usage, "_MAX_PROMPT_BUCKETS"),
    (usage, "_BUCKET_FIELDS"),
]


@pytest.mark.parametrize(("module", "name"), DROPPED, ids=[n for _, n in DROPPED])
def test_dropped_reexport_is_not_bound_in_lifecycle(module, name):
    assert hasattr(module, name), f"{module.__name__} no longer defines {name}"
    assert not hasattr(lifecycle, name), (
        f"lifecycle.{name} is bound again; read {module.__name__}.{name}"
    )


def _reexports(tree: ast.Module) -> list[str]:
    """Each module-level `NAME = <module>_mod.NAME` in `lifecycle.py`."""
    names = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id.endswith("_mod")
            and value.attr == target.id
        ):
            names.append(target.id)
    return names


def _read_in_lifecycle(tree: ast.Module) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _read_through_lifecycle(names: list[str]) -> set[str]:
    """Names read as `lifecycle*.NAME` or imported from `lifecycle` elsewhere.

    Comments and docstrings that cite a name are not reads: only the syntax
    tree counts. The text filter keeps the parse to the few files that could.
    """
    wanted = set(names)
    word = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    found: set[str] = set()
    for root in (REPO / "apps", REPO / "tests"):
        for path in root.rglob("*.py"):
            if path == LIFECYCLE or path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "lifecycle" not in text or not word.search(text):
                continue
            for node in ast.walk(ast.parse(text, filename=str(path))):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in wanted
                    and isinstance(node.value, ast.Name)
                    and "lifecycle" in node.value.id
                ):
                    found.add(node.attr)
                elif isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "lifecycle"
                ):
                    found.update(a.name for a in node.names if a.name in wanted)
    return found


def test_every_reexport_in_lifecycle_is_read():
    tree = ast.parse(LIFECYCLE.read_text(encoding="utf-8"))
    names = _reexports(tree)
    assert names, "found no `NAME = <module>_mod.NAME` binding; the guard reads nothing"
    unread = set(names) - _read_in_lifecycle(tree) - _read_through_lifecycle(names)
    assert not unread, (
        f"lifecycle.py binds {sorted(unread)} from another module and nothing reads "
        "them there; drop the binding and read the owning module"
    )
