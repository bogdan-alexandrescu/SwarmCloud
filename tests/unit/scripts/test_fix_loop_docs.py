"""The fix loop's docs describe what is built (lane FL-DOCS, docs/design/fix-loop.md §8).

WHY THIS EXISTS. The owner turns the fix loop on once its docs land, and a
person deciding whether to add a round, stop a loop or label a pull request
`ready` reads these four files, not the code. Every bound, name and route they
state is read from the code here, so a change to the round range, the
ceiling, the seven-day park, a phase, a route, a refusal code or the label
fails until the documents follow:

* docs/workflows.md: the loop's own section, and "What this does not do";
* docs/merge-step.md: the merge reads the loop's final review, and the
  `review-not-yet` label;
* docs/issue-runs.md: an issue run carries the loop at its `fix_rounds`;
* plugin/skills/delegate/SKILL.md: how a spec declares or gets a loop, and
  what a parked loop needs.

The code is read as source (ast), never imported, so the test needs no
service package and no cloud credential.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / "docs" / "workflows.md"
MERGE_STEP = REPO / "docs" / "merge-step.md"
ISSUE_RUNS = REPO / "docs" / "issue-runs.md"
DELEGATE = REPO / "plugin" / "skills" / "delegate" / "SKILL.md"

VALIDATION = REPO / "apps" / "swarm-api" / "swarm_api" / "validation.py"
FIXLOOP = REPO / "apps" / "swarm-api" / "swarm_api" / "fixloop.py"
ROUTES = REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "fix_loop.py"
ISSUERUNS = REPO / "apps" / "swarm-api" / "swarm_api" / "issueruns.py"
ISSUECI = REPO / "apps" / "swarm-api" / "swarm_api" / "issueci.py"
SCHEDULER = REPO / "apps" / "scheduler" / "scheduler" / "loop.py"
MERGE = REPO / "apps" / "agent-worker" / "agent_worker" / "merge.py"
MCP_SERVER = REPO / "apps" / "swarm-mcp" / "swarm_mcp" / "server.py"
ACCEPTANCE = REPO / "scripts" / "acceptance" / "groups" / "workflow.sh"
UI = REPO / "apps" / "swarm-ui" / "src" / "WorkflowViews.tsx"

LOOP_HEADING = "## The fix loop: review-fix rounds until a review says MERGE"
MERGE_REVISION = (
    "## Revised 2026-11-09: the merge reads the fix loop's final review, "
    "and a NOT_YET holds the pull request with `review-not-yet`"
)
ISSUE_HEADING = "## The fix loop in an issue run"
DELEGATE_HEADING = "## A fix loop: rounds until a review says MERGE"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(markdown: str) -> str:
    """Prose with its wrapping removed, so a phrase is found wherever the line breaks."""
    return " ".join(markdown.split())


def _section(path: Path, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same or a higher level."""
    text = _text(path)
    assert heading in text, f"{path.name} has no heading {heading!r}"
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[text.index(heading) + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return _flat(rest[: nxt.start()] if nxt else rest)


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
            found[target.id] = _literal(value)
        except ValueError:
            continue
    return found


def _literal(node: ast.expr) -> object:
    """A literal, or a product of literals (`7 * 86_400`, `4 * 1024`)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _literal(node.left), _literal(node.right)
        if isinstance(left, int) and isinstance(right, int):
            return left * right
        raise ValueError("not a product of integers")
    return ast.literal_eval(node)


def _defines(path: Path, name: str) -> bool:
    tree = ast.parse(_text(path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
        for node in ast.walk(tree)
    )


VALID = _constants(VALIDATION)
LOOP = _constants(FIXLOOP)
ROUTE = _constants(ROUTES)
WORKER = _constants(MERGE)
SCHED = _constants(SCHEDULER)


# --------------------------------------------------------------------------
# docs/workflows.md
# --------------------------------------------------------------------------


def test_workflows_has_a_section_on_the_loop_that_states_the_codes_bounds():
    body = _section(WORKFLOWS, LOOP_HEADING)
    low, high = VALID["FIX_LOOP_MIN_ROUNDS"], VALID["FIX_LOOP_MAX_ROUNDS"]
    assert f"`max_rounds` is {low}-{high}" in body, "the range the validator enforces"
    assert f"default {VALID['FIX_LOOP_DEFAULT_ROUNDS']}" in body
    assert f"{LOOP['FIX_LOOP_ROUNDS_CEILING']} rounds in all" in body, "the ceiling added rounds stop at"
    days = SCHED["FIX_LOOP_PARK_MAX_SECONDS"] // 86_400
    assert f"{days} days" in body, "the park's expiry, from the scheduler"
    for key in VALID["FIX_LOOP_KEYS"]:
        assert f"`{key}`" in body, f"the block's key {key!r} is not described"
    for strategy in VALID["FIX_LOOP_STRATEGIES"]:
        assert f"`{strategy}`" in body
    assert f"`runner_profile: \"{VALID['FIX_LOOP_PROFILE']}\"`" in body


def test_workflows_loop_section_names_every_round_step_and_every_phase():
    body = _section(WORKFLOWS, LOOP_HEADING)
    assert f"`{LOOP['RE_REVIEW_STEP']}:<k>`" in body and f"`{LOOP['REVIEW_FIX_STEP']}:<k>`" in body
    for phase in LOOP["PERSON_PHASES"]:
        assert f"`{phase}`" in body, f"the person phase {phase!r} is not described"
    for outside in (LOOP["MERGED_OUTSIDE"], LOOP["CLOSED_OUTSIDE"]):
        assert f"`{outside}`" in body
    assert "holds nothing" in body, "invariant 1: a parked loop costs nothing"


def test_workflows_loop_section_names_the_routes_the_tool_and_the_buttons():
    body = _section(WORKFLOWS, LOOP_HEADING)
    assert f"`POST {ROUTE['ROUNDS_ROUTE']}`" in body
    assert f"`POST {ROUTE['END_ROUTE']}`" in body
    assert f'{{"outcome": "{LOOP["STOP"]}"}}' in body
    assert f"{LOOP['NOTE_MAX_BYTES'] // 1024} KiB" in body
    assert '"name": "swarm_fix_loop"' in _text(MCP_SERVER)
    assert "`swarm_fix_loop`" in body and "`add_round`" in body
    ui = _text(UI)
    for button in ("Add a round", "Stop"):
        assert button in ui, f"the console has no {button!r} button"
        assert f"**{button}**" in body, f"the console's {button!r} button is not described"
    assert "round k of N" in body


def test_workflows_loop_section_states_the_owners_rules_of_9_november_and_why():
    body = _section(WORKFLOWS, LOOP_HEADING)
    assert "9 November 2026" in body
    assert "a merge behind a loop reads only the loop" in body, "the security-review rule's reason"
    assert "`fix_rounds`" in body, "issue runs carry the loop at their fix_rounds"
    assert "contract request 151" in body, "the request that makes the runner type available"
    assert "fix-loop" in _text(ACCEPTANCE)
    assert "`scripts/acceptance/groups/workflow.sh`" in body
    for symbol in ("verdict_fix_loop_step", "is_security_review", "resolve_fix_loop"):
        assert _defines(VALIDATION, symbol), symbol
        assert f"`validation.{symbol}`" in body, f"cite {symbol} by name"
    for symbol in ("on_task_finished", "sweep_tenant", "add_rounds", "stop"):
        assert _defines(FIXLOOP, symbol), symbol
        assert f"`fixloop.{symbol}`" in body, f"cite {symbol} by name"


def test_what_this_does_not_do_no_longer_says_the_fix_is_never_reviewed():
    body = _section(WORKFLOWS, "### What this does not do")
    assert "It does not review the fix." not in body, "the loop reviews it"
    assert "(#the-fix-loop-review-fix-rounds-until-a-review-says-merge)" in body


# --------------------------------------------------------------------------
# docs/merge-step.md
# --------------------------------------------------------------------------


def test_merge_step_revision_is_listed_and_leads_the_revisions():
    text = _text(MERGE_STEP)
    assert MERGE_REVISION in text
    assert text.index(MERGE_REVISION) < text.index("## Revised 2026-11-05"), "the newest revision comes first"
    status = text[: text.index("## Revised")]
    assert "**Revised 2026-11-09" in status, "the status lines list every revision"


def test_merge_step_revision_names_the_loop_target_and_every_refusal():
    body = _section(MERGE_STEP, MERGE_REVISION)
    assert "`review_loop`" in body and "`final_review`" in body
    codes = [v for k, v in WORKER.items() if k.startswith("LOOP_") and isinstance(v, str)]
    assert len(codes) >= 4, codes
    for code in codes:
        assert f"`{code}`" in body, f"the merge's loop refusal {code!r} is not described"


def test_merge_step_revision_describes_the_review_not_yet_label():
    body = _section(MERGE_STEP, MERGE_REVISION)
    label, ready = WORKER["REVIEW_NOT_YET_LABEL"], WORKER["REPOSITORY_TRAIN_LABEL"]
    assert f"`{label}`" in body and f"`{ready}`" in body
    assert "auto-merge" in body and "findings" in body
    assert "`verdict_not_merge`" in body
    assert "`scripts/check-closing-references.sh`" in body, "where `ready` is refused"
    assert f"REVIEW_NOT_YET={label}" in _text(REPO / "scripts" / "check-closing-references.sh")
    for symbol in ("_hold_not_yet", "_clear_not_yet", "_loop_review"):
        assert _defines(MERGE, symbol), symbol
        assert f"`merge.{symbol}`" in body, f"cite {symbol} by name"
    assert "Why" in body


# --------------------------------------------------------------------------
# docs/issue-runs.md
# --------------------------------------------------------------------------


def test_issue_runs_no_longer_says_one_round_is_all_that_compiles():
    assert "only **one** is compiled today" not in _flat(_text(ISSUE_RUNS))


def test_issue_runs_describes_the_loop_at_its_fix_rounds():
    body = _section(ISSUE_RUNS, ISSUE_HEADING)
    assert "`max_rounds`" in body and "`fix_rounds`" in body
    assert "security review" in body
    for path, symbol in ((ISSUERUNS, "_with_review_rounds"), (ISSUECI, "_loop_final_review")):
        assert _defines(path, symbol), symbol
        assert f"`{path.stem}.{symbol}`" in body, f"cite {symbol} by name"
    assert "`Closes #N`" in body
    assert "Why" in body


# --------------------------------------------------------------------------
# plugin/skills/delegate/SKILL.md
# --------------------------------------------------------------------------


def test_delegate_says_how_a_spec_gets_a_loop_and_what_a_parked_one_needs():
    body = _section(DELEGATE, DELEGATE_HEADING)
    assert '"runner_profile": "fix-loop"' in body and '"fix_loop"' in body
    assert "`on_merge_verdict`" in body
    assert "`max_rounds`" in body
    assert "`swarm_fix_loop`" in body and '"add_round"' in body and '"stop"' in body
    assert "developer's word" in body, "the tool changes state"
    assert f"{LOOP['FIX_LOOP_ROUNDS_CEILING']} rounds" in body
