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
this file pins two things a served value still needs:

  1. the client CONSUMES it rather than recomputing it -- otherwise the
     restatement is still there, just better hidden;
  2. the DEV FIXTURE still shows a ceiling that is pointless to raise alone.

That the fixture is what the analyser produces over its pool rows, that its
reason groups are the served ones and that no screen declares a grouping of its
own are `contract.constants.test.ts`'s, against the generated constants (part
of #72).

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


def test_blocker_group_prefers_the_served_grouping():
    fn = body_of(src("types.ts"), "export function blockerGroup(")
    assert "blocker.group" in fn
    assert "groups.needs_action" in fn and "groups.no_room" in fn


# --------------------------------------------------------------------------
# 2. What the screens actually render -- MOVED OUT OF PYTHON
# --------------------------------------------------------------------------
#
# Six tests lived here and asserted on the TEXT of Capacity.tsx and
# Blockers.tsx. Every one of them was a claim about what a person SEES, and a
# source grep cannot make that claim: it cannot catch a render error, it cannot
# catch a runtime exception, and it passes when the string it looks for appears
# in a comment -- which, in files written in this repository's house style, is
# where most of these strings also appear. `test_an_incomplete_list_says_so`
# said so about itself in its own docstring.
#
# They are now rendered and asserted on the DOM, in
# apps/swarm-ui/src/__tests__/ (vitest + jsdom + Testing Library), run by
# `scripts/ui-test.sh` from `make test` and by the `ui` job in
# .github/workflows/application.yml. Replaced one for one:
#
#   test_the_capacity_board_renders_every_blocker_not_the_binding_one
#     -> honesty.capacity.test.tsx
#        "names EVERY refusing pool, so raising one is not mistaken for the fix"
#        -- counts the rendered chips instead of grepping for `h.blockers.map(`.
#
#   test_a_paused_blocker_is_drawn_differently_from_a_full_one
#     -> honesty.capacity.test.tsx
#        "draws a paused pool differently from a full one" -- reads the class
#        on the rendered element for a pool at 0 of 8, the case that looks
#        healthiest and is not.
#
#   test_an_incomplete_list_says_so
#     -> honesty.capacity.test.tsx, "an incomplete read" (four cases) -- asserts
#        the banner's rendered words AND that an empty blocker list under an
#        incomplete read is not reported as "no pool is refusing this profile".
#        The grep could not reach that second half at all.
#
#   test_a_missing_headroom_renders_an_em_dash_and_never_a_zero
#     -> honesty.capacity.test.tsx, "renders an em dash, not a 0, when the API
#        sent no admission block" plus "renders 0 when zero is what was
#        measured". Both the em dash and the 0 are in the SOURCE of those
#        components, so no grep can tell those two cases apart; only rendering
#        can, and both halves are needed or the rule is kept by accident.
#
#   test_the_counterfactual_is_worded_as_a_snapshot_and_not_as_a_promise
#     -> honesty.capacity.test.tsx, "is worded in the past conditional, against
#        the server's own instant" -- and additionally checks the rendered
#        <time dateTime> carries `generated_at`.
#
#   test_a_zero_delta_names_what_still_binds
#     -> honesty.capacity.test.tsx, "a zero delta names what still binds".
#
# REMOVED 2026-10-01 (owner's decision): the surfaces those six DOM tests
# rendered -- Pools' `Could start / Held back by` panel, the Profile headroom
# cards, their blocker list, incomplete-read banner and counterfactual column
# -- were deleted with the rebrand, and the tests went with them. A refusing
# pool's tag and figure are still asserted on the row Submit draws
# (waitreason.test.tsx, blockers.belowunits.test.tsx); a disabled profile on
# the Profiles matrix (honesty.capacity.test.tsx, CP-3).
#
# WHAT STAYED IN PYTHON, and why it is not a grep in disguise. Section 1 above
# asserts the ABSENCE of arithmetic in `headroomFor` -- `Infinity`,
# `Math.floor`, `.available`. No behavioural test can assert that a function
# does not re-derive something: a re-derivation that happens to agree with the
# server produces identical pixels. That is a genuine static property of the
# source, and it is the one that collapsed a list of blockers to a single pool.
# The comparison of the dev fixture with what `analyse_profile` produces moved
# to `contract.constants.test.ts` (part of #72), which reads the analyser's own
# answer from the generated constants.

# --------------------------------------------------------------------------
# 3. The dev fixture teaches the rule
# --------------------------------------------------------------------------

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
