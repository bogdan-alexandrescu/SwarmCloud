"""The web UI's dispatch control, and the seams it sits on.

`strategy` and `carrier` were accepted by swarm-api, stored on the task, honoured
by the worker -- and reachable from nothing a person could click. This file is
the contract for the surface that closes that, and it is deliberately written as
SEAM tests rather than as rendering tests, because the failure this repository
keeps producing is not a component that renders badly. It is two ends built and
nothing in the middle.

Three seams, each with its own section:

  1. VOCABULARY. Every strategy and carrier the UI offers must be one swarm-api
     accepts, and every one swarm-api accepts must be offered. The first half is
     not hypothetical: the lane brief that asked for this control described the
     carriers as `patches | branches`, which is the WORKER's spelling. swarm-api
     accepts `checkpoints | branches` and refuses `patches` outright, so a UI
     built from that description would have 422'd on every submission it made.

  2. THE REQUEST BODY. The exact JSON the submit forms build, posted at the real
     API. `TaskCreate` and `WorkflowCreate` are `extra="forbid"`, so a field the
     UI invents is a 422 naming it, and a field it omits is a silent default.

  3. THE PUBLISH REASON. The worker writes a free-text `publish_reason` on every
     path including the successful ones, and the agent detail screen renders a
     diagnosis from it. Two of those paths are outcomes a caller ASKED FOR --
     `collect` publishes nothing, an `integrate` contributor opens nothing -- and
     both used to render as failures, one of them telling the reader to open a
     pull request by hand against a strategy whose entire promise is that there
     is only one.

No network, no credentials, no node: the TypeScript is read as text, which is the
same technique `test_every_path_the_ui_calls_exists_on_this_api` uses and for the
same reason -- a mock of the UI would agree with whatever the UI happens to do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_api.validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    DISPATCH_CARRIERS,
    DISPATCH_STRATEGIES,
)

from .conftest import auth_header

REPO = "https://github.com/saga-xyz/example.git"

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"
WORKER_LIFECYCLE = ROOT / "apps/agent-worker/agent_worker/lifecycle.py"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing api.ts seam test: this suite has to pass in a tree that holds only
#: the Python services.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")


def _src(name: str) -> str:
    return (UI / name).read_text()


def _string_array(source: str, const: str) -> list[str]:
    """The string literals of `export const <const> = [...] as const`.

    Parsed rather than imported because there is no JavaScript runtime in this
    suite. The match is anchored on the declaration so a mention of the name in
    a comment cannot satisfy it.
    """
    match = re.search(
        rf"export const {re.escape(const)}\s*=\s*\[(.*?)\]\s*as const", source, re.S
    )
    assert match is not None, f"{const} is not declared in types.ts as a const array"
    return re.findall(r"'([^']*)'", match.group(1))


# --------------------------------------------------------------------------
# 1. The vocabulary seam
# --------------------------------------------------------------------------

def test_every_strategy_the_ui_offers_is_one_this_api_accepts():
    """The UI must not offer a word the API refuses.

    `_accepted_value` rejects an unknown strategy at submission, so an option
    the API does not know is a control that 422s every time it is used -- and
    the caller is shown a refusal about a value they picked from a list the
    product gave them.
    """
    offered = _string_array(_src("types.ts"), "DISPATCH_STRATEGIES")
    assert offered, "types.ts offers no strategies at all"
    unknown = sorted(set(offered) - set(DISPATCH_STRATEGIES))
    assert not unknown, (
        f"the UI offers strategies this API refuses: {unknown}; "
        f"accepted values are {list(DISPATCH_STRATEGIES)}"
    )


def test_every_carrier_the_ui_offers_is_one_this_api_accepts():
    """THE `patches` TRAP, pinned.

    agent-worker's `_dispatch_carrier` accepts `("patches", "branches")` and
    swarm-api accepts `("checkpoints", "branches")` -- a live disagreement
    recorded in docs/contract-change-requests.md. The browser talks to
    swarm-api, so the UI must speak swarm-api's vocabulary; a UI that copied the
    worker's would refuse every submission a caller made.
    """
    offered = _string_array(_src("types.ts"), "DISPATCH_CARRIERS")
    assert offered, "types.ts offers no carriers at all"
    unknown = sorted(set(offered) - set(DISPATCH_CARRIERS))
    assert not unknown, (
        f"the UI offers carriers this API refuses: {unknown}; "
        f"accepted values are {list(DISPATCH_CARRIERS)}. Note that 'patches' is "
        "the WORKER's spelling of the default and swarm-api refuses it."
    )


def test_every_strategy_and_carrier_this_api_accepts_is_reachable_in_the_ui():
    """The other direction, which is the one that fails quietly.

    A strategy the API accepts and the UI does not offer is a feature that
    exists, is documented, is tested, and that no caller can ever choose.
    """
    strategies = _string_array(_src("types.ts"), "DISPATCH_STRATEGIES")
    carriers = _string_array(_src("types.ts"), "DISPATCH_CARRIERS")
    assert sorted(strategies) == sorted(DISPATCH_STRATEGIES), (
        "the UI does not offer every strategy this API accepts: "
        f"missing {sorted(set(DISPATCH_STRATEGIES) - set(strategies))}"
    )
    assert sorted(carriers) == sorted(DISPATCH_CARRIERS), (
        "the UI does not offer every carrier this API accepts: "
        f"missing {sorted(set(DISPATCH_CARRIERS) - set(carriers))}"
    )


def test_the_ui_defaults_are_the_api_defaults():
    """A caller who touches nothing must get the dispatch they got before.

    `collect`/`checkpoints` is what makes a read-only tenant token keep working,
    so a UI that pre-selected anything else would start pushing on behalf of
    deployments that never asked for it.
    """
    source = _src("types.ts")
    assert re.search(
        rf"export const DEFAULT_STRATEGY: DispatchStrategy = '{re.escape(DEFAULT_STRATEGY)}'",
        source,
    ), f"the UI's default strategy is not {DEFAULT_STRATEGY!r}"
    assert re.search(
        rf"export const DEFAULT_CARRIER: DispatchCarrier = '{re.escape(DEFAULT_CARRIER)}'",
        source,
    ), f"the UI's default carrier is not {DEFAULT_CARRIER!r}"


def test_both_submit_screens_send_the_choice():
    """The control has to be WIRED, not merely present.

    A picker that sets state nothing sends is the same defect in a new place:
    the screen says `direct-pr`, the request says nothing, and the API applies
    its default. Both forms build their body inline, so the keys are asserted in
    the source of each.
    """
    for screen in ("Submit.tsx", "SubmitWorkflow.tsx"):
        source = _src(screen)
        assert "strategy: dispatch.strategy" in source, f"{screen} does not send `strategy`"
        assert "carrier: dispatch.carrier" in source, f"{screen} does not send `carrier`"
        assert "repository_url: repo" in source, f"{screen} does not send `repository_url`"


# REPLACED BY A COMPONENT TEST: test_the_default_option_never_promises_a_pull_request
#
# It read the `case 'collect':` arm of `consequenceOf` out of types.ts and
# asserted on the literals in it. The claim is about what a person picking the
# default is told, and the literals are also what the file's own explanatory
# comments quote -- so the grep passed on a `consequenceOf` that had been
# rewritten to return the right object from the wrong branch.
#
# Now: apps/swarm-ui/src/__tests__/dispatch.test.ts, "says plainly that collect
# pushes nothing" and "the default option never promises a pull request", which
# CALL `consequenceOf` and `STRATEGY_LABEL[DEFAULT_STRATEGY]` and read the
# values back -- across every strategy and 0, 1, 2 and 7 steps, which also
# catches the broken plurals a grep never looked for.


# --------------------------------------------------------------------------
# 2. The request-body seam
# --------------------------------------------------------------------------
# The bodies below are what the forms build, written out here so a change on
# either side shows up as a failure rather than being followed silently. Every
# key is one `Submit.tsx` / `SubmitWorkflow.tsx` actually puts in the object.

def test_the_task_body_the_submit_form_builds_is_accepted(client, db):
    body = {
        "runner_profile": "mock",
        "input": {"prompt": "audit the destroy guard"},
        "strategy": "direct-pr",
        "carrier": "checkpoints",
        "repository_url": REPO,
    }
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    # Read back off the 201, because that is what `Created` renders.
    assert task["dispatch"] == {
        "strategy": "direct-pr",
        "carrier": "checkpoints",
        "role": None,
        "integrates": [],
    }
    assert task["repository_url"] == REPO


def test_the_task_form_omits_the_repository_rather_than_sending_an_empty_one():
    """`""` fails the scheme validator with a message about URL schemes.

    Which is a 422 about the wrong thing: the field is not malformed, it is
    blank, and `repository_url` is `str | None`. The form omits the key.
    """
    for screen in ("Submit.tsx", "SubmitWorkflow.tsx"):
        source = _src(screen)
        assert "repo === '' ? {} : { repository_url: repo }" in source, (
            f"{screen} must omit `repository_url` when it is blank, not send an empty string"
        )


def test_an_empty_repository_url_is_in_fact_refused(client):
    """The reason the form omits it, measured rather than assumed."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "repository_url": ""},
    )
    assert response.status_code == 422, response.text
    assert "scheme" in response.text.lower() or "url" in response.text.lower()


def test_the_workflow_body_the_form_builds_is_accepted(client):
    """Six steps into one pull request: the case the control exists to explain.

    `input` is on every step because the form now puts it there. It was absent
    from this body for as long as it was absent from the form, and a fixture
    that agrees with a broken screen is how the blocker survived an audit: the
    request shape was asserted, accepted and green, and every workflow it
    described still failed at the agent. `test_workflow_step_input_surface.py`
    owns that seam; this body simply must not go back to contradicting it.
    """
    body = {
        "steps": [
            {"step_id": "plan", "runner_profile": "mock", "input": {"prompt": "plan"}, "depends_on": []},
            {"step_id": "scan-a", "runner_profile": "mock", "input": {"prompt": "a"}, "depends_on": ["plan"]},
            {"step_id": "scan-b", "runner_profile": "mock", "input": {"prompt": "b"}, "depends_on": ["plan"]},
            {"step_id": "scan-c", "runner_profile": "mock", "input": {"prompt": "c"}, "depends_on": ["plan"]},
            {"step_id": "scan-d", "runner_profile": "mock", "input": {"prompt": "d"}, "depends_on": ["plan"]},
            {
                "step_id": "report",
                "runner_profile": "mock",
                "input": {"prompt": "report"},
                "depends_on": ["scan-a", "scan-b", "scan-c", "scan-d"],
            },
        ],
        "strategy": "integrate",
        "carrier": "checkpoints",
        "repository_url": REPO,
    }
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    echo = response.json()["dispatch"]
    # The three keys `DispatchEcho` in SubmitWorkflow.tsx reads.
    assert echo["strategy"] == "integrate"
    assert echo["carrier"] == "checkpoints"
    # `Accepted` renders this as "Step <id> opens it". Six steps, one PR, and
    # the UI names which step opens it rather than leaving it a promise.
    assert echo["integrator_step_id"] == "report"


def test_the_forms_terminal_preview_agrees_with_the_api_on_the_integrator(client):
    """The preview names a step; the API decides. They must not disagree.

    SubmitWorkflow computes "the ids nothing else depends on" and shows the
    single one as the step that would open the pull request.
    `resolve_integrator_step` picks the graph's only sink. Same graph, same
    answer -- asserted here against the API rather than trusted, because the
    preview is the part a caller reads BEFORE there is an API answer to compare.
    """
    steps = [
        {"step_id": "a", "runner_profile": "mock", "depends_on": []},
        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
        {"step_id": "c", "runner_profile": "mock", "depends_on": ["b"]},
    ]
    # What the form computes, in the same one line it computes it in.
    ids = [s["step_id"] for s in steps]
    depended_on = {d for s in steps for d in s["depends_on"]}
    terminals = [i for i in ids if i not in depended_on]
    assert terminals == ["c"]

    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": steps, "strategy": "integrate", "repository_url": REPO},
    )
    assert response.status_code == 201, response.text
    assert response.json()["dispatch"]["integrator_step_id"] == terminals[0]


def test_integrate_on_a_single_task_is_why_the_option_is_disabled(client):
    """The control shows `integrate` disabled at task scale. This is the reason.

    Asserted so the disabled state is grounded in the API's behaviour rather
    than in a belief about it -- if this ever became legal, the option would be
    disabled for no reason and nobody would notice.
    """
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "integrate", "repository_url": REPO},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_the_422_detail_keys_the_form_attributes_on_are_the_ones_sent(client):
    """`attribute()` pins a refusal to a field by its DETAIL KEY, not its prose.

    Those keys are `accepted_strategies`, `accepted_carriers` and
    `missing: "repository_url"`. A message under the wrong input sends someone
    editing a value that was never the problem, so the keys are measured.
    """
    unknown_strategy = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "pr"},
    ).json()
    assert "accepted_strategies" in unknown_strategy["detail"]

    unknown_carrier = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "carrier": "patches"},
    ).json()
    assert "accepted_carriers" in unknown_carrier["detail"]

    no_repo = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "direct-pr"},
    ).json()
    assert no_repo["detail"]["missing"] == "repository_url"

    source = _src("Submit.tsx")
    for key in ("accepted_strategies", "accepted_carriers", "repository_url"):
        assert key in source, f"Submit.tsx does not attribute a refusal on {key!r}"


def test_the_ui_task_type_declares_every_key_the_api_sends_in_dispatch(client, db):
    """`TaskDispatch` mirrors `codec.dispatch_of` by hand; nothing checks that.

    types.ts says as much in its own header -- "If a field changes there, nothing
    here notices". For THIS block it does now, because a missing key is a fact
    about the run that no screen can show.
    """
    created = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    sent = set(created.json()["task"]["dispatch"])

    block = re.search(r"export interface TaskDispatch \{(.*?)\n\}", _src("types.ts"), re.S)
    assert block is not None, "types.ts no longer declares TaskDispatch"
    declared = set(re.findall(r"^\s{2}(\w+)[?]?:", block.group(1), re.M))
    assert declared == sent, (
        f"TaskDispatch and codec.dispatch_of disagree: "
        f"only in the API {sorted(sent - declared)}, only in the UI {sorted(declared - sent)}"
    )


# --------------------------------------------------------------------------
# 3. The publish-reason seam
# --------------------------------------------------------------------------
# `AgentDetail.PublishOutcome` turns the worker's free-text `publish_reason`
# into a diagnosis. These are the worker's exact strings for the two outcomes
# that are CORRECT BY REQUEST. Kept literal rather than imported so a change on
# the worker side shows up here as a failure rather than being silently
# followed -- the same reason `test_artifacts_read_path` keeps FINISHED_SUMMARY.

COLLECT_REASON = (
    "strategy is 'collect': the patch is harvested and nothing "
    "is pushed. Submit with strategy 'direct-pr' to open a pull "
    "request from this agent"
)

CONTRIBUTOR_REASON = (
    "strategy is 'integrate' and this step is a contributor: its "
    "branch was pushed and no pull request was opened. The "
    "integrator step merges this branch and opens the single pull "
    "request for the whole workflow."
)


@pytest.mark.skipif(
    not WORKER_LIFECYCLE.exists(), reason="agent-worker is not present"
)
@pytest.mark.parametrize(
    "reason", [COLLECT_REASON, CONTRIBUTOR_REASON], ids=["collect", "contributor"]
)
def test_the_quoted_worker_reasons_are_still_the_worker_s(reason: str):
    """The copies above must still match `_publish`, or the matchers below lie.

    Compared with whitespace collapsed: the worker's are implicitly-concatenated
    literals split across source lines, so the line breaks are an artefact of
    formatting rather than part of the string.
    """
    source = " ".join(WORKER_LIFECYCLE.read_text().split())
    needle = " ".join(reason.split())
    # The worker's literal is split across lines with quotes between the parts,
    # so the joined source carries those quotes; comparing the distinctive
    # opening clause is what survives a reflow.
    opening = " ".join(needle.split()[:6])
    assert opening in source, (
        f"agent-worker no longer produces a publish_reason beginning {opening!r}; "
        "the UI matches on it as a fallback and would stop recognising this outcome"
    )


def test_the_detail_screen_recognises_collect_rather_than_calling_it_a_failed_push():
    """THE REGRESSION.

    `collect` is the DEFAULT, so this was the common case: nothing was pushed by
    request, and the screen ended on "Not published, and the reason is git's own
    -- a rejected push usually means something else moved the branch". Nothing
    was pushed and nothing was rejected.

    The fix keys on the structured dispatch, with the prose as a fallback. Both
    are asserted: the structured path for tasks that carry a dispatch, the prose
    for the ones that do not.
    """
    source = _src("AgentDetail.tsx")
    assert "const isCollect =" in source, "PublishOutcome no longer identifies `collect`"
    assert "strategy === 'collect'" in source, (
        "`collect` must be recognised from the stored dispatch, not only from prose"
    )
    assert "lower.includes(\"strategy is 'collect'\")" in source, (
        "the prose fallback must match the worker's own words, for a task carrying "
        "no dispatch block"
    )
    # And it must be decided BEFORE the endings that describe a push: on the
    # collect path the worker returns before `probe_repository`, so can_push,
    # branch and the git error are all absent.
    assert source.index("if (isCollect) {") < source.index("if (git.can_push === false) {"), (
        "`collect` must be diagnosed before the can_push branch: the forge is "
        "never contacted on that path, so can_push is absent rather than false"
    )


def test_the_detail_screen_does_not_tell_a_contributor_to_open_a_pull_request():
    """The second wrong answer, and the more expensive one.

    A contributor publishes `published: true` with no pull request, which read
    as "only the pull-request call did not complete, so opening one by hand from
    that branch is all that is left". Doing that produces a second pull request
    against a strategy whose whole promise is exactly one -- the defect 1cfdf57
    fixed in the worker, reintroduced by the screen describing it.
    """
    source = _src("AgentDetail.tsx")
    assert "const isContributor =" in source, "PublishOutcome no longer identifies a contributor"
    assert source.index("if (isContributor && git.published === true) {") < source.index(
        "if (git.published === true) {"
    ), (
        "the contributor case must be decided before the general pushed-but-no-PR "
        "case, which tells the reader to open one by hand"
    )
    # The instruction itself, so a future edit cannot quietly drop it.
    assert "Do not open one from this branch" in source


def test_the_publish_reason_strings_reach_the_screen_through_the_worker_shape(client, db):
    """End to end on the data, not on the source: a stored summary reads back.

    `result_summary` is a free-form dict and the git block lives inside it, so
    this asserts the two shapes the screen branches on survive a real write and
    a real read through `GET /v1/tasks/{id}` -- the request `loadAgentRun` makes.
    """
    created = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "collect"},
    )
    task_id = created.json()["task"]["id"]
    db.docs[f"tasks/{task_id}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{task_id}"]["result_summary"] = {
        "git": {"strategy": "collect", "published": False, "publish_reason": COLLECT_REASON},
    }

    read = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice")).json()["task"]
    git = read["result_summary"]["git"]
    assert git["strategy"] == "collect"
    assert git["published"] is False
    assert read["dispatch"]["strategy"] == "collect"


# --------------------------------------------------------------------------
# The read-back surface
# --------------------------------------------------------------------------

# REPLACED BY A COMPONENT TEST: test_an_api_that_reports_no_dispatch_is_not_rendered_as_collect
#
# It counted `return null` statements in the body of `dispatchOf` and asserted
# the default strategy's literal was absent from it. Both are proxies: a
# function with two `return null` statements on unreachable branches passes,
# and so does one that reaches the right branch and then has its answer
# discarded by the caller.
#
# Now: apps/swarm-ui/src/__tests__/dispatch.test.ts, which calls `dispatchOf`
# with a missing block, a null block, an array, a scalar, the worker's
# `patches` spelling and an unknown strategy, and asserts null for each --
# alongside the positive case, without which "always null" would pass.


def test_the_workflow_board_does_not_invent_a_strategy_when_the_join_failed():
    """A failed task read must not render as a workflow that publishes nothing.

    This screen's whole premise -- and the banner directly above this line --
    is that a failed read never renders as empty data. A dispatch rolled up from
    zero joined tasks is exactly that.
    """
    source = _src("Workflows.tsx")
    assert "workflowDispatchOf" in source, "the board no longer rolls up a dispatch"
    assert "joined" in source, (
        "the board must distinguish 'no task was joined' from 'no dispatch was reported'"
    )
    dispatch_src = _src("Dispatch.tsx")
    assert re.search(r"return found === null \? null :", dispatch_src), (
        "workflowDispatchOf must return null when no task carried a dispatch, "
        "rather than falling back to the default pair"
    )


def test_every_fixture_task_carries_a_dispatch_the_api_could_have_sent():
    """Fixtures are the development API, so they must not be a shape it cannot produce.

    A fixture with no `dispatch` would exercise only the "this API did not report
    a dispatch" path -- the one production should never take -- and leave every
    other rendering untested until it reached the real platform.
    """
    source = _src("api.ts")
    blocks = re.findall(r"dispatch: \{\s*strategy: '([^']+)',\s*carrier: '([^']+)'", source)
    assert blocks, "api.ts fixtures carry no dispatch block at all"
    for strategy, carrier in blocks:
        assert strategy in DISPATCH_STRATEGIES, f"fixture strategy {strategy!r} is not accepted"
        assert carrier in DISPATCH_CARRIERS, f"fixture carrier {carrier!r} is not accepted"
    # And the default is represented, because it is what most tasks are.
    assert any(s == DEFAULT_STRATEGY for s, _ in blocks)
    # ...as is the shape that opens exactly one pull request, which is the whole
    # point of the control and the only one with a role to render.
    assert any(s == "integrate" for s, _ in blocks), (
        "no fixture exercises `integrate`, so the integrator badge, the "
        "contributor publish panel and the one-PR rollup render nowhere in development"
    )


def test_the_fixture_dispatch_block_is_json_the_codec_would_produce():
    """The keys, not just the values: a fixture missing `role` would type-error
    in the bundle but pass every runtime path that reads it as undefined."""
    source = _src("api.ts")
    for raw in re.findall(r"dispatch: (\{[^}]*\})", source):
        keys = set(re.findall(r"(\w+):", raw))
        assert keys == {"strategy", "carrier", "role", "integrates"}, (
            f"fixture dispatch block has keys {sorted(keys)}; codec.dispatch_of "
            "always sends all four"
        )
        # Parseable as JSON once the TS quoting is normalised and the trailing
        # commas prettier leaves behind are dropped -- the cheapest proof that
        # this is a literal block and not an expression that happens to look
        # like one.
        as_json = re.sub(r"(\w+):", r'"\1":', raw).replace("'", '"')
        json.loads(re.sub(r",(\s*[}\]])", r"\1", as_json))
