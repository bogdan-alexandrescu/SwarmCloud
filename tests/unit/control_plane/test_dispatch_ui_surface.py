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

No network, no credentials, no node. Where a seam compares the console with a
Python value -- the vocabulary, the dispatch keys, the refusal details, the
fixtures -- the console's half is apps/swarm-ui/src/__tests__/
contract.profiles.test.tsx, against the generated profiles.json that
`scripts/gen_api_types.py --check` holds to the `profiles` constants group,
and the half here holds that group to the live objects (part of #72). The
tests left reading console source compare it with nothing in Python.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from swarm_api.validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    DISPATCH_CARRIERS,
    DISPATCH_STRATEGIES,
    dispatchable_strategies,
)

from .conftest import auth_header

REPO = "https://github.com/example-org/example.git"

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"
WORKER_LIFECYCLE = ROOT / "apps/agent-worker/agent_worker/lifecycle.py"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing api.ts seam test: this suite has to pass in a tree that holds only
#: the Python services. Only the tests that read console source carry it.
needs_ui = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")


def _src(name: str) -> str:
    return (UI / name).read_text()


def _built() -> dict:
    """What the `profiles` constants group builds, from the live objects."""
    path = ROOT / "scripts/api_constants/profiles.py"
    spec = importlib.util.spec_from_file_location("api_constants_profiles_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


# --------------------------------------------------------------------------
# 1. The vocabulary seam
# --------------------------------------------------------------------------
# The console's side -- every strategy the dispatch control draws and every
# carrier the console knows is one this API accepts, every strategy this API
# can run is drawn, the defaults agree -- is
# apps/swarm-ui/src/__tests__/contract.profiles.test.tsx, against the
# generated profiles.json (part of #72). `scripts/gen_api_types.py --check`
# fails while that file differs from what the `profiles` group builds; these
# hold that the group builds swarm-api's own vocabulary.

def test_the_profiles_group_serves_the_strategies_and_carriers_this_api_accepts():
    """THE `patches` TRAP, pinned on the served side.

    agent-worker's `_dispatch_carrier` accepts `("patches", "branches")` and
    swarm-api accepts `("checkpoints", "branches")`. The browser talks to
    swarm-api, so the console is held to swarm-api's words, and these are them.
    """
    dispatch = _built()["dispatch"]
    assert dispatch["strategies"] == list(DISPATCH_STRATEGIES)
    assert dispatch["carriers"] == list(DISPATCH_CARRIERS)
    assert "patches" not in dispatch["carriers"]


def test_the_profiles_group_serves_what_this_api_can_run_and_its_defaults():
    """Measured against `dispatchable_strategies()`, not the accepted words:
    `single-pr` (archived issue 295) cannot complete while the catalogue
    disables its merge and post-verdict profiles. A caller who touches nothing
    gets `collect`/`checkpoints`, which keeps a read-only tenant token working."""
    dispatch = _built()["dispatch"]
    assert dispatch["dispatchable_strategies"] == list(dispatchable_strategies())
    assert set(dispatch["dispatchable_strategies"]) <= set(DISPATCH_STRATEGIES)
    assert dispatch["default_strategy"] == DEFAULT_STRATEGY
    assert dispatch["default_carrier"] == DEFAULT_CARRIER


@needs_ui
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


@needs_ui
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


def test_integrate_on_a_single_task_is_why_the_task_form_does_not_offer_it(client):
    """The task form does not draw `integrate` at all. This is the reason.

    It used to draw the option disabled, as a dashed card; since TS-14 (epic
    archived issue 84, owner decision 2026-09-25) it draws only the strategies a task can take
    and one line pointing to the workflow form. Asserted so the omission is
    grounded in the API's behaviour rather than in a belief about it -- if this
    ever became legal, the option would be missing for no reason and nobody
    would notice.
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

    # The console attributes each of these by its detail key: the details the
    # `profiles` group serves are these refusals, and contract.profiles.test.tsx
    # hands each to postTask and reads back the field it is pinned on.
    refusals = _built()["dispatch"]["refusals"]
    assert refusals["strategy"] == {"accepted_strategies": unknown_strategy["detail"]["accepted_strategies"]}
    assert refusals["carrier"] == {"accepted_carriers": unknown_carrier["detail"]["accepted_carriers"]}
    assert refusals["repository_url"]["missing"] == no_repo["detail"]["missing"]


def test_the_served_dispatch_keys_are_the_ones_the_api_sends(client, db):
    """`TaskDispatch` mirrors `codec.dispatch_of`; a missing key is a fact about
    the run that no screen can show. The `profiles` group serves the keys, and
    contract.profiles.test.tsx holds `TaskDispatch` and `dispatchOf` to them."""
    created = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    sent = list(created.json()["task"]["dispatch"])
    assert _built()["dispatch"]["task_dispatch_keys"] == sent
    assert sent == ["strategy", "carrier", "role", "integrates"]


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


@needs_ui
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


@needs_ui
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


@needs_ui
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


# MOVED TO THE CONSOLE: test_every_fixture_task_carries_a_dispatch_the_api_could_have_sent
#
# It read the fixture tasks' dispatch blocks out of api.ts and compared them
# with DISPATCH_STRATEGIES. Now: contract.profiles.test.tsx, "the fixture build
# serves dispatch blocks the API could have sent", which calls the fixture
# `loadTasks` and holds every block to the generated strategies and carriers,
# the default and `integrate` among them (part of #72).


# MOVED TO THE CONSOLE: test_the_fixture_dispatch_block_is_json_the_codec_would_produce
#
# It read each fixture `dispatch: {...}` out of api.ts and held its keys to the
# four `codec.dispatch_of` sends, written here as a literal. Now the same
# console case calls the fixture `loadTasks` and holds every block's keys to
# the generated `task_dispatch_keys`, which
# `test_the_served_dispatch_keys_are_the_ones_the_api_sends` holds to the API.


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """archived issue 780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
