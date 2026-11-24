# source-text: contract - the fix loop's bounds and mirrored literals must agree across validation.py, fixloop.py, the scheduler's loop.py, merge.py and the swarm-mcp server
"""The fix loop's bounds and mirrored literals agree across the modules that
each keep their own copy (lane FL-DOCS, slice 1 of issue #71).

The code is read as source (ast), never imported, so the test needs no
service package and no cloud credential.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

VALIDATION = REPO / "apps" / "swarm-api" / "swarm_api" / "validation.py"
FIXLOOP = REPO / "apps" / "swarm-api" / "swarm_api" / "fixloop.py"
SCHEDULER = REPO / "apps" / "scheduler" / "scheduler" / "loop.py"
MERGE = REPO / "apps" / "agent-worker" / "agent_worker" / "merge.py"
MCP_SERVER = REPO / "apps" / "swarm-mcp" / "swarm_mcp" / "server.py"
CLOSING_REFERENCES = REPO / "scripts" / "check-closing-references.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _constants(path: Path) -> dict[str, object]:
    """Every module-level `NAME = <literal or simple arithmetic>` in `path`."""
    tree = ast.parse(_text(path))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            found[target.id] = _literal(value, found)
        except ValueError:
            continue
    return found


def _literal(node: ast.expr, known: dict[str, object]) -> object:
    """A literal, a product of literals (`7 * 86_400`), or a tuple of names
    already bound to literals (`PERSON_PHASES = (EXHAUSTED, ...)`)."""
    if isinstance(node, ast.Name) and node.id in known:
        return known[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_literal(item, known) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _literal(node.left, known), _literal(node.right, known)
        if isinstance(left, int) and isinstance(right, int):
            return left * right
        raise ValueError("not a product of integers")
    return ast.literal_eval(node)


VALID = _constants(VALIDATION)
LOOP = _constants(FIXLOOP)
WORKER = _constants(MERGE)
SCHED = _constants(SCHEDULER)
MCP = _constants(MCP_SERVER)


def test_fix_loop_round_bounds_nest_min_default_max_ceiling():
    """validation.py clamps a requested max_rounds to [MIN, MAX] and fixloop.py's
    add_rounds then caps the running total at ROUNDS_CEILING: the chain only ever
    binds if MIN <= DEFAULT <= MAX <= CEILING."""
    low, default, high = VALID["FIX_LOOP_MIN_ROUNDS"], VALID["FIX_LOOP_DEFAULT_ROUNDS"], VALID["FIX_LOOP_MAX_ROUNDS"]
    ceiling = LOOP["FIX_LOOP_ROUNDS_CEILING"]
    # MUTATION: move DEFAULT outside [MIN, MAX], or MAX above CEILING -> red.
    assert low <= default <= high <= ceiling


def test_fix_loop_profile_name_matches_between_swarm_api_and_the_scheduler():
    """swarm-api's validation.py and the scheduler's loop.py each define their own
    FIX_LOOP_PROFILE literal (the scheduler does not import swarm-api); a drift
    between them would route the control-plane profile to the wrong admission path."""
    # MUTATION: rename one copy without the other -> red.
    assert VALID["FIX_LOOP_PROFILE"] == SCHED["FIX_LOOP_PROFILE"]


def test_fix_loop_note_byte_limit_matches_the_mcp_bridges_mirrored_copy():
    """swarm_mcp/server.py keeps its own FIX_LOOP_NOTE_MAX_BYTES because swarm-api is
    not installed with the bridge (its own comment says so); it must still equal
    fixloop.NOTE_MAX_BYTES or the bridge accepts a note the API then refuses."""
    # MUTATION: change either copy alone -> red.
    assert LOOP["NOTE_MAX_BYTES"] == MCP["FIX_LOOP_NOTE_MAX_BYTES"]


def test_the_review_not_yet_label_is_the_same_string_in_the_script_and_the_worker():
    """merge.py's REVIEW_NOT_YET_LABEL and check-closing-references.sh's REVIEW_NOT_YET
    are two independently maintained copies of the same label; a drift would let the
    worker label a pull request the script never recognises as held."""
    label = WORKER["REVIEW_NOT_YET_LABEL"]
    # MUTATION: change the label in either file alone -> red.
    assert f"REVIEW_NOT_YET={label}" in _text(CLOSING_REFERENCES)
