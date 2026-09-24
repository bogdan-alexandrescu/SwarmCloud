"""The workflow graph, and the one state its heading is allowed to print.

Two findings from the 2026-09-22 audit of the live deployment, and the open
question that sat between them.

  V1  The view could not express a FAN-IN. `wf_5e5ad3b6f7da4299a839` -- five
      independent steps and a sixth depending on all five -- drew as five boxes
      in a row, one "THEN" bar, then the join: the identical bar a strictly
      linear workflow draws between two sequential steps. There were no edges,
      so a fan-in of five and a chain of five rendered alike, and the line that
      would have disambiguated it was ellipsed to
      "= cold-start, fencing, allorn...".

  V2  The heading read "WF_... - QUEUED - UPDATED 2H AGO" over step chips
      reading succeeded, failed and cancelled. Same card, same typeface, nothing
      saying which to believe.

  W6  And the question that had to be settled BEFORE V2's fix could be built:
      `routes/workflows.py` says "EVERY READ DERIVES", yet the live GET returned
      `state: "QUEUED"` for a workflow with five dispatched steps and carried no
      `state_source` at all. Either the docstring lied or the deployment was old.

THE ANSWER IS BELOW, IN TEST FORM. `test_a_fan_in_read_derives_its_state_...`
drives the real route over the real rollup and shows the derivation running: the
derive path IS wired, and the live payload was an API deployed before
`rollup.py` existed (commit 8358d35, which is not an ancestor of `main`). That
makes it a deployment lag rather than a server defect -- and it makes the UI's
handling of that payload a real requirement rather than a defensive flourish,
because the fleet serves it today. `workflowHeaderState` is pinned here for it.

The UI half is read as TEXT. There is no JavaScript runtime in this suite --
`apps/swarm-ui` has no test runner and `make test` is offline, so nothing here
may install one -- and the same technique is already how
`test_dispatch_ui_surface.py` and `test_blocker_ui_surface.py` hold their
screens. A mock of the UI would agree with whatever the UI happens to do; the
source does not.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing UI-surface suites: this has to pass in a tree holding only the
#: Python services.
pytestmark = pytest.mark.skipif(
    not UI.is_dir(), reason="apps/swarm-ui/src is not present"
)

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)

#: The real workflow from the audit. Five roots, one join naming all five.
FAN_IN = "wf_5e5ad3b6f7da4299a839"
ROOTS = ("cold-start", "fencing", "allornothing", "checkpoints", "absentzero")


def _src(name: str) -> str:
    return (UI / name).read_text()


def _body(source: str, name: str) -> str:
    """The text of `function <name>(` up to the next top-level declaration.

    Crude on purpose: it only has to be tight enough that an assertion about one
    function cannot be satisfied by a different one in the same file.
    """
    start = re.search(rf"^(?:export )?function {re.escape(name)}\b", source, re.M)
    assert start is not None, f"{name} is not declared"
    rest = source[start.start() :]
    nxt = re.search(r"^(?:export )?(?:function|const|interface) ", rest[1:], re.M)
    return rest[: nxt.start() + 1] if nxt else rest


# ==========================================================================
# W6 -- the open question, settled against the real route
# ==========================================================================

def _seed_fan_in(db, *, stored: TaskState = TaskState.QUEUED) -> None:
    """Five independent steps and a sixth joining all five, as it ran live."""
    seed_tenant(db, "eng")
    for i, step_id in enumerate(ROOTS):
        seed_task(
            db,
            task_id=f"t_{step_id}",
            tenant_id="eng",
            # Four succeeded, one failed: the mixed set that made the stored
            # QUEUED so obviously wrong on screen.
            state="FAILED" if step_id == "checkpoints" else "SUCCEEDED",
            workflow_id=FAN_IN,
        )
    seed_task(
        db, task_id="t_synthesis", tenant_id="eng", state="CANCELLED",
        workflow_id=FAN_IN,
    )

    from swarm_api.codec import workflow_to_firestore

    workflow = Workflow(
        workflow_id=FAN_IN,
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=stored,
        submitted_by="alice@saga.xyz",
        steps=[
            *(
                WorkflowStep(
                    step_id=step_id,
                    runner_profile="mock",
                    input={},
                    depends_on=[],
                    task_id=f"t_{step_id}",
                )
                for step_id in ROOTS
            ),
            WorkflowStep(
                step_id="synthesis",
                runner_profile="mock",
                input={},
                depends_on=list(ROOTS),
                task_id="t_synthesis",
            ),
        ],
    )
    db.docs[f"workflows/{FAN_IN}"] = workflow_to_firestore(workflow)


def test_a_fan_in_read_derives_its_state_and_says_so(db, client):
    """THE ANSWER TO W6: the derive path runs on GET /v1/workflows/{id}.

    The live probe saw `state: "QUEUED"` with no `state_source` on exactly this
    workflow. Against this code it does not: the state is derived from the six
    step tasks, the stored copy is served beside it and named as stored, and
    `state_source` is present. So the docstring is true here and the deployment
    that produced the screenshot predates `rollup.py`.
    """
    _seed_fan_in(db)

    response = client.get(f"/v1/workflows/{FAN_IN}", headers=auth_header("alice"))
    assert response.status_code == 200
    workflow = response.json()["workflow"]

    # The field every consumer reads is the DERIVED one. FAILED, not CANCELLED:
    # the cancellation of the join is the consequence of the failed step.
    assert workflow["state"] == "FAILED"
    assert workflow["state_source"] == "derived"
    # The field the probe could not find. Its absence is what told us the server
    # was old, so its presence is the thing worth pinning.
    assert "state_source" in workflow
    assert workflow["stored_state"] == "QUEUED"
    assert workflow["drift"]["stored"] == "QUEUED"
    assert workflow["drift"]["derived"] == "FAILED"
    assert workflow["drift"]["agrees"] is False
    assert workflow["rollup"]["complete"] is True
    assert workflow["rollup"]["counts"] == {
        "SUCCEEDED": 4, "FAILED": 1, "CANCELLED": 1
    }


def test_the_fan_in_topology_survives_the_read(db, client):
    """The join's five parents all come back, in full.

    The heading's state is only half of V1/V2. The other half is that the UI can
    only draw five edges if the payload still carries five `depends_on` entries
    -- and the ellipsed line on screen was five parents cut to three.
    """
    _seed_fan_in(db)

    workflow = client.get(
        f"/v1/workflows/{FAN_IN}", headers=auth_header("alice")
    ).json()["workflow"]

    by_id = {s["step_id"]: s for s in workflow["steps"]}
    assert set(by_id) == {*ROOTS, "synthesis"}
    assert by_id["synthesis"]["depends_on"] == list(ROOTS)
    for step_id in ROOTS:
        assert by_id[step_id]["depends_on"] == []

    # One edge per dependency, which is the number the UI has to draw. A chain of
    # six would give five; this fan-in gives five from one node. The count alone
    # is not the distinction -- where they LAND is -- which is precisely why a
    # single shared separator could not encode it.
    edges = [
        (parent, step["step_id"])
        for step in workflow["steps"]
        for parent in step["depends_on"]
    ]
    assert sorted(edges) == sorted((r, "synthesis") for r in ROOTS)


# ==========================================================================
# B14 -- the graph draws real edges, and never ellipses the dependency list
# ==========================================================================

def test_the_graph_emits_one_edge_per_dependency():
    """One edge per `depends_on` entry, not one bar per level.

    `dag.ts` still owns the shape: the canvas and the collapsed row read the
    same edge list, so there is one place it can be wrong. The builder is
    `layoutOf` now rather than `edgesOf` -- boxes and edges are computed
    together so the two cannot disagree about where a line ends.

    THE OUTER LOOP RUNS OVER STEPS, NOT NODES, and that is the assertion this
    test exists for now. It used to require `for (const child of nodes) {`,
    which pinned a spelling rather than the rule: once a stage too wide to draw
    became a BAND, that stage had no nodes, and iterating `nodes` dropped every
    dependency into or out of it -- ten of ten on a 1 -> 5 -> 1 workflow --
    while still satisfying the old assertion word for word. Both endpoints
    resolve through a step-keyed map, and a step inside a band resolves to the
    band's box.
    """
    body = _src("dag.ts")
    assert "for (const child of nodes) {" not in body, (
        "edges are being built from NODES again; a collapsed stage has none, so its "
        "dependencies are silently not drawn"
    )
    assert "for (const child of level) {" in body
    assert "for (const parentId of child.depends_on) {" in body
    assert "edges.push({" in body, (
        "the edge list is no longer built by pushing one edge per parent"
    )



def test_the_graph_renders_an_svg_path_for_every_edge():
    """Each edge becomes a drawn curve between the two node boxes.

    Hand-rolled SVG on purpose -- no charting dependency was added to draw six
    boxes, and the last assertion is what keeps it that way.
    """
    source = _src("Workflows.tsx")
    assert "layout.edges.map((e) => (" in source
    assert "<path" in source and 'className="wf-edge"' in source
    assert "edgePath(e)" in source
    # The old separator is gone: it was one mark for every shape of level break.
    assert "level-label" not in source
    assert "then ${level.length} in parallel" not in source

    package = (UI / ".." / "package.json").resolve().read_text()
    for library in ("d3", "recharts", "reactflow", "react-flow", "cytoscape", "vis-network"):
        assert f'"{library}' not in package, f"{library} was added to draw six boxes"



def test_the_dependency_list_is_never_ellipsed():
    """It is the only complete statement of the graph in text.

    `.node-dep` used to carry `max-width: 190px` with `text-overflow: ellipsis`
    and `white-space: nowrap`, which is what cut a five-parent join to
    "= cold-start, fencing, allorn...". Whatever else the rule does, it may not
    clip.
    """
    rule = _rule(_src("styles.css"), ".node-dep")
    assert "text-overflow" not in rule
    assert "ellipsis" not in rule
    assert "white-space: nowrap" not in rule
    assert "max-width" not in rule


def test_the_graph_spends_the_full_content_width():
    """The canvas is sized by the graph, not by the column it sits in.

    The audit's measured complaint was a graph huddled in the middle of its
    page. The rebuilt board sizes the canvas from `layoutOf` -- width grows
    with the number of levels -- and scrolls its wrapper when that exceeds the
    card. `.node-wrap` is gone with the flex row it was the child of.

    MUTATION: hard-code the canvas width in the stylesheet, or set it to 100%.
    The graph is then sized by its container again and a sixth level is lost.
    """
    source = _src("Workflows.tsx")
    assert "style={{ width: layout.width, height: layout.height }}" in source, (
        "the canvas is no longer sized from the computed layout"
    )
    body = _rule(_src("styles.css"), ".wf-canvas")
    assert body is not None, ".wf-canvas has no rule"
    assert "width:" not in body, (
        f"the stylesheet sets the canvas width, overriding the computed one: "
        f"{body.strip()!r}"
    )



def _rule(css: str, selector: str) -> str:
    """The declaration block of `selector`, for the rule that declares it alone."""
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(selector)}\s*(?:,[^{{]*)?\{{(.*?)\}}", css, re.S
    )
    assert match is not None, f"{selector} has no rule in styles.css"
    return match.group(1)


# ==========================================================================
# B15 -- one state in the heading, derived, never the stored copy
# ==========================================================================

def test_the_heading_prints_the_derived_state_and_nothing_else():
    """`workflowHeaderState` is the only source of the word in the heading.

    THE MUTATION THIS CATCHES: restoring `{workflow.state.toLowerCase()}` to the
    heading. That is the exact line that printed QUEUED over steps reading
    succeeded, failed and cancelled -- and it is a plausible "simplification",
    because on a current server `workflow.state` IS the derived value. It is not
    on the server the fleet is running, which is the whole point.
    """
    source = _src("Workflows.tsx")
    head = _body(source, "WorkflowCard")

    assert "workflowHeaderState(workflow)" in head
    assert "{header.word}" in head

    # Neither ambiguous field is rendered by the card. `workflow.state` means
    # "derived" or "stored" depending on which server answered, and a heading
    # cannot be built on a field whose meaning it cannot see.
    assert "workflow.state." not in head
    assert "{workflow.state}" not in head
    assert "workflow.stored_state" not in head


def test_an_underived_read_claims_no_state_at_all():
    """The payload the live deployment actually serves.

    No `rollup`, no `state_source`, `state` straight off the Firestore document.
    `workflowHeaderState` must refuse to print it as the workflow's state --
    printing "queued" there reproduces the contradiction against the server half
    the fleet is running, which is not a hypothetical server.
    """
    body = _body(_src("types.ts"), "workflowHeaderState")
    assert "workflow.state_source !== 'derived' || !rollup" in body
    assert "word: 'state not derived'" in body
    # The derived branch reads the ROLLUP, not the ambiguous top-level field.
    assert "rollup.state as TaskState" in body
    assert "state.toLowerCase()" in body
    # An incomplete derivation is its own answer, never an idle-looking one.
    assert "word: 'state unread'" in body


def test_the_step_count_is_printed_plainly():
    """"6 steps", never struck through, never "not counted".

    It was rendered amber and struck through as "3 steps - not counted", which
    told a reader the console could not count to six. The count is the one
    figure on the row that is always knowable: it is the length of an array
    that was read.

    MUTATION: reintroduce a `line-through` on the untrusted rollup, or make the
    count itself carry the unconfirmed treatment. The count is not a claim
    about state and must not be dressed as one.
    """
    source = _src("Workflows.tsx")
    assert "${total} steps" in source or "of ${total} steps" in source, (
        "the step total is no longer printed plainly"
    )
    assert "step${total === 1 ? '' : 's'} · not counted" not in source, (
        "the step count is again described as uncounted. What a missing rollup "
        "denies is the per-step census, not the length of the steps array."
    )
    css = _src("styles.css")
    for sel in (".wf-progress.untrusted", ".rollup.untrusted"):
        body = _rule(css, sel)
        if body is None:
            continue
        assert "line-through" not in body, (
            f"{sel} strikes through a figure that was read, not guessed"
        )



# ==========================================================================
# B17 -- an identifier is never restyled
# ==========================================================================

def test_identifiers_opt_out_of_text_transform_through_one_rule():
    """One rule, `.ident`, matching QuotaDetail's stated reason.

    `.section > h2` uppercases, so `wf_bcdc9180e4fb4a209f31` was DISPLAYED as
    `WF_BCDC9180E4FB4A209F31` and every copy of it was wrong. `text-transform`
    is inherited, so a rule that merely matches the element wins over the
    ancestor's value -- `!important` is not needed and must not appear.
    """
    rule = _rule(_src("styles.css"), ".id")
    assert "text-transform: none" in rule
    assert "letter-spacing: 0" in rule
    assert "!important" not in rule
    # And it is reached through one component, so a caller has one thing to
    # remember rather than a class name to get right.
    assert "export function Id(" in _src("Shell.tsx")


def test_every_identifier_rendered_under_an_uppercasing_rule_carries_it():
    """The known offenders, each named.

    A test that only checked the workflow heading would have let the other three
    keep uppercasing their ids -- which is how the first attempt at this rule
    ended up as an inline override in Runtimes.tsx that no other screen knew
    about.
    """
    workflows = _src("Workflows.tsx")
    # MATCHED AS A PATTERN, NOT A LITERAL, and the reason is the defect this
    # very assertion produced. It required `<Id>{workflow.workflow_id}</Id>`
    # exactly, so adding a `title` carrying the COMPLETE id -- inventory F11:
    # `[name]` ellipses at 390px and an ellipsed id cannot be pasted anywhere,
    # which is the only thing an id is for -- failed a test whose subject is
    # that the id goes through `Id` at all. The rule is the wrapper; any
    # attribute on it is not this test's business.
    assert re.search(r"<Id\b[^>]*>\{workflow\.workflow_id\}</Id>", workflows), (
        "the workflow id is no longer rendered through <Id>, so `.section > h2` is "
        "free to uppercase it again"
    )
    assert "{step.step_id}" in workflows

    # `.tag` uppercases too, and both of these hold identifiers. The value is
    # WRAPPED rather than the chip re-classed: `.id` beats the ancestor by
    # inheritance, so it cannot be out-specified from above.
    assert "<Id>{task.step_id}</Id>" in _src("Agents.tsx")
    assert "<Id>{c}</Id>" in _src("Activity.tsx")

    # The inline override is gone; the shared rule replaced it.
    runtimes = _src("Runtimes.tsx")
    assert "textTransform: 'none'" not in runtimes
    assert "<Id>{runtime.name}</Id>" in runtimes

    # And the rule has exactly one name. A second class doing the same job is
    # the drift this test exists to prevent.
    assert ".ident" not in _src("styles.css")
