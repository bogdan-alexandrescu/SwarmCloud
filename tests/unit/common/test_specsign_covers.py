"""The rule that keeps the signed field list honest (contract request 34).

"A metadata key the worker starts reading is added to SIGNED_METADATA_KEYS in
the same change, with a format bump." A worker that acts on an unsigned key
acts on something any agent of the tenant can write, so this test finds every
metadata key the worker reads -- by an AST scan of `agent_worker`, not by a
list restated here -- and holds each one inside the signature. It also holds
the signed keys to swarm-api's reserved ones: every key the platform writes
into `metadata` is signed, except the reconciler's `startup_refunds`, which
changes after submission by design.
"""

from __future__ import annotations

import ast
from pathlib import Path

from swarm_common.specsign import SIGNED_METADATA_KEYS

import agent_worker
from agent_worker import expected_outputs, inputs

WORKER = Path(agent_worker.__file__).resolve().parent


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _key_of(node: ast.expr, constants: dict[str, str], imported: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return imported.get(f"{node.value.id}.{node.attr}")
    return None


def _reads_metadata(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id == "metadata"
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "metadata"
    # task.get("metadata").get(...) / (task.get("metadata") or {}).get(...)
    if isinstance(receiver, ast.BoolOp):
        return any(_reads_metadata(v) for v in receiver.values)
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute):
        return (
            receiver.func.attr == "get"
            and bool(receiver.args)
            and isinstance(receiver.args[0], ast.Constant)
            and receiver.args[0].value == "metadata"
        )
    return False


def worker_metadata_keys() -> set[str]:
    imported = {
        "inputs_mod.METADATA_KEY": inputs.METADATA_KEY,
        "inputs.METADATA_KEY": inputs.METADATA_KEY,
        "expected_mod.METADATA_KEY": expected_outputs.METADATA_KEY,
        "expected_outputs.METADATA_KEY": expected_outputs.METADATA_KEY,
    }
    found: set[str] = set()
    for path in sorted(WORKER.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            receiver = key = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "get" and node.args:
                    receiver, key = node.func.value, node.args[0]
            elif isinstance(node, ast.Subscript):
                receiver, key = node.value, node.slice
            if receiver is None or not _reads_metadata(receiver):
                continue
            name = _key_of(key, constants, imported)
            if name is not None and name != "metadata":
                found.add(name)
    return found


def test_the_scan_sees_the_keys_the_worker_is_known_to_read():
    """Proof the scan RAN over real code, not an empty walk."""
    keys = worker_metadata_keys()
    assert {inputs.METADATA_KEY, expected_outputs.METADATA_KEY, "dispatch"} <= keys, keys


def test_every_metadata_key_the_worker_reads_is_signed():
    # The two child counters/markers the worker reads are written after
    # submission by design, so they are named here and nowhere else.
    unsigned = worker_metadata_keys() - set(SIGNED_METADATA_KEYS) - {
        "child_await_resumes",
        "child_cascade",
    }
    assert not unsigned, (
        f"agent_worker reads metadata keys {sorted(unsigned)} that the step-spec signature "
        "does not cover. Add them to swarm_common.specsign.SIGNED_METADATA_KEYS with a "
        "SPEC_FORMAT bump (contract request 34, section 1)."
    )


#: The platform's metadata keys that change AFTER submission, or that no
#: worker acts on, and so cannot be inside a signature made at submission:
#: the reconciler's refund counter, the worker's await-refund counter and the
#: cascade marker (written on a running child), and the child's request id,
#: which only swarm-api's dedupe reads (docs/design/child-tasks.md §6.4).
UNSIGNED_PLATFORM_KEYS = {
    "startup_refunds",
    "child_await_resumes",
    "child_cascade",
    "child_request_id",
}


def test_the_signed_keys_are_swarm_apis_reserved_keys_but_the_counters():
    from swarm_api.validation import (
        CHILD_AWAIT_RESUMES_METADATA_KEY,
        CHILD_CASCADE_METADATA_KEY,
        CHILD_REQUEST_ID_METADATA_KEY,
        RESERVED_METADATA_KEYS,
        STARTUP_REFUNDS_METADATA_KEY,
    )

    assert set(SIGNED_METADATA_KEYS) == set(RESERVED_METADATA_KEYS) - UNSIGNED_PLATFORM_KEYS
    assert {
        STARTUP_REFUNDS_METADATA_KEY,
        CHILD_AWAIT_RESUMES_METADATA_KEY,
        CHILD_CASCADE_METADATA_KEY,
        CHILD_REQUEST_ID_METADATA_KEY,
    } == UNSIGNED_PLATFORM_KEYS
