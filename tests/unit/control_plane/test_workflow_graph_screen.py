"""The workflow DAG: real edges, a node you can click, and a cost that is never 0.

WHAT WAS MEASURED, on 2026-09-22, against `wf_5e5ad3b6f7da4299a839` -- five
independent steps and a sixth joining all five:

    [cold-start] [fencing] [allornothing] [checkpoints] [absentzero]
                        ---- THEN ----
                         [synthesis]

ONE separator bar. The identical bar a strictly linear workflow draws between
two sequential steps. There were no edges at all: siblings were laid out in a
row and dependency was implied by vertical order plus that one mark, so the view
could not distinguish a fan-in of five from a chain of five -- which is the one
question the screen exists to answer. The only complete statement of the graph
in words, the dependency line under the join, was ellipsed by CSS:
`<- cold-start, fencing, allorn...`, hiding two of its five parents.

Three properties are pinned here, and each has a mutation named in its
docstring that must turn it red:

  1. ONE DRAWN EDGE PER (parent, child) PAIR. Not one mark per level, not one
     per step: per pair. A shared mark between two pairs is precisely how the
     distinction was lost, so the tests check that the push is inside the loop
     over `depends_on` and that the screen maps the edge list to paths.
  2. THE NODE IS THE WAY IN. `StepNode` was a plain component with no href and
     no onClick, so from "draft is parked" there was no click that reached
     `draft`. It is an anchor to a hash `App.tsx` actually resolves -- checked
     against the router here, not assumed.
  3. AN ABSENT MEASUREMENT IS NEVER A NUMBER. The run panel's writing --
     "not reported -- No attempt reported a cost. This is an absent
     measurement, not $0.00." -- is the best thing in the product, and a DAG
     node carrying a cost is the obvious place to lose it. `measure.ts` makes
     the absence structurally unable to carry a value, and the node's absent
     branch renders a word.

MECHANISM. These read the shipped TypeScript and CSS as text, which is the house
pattern -- `test_runtimes_screen.py`'s docstring says why: "A mock would agree
with whatever the screen happens to do." They are SOURCE-STRUCTURE tests and
they are not renders: nothing here mounts a component, because `make test` is
offline and this repository has no JavaScript test runner in the gate at all
(apps/swarm-ui has four npm scripts and none of them is `test`). What that buys
and what it does not is stated plainly: these catch a reverted mechanism, and
they would not catch a mechanism that is present and mis-wired at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing api.ts seam tests: this suite has to pass in a tree that holds only
#: the Python services.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")


def _src(name: str) -> str:
    path = UI / name
    # A missing client file must not skip and must not pass. A rename this test
    # cannot follow is exactly the silent hole it exists to close.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text()
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def _code(source: str) -> str:
    """The source with its comments removed.

    Every file here carries long explanatory headers -- the house style -- and
    those headers QUOTE the defect they are about: `.level-label`, `content ??
    ''`, `resumable ?? false`. A test searching the raw text finds the warning
    against a pattern and reports it as the pattern, which is a false red that
    would eventually be "fixed" by deleting the comment.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    kept = [
        line
        for line in without_blocks.splitlines()
        if not line.lstrip().startswith("//") and not line.lstrip().startswith("* ")
    ]
    return "\n".join(kept)


def _work_id() -> str:
    """The Work section's id, read out of App.tsx rather than spelled here.

    It has been `agents` and is now `work`, and every assertion in this file
    that pinned the old spelling went red on a rename that changed no
    behaviour -- while a rewrite that broke the link would have passed, because
    the string was all they checked.

    App.tsx declares the id twice on purpose: as a string LITERAL in SECTIONS,
    because `test_nav_headings_agree.py` parses that array with a regex and
    cannot resolve a constant, and as `export const WORK` for the nine
    SectionBody cases and the router comparisons, which must not be renamable
    apart. The constant is what is read here, and
    `apps/swarm-ui/src/__tests__/nav.links.test.tsx` asserts the two agree.
    """
    m = re.search(r"export const WORK = '([a-z-]+)'", _src("App.tsx"))
    assert m, "App.tsx no longer exports a WORK section id"
    return m.group(1)


def _decl(source: str, opener: str) -> str:
    """One top-level declaration: from `opener` to the closing brace in column 0.

    Column-anchored rather than brace-counted, because a function signature in
    this codebase can itself open a brace -- a destructured parameter list, or a
    `): { edges: DagEdge[] }` return type -- and counting from the first `{`
    would return the parameter list instead of the body.
    """
    assert opener in source, f"{opener!r} is not in the source; the check would be vacuous"
    # A trailing newline so the LAST declaration in a file -- which ends `}\n`
    # with nothing after it -- is found by the same search as every other one.
    padded = source + "\n"
    start = padded.index(opener)
    end = padded.find("\n}\n", start)
    assert end != -1, f"no top-level close found after {opener!r}"
    return padded[start : end + 2]


def _block(source: str, opener: str) -> str:
    """The brace-balanced block opened by `opener`, which must end with `{`.

    Used instead of a regex so that a nested `{}` -- a JSX expression, an object
    literal -- cannot end the block early and make an assertion vacuous.
    """
    assert opener.rstrip().endswith("{"), f"{opener!r} must end with the brace it opens"
    assert opener in source, f"{opener!r} is not in the source; the check would be vacuous"
    brace = source.index(opener) + source[source.index(opener) :].index("{")
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : i + 1]
    raise AssertionError(f"braces never balanced after {opener!r}")


def _rules_for(css: str, selector: str) -> list[str]:
    """Every declaration block whose selector list mentions `selector`.

    ALL of them, not the first: the defect this repository keeps producing is a
    second rule for the same selector further down the sheet, and a check that
    stopped at the first would pass while the second one undid it.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    blocks: list[str] = []
    for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", stripped):
        head, body = match.group(1), match.group(2)
        parts = [p.strip() for p in head.split(",")]
        if any(
            p == selector or p.startswith(selector + " ") or p.startswith(selector + ".")
            for p in parts
        ):
            blocks.append(body)
    return blocks


# ---------------------------------------------------------------------------
# 1. Real edges
# ---------------------------------------------------------------------------


def test_one_edge_is_produced_for_every_parent_child_pair():
    """One drawn edge per (parent, child) pair. Not one per level, not one per step.

    THE MECHANISM MOVED. `edgesOf` and `dagShape` were replaced by `layoutOf`,
    which computes the node boxes and the edges together so the two cannot
    disagree about where a line ends. The property is unchanged and is checked
    in its new home.

    MUTATION: move `edges.push` out of the loop over `depends_on`, so a step
    with five parents contributes one edge instead of five. That is the defect
    this screen was rebuilt to remove -- a fan-in of five drew one mark,
    identical to the mark a chain of five draws.
    """
    dag = _src("dag.ts")
    fn = _decl(dag, "export function layoutOf(")
    inner = _block(fn, "for (const parentId of child.step.depends_on) {")

    assert "for (const child of nodes) {" in fn, (
        "layoutOf no longer iterates the nodes; it cannot be producing an edge per pair"
    )
    assert "edges.push({" in inner, (
        "the edge is not pushed inside the loop over `depends_on`. One push per STEP "
        "is the defect this whole screen was rebuilt to remove: five parents joining "
        "into one node drew one mark, identical to the mark a chain draws."
    )
    assert fn.count("edges.push") == 1, (
        "more than one push site in layoutOf; there is no longer a single place that "
        "decides what an edge is"
    )
    # A dependency naming a step this workflow does not contain is NOT drawn --
    # an edge to nowhere would be a claim about a graph that was never read.
    # The name is not lost: it stays in the node's own `depends on` line, which
    # test_the_dependency_list_is_never_truncated pins as unellipsable.
    assert "if (!parent) continue" in inner, (
        "a dependency on a step outside this workflow is being drawn as an edge"
    )



def test_the_screen_draws_one_path_per_edge():
    """Every edge in the list becomes its own drawn path, keyed by its own pair.

    MUTATION: replace the `layout.edges.map(...)` path list with a single
    separator element between levels -- the shape of the original defect.
    """
    wf = _src("Workflows.tsx")
    assert "layout.edges.map((e) => (" in wf, "the screen no longer maps the edge list to anything"
    assert "<path" in wf, "no path element is rendered, so no edge is drawn"
    assert "d={edgePath(e)}" in wf, (
        "the path's geometry is not computed from the edge's own measured endpoints"
    )
    assert "key={`${e.from}->${e.to}`}" in wf, (
        "each drawn edge must name its own (parent, child) pair, so two pairs cannot "
        "share one mark"
    )



def test_no_shared_separator_stands_in_for_the_edges():
    """No caption may STAND IN FOR the edges. It may sit beside them.

    NARROWED, deliberately, when the board was rebuilt. The old form banned the
    phrase "in parallel" anywhere in the screen, because the only thing that
    said it was the level caption that had replaced the edges. The rebuilt
    board draws a real path per pair AND labels each column above the canvas
    ("starts", "then 5 in parallel"), positioned from the same constants the
    nodes are laid out with. A caption next to drawn edges is not the defect;
    a caption INSTEAD of them is. So the ban is now on the separator element
    itself, plus the requirement that real paths exist.

    MUTATION: delete the `layout.edges.map` path list and keep the captions.
    The second assertion goes red.
    """
    wf = _code(_src("Workflows.tsx"))
    css = _code(_src("styles.css"))
    assert "level-label" not in wf, "the shared level separator is back in the screen"
    assert "level-label" not in css, "the shared level separator's rule is back in the sheet"
    assert "layout.edges.map" in wf and "<path" in wf, (
        "the captions are no longer accompanied by drawn edges, so a caption is once "
        "again the only thing expressing dependency"
    )



def test_the_dependency_list_is_never_truncated():
    """MUTATION: re-add `overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; max-width: 190px` to `.node-dep`. This goes red on the
    declaration.

    Measured: the join's dependency line read `<- cold-start, fencing,
    allorn...` and hid two of its five parents. It is the only complete
    statement of the graph in words on the page.
    """
    css = _src("styles.css")
    rules = _rules_for(css, ".node-dep")
    assert rules, ".node-dep has no rule at all; this test would check nothing"
    for body in rules:
        for banned in ("text-overflow", "white-space", "max-width", "-webkit-line-clamp"):
            assert banned not in body, (
                f".node-dep declares {banned}, which truncates the only complete "
                f"statement of the graph on the page: {body.strip()!r}"
            )

    wf = _code(_src("Workflows.tsx"))
    assert "step.depends_on.join(', ')" in wf, (
        "the node no longer renders the whole dependency list"
    )
    # A slice or a cap in the markup is the same defect one layer up.
    assert "depends_on.slice" not in wf
    assert "depends_on.join(', ').slice" not in wf


#: How many parents the fixture's widest join must have.
#:
#: THREE, not two. A two-way join was already in the fixture while the screen
#: was drawing every level with one shared separator -- so a two-way join is
#: demonstrably not enough to make the defect visible to someone working on the
#: screen. The case that WAS measured is five parents into one
#: (`wf_5e5ad3b6f7da4299a839`, 2026-09-22), and it is the one that overflows a
#: node's dependency line and a level's width. Three is the floor at which both
#: of those start to bite.
WIDEST_FIXTURE_JOIN = 3


def test_the_development_fixture_contains_a_fan_in_to_draw():
    """A graph screen whose fixtures are all narrow proves nothing in
    development. MUTATION: cut `synthesis`'s five parents down to one. The
    widest join in the fixture drops to the two-way one that was there all
    along while the bug shipped, and this goes red.
    """
    api = _src("api.ts")
    # `fixtureWorkflowBoard` delegates to `fixtureWorkflowRows`, which is where
    # the rows live: the graph and Overview's progress check read the same
    # route in production, so the fixture is shared rather than duplicated.
    fixture = _decl(api, "function fixtureWorkflowRows(")
    # Both call shapes: `step('x', ['a'], ...)` on one line, and the multi-line
    # form the wide join is written in.
    parents = re.findall(r"step\(\s*'([^']+)',\s*\[([^\]]*)\]", fixture, flags=re.DOTALL)
    assert parents, "no steps parsed out of the workflow fixture; the check is vacuous"

    widest = max((deps.count(",") + 1 if deps.strip() else 0) for _, deps in parents)
    assert widest >= WIDEST_FIXTURE_JOIN, (
        f"the widest join in the development fixture has {widest} parent(s). A join "
        f"narrower than {WIDEST_FIXTURE_JOIN} does not exercise the case that was "
        f"measured wrong -- five steps joining into one, drawn identically to a chain "
        f"of five, with the dependency line ellipsed at 'allorn...'."
    )


def test_the_collapsed_mode_states_the_shape_and_not_only_a_count():
    """The collapsed row names the shape, its degree, and the step it turns on.

    MOVED from `summarise` to `shapeOf`, which returns the sentence as `label`.
    MUTATION: return `${total} steps` for every kind. A fan-in of five and a
    chain of five then read identically on the collapsed row, which is the one
    place they must not.
    """
    dag = _src("dag.ts")
    fn = _decl(dag, "export function shapeOf(")

    assert "kind === 'fan-in'" in fn and "kind === 'fan-out'" in fn and "kind === 'diamond'" in fn, (
        "shapeOf no longer distinguishes the shapes; the check would be vacuous"
    )
    # The DEGREE, not just the word: "a join" alone does not say five.
    assert "${widest}" in fn, "the sentence never names how many run at the widest point"
    # And the step it converges INTO / opens FROM, by name where one exists.
    # `converge into 1` is a count of something the reader cannot go and look
    # at; `converge into synthesis` is the same line and can be acted on.
    assert "joinStep" in fn and "forkStep" in fn, (
        "the sentence no longer names the joining or forking step, only counts them"
    )
    assert "${into}" in fn and "${from}" in fn, (
        "the named join/fork are computed but not put into the sentence"
    )
    assert "in a chain" in fn or "each waiting for the one before it" in fn, (
        "a chain is no longer named as a chain"
    )



def test_every_dag_node_with_a_task_is_a_link_to_that_run():
    """MUTATION: revert the `<a>` to a `<div>`, as it was. This goes red on the
    missing href.
    """
    wf = _src("Workflows.tsx")
    # THE SECTION ID IS READ, NOT SPELLED. It was `agents` and is now `work`;
    # pinning either spelling makes this assertion fail on a rename that
    # changed nothing, which is how four assertions in this suite went stale in
    # one commit. What must hold is that the node links to THE DRAWER ROUTE of
    # whatever the section is currently called.
    assert (
        "href={`#%s/task/${encodeURIComponent(taskId)}`}" % _work_id() in wf
    ), "the step node carries no href, so there is no click that reaches the agent run"
    assert "<a\n" in wf or "<a " in wf, "no anchor element is rendered by the screen"


def test_the_hash_the_node_builds_is_one_the_router_resolves():
    """The seam, not the string. MUTATION: change the node's hash to
    `#agents/<id>` only, or change `fromHash` to stop accepting `task`; either
    end alone turns a link into a click that lands on the wrong screen.
    """
    wf = _src("Workflows.tsx")
    app = _src("App.tsx")

    hashes = re.findall(r"href=\{`#([^`$]*)\$\{", wf)
    assert hashes, "the node builds no hash; this test would check nothing"
    assert hashes[0] == f"{_work_id()}/task/", f"unexpected node hash prefix {hashes[0]!r}"

    router = _decl(app, "function fromHash(")
    # `head === WORK`, through the CONSTANT. App.tsx declares the id as a
    # literal in SECTIONS (a regex in test_nav_headings_agree.py reads that
    # array and cannot resolve a constant) and compares against the constant
    # everywhere else, so the comparison here is the constant's name. That is
    # the stronger thing to assert anyway: it is what makes the nine
    # SectionBody cases and the four router comparisons impossible to rename
    # apart.
    assert "head === WORK" in router and "tail[0] === 'task'" in router, (
        f"App.tsx no longer resolves `{_work_id()}/task/<id>`, so every node link is dead"
    )
    assert "taskId: decodeURIComponent(id)" in router, (
        "the resolved route carries no task id, so the drawer would open on nothing"
    )
    # And the route with a taskId is what mounts the run panel.
    assert "AgentDetailScreen" in app


def test_a_step_with_no_task_is_not_a_dead_link():
    """A step the workflow has not reached has nothing to open, and says so.

    MUTATION: link every node unconditionally. A step with no task then renders
    an anchor to `#agents/task/undefined`, and its inertness reads as a bug in
    the console rather than as a fact about the run.
    """
    wf = _src("Workflows.tsx")
    node = _decl(wf, "function StepNode(")
    # The guard is a ternary on the id now rather than an early return, so the
    # check is on the BRANCH existing, not on one spelling of it.
    assert "{taskId ? (" in node, (
        "every node is linked, including steps that have no task to open"
    )
    assert "has not reached" in node or "no task yet" in node, (
        "a node with nothing to open does not say so, so its inertness reads as a bug"
    )



def test_the_checkpoint_and_log_loaders_now_have_a_caller():
    """Both were written in full and NOTHING had ever called them -- fully
    written dead code behind two routes that exist and are tested.

    MUTATION: delete the `<RunFiles task={task} />` mount from AgentDetail. The
    two loaders have no caller again and this goes red.
    """
    api = _src("api.ts")
    assert "export async function loadCheckpoints(" in api
    assert "export async function loadTaskLogs(" in api

    callers = {
        name: [p.name for p in sorted(UI.glob("*.tsx")) if re.search(rf"\b{name}\b", p.read_text())]
        for name in ("loadCheckpoints", "loadTaskLogs")
    }
    for name, files in callers.items():
        assert files, f"{name} is exported from api.ts and no screen calls it"

    agent = _src("AgentDetail.tsx")
    assert "<RunFiles task={task} />" in agent, (
        "the panel that calls both loaders is not mounted on the run screen, so the "
        "node link opens a run that still cannot show logs or checkpoints"
    )


def test_the_log_footnote_no_longer_denies_a_route_that_exists():
    """MUTATION: restore "there is no log-tail read path on the API". The screen
    would again tell a reader not to look for something it can now show.
    """
    agent = _src("AgentDetail.tsx")
    foot = _decl(agent, "function LogsFoot(")
    assert "no log-tail read path" not in foot, (
        "the screen still states a constraint the API no longer has"
    )
    assert "not readable here at all" not in foot


def test_the_log_window_keeps_absent_apart_from_empty():
    """`content: null` is absent or unreadable; `content: ''` is an object that
    exists and is empty. MUTATION: write `content ?? ''` anywhere in the stream
    renderer -- an unreadable stream then draws an empty box that reads as a
    silent agent. This goes red.
    """
    run_files = _code(_src("RunFiles.tsx"))
    assert "content ?? ''" not in run_files
    assert 'content ?? ""' not in run_files
    assert "content || ''" not in run_files
    stream = _decl(run_files, "function Stream(")
    # THE GUARD, not the mention. `{false && stream.content === null && (` still
    # contains the comparison while rendering nothing, so a test searching for
    # the comparison alone passes over a branch that has been switched off --
    # and an unreadable stream then draws the measured-empty sentence.
    for guard in (
        "{stream.status === 'absent' && (",
        "{stream.status === 'unreadable' && (",
        "{stream.status === 'ok' && stream.content === null && (",
        "{stream.status === 'ok' && stream.content === '' && (",
    ):
        assert guard in stream, (
            f"the stream renderer no longer reaches {guard!r}, so two of the four "
            f"answers that route can give render alike"
        )


def test_the_checkpoint_row_keeps_cannot_tell_apart_from_cannot_resume():
    """MUTATION: `resumable ?? false`. "we could not tell" then renders as "a
    retry cannot use this", which is a different and much more alarming claim.
    """
    run_files = _code(_src("RunFiles.tsx"))
    assert "resumable ?? false" not in run_files
    row = _decl(run_files, "function CheckpointRow(")
    assert "record.resumable === null" in row
    assert "cannot tell" in row
    # A manifest that could not be read yields no file count, and an absent
    # count must not be a zero either.
    assert "record.file_count === null" in row


# ---------------------------------------------------------------------------
# 3. An absent measurement is never a number
# ---------------------------------------------------------------------------


def test_an_absent_cell_has_no_value_to_render():
    """The type makes it unrepresentable. MUTATION: add `value: number` to the
    `absent` member of `Cell`, which is the first edit anyone reaching for
    `$0.00` on a node would make. This goes red before any rendering does.
    """
    facts = _src("measure.ts")
    union = facts[facts.index("export type Cell") :]
    union = union[: union.index("\n\n")]
    absent = [line for line in union.splitlines() if "'absent'" in line]
    assert len(absent) == 1, f"expected one absent member of Cell, parsed {absent!r}"
    assert "value" not in absent[0], (
        f"the absent case of Cell carries a value, so an absence can be rendered as a "
        f"number: {absent[0].strip()!r}"
    )
    # `text` is the WORD that goes in the value slot and `note` is the sentence
    # behind it. Neither is a number and there is no numeric field to reach.
    assert "text: string" in absent[0] and "note: string" in absent[0]


def test_an_absent_cost_renders_the_word_and_never_a_number():
    """An absent cost renders a word. Never a digit, and never $0.00.

    MOVED. The rebuilt board carries no per-node cost cell -- a node states its
    name, status, runner profile and duration -- so `NodeNum`/`figuresFor` are
    gone and the cost is stated once per workflow. The discipline is unchanged
    and is checked where the figure now lives.

    MUTATION -- the one the brief names: make the absent branch render `0`
    instead of the word. $0.00 on a six-step workflow is a bill nobody owes.
    """
    wf = _src("Workflows.tsx")
    spend = _decl(wf, "function Spend(")

    assert "spend.usd === null" in spend, (
        "the spend cell no longer branches on the absence at all"
    )
    absent = spend[spend.index("spend.usd === null"):]
    cut = absent.find("return (")
    branch = absent[: cut if cut == -1 else absent.find("}", cut)]
    assert "not reported" in absent, (
        "the absent branch does not render a word"
    )
    assert "not $0.00" in absent, (
        "the absent branch no longer says what it is NOT, which is the half that "
        "stops a reader reading an absence as a free run"
    )
    assert "?? 0" not in branch and "|| 0" not in branch, (
        "the absent branch coalesces the absence to a number"
    )



def test_every_absent_cost_carries_its_own_reason():
    """Several absences, several sentences. Not one "not reported" for all.

    MUTATION: return the same word and note for all of them -- "no run yet",
    "state unread" and "reported nothing" then become indistinguishable, and
    the reader cannot tell a missing read from a missing charge.
    """
    facts = _src("measure.ts")
    for const in (
        "NO_ATTEMPT_YET",
        "STATE_UNREAD",
        "NEVER_RAN",
        "COST_NOT_REPORTED",
        "USAGE_NOT_SAMPLED",
        "USAGE_NOT_READ",
    ):
        assert f"export const {const}: Absence" in facts, (
            f"the step figures no longer distinguish {const}"
        )

    # The workflow-level figure keeps the same discipline: `usd` stays null
    # until something real is added to it, and the accumulator seed is the only
    # `?? 0` allowed anywhere near it.
    spend = _decl(_src("dag.ts"), "export function workflowSpend(")
    assert "let usd: number | null = null" in spend, (
        "the spend total no longer starts as an absence"
    )
    assert spend.count("?? 0") <= 1, (
        "the spend total coalesces an absence to a number in more than the one place "
        "the running sum is seeded"
    )
    # And it reports how much of the workflow it actually saw, so a partial
    # total is never shown as a complete one.
    assert "covered" in spend and "joined" in spend, (
        "the spend total no longer carries its own coverage"
    )



def test_a_measured_zero_is_still_a_digit():
    """The other half, and the one a redesign loses first. A `total_cost_usd`
    the runner actually wrote is a REAL zero -- a mock run costs nothing on
    purpose -- and must render as a figure, not as a sentence.

    MUTATION: add `if (raw === 0) return { kind: 'absent', ... }` to `stepCost`.
    A measured zero then reads as an absence and this goes red.
    """
    facts = _src("measure.ts")
    fn = _decl(facts, "export function numberCell(")
    # The ONE guard, and it is on the TYPE rather than on the truthiness. A
    # falsy guard is the same lie from the other side: it sends a measured zero
    # down the absent branch.
    measured = re.search(
        r"if \(typeof value !== 'number' \|\| !Number\.isFinite\(value\)\) \{", fn
    )
    assert measured, (
        "the measured branch no longer accepts every finite number, so a measured zero "
        "may be being turned into an absence"
    )
    for forbidden in ("value === 0", "value > 0", "value !== 0", "if (!value)"):
        assert forbidden not in fn, (
            f"numberCell special-cases zero ({forbidden!r}), which turns a measured "
            f"zero into an absence"
        )
    # And a measured zero really does come out as a digit.
    usd = _decl(facts, "export function usd(")
    assert "if (n === 0) return '$0.00'" in usd, (
        "a measured zero cost no longer renders as a figure"
    )


def test_the_run_panel_keeps_the_writing_the_node_borrows():
    """The node's discipline is a copy of the run panel's, so the run panel's
    must not regress while this lane is editing the file it lives in.

    MUTATION: change "not reported" to "$0.00", or render the checkpoint count
    as a sentence when it is zero. Either goes red.
    """
    agent = _src("AgentDetail.tsx")
    # THE SENTENCES MOVED, and moving them is the point: the node and the run
    # panel now read the same constants out of `measure.ts` instead of keeping
    # two copies that can drift. What the panel must still do is show the WORD
    # in the value slot rather than a number.
    facts = _src("measure.ts")
    assert "No attempt reported a cost. This is an absent measurement, not $0.00." in facts
    assert "Written when an attempt ends." in facts
    assert 'value="not recorded"' in agent
    assert 'value="not reported"' in agent

    metrics = _decl(agent, "function RunMetrics(")
    checkpoints = metrics[metrics.index('label="Checkpoints"') :]
    assert "value={`${ckpts}`}" in checkpoints, (
        "the checkpoint count is no longer rendered as a digit. That zero was MEASURED "
        "-- it is the one number on the panel that is allowed to be one."
    )

    # `usd` is the run panel's own guard and the node's `usdLabel` is its
    # sibling; neither may produce a figure from a non-number.
    usd = _decl(agent, "function usd(")
    assert "return <Em />" in usd


# ---------------------------------------------------------------------------
# 4. The graph gets the glass
# ---------------------------------------------------------------------------


def test_the_canvas_is_not_capped_inside_the_app_measure():
    """A graph wider than the column is REACHABLE, never crushed into it.

    MEASURED, 2026-09-22: the nodes occupied about 700px of a 1070px content
    area inside a page centred in 1100px of a 1600px window. The first fix
    widened the canvas past the app measure with `100vw`. The rebuilt board
    does it the other way -- the canvas keeps its computed width and its
    container scrolls -- because the canvas now lives inside a workflow card,
    and an element breaking out of a bordered card looks like a layout bug
    rather than a wide graph. Either satisfies the requirement; being capped
    satisfies neither.

    MUTATION: add `max-width: 100%` to `.wf-canvas`, or drop the
    `overflow-x` from its wrapper. A fan-out of six is then crushed or clipped.
    """
    css = _src("styles.css")
    canvas = _rules_for(css, ".wf-canvas")
    assert canvas, ".wf-canvas has no rule; the canvas has no container styling at all"
    for body in canvas:
        assert "max-width" not in body, f".wf-canvas caps its own width: {body.strip()!r}"

    wrap = _rules_for(css, ".wf-canvas-wrap")
    assert wrap, ".wf-canvas-wrap has no rule, so nothing makes a wide graph reachable"
    assert any("overflow-x: auto" in body for body in wrap), (
        "the canvas does not scroll, so a graph too wide for the column is crushed "
        "rather than reachable"
    )



def test_a_level_never_wraps_into_a_second_row():
    """A level cannot wrap, because a wrapped level reads as two levels.

    STRONGER THAN IT WAS. The old layout was flex and needed `flex-wrap:
    nowrap` to hold this. The rebuilt canvas positions every node absolutely at
    an (x, y) computed in `layoutOf` from its indices, so there is no flow to
    wrap and no stylesheet value that could reintroduce one.

    RE-POINTED ONTO THE OTHER AXIS, AND WHAT MOVED IS WHICH INDEX DRIVES WHICH
    COORDINATE. The graph ran left to right: the dependency LEVEL drove x, a
    step's position within its level drove y, and this test read
    `NODE_W + COL_GAP` out of `layoutOf` to pin the first of those. The owner's
    words were "all nodes at the same stage displayed horizontally not
    vertically ... the natural flow of the workflow should be top to bottom
    rendered rather than left to right", so LEVEL drives y and POSITION drives
    x, and `COL_GAP`/`ROW_GAP` are `LEVEL_GAP`/`SIB_GAP` -- named for what they
    separate rather than for the axis they used to run along.

    The claim is not weakened by the move; it is the more direct reading of it.
    The wrapping this test forbids is a LEVEL flowing onto a second line, and a
    level is now laid out across x -- so the constant that pins it is the
    sibling pitch, applied to the position index, which is exactly the thing
    that would have to stop being arithmetic for a wrap to become possible.
    Both axes are pinned rather than one, because a transpose that only half
    landed would leave the other coordinate constant and every node stacked.

    MUTATION: give a node `position: static`; or compute `x` from anything but
    the position index and the sibling pitch; or leave `y` off the level index,
    which collapses every level onto one band.
    """
    css = _src("styles.css")
    node = _rules_for(css, ".node")
    assert node, ".node has no rule; this test would check nothing"
    assert any("position: absolute" in body for body in node), (
        "nodes are no longer absolutely positioned, so a level can flow and wrap"
    )

    dag = _src("dag.ts")
    fn = _decl(dag, "export function layoutOf(")
    assert "pos * (NODE_W + SIB_GAP)" in fn, (
        "a node's x is no longer derived from its position within its level and the "
        "sibling pitch, so a level is no longer laid out across the page by arithmetic"
    )
    assert "y: levelTop[lvl]" in fn, (
        "a node's y is no longer its dependency level's band top, so the flow does "
        "not descend and every level shares one band"
    )
    assert "+ LEVEL_GAP" in fn, (
        "the band tops no longer accumulate a per-level gap, so two levels can be "
        "laid out touching and read as one"
    )


