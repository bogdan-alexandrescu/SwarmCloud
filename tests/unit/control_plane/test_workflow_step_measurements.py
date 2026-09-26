"""A step node reported none of the four things the brief asks a run to report.

THE DEFECT. `StepNode` in `apps/swarm-ui/src/Workflows.tsx` rendered a step id,
a state word, a runner profile and a dependency line: four facts about the PLAN
and not one about the RUN. The one screen in this product about a multi-agent
run reported no duration, no cost, no tokens, and offered no way to reach a
step's input or output -- while `codec.attempt_from_dict` had been decoding
`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` and `cost_usd` onto every attempt for some time.

THE DEFECT THAT ADDING THEM CREATES, which is what most of this file is about.
Every one of those five fields is nullable, and `record_usage` in
`agent_worker/control.py` says why in its own docstring: "None means not
reported and zero means cost nothing, and a mock task is genuinely the second
while a result that failed to parse is the first". A screen that renders both as
`0` has not added information, it has added a confident lie -- and the task
drawer two screens away already gets this exactly right:

    not reported -- No attempt reported a cost. This is an absent measurement,
                    not $0.00.
    not recorded -- Written when an attempt ends. A running attempt has none.

with a checkpoint count of 0 rendered as a DIGIT beside them, because that zero
was counted. A redesign that loses that discipline has regressed, whatever else
it gained.

So the rule moved into one module, `apps/swarm-ui/src/measure.ts`, with exactly
one guard in it, and this file pins:

  1. that the guard is `typeof`/`Number.isFinite` and NOT falsiness -- the two
     lies are mirror images and only that guard is neither;
  2. that the absent branch returns a SENTENCE and never reaches a formatter;
  3. that a measured zero reaches a formatter that prints a DIGIT;
  4. that the step node gets every figure through that module, with no
     `?? 0` on the way;
  5. that the four (now five) different reasons a figure can be missing print
     five different words, because they send an operator to five different
     places.

HOW THIS IS CHECKED. The TypeScript is read as text -- no node, no network, no
credentials -- which is the technique `test_blocker_ui_surface.py`,
`test_dispatch_ui_surface.py` and `test_runtimes_screen.py` already use and for
the reason they record: `make test` is offline by contract, the repository ships
no JavaScript toolchain, and a mock of the UI would agree with whatever the UI
happens to do. What that buys and what it does not is worth stating plainly: it
holds the SHAPE of the rule, so undoing the rule in the source turns these red,
and it does not execute the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing seam tests: this suite has to pass in a tree that holds only the
#: Python components.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui is not checked out")


def src(name: str) -> str:
    path = UI / name
    # A missing client file must not skip or pass. A rename this test cannot
    # follow is exactly the silent hole it exists to close.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text()
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def body_of(text: str, signature: str) -> str:
    """One top-level function's source, signature to closing brace.

    Terminated on a `}` in column 1 rather than by counting braces: every
    function read here is top-level, so its closing brace is the only one at the
    start of a line, and brace counting would have to be told apart from
    destructured parameters and inline return types.
    """
    assert signature in text, f"{signature!r} is not in the source any more"
    start = text.index(signature)
    end = text.index("\n}\n", start)
    return text[start : end + 3]


#: `export const NAME: Absence = {\n  text: '...',`
ABSENCE_TEXT = re.compile(r"export const (\w+): Absence = \{\n  text: '([^']*)',")
#: the `note:` line of the same literal
ABSENCE_NOTE = re.compile(r"export const (\w+): Absence = \{\n  text: '[^']*',\n  note:\s*\n?\s*'((?:[^'\\]|\\.)*)',")


def absences() -> dict[str, str]:
    """Every exported `Absence` and the WORD it puts in the value slot."""
    found = dict(ABSENCE_TEXT.findall(src("measure.ts")))
    assert found, "no Absence constants found; the parse below checks nothing"
    return found


def absence_notes() -> dict[str, str]:
    found = dict(ABSENCE_NOTE.findall(src("measure.ts")))
    assert found, "no Absence notes found; the parse below checks nothing"
    return found


# ---------------------------------------------------------------------------
# 1. The guard
# ---------------------------------------------------------------------------

#: The ONE guard. Written out so a change to it is a change to this test too.
GUARD = "if (typeof value !== 'number' || !Number.isFinite(value)) {"


def test_the_absent_guard_is_typeof_and_finite_not_falsiness():
    """`if (!value)` is the same defect from the other side.

    A falsy guard sends a MEASURED zero down the absent branch, so `$0.00` --
    a real reading a mock runner really produces -- would print as "not
    reported". `typeof`/`isFinite` is the only guard that is neither lie.
    """
    fn = body_of(src("measure.ts"), "export function numberCell(")
    assert GUARD in fn, (
        "numberCell no longer guards on typeof + Number.isFinite. A falsiness "
        "guard renders a measured zero as an absent measurement, and any "
        "narrower guard renders an absent measurement as a number."
    )
    for falsy in ("if (!value)", "if (value)", "value ?? 0", "value || 0", "!value ?"):
        assert falsy not in fn, f"numberCell uses a falsiness test ({falsy!r})"


def test_numbercell_has_exactly_one_guard():
    """Two guards is two rules, and the second one is the one nobody reads."""
    fn = body_of(src("measure.ts"), "export function numberCell(")
    assert fn.count("Number.isFinite") == 1, "numberCell has more than one guard"


# ---------------------------------------------------------------------------
# 2. An absent measurement renders as the absent copy, NOT as 0
# ---------------------------------------------------------------------------
#
# This is the test the brief names. Undo the fix -- make the absent branch
# return a formatted zero -- and it goes red.


def test_an_absent_measurement_returns_the_sentence_and_never_a_number():
    """The guarded branch returns `absentCell(absence)`. Nothing else."""
    fn = body_of(src("measure.ts"), "export function numberCell(")
    guarded = re.search(
        r"if \(typeof value !== 'number' \|\| !Number\.isFinite\(value\)\) \{\s*"
        r"return absentCell\(absence\)\s*\}",
        fn,
    )
    assert guarded is not None, (
        "numberCell's absent branch no longer returns absentCell(absence). "
        "An absent measurement must render as the sentence the Absence "
        "carries -- never as 0, never as a formatted zero, never as an em "
        "dash that means six different things."
    )
    # The formatter is reached ONLY after the guard. A `format(` before the
    # closing brace of the guard would be a zero manufactured inside it.
    before_guard, _, after_guard = fn.partition("return absentCell(absence)")
    assert "format(" not in before_guard, "numberCell formats a value before deciding it exists"
    assert "format(value)" in after_guard, "numberCell no longer formats the measured value"


def test_no_absence_puts_a_numeral_in_the_value_slot():
    """"not reported" is a sentence. "0" is a claim, and a false one."""
    for name, text in absences().items():
        assert not any(ch.isdigit() for ch in text), (
            f"{name} renders as {text!r}, which contains a digit. An absent "
            "measurement must not look like a measurement."
        )
        assert text.strip(), f"{name} renders as empty text"
        assert text not in {"-", "—", "n/a", "N/A"}, (
            f"{name} renders as {text!r}, a mark that means nothing in "
            "particular. The whole point is which absence this is."
        )


def test_the_cost_absence_says_it_is_not_zero_dollars():
    """The best sentence in the product, kept verbatim rather than paraphrased."""
    notes = absence_notes()
    assert (
        notes.get("COST_NOT_REPORTED")
        == "No attempt reported a cost. This is an absent measurement, not $0.00."
    ), "the cost absence no longer explains that it is not $0.00"
    assert (
        notes.get("TOKENS_NOT_REPORTED")
        == "No attempt reported a token count. Not the same as a run that used none."
    )


# ---------------------------------------------------------------------------
# 3. A measured zero renders as a digit
# ---------------------------------------------------------------------------


def test_a_measured_zero_renders_as_a_digit():
    """`usd(0)`, `tokenText(0)` and `countText(0)` are digits, not sentences.

    The formatters are only ever reached with a finite number, so an exact zero
    arriving here is a READING. It gets the same treatment as any other
    reading -- which is what makes the checkpoint count of 0 beside three
    sentences the honest thing on the node rather than an inconsistency.
    """
    text = src("measure.ts")

    money = body_of(text, "export function usd(")
    assert "if (n === 0) return '$0.00'" in money, (
        "usd() no longer prints an exact zero as $0.00. A measured zero is a "
        "measurement and renders as a digit."
    )
    tokens = body_of(text, "export function tokenText(")
    assert "if (n === 0) return '0'" in tokens, (
        "tokenText() no longer prints an exact zero as 0."
    )
    count = body_of(text, "export function countText(")
    assert "Math.round(n)" in count and "return" in count

    # And none of them may bail out on falsiness, which would route 0 back to
    # an absence by a different door.
    for name, fn in (("usd", money), ("tokenText", tokens), ("countText", count)):
        for falsy in ("if (!n)", "n || ", "n ?? "):
            assert falsy not in fn, f"{name} treats a measured zero as missing ({falsy!r})"


def test_usd_never_rounds_a_real_cost_down_to_zero():
    """$0.004 printed as "$0.00" is the same lie arriving by a different door."""
    money = body_of(src("measure.ts"), "export function usd(")
    assert "Math.abs(n) < 0.01" in money and "toFixed(4)" in money, (
        "usd() rounds every value to two places, so a real sub-cent cost "
        "prints as $0.00 and claims a run was free."
    )


# ---------------------------------------------------------------------------
# 4. The step node gets every figure through the module
# ---------------------------------------------------------------------------


def test_the_step_node_renders_duration_cost_and_tokens():
    """The four facts the brief asks for, on the node itself."""
    node = body_of(src("Workflows.tsx"), "function StepNode(")
    for label in ('label="ran"', 'label="cost"', 'label="tokens"', 'label="ckpts"'):
        assert label in node, f"the step node no longer carries {label}"
    assert "node-nums" in node


def test_the_step_node_reaches_its_inputs_and_outputs():
    """A node used to be a dead end: the task id lived in a native `title`.

    From "draft is parked" there was no click that reached draft -- the route
    was Agents, then Waiting, then find the row, re-identifying by the step chip
    a step you were already looking at.

    RE-POINTED WITH WF-7 (epic #83). The node became one anchor to the task
    drawer -- whose own tabs hold the input and output and the attempts -- and
    the owner then decided that clicking a node fills the step inspector in
    place and that the INSPECTOR carries `open agent ->` to that drawer. The
    route this test exists for is that link now. The old assertions here also
    passed on the node's COMMENTS alone (`input & output ->` and `/attempts`
    are quoted in its explanation of the anchors it replaced), which is the
    grep-passes-on-a-comment failure `apps/swarm-ui/src/__tests__/README.md`
    describes; they read the code now.
    """
    views = src("WorkflowViews.tsx")
    inspector = body_of(views, "export function StepInspector(")
    code = "\n".join(
        line
        for line in re.sub(r"/\*.*?\*/", "", inspector, flags=re.DOTALL).splitlines()
        if not line.lstrip().startswith("//")
    )
    # Read, not spelled: the section was renamed `agents` -> `work` and this
    # assertion checked the old string. See `_work_id` in
    # test_workflow_graph_screen.py for why the constant is the thing to read.
    work = re.search(r"export const WORK = '([a-z-]+)'", src("App.tsx"))
    assert work, "App.tsx no longer exports a WORK section id"
    assert f"#{work.group(1)}/task/" in code, "a picked step links nowhere"
    # The drawer that route opens is the one whose tabs are the input and
    # output and the attempts: the router resolves `task/<id>` and
    # `task/<id>/attempts` to the same drawer.
    router = body_of(src("App.tsx"), "function fromHash(")
    assert "tail[0] === 'task'" in router, "the router no longer opens the task drawer"


def test_every_step_figure_goes_through_measure():
    """No screen-local formatting, and no `?? 0` on the way in."""
    text = src("Workflows.tsx")
    figures = body_of(text, "function figuresFor(") + body_of(text, "function tokensOf(")
    for helper in ("costCell(", "tokenCell(", "countCell(", "absentCell(", "measuredCell("):
        assert helper in figures, f"the step figures no longer use {helper}"
    for forbidden in ("?? 0", "|| 0", "toFixed(", "toLocaleString("):
        assert forbidden not in figures, (
            f"the step figures contain {forbidden!r}, which manufactures a zero "
            "outside measure.ts and outside anything that checks it."
        )


def test_the_running_duration_is_never_a_zero_for_a_step_that_never_started():
    """`started_at` is written on DISPATCHED -> STARTING.

    A QUEUED, LEASED or DISPATCHED step legitimately has none, and "0s" for it
    is a lie in the most literal sense available.
    """
    fn = body_of(src("Workflows.tsx"), "function ranCell(")
    assert "Number.isFinite(started)" in fn
    assert "absentCell(" in fn, "ranCell has no absent branch"
    assert "FINISH_NOT_RECORDED" in fn, (
        "ranCell no longer distinguishes a task that ended without a "
        "completed_at from one that is still running"
    )
    for forbidden in ("?? 0", "|| 0"):
        assert forbidden not in fn


def test_the_checkpoint_zero_is_counted_not_assumed():
    """The digit beside the sentences, and the reason it is a digit.

    The attempt document carries its own list of checkpoint ids, so once the
    attempts are in hand this figure is never absent and a zero IS the
    measurement. Routing it through an absence on zero would be the same
    collapse the rest of this file prevents, applied backwards.
    """
    fn = body_of(src("Workflows.tsx"), "function figuresFor(")
    assert "countCell(\n      u.checkpoints," in fn, (
        "the checkpoint count no longer comes straight off the rolled-up "
        "attempts; a zero reached by any other route is not a counted zero"
    )
    assert "u.checkpoints === 0" in fn, "the counted-zero case has no note of its own"


# ---------------------------------------------------------------------------
# 5. The absences are told apart
# ---------------------------------------------------------------------------

#: Every absence that can land in the SAME slot on a node. Each is a different
#: fact with a different remedy: nothing has run, the task was not read, the
#: board did not read the attempts, the read failed, no attempt exists yet, the
#: runner reported no figure. One word for all six is six wrong signposts.
SAME_SLOT = (
    "NEVER_RAN",
    "STATE_UNREAD",
    "USAGE_NOT_SAMPLED",
    "USAGE_NOT_READ",
    "NO_ATTEMPT_YET",
    "COST_NOT_REPORTED",
)


def test_absences_that_share_a_slot_do_not_share_a_word():
    found = absences()
    missing = [n for n in SAME_SLOT if n not in found]
    assert not missing, f"absences went missing from measure.ts: {missing}"
    words = [found[n] for n in SAME_SLOT]
    assert len(set(words)) == len(words), (
        f"two absences that can occupy the same slot print the same word: {words}. "
        "A reader cannot tell them apart, and they have different remedies."
    )


def test_every_absence_carries_its_own_cause():
    notes = absence_notes()
    for name in SAME_SLOT:
        assert name in notes, f"{name} has no note; an absence with no cause is a dead end"
        assert len(notes[name]) > 30, f"{name}'s note is too short to say why"
    assert len(set(notes.values())) == len(notes), "two absences share a cause sentence"


# ---------------------------------------------------------------------------
# 6. The rollup, and the loader with a reader
# ---------------------------------------------------------------------------


def test_the_rollup_stays_null_until_something_reported():
    """`reduce((t, a) => t + (a.cost ?? 0), 0)` is how an unmeasured sample
    becomes a confident $0.00. Each field sums separately and seeds at null."""
    fn = body_of(src("measure.ts"), "export function sumReported<T>(")
    assert "let total: number | null = null" in fn, (
        "sumReported seeds its accumulator at a number, so a sample in which "
        "nothing reported anything totals to zero"
    )
    assert "if (typeof v !== 'number' || !Number.isFinite(v)) continue" in fn

    roll = body_of(src("api.ts"), "function rollUpAttempts(")
    for field in ("costUsd", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
        assert f"{field}: sumReported(" in roll, f"{field} is not summed through sumReported"
    assert "?? 0" not in roll, "rollUpAttempts counts a missing half as a zero"


def test_the_usage_loader_has_a_reader():
    """A route read by nothing fails silently for ever, because nothing about
    it looks broken. `test_runtimes_screen.py` pins the same three links."""
    api = src("api.ts")
    assert "export async function loadWorkflowUsage(" in api
    assert "loadWorkflowUsage" in src("Workflows.tsx"), (
        "loadWorkflowUsage is exported and no screen calls it"
    )
    assert re.search(r"<Board\s+board=\{d\}", src("Workflows.tsx")), (
        "the board is not rendered"
    )


def test_the_sample_ceiling_is_stated_rather_than_implied():
    """The board reads a bounded number of attempt sets, so some steps have no
    figure at all. Silence there reads as zero; the ceiling is published."""
    api = src("api.ts")
    assert "sampleLimit" in api, "the read ceiling is not published to the screen"
    fn = body_of(src("Workflows.tsx"), "function SampleNote(")
    assert "sampleLimit" in fn and "not zero" in fn


# ---------------------------------------------------------------------------
# 7. B20 -- a measured zero bar must look measured
# ---------------------------------------------------------------------------


def test_a_measured_zero_bar_gets_a_baseline():
    """A zero-width fill on a near-white track is a widget that failed to paint.

    The figure beside it read "0 / 10" and nothing on the page could tell an
    operator whether that zero was a reading or a rendering bug -- on the one
    screen that is scrupulous about exactly this distinction in prose.
    """
    # THE SHARED TRACK, not Overview's copy of it. U8 collapsed the six hand-
    # drawn tracks (Overview x3, AgentDetail, Capacity, Holders) onto one
    # component, so the baseline tick is asserted where every screen gets it.
    fn = body_of(src("primitives.tsx"), "export function UtilTrack(")
    assert "if (pct === null) {" in fn, "an unmeasured track no longer hatches"
    assert "is-unknown" in fn, "an unmeasured track no longer hatches"
    assert "if (pct === 0) {" in fn, "a measured zero draws nothing distinguishable"
    assert "ctl-util-zero" in fn, "there is no baseline mark for a measured zero"

    css = src("styles.css")
    zero = re.search(r"\.ctl-util-zero \{[^}]*\}", css, re.S)
    assert zero is not None, ".ctl-util-zero is not styled, so the tick has no size"
    assert re.search(r"width:\s*[1-9]", zero.group(0)), (
        ".ctl-util-zero has no visible width; a zero-width baseline is the "
        "defect it exists to fix"
    )
    # A min-width on the FILL would be the other lie: it makes 0 and 0.4%
    # identical instead of making 0 and unmeasured identical.
    # The fill shares a rule with the other bar fills, so match every rule
    # whose SELECTOR LIST names it rather than assuming it stands alone. A
    # min-width on the fill would be the other lie: it makes 0 and 0.4%
    # identical instead of making 0 and unmeasured identical.
    fills = [
        m.group(0)
        for m in re.finditer(r"(?:^|\n)([^{}]*?)\{([^}]*)\}", css, re.S)
        if ".ctl-util-fill" in m.group(1)
    ]
    assert fills, ".ctl-util-fill is not styled at all"
    for rule in fills:
        assert "min-width" not in rule, (
            "the fill has a minimum width, which renders a measured zero as a "
            "small amount of use rather than as zero"
        )


def test_both_utilisation_bars_go_through_one_component():
    """One component and one rule, for the whole product now.

    This asserted the profile rows and the account rows on Overview shared one
    track, because a second copy is how the account bar kept the treatment and
    the profile bar lost it. That held -- and AgentDetail, Capacity and Holders
    each drew a third, fourth and fifth copy beside it, and AgentDetail's had
    already lost the measured-zero tick. So the property is now the one the
    first version was standing in for: `.ctl-util-track` is drawn by exactly one
    file, and every screen that shows a utilisation goes through it.
    """
    drawers = sorted(
        p.name for p in UI.glob("*.tsx")
        if re.search(r"""className=[{"'`][^>]*\bctl-util-track\b""", p.read_text())
    )
    assert drawers == ["primitives.tsx"], (
        f"a utilisation track is drawn outside the shared primitive, in {drawers}; "
        "each copy is a place the hatch, the baseline tick or the over-ceiling "
        "segment can be kept on one screen and lost on the next"
    )
    ov = src("Overview.tsx")
    assert ov.count("<UtilRow") == 2, (
        "the utilisation row has stopped being shared between the profile rows "
        "and the account rows"
    )
    assert "ctl-util-track" not in body_of(ov, "function ProfileRow("), (
        "ProfileRow builds its own track again"
    )


# ---------------------------------------------------------------------------
# 8. B18 -- the telemetry strip collapses, and is not deleted
# ---------------------------------------------------------------------------


# WHERE THE COLLAPSE ACTUALLY LANDED. This section was written against a
# `<details>` inside `DataSources.tsx`. The strip has since moved into the dock
# (`Dock.tsx`), which is the same decision carried further: `DataSources.tsx`
# now renders the CELLS only, and the dock owns the one collapsed line, the
# subscription and the tick. The assertions below are unchanged in substance
# and re-pointed at the component that now holds each one -- B18 is "one line,
# not 200px on every screen", not "a <details> element in this file".


def test_the_data_source_strip_collapses_to_one_line():
    dock = src("Dock.tsx")
    assert 'className="ctl-dock-line"' in dock and "aria-expanded={open}" in dock, (
        "the strip is not a disclosure"
    )
    assert "p95Ms" in dock and "summariseProbes(" in dock, (
        "the collapsed line reports no p95"
    )
    assert "{s.routes} route{s.routes === 1 ? '' : 's'}" in dock, (
        "the collapsed line reports no route count"
    )
    # NOT DELETED. The cells are the thing an operator reads when a number looks
    # wrong; the disclosure is about where they sit, not about losing them.
    assert "<DataSourceCells probes={probes} />" in dock, (
        "the per-route cells were deleted rather than collapsed"
    )
    assert "source-cells" in src("DataSources.tsx"), "the per-route cells are gone"
    assert '<a href="#reference">' in dock, "there is no route to the full detail"


def test_the_reauth_button_is_not_behind_the_disclosure():
    """A control that appears only after a click is a control that is not
    there. An expired session is the one thing on this strip that needs an
    action, so it sits outside the collapsed/expanded boundary entirely."""
    dock = src("Dock.tsx")
    reauth = dock.index('className="reauth"')
    body = dock.index('className="ctl-dock-body"')
    assert reauth < body, "the re-auth button moved inside the disclosure"
    # And it is not merely ABOVE the body while still gated on `open`.
    assert "{open && (" not in dock[:reauth].rsplit("{s.expired && (", 1)[-1], (
        "the re-auth button is gated on the dock being open"
    )


def test_an_admin_gate_is_not_counted_as_a_failure():
    """A non-admin genuinely cannot read /v1/admin/*. Counting it makes a
    working page report itself broken on every single screen."""
    fn = body_of(src("panes.ts"), "export function summariseProbes(")
    assert "if (p.lastKind === 'admin_required') adminOnly += 1" in fn, (
        "an admin gate is no longer counted apart from a fault"
    )
    assert "else if (p.lastKind !== null) failed += 1" in fn, (
        "the collapsed line counts an admin gate as a fault"
    )


def test_the_strip_is_still_mounted_on_every_screen():
    assert "<Dock />" in src("App.tsx"), (
        "the provenance strip is not mounted on every screen any more"
    )


# ---------------------------------------------------------------------------
# 9. B19 -- the layout stops wasting the screen
# ---------------------------------------------------------------------------


def test_the_page_widens_past_1100px():
    """Content ran x=266..1334 on a 1600px viewport -- 250px of dead page on
    each side -- on a console whose brief asks for graphs everywhere possible.

    The cap is read through the token as well as the literal: the fix that
    landed removes the cap rather than stepping it up, so `.app` reads
    `max-width: var(--app-max)` and the figure lives on `:root`. Either shape
    satisfies this; a page still pinned at 1100px does not.
    """
    css = src("styles.css")
    app_rule = re.search(r"\n\.app \{[^}]*\}", css, re.S)
    assert app_rule is not None, ".app has no rule at all"
    assert "max-width" in app_rule.group(0), ".app has no max-width at all"

    caps = [int(m) for m in re.findall(r"\.app \{ max-width: (\d+)px", css)]
    caps += [int(m) for m in re.findall(r"--app-max:\s*(\d+)px", css)]
    assert caps, "the page width is set by neither a literal nor --app-max"
    assert max(caps) > 1100, (
        "the page is still capped at 1100px or narrower. Content ran "
        "x=266..1334 on a 1600px viewport -- 250px of dead page on each side."
    )


def test_widening_the_page_did_not_widen_the_prose():
    """The container was capping the CHARTS in order to protect the
    PARAGRAPHS. Lines of 157-192 characters were already being rendered against
    a comfortable 45-90; removing the cap without a measure makes that worse."""
    css = src("styles.css")
    assert "--measure:" in css, "there is no measure token"
    measure_rule = re.search(r"((?:^\.[\w.\- >,\n]+)\{ max-width: var\(--measure\); \})", css, re.M)
    assert measure_rule is not None, "nothing is constrained to the measure"
    for selector in (".sub", ".state p", ".ctl-section-q"):
        assert selector in measure_rule.group(0), f"{selector} has no measure"


def test_the_dag_fills_the_width_it_is_given():
    """The canvas is sized by the graph, not by the column it sits in.

    MEASURED: `align-items: center` plus `justify-content: center` drew the
    graph as a ~132px column with roughly 430px of blank page beside it at
    1024px.

    THE MECHANISM CHANGED when the board was rebuilt. The flex row that
    `.node-wrap { flex: 1 1 0 }` grew inside is gone; nodes are positioned
    absolutely at coordinates `layoutOf` computes, and the canvas is given that
    computed width. Nothing can centre it into a narrow column any more,
    because nothing is laying it out. What CAN go wrong is the stylesheet
    overriding the computed size, so that is what is pinned.

    MUTATION: set a width on `.wf-canvas`, or cap it with a max-width. The
    graph is sized by its container again and a sixth level is lost.
    """
    source = src("Workflows.tsx")
    assert "style={{ width: layout.width, height: layout.height }}" in source, (
        "the canvas is no longer sized from the computed layout"
    )

    css = src("styles.css")
    canvas = re.search(r"\n\.wf-canvas \{[^}]*\}", css, re.S)
    assert canvas is not None, ".wf-canvas has no rule; the canvas has no container styling"
    assert "max-width" not in canvas.group(0), (
        f".wf-canvas caps its own width: {canvas.group(0).strip()!r}"
    )
    assert "width:" not in canvas.group(0), (
        "the stylesheet sets the canvas width, overriding the computed one"
    )

    # And a graph wider than the card stays REACHABLE rather than being clipped.
    wrap = re.search(r"\n\.wf-canvas-wrap \{[^}]*\}", css, re.S)
    assert wrap is not None, ".wf-canvas-wrap has no rule"
    assert "overflow-x: auto" in wrap.group(0), (
        "the canvas does not scroll, so a graph too wide for the card is crushed"
    )



def test_no_overview_row_ends_with_a_blank_right_column():
    """The property, not the grid that used to satisfy it.

    IT WAS five panels in `.ov-cols` over two and three tracks, and the orphan
    was managed with `:last-child:nth-child(odd)` and
    `:last-child:nth-child(3n + 2)` parity rules. The landing page was then
    rewritten -- `.ov-cols` and both parity rules are gone -- and this
    assertion went red while the guarantee it exists for held perfectly:
    `.ov-grid` is THREE panels over ONE breakpoint, and the one that would be
    left alone in the second row is named and spans (`.ov-headroom`).

    So what is asserted is the guarantee. For every grid on this page that
    declares a fixed track count: the track count is EXPLICIT (an auto-fitting
    list cannot be asked how many tracks it produced, so no rule below it can
    be known to be correct), and if the panel count leaves an orphan in the
    last row, some child is given a span.

    READ FROM styles.css, where the Overview block lives since U8 folded the
    `OVERVIEW_CSS` template literal into the sheet (design-system.md §9.5
    asked for this test to be rewritten in the same change). The sheet has a
    dozen `@media (min-width: 1280px)` blocks, so the breakpoint is found as
    the one that sets `.ov-grid`'s tracks rather than as the first one.
    """
    css = src("styles.css")

    grids = re.findall(r"\n\.(ov-[a-z-]+) \{([^}]*)\}", css, re.S)
    multi = [
        (name, body)
        for name, body in grids
        if "display: grid" in body and f".{name} {{ grid-template-columns: repeat(" in css
    ]
    assert multi, "no Overview grid declares a fixed multi-track layout any more"

    for name, body in multi:
        # EXPLICIT TRACKS. This is the assertion the parity rules depended on
        # and it is the one that still matters: `auto-fit` makes the number of
        # tracks a runtime property of the container width, and nothing written
        # in CSS can then know which panel lands last.
        assert "auto-fit" not in body and "auto-fill" not in body, (
            f".{name} fits its own tracks, so nothing knows which panel lands in "
            f"the last row"
        )

    # AND NO ORPHAN IS LEFT UNMANAGED. `.ov-grid` holds three panels over two
    # tracks, so exactly one would sit alone; it spans. Asserted as a span
    # existing inside the same breakpoint that sets the track count, because a
    # span outside it would apply at the stacked width too, where it means
    # nothing and hides the mistake.
    two_track = next(
        (
            m for m in re.finditer(r"@media \(min-width: 1280px\) \{(.*?)\n\}", css, re.S)
            if ".ov-grid {" in m.group(1)
        ),
        None,
    )
    assert two_track is not None, "the Overview grid has no two-track breakpoint"
    assert ".ov-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in two_track.group(1)
    assert "grid-column: 1 / -1" in two_track.group(1), (
        "three panels in two tracks leaves one alone in the second row, and "
        "nothing spans it, so the right column ends blank under a column that "
        "is still going"
    )


def test_no_overview_panel_spans_tracks_from_an_inline_style():
    """An inline `gridColumn` shifts every rule below it invisibly.

    RENAMED FROM `test_the_alarm_span_is_in_css_where_the_parity_rules_can_see_it`,
    because the alarm span it was named after is deliberately gone: the
    `has-alarm` class fed the old five-card grid's orphan handling, and
    Overview.tsx says so where the count used to be computed -- "the grid
    because three panels in two tracks has no orphan to manage".

    What survives is the assertion that mattered, and it is the whole test now:
    a track span lives in CSS where every other rule can be read against it.
    An inline style is invisible to the stylesheet, so a panel spanning from
    one silently invalidates every layout rule that follows.
    """
    ov = src("Overview.tsx")
    assert "gridColumn" not in ov, "a panel still spans tracks from an inline style"
    # The span lives in the sheet now (U8 folded Overview's injected CSS into
    # styles.css), which is where "every other rule can be read against it"
    # was always pointing.
    assert re.search(r"\.ov-headroom \{ grid-column:", src("styles.css")), (
        "no panel spans tracks at all now; if that is deliberate, the grid must "
        "have no orphan -- which is what the test above measures"
    )


def test_the_bars_and_tabs_are_capped():
    """`flex: 1` drew two ~534px bars for seven tasks and three ~356px buttons
    for four-character labels. Every pixel of new page width made both worse."""
    css = src("styles.css")
    # RE-POINTED (#185, decision 7): the row-window chart's `.chart .col` and
    # its 72px cap are deleted with the page that drew them. The ledger draws
    # its bars in SVG, and its geometry caps each at 28px however wide the
    # drawing grows.
    ledger = src("charts/OutcomeLedger.tsx")
    bar = re.search(r"bar: Math\.max\(1, Math\.min\([^)]*\b28\)\)", ledger)
    assert bar is not None, "the ledger's bar is no longer capped: it grows with the drawing"
    tab = re.search(r"\n\.tabs button \{[^}]*\}", css, re.S)
    assert tab is not None and "flex: 0 0 auto" in tab.group(0), ".tabs button still fills the row"
    split = re.search(r"\n\.split-row \{[^}]*\}", css, re.S)
    assert split is not None and "minmax(0, 1fr)" not in split.group(0), (
        ".split-row's bar still takes the whole content column"
    )
