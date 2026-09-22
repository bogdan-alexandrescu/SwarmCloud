"""The browser renders the server's blocker list; it does not compute one.

THE DEFECT THIS PINS. `evaluate_capacity` returns a LIST -- one entry per pool
that refused -- and `headroomFor` in `apps/swarm-ui/src/types.ts` re-derived it
in TypeScript, keeping only the running minimum. `binding = name` overwrote on
each new minimum, so with `resource:large` and `provider:anthropic` both at
their ceilings the screen named one of them. An operator raised it, nothing
moved, and the pool still refusing appeared nowhere.

WHY A TEST HERE RATHER THAN A PARITY CHECK. `scripts/lib/check-contract-parity.sh`
reads shell and jq and no `.ts` file at all, and neither `make lint` nor
`make test` runs `tsc`. `docs/contract-change-requests.md` records the
conclusion under "Why these requests keep arising": where one reader is
TypeScript, a route that serves the value is the only remedy this repository
has made work. A parity check can pin a TABLE of constants; the counterfactual
is an algorithm, and no text comparison holds an algorithm to another one.

So the rule moved to `swarm_api/headroom.py`, `/v1/capacity` serves it, and
this file pins the two things a served value still needs:

  1. the client CONSUMES it rather than recomputing it -- otherwise the
     restatement is still there, just better hidden;
  2. the DEV FIXTURE is byte-for-byte what the analyser produces over the pool
     rows beside it, so development cannot be done against a shape the API
     never sends. `test_runtimes_screen.py` makes the same demand of the
     runtime fixture, for the same reason.

The TypeScript is read as text. No node, no network, no credentials -- the same
technique `test_dispatch_ui_surface.py` and `test_runtimes_screen.py` use, and
for the same reason: a mock of the UI would agree with whatever the UI happens
to do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_common.models import SlotPool
from swarm_common.states import BlockedReason

from swarm_api.headroom import analyse_profile, blocked_reason_groups

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing seam tests: this suite has to pass in a tree that holds only the
#: Python components.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui is not checked out")


def src(name: str) -> str:
    return (UI / name).read_text()


def body_of(text: str, signature: str) -> str:
    """One top-level function's source, signature to closing brace.

    Terminated on a `}` in column 1 rather than by counting braces. Brace
    counting has to be told apart from destructured parameters (`{ h, groups }`)
    and inline return types (`: { text: string }`), both of which open before
    the body does; every function read here is top-level, so its closing brace
    is the only one at the start of a line.
    """
    start = text.index(signature)
    end = text.index("\n}\n", start)
    return text[start : end + 3]


def json_literal(text: str, declaration: str) -> object:
    """A strict-JSON object literal assigned in the TypeScript source."""
    start = text.index(declaration)
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[i : j + 1])
    raise AssertionError(f"unbalanced braces after {declaration!r}")


# --------------------------------------------------------------------------
# 1. The client consumes the decision instead of making a second one
# --------------------------------------------------------------------------

def test_headroom_for_computes_nothing():
    """It reads `profile.admission`. Arithmetic here is the bug coming back.

    Each token below was in the old body and is the signature of a private
    re-derivation: `Infinity` seeded the running minimum, `Math.floor` divided
    free units by the weight, and `pool.available` / `effective_limit` are the
    pool fields it did that arithmetic on.
    """
    fn = body_of(src("types.ts"), "export function headroomFor(")
    assert "profile.admission" in fn, "headroomFor must read the served block"
    for token in ("Infinity", "Math.floor", "Math.max", ".available", "effective_limit"):
        assert token not in fn, (
            f"headroomFor re-derives admission ({token!r} in its body). "
            "The list of blockers is served by /v1/capacity; recomputing it "
            "here is the restatement that collapsed the list to one pool."
        )


def test_headroom_for_takes_no_pool_map():
    """A pool map argument is what a recomputation needs and a reader does not."""
    signature = re.search(
        r"export function headroomFor\(([^)]*)\)", src("types.ts"), re.S
    )
    assert signature is not None
    assert "poolsByName" not in signature.group(1)
    assert "Map" not in signature.group(1)


def test_no_screen_declares_its_own_grouping_of_reasons():
    """The split lives in Python and travels as data on the response.

    What is forbidden is a GROUP -- several reason names collected together,
    which is a copy of a decision nothing in this repository would compare to
    the enum. What is allowed is a reason named on its own:

      * `REASON_COPY` in types.ts is one prose sentence per reason, keyed by
        it. That is a phrasebook, not a grouping, and it already falls back to
        printing an unknown reason verbatim so the staleness is visible;
      * a renderer tests for `MANUAL_PAUSE` alone to choose a purple "paused"
        chip over a red "full" one. Those two have opposite remedies and the
        group they share cannot say which, so the single name is load-bearing.

    A line carrying two or more of them is the shape a grouping takes.
    """
    for name in ("types.ts", "Blockers.tsx", "Capacity.tsx", "Profiles.tsx"):
        # The `ParkReason` union is cut out first. It is a DIFFERENT enum --
        # `states.ParkReason`, why a task is durably ineligible -- that happens
        # to spell two of its members the same way (`MANUAL_PAUSE`,
        # `SCHEDULED_RETRY`). It is a type declaration this change does not
        # touch, and reading it as a grouping of blocker reasons would be the
        # test misunderstanding the file rather than the file being wrong.
        text = re.sub(r"export type ParkReason =(?:\n\s*\|[^\n]*)+", "", src(name))
        for number, line in enumerate(text.splitlines(), start=1):
            # Comments are prose, not a grouping. types.ts documents at length
            # WHICH members of the enum are never written as blockers and why
            # its copy table omits them -- that reasoning is the kind of thing
            # this repository keeps in the file on purpose, and deleting it to
            # satisfy a lint would remove the record and keep nothing.
            stripped = line.lstrip()
            if stripped.startswith(("*", "//", "/*")):
                continue
            # Whole tokens. A substring test matches `PROVIDER_COOLDOWN` on
            # `COOLDOWN` and `PROVIDER_QUOTA_EXHAUSTED` on `QUOTA_EXHAUSTED`,
            # which would flag the pre-existing `ParkReason` union -- a
            # different enum that this change does not touch.
            hits = [
                r.value
                for r in BlockedReason
                if re.search(rf"(?<![A-Z_]){re.escape(r.value)}(?![A-Z_])", line)
            ]
            assert len(hits) < 2, (
                f"{name}:{number} lists {hits} together, which is a grouping. "
                "The grouping is served as `blocked_reason_groups`; a copy "
                "here drifts the first time the enum gains a member."
            )
        for literal in ("needs_action: [", "no_room: [", "needs_action = [", "no_room = ["):
            assert literal not in text, f"{name} declares its own {literal!r}"


def test_blocker_group_prefers_the_served_grouping():
    fn = body_of(src("types.ts"), "export function blockerGroup(")
    assert "blocker.group" in fn
    assert "groups.needs_action" in fn and "groups.no_room" in fn


# --------------------------------------------------------------------------
# 2. What the screens actually render
# --------------------------------------------------------------------------

def test_the_capacity_board_renders_every_blocker_not_the_binding_one():
    """`h.blockers.map` is the fix; `h.binding` alone in that cell was the bug."""
    fn = body_of(src("Capacity.tsx"), "function HeldBackBy(")
    assert "h.blockers.map(" in fn, (
        "the 'Held back by' cell must list every refusing pool -- mapping the "
        "list is the difference between naming one ceiling and naming all of them"
    )


def test_a_paused_blocker_is_drawn_differently_from_a_full_one():
    """Same headroom of 0, opposite remedies, so they cannot share a chip.

    Both files pick the class from the REASON rather than from the count: a
    paused pool can read 0 of 8 units in use and still admit nothing, which is
    the case that looks healthiest and is not.
    """
    for name, fn_sig in (("Capacity.tsx", "function HeldBackBy("), ("Blockers.tsx", "function BlockerRow(")):
        fn = body_of(src(name), fn_sig)
        assert "MANUAL_PAUSE" in fn, f"{name} must tell a pause from a full pool"
        assert "'paused'" in fn or '"paused"' in fn or "tag paused" in fn
        assert "'full'" in fn or '"full"' in fn or "tag full" in fn


def test_an_incomplete_list_says_so():
    """The word has to reach the screen, not just the function's own name.

    Asserted against the rendered markup rather than against the source text:
    `IncompleteNote` contains "incomplete" in its identifier, so a substring
    test over the whole body passes even when the banner has been reworded to
    say the opposite.
    """
    fn = body_of(src("Blockers.tsx"), "export function IncompleteNote(")
    assert "h.complete" in fn, "it must render only when the list is partial"
    assert "<strong>incomplete</strong>" in fn, (
        "the banner has to say the list is incomplete in words -- an "
        "incomplete blocker list presented as complete tells an operator they "
        "have cleared everything when they have not"
    )
    assert "h.unread" in fn, "it must say WHICH pools it could not see"


def test_a_missing_headroom_renders_an_em_dash_and_never_a_zero():
    fn = body_of(src("Blockers.tsx"), "export function headroomFigure(")
    assert "h.agents === null" in fn
    # Either spelling: the file may carry the character or the escape.
    assert "\u2014" in fn or "\\u2014" in fn, "not measured must render as an em dash"
    # And the two kinds of not-measured are worded differently, because
    # "nothing caps this" and "a pool could not be read" are opposite facts.
    assert "uncapped" in fn


def test_the_counterfactual_is_worded_as_a_snapshot_and_not_as_a_promise():
    """It is a prediction, and this is where a screen most easily starts lying.

    A lease can be released between the read and the render, so every line is
    past-conditional and anchored to the server's own `generated_at` rather
    than to the browser's clock.
    """
    text = src("Blockers.tsx")
    fn = body_of(text, "function counterfactualText(")
    assert "would have" in fn, "the effect must be past-conditional"
    for promise in ("will start", "you can start", "can start now", "are free"):
        assert promise not in fn.lower(), f"{promise!r} states a prediction as a fact"
    panel = body_of(text, "export function Counterfactuals(")
    assert "generatedAt" in panel, "the snapshot's own timestamp must be shown"
    assert "instant" in panel or "at that" in panel


def test_a_zero_delta_names_what_still_binds():
    """A bare "no change" reads as a glitch and gets ignored."""
    fn = body_of(src("Blockers.tsx"), "function counterfactualText(")
    assert "next_binding" in fn
    assert "still binds" in fn


# --------------------------------------------------------------------------
# 3. The dev fixture is the server's answer, not an invented one
# --------------------------------------------------------------------------

FIXTURE_PROFILES = {
    "mock": (1, ["global", "tenant:u-bogdan", "resource:standard", "runner:mock",
                 "backend:CLOUD_RUN_JOB"]),
    "generic": (1, ["global", "tenant:u-bogdan", "resource:standard", "runner:generic",
                    "backend:CLOUD_RUN_JOB"]),
    "claude-code": (1, ["global", "tenant:u-bogdan", "resource:standard",
                        "runner:claude-code", "backend:CLOUD_RUN_JOB",
                        "provider:anthropic", "provider:anthropic:tenant:u-bogdan"]),
    "codex": (1, ["global", "tenant:u-bogdan", "resource:standard", "runner:codex",
                  "backend:CLOUD_RUN_JOB", "provider:openai"]),
    "browser": (2, ["global", "tenant:u-bogdan", "resource:browser", "runner:browser",
                    "backend:GKE_AUTOPILOT", "provider:anthropic",
                    "provider:anthropic:tenant:u-bogdan"]),
}


def fixture_pools() -> dict[str, SlotPool]:
    """The pool rows in `fixtureCapacity`, read out of the TypeScript.

    Parsed rather than copied: a second hand-written table here would drift
    from the fixture exactly the way the fixture must not drift from the API.
    """
    text = src("api.ts")
    start = text.index("async function fixtureCapacity(")
    chunk = text[start : text.index("async function fixtureTasks(", start)]
    pools: dict[str, SlotPool] = {}
    for m in re.finditer(
        r"pool\('([^']+)',\s*(\d+),\s*(\d+)(?:,\s*\{([^}]*)\})?\)", chunk
    ):
        name, hard, active, extra = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4) or ""
        enabled = "enabled: false" not in extra
        adaptive = re.search(r"adaptive_target:\s*(\d+)", extra)
        pools[name] = SlotPool(
            name=name,
            hard_limit=hard,
            active=active,
            enabled=enabled,
            adaptive_target=int(adaptive.group(1)) if adaptive else None,
        )
    assert len(pools) >= 15, f"the fixture pool rows did not parse: {sorted(pools)}"
    return pools


def test_the_fixture_admission_block_is_what_the_analyser_produces():
    """Development happens against the real shape or it proves nothing.

    If this fails, the fixture's pool rows were edited without regenerating the
    `admission` literal beside them. Regenerate it -- do not compute it in
    TypeScript, which is the restatement the served block removed.
    """
    fixture = json_literal(src("api.ts"), "const FIXTURE_ADMISSION")
    pools = fixture_pools()
    assert set(fixture) == set(FIXTURE_PROFILES)
    for name, (units, required) in FIXTURE_PROFILES.items():
        readable = {n: pools[n] for n in required if n in pools}
        expected = analyse_profile(required=required, pools=readable, units=units)
        assert fixture[name] == expected, f"fixture for {name!r} is not what the server sends"


def test_the_fixture_reason_groups_are_the_served_ones():
    fixture = json_literal(src("api.ts"), "const FIXTURE_REASON_GROUPS")
    assert fixture == blocked_reason_groups()


def test_the_fixture_still_shows_a_ceiling_that_is_pointless_to_raise():
    """The zeros are the half of the counterfactual that teaches the rule.

    A fixture whose every profile has exactly one binding pool would render a
    panel in which raising the named ceiling always works -- which is the
    belief the minimum-across-pools rule punishes. At least one profile must
    have several ceilings tied, so that lifting any one of them buys nothing.
    """
    fixture = json_literal(src("api.ts"), "const FIXTURE_ADMISSION")
    tied = [
        name for name, a in fixture.items()
        if len(a["binding"]) > 1 and all(c["delta"] == 0 for c in a["counterfactual"])
    ]
    assert tied, (
        "no fixture profile has two ceilings binding at once, so development "
        "never sees the case this panel was built for"
    )
