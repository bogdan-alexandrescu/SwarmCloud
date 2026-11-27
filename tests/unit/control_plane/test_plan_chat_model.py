"""The chat's model per plan, and the run's review usage
(docs/design/plans-pipeline.md §7.8; lane P10 PLANS-CHAT-MODEL-API of §11).

  1. THE SUGGESTION. `chat_model_suggestion` on the run read: the planner's
     class, with the step named, when a step carries a risk stop and the
     planner's class is stronger; nothing, with its sentence, for a one-step
     plan with no risk paths.
  2. THE OVERRIDE. `POST plan:chat-model`: a cheaper class on any plan; a
     stronger one only when suggested (422 `chat_model_not_needed`); a class
     the profile does not carry 422 `unknown_model_class`; the edit level.
     The next turn is submitted at it, `from: "plan"`; `null` clears it.
  3. STRONGER IS MEASURED by the `model_prices` output price of the class's
     pinned model; an unpriced class needs the suggestion.
  4. THE USAGE. `plan_review_usage` counts re-plans, diagram asks, chat
     turns and review dollars against the caps read now; `review_usd` sums
     chat turns and diagram agents only, never a re-plan (Q11).
"""

from __future__ import annotations

import secrets

import pytest

from swarm_api import forge, forgewrite, modelprices, planchat, plansettings, refusals, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header
from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    BUCKET,
    PLAN,
    _create,
    _finish_planner,
    _run,
    forge_tokens,
    github,
)
from .test_plan_chat import UNDRAWN, _asked, _code, _expire, _stored, _sub
from .test_roles_store import grant

pytestmark = pytest.mark.usefixtures("fix_loop_off", "plan_diagrams_on")

SWITCHES = tuple(refusals.env_name(code) for code in (
    "role_member_required", "anchor_unknown", "edit_level_required", "replan_running",
    "diagrams_off", "diagram_cap", "diagrams_cap", "diagram_running",
    "chat_closed", "comment_level_required", "chat_cap",
    # This lane's own refusal ships off; these tests run it on.
    "chat_model_not_needed"))

#: The deployment's pins, at models the price table lists, so "stronger" is
#: each class's list output price: haiku 0.50 < sonnet 10 < opus 20 < fable 40.
PINS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5",
        "opus": "claude-opus-5-5", "fable": "claude-fable-5-2"}

ONE_STEP = {"summary": "Rename the sort label.", "no_diagram": "a wording change",
            "steps": [{"step_id": "label", "title": "Rename the label",
                       "prompt": "Rename the Name header's label to Title.",
                       "files": ["src/ui/list.py"]}]}

IAM = {"summary": "Grant the reader role and wire the header.",
       "no_diagram": "a binding and a header",
       "steps": [{"step_id": "grant", "title": "Grant the reader role",
                  "prompt": "Add the binding in terraform/infra/iam.tf.",
                  "files": ["terraform/infra/iam.tf"]},
                 {"step_id": "ui", "title": "Wire the header",
                  "prompt": "Make the header sortable.", "files": ["src/ui/list.py"]}]}


@pytest.fixture(autouse=True)
def switched_on(monkeypatch):
    for name in SWITCHES:
        monkeypatch.setenv(name, "on")
    monkeypatch.setattr(planchat, "STREAM_WINDOW_SECONDS", 0)


@pytest.fixture
def api_context(db, tokens, group_map, objects, github, forge_tokens):
    return build_context(
        settings=api_settings(settings_key=secrets.token_hex(32), worker_class_models=dict(PINS)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _read(client, run_id, user="alice") -> dict:
    read = _run(client, run_id, user)
    assert read.status_code == 200, read.text
    return read.json()["run"]


def _planned(client, db, objects, plan=ONE_STEP) -> dict:
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    return _read(client, run["id"])


def _set(client, run, model_class, user="alice"):
    return client.post(f"/v1/runs/{run['id']}/plan:chat-model", headers=auth_header(user),
                       json={"model_class": model_class})


def _prices(db, **outputs) -> None:
    """The platform's price table, as written: each named model at `output`."""
    table = {name: dict(price) for name, price in modelprices.SEED.items()}
    for name, output in outputs.items():
        name = name.replace("_", "-")
        if output is None:
            table.pop(name, None)
        else:
            table[name] = {**table[name], "output": output}
    db.docs.setdefault("control/settings", {})["model_prices"] = table


# --------------------------------------------------------------------------
# 1. the suggestion
# --------------------------------------------------------------------------

def test_a_plan_with_a_step_on_iam_suggests_the_planners_class_naming_the_step(
        client, db, objects):
    run = _planned(client, db, objects, IAM)
    suggestion = run["chat_model_suggestion"]
    # The planner's role class, fable, priced above the resolved sonnet.
    assert suggestion["suggested"] == "fable"
    assert "step 1" in suggestion["why"] and "grant" in suggestion["why"]
    assert "iam" in suggestion["why"]


def test_a_one_step_plan_with_no_risk_paths_suggests_nothing_with_its_sentence(
        client, db, objects):
    run = _planned(client, db, objects)
    assert run["chat_model_suggestion"] == {"suggested": None,
                                            "why": "a one-step plan with no risk paths"}


def test_a_plan_of_four_steps_suggests_on_its_size(client, db, objects):
    steps = [{"step_id": f"s{n}", "title": f"Step {n}", "prompt": f"Change part {n}.",
              "files": [f"src/part{n}.py"]} for n in range(1, 5)]
    run = _planned(client, db, objects, {"summary": "Four parts.", "steps": steps,
                                         "no_diagram": "four small edits"})
    assert run["chat_model_suggestion"]["suggested"] == "fable"
    assert "4 steps" in run["chat_model_suggestion"]["why"]


def test_no_suggestion_when_the_planners_class_is_not_stronger(client, db, objects):
    run = _planned(client, db, objects, IAM)
    put = client.put("/v1/workspaces/eng/plan-settings", headers=auth_header("root"),
                     json={"chat_model_class": "fable"})
    assert put.status_code == 200, put.text
    suggestion = _read(client, run["id"])["chat_model_suggestion"]
    assert suggestion["suggested"] is None
    assert "not stronger" in suggestion["why"]


def test_the_run_read_serves_the_model_the_next_turn_uses(client, db, objects):
    run = _planned(client, db, objects)
    assert run["chat_model"] == {"class": "sonnet", "from": "platform", "fell_through": None}
    assert run["plan_chat_model"] is None


# --------------------------------------------------------------------------
# 2. the override
# --------------------------------------------------------------------------

def test_a_cheaper_class_is_accepted_on_any_plan(client, db, objects):
    run = _planned(client, db, objects)
    response = _set(client, run, "haiku")
    assert response.status_code == 200, response.text
    record = response.json()["plan_chat_model"]
    assert record["class"] == "haiku" and record["set_by"] == "alice@example.com"
    assert record["why"] == "cheaper than the platform's" and record["set_at"]
    read = _read(client, run["id"])
    assert read["plan_chat_model"]["class"] == "haiku"
    assert read["chat_model"]["from"] == "plan"
    # Not signed, not in the digest: the plan is unchanged.
    assert read["plan_digest"] == run["plan_digest"] and read["plan_revision"] == 1


def test_a_stronger_class_without_the_suggestion_is_422_with_its_sentence(client, db, objects):
    run = _planned(client, db, objects)
    refused = _set(client, run, "opus")
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "chat_model_not_needed"
    assert ("this plan's reading suggests no stronger model: a one-step plan with no risk paths"
            in refused.json()["message"])
    assert "plan_chat_model" not in _stored(db, run["id"]) or \
        _stored(db, run["id"])["plan_chat_model"] is None


def test_the_suggested_stronger_class_is_accepted_with_the_suggestions_why(
        client, db, objects):
    run = _planned(client, db, objects, IAM)
    response = _set(client, run, "fable")
    assert response.status_code == 200, response.text
    record = response.json()["plan_chat_model"]
    assert record["class"] == "fable"
    assert record["why"] == run["chat_model_suggestion"]["why"]
    # A stronger class that is not the one suggested is still refused.
    refused = _set(client, run, "opus")
    assert refused.status_code == 422 and _code(refused) == "chat_model_not_needed"


@pytest.mark.parametrize("wanted", ["nonesuch", "claude-opus-5-5", "OPUS"])
def test_a_name_that_is_no_class_is_422_unknown_model_class(client, db, objects, wanted):
    run = _planned(client, db, objects)
    refused = _set(client, run, wanted)
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "unknown_model_class"
    assert "sonnet" in refused.json()["message"]


def test_a_class_the_profile_does_not_carry_is_422(client, db, objects, monkeypatch):
    run = _planned(client, db, objects)
    monkeypatch.setattr(plansettings, "chat_model_classes", lambda: ("opus", "sonnet"))
    refused = _set(client, run, "haiku")
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "unknown_model_class"
    assert "opus" in refused.json()["message"] and "sonnet" in refused.json()["message"]


@pytest.mark.parametrize("body", [
    {"model_class": "haiku", "model": "claude-haiku-5-5"},
    {"model_class": "haiku", "image": "example.invalid/agent:latest"},
    {"model_class": "haiku", "runner_profile": "generic"},
    {"model_class": "haiku", "resources": {"cpu": 8}},
    {},
])
def test_a_caller_names_a_class_and_nothing_else(client, db, objects, body):
    run = _planned(client, db, objects)
    response = client.post(f"/v1/runs/{run['id']}/plan:chat-model",
                           headers=auth_header("alice"), json=body)
    assert response.status_code == 422
    assert _stored(db, run["id"]).get("plan_chat_model") is None


def test_a_member_who_may_not_edit_is_refused(client, db, objects):
    run = _planned(client, db, objects)
    _expire(db, run)
    refused = _set(client, run, "haiku")
    assert refused.status_code == 403, refused.text
    assert _code(refused) == "edit_level_required"
    assert _stored(db, run["id"]).get("plan_chat_model") is None


def test_a_viewer_is_refused(client, db, objects, api_context):
    run = _planned(client, db, objects)
    grant(api_context.roles, "alice@example.com", roles.VIEWER)
    refused = _set(client, run, "haiku")
    assert refused.status_code == 403 and _code(refused) == "role_member_required"


def test_another_tenants_run_is_the_same_404_as_none(client, db, objects):
    run = _planned(client, db, objects)
    for user in ("bob", "carol"):
        mine = _set(client, run, "haiku", user=user)
        none = _set(client, {**run, "id": "run_doesnotexist"}, "haiku", user=user)
        assert mine.status_code == none.status_code == 404
    assert _stored(db, run["id"]).get("plan_chat_model") is None


def test_only_while_the_run_is_planned(client, db, objects):
    run = _planned(client, db, objects)
    rejected = client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("alice"),
                           json={"plan_digest": run["plan_digest"], "reason": "not now"})
    assert rejected.status_code == 200, rejected.text
    assert _set(client, run, "haiku").status_code == 409


def test_the_next_turn_is_submitted_at_the_override_from_plan(client, db, objects):
    run = _planned(client, db, objects, UNDRAWN)
    assert _set(client, run, "haiku").status_code == 200
    turn = _asked(client, run)
    assert turn["model"]["class"] == "haiku" and turn["model"]["from"] == "plan"
    assert db.docs[f"tasks/{turn['task_id']}"]["model_class"] == "haiku"


def test_an_on_demand_diagram_is_drawn_at_the_override(client, db, objects):
    run = _planned(client, db, objects, UNDRAWN)
    assert _set(client, run, "haiku").status_code == 200
    asked = client.post(f"/v1/runs/{run['id']}/plan:diagram", headers=auth_header("alice"),
                        json={"plan_digest": run["plan_digest"], "ask": "Draw the sort.",
                              "anchor": {"part": "step", "ref": "ui"}})
    assert asked.status_code == 202, asked.text
    tasks = [d for k, d in db.docs.items() if k.startswith("tasks/")
             and (d.get("metadata") or {}).get("plan_diagram")]
    assert [t["model_class"] for t in tasks] == ["haiku"]


def test_clearing_returns_to_the_workspaces_class(client, db, objects):
    run = _planned(client, db, objects, UNDRAWN)
    put = client.put("/v1/workspaces/eng/plan-settings", headers=auth_header("root"),
                     json={"chat_model_class": "sonnet"})
    assert put.status_code == 200, put.text
    assert _set(client, run, "haiku").status_code == 200
    cleared = _set(client, run, None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["plan_chat_model"] is None
    read = _read(client, run["id"])
    assert read["plan_chat_model"] is None
    assert read["chat_model"]["class"] == "sonnet" and read["chat_model"]["from"] == "workspace"
    turn = _asked(client, run)
    assert turn["model"]["class"] == "sonnet" and turn["model"]["from"] == "workspace"


def test_each_change_is_a_plan_thread_event(client, db, objects):
    run = _planned(client, db, objects, IAM)
    assert _set(client, run, "fable").status_code == 200
    assert _set(client, run, None).status_code == 200
    events = [e for e in _read(client, run["id"])["plan_thread"]
              if e.get("event") == "chat_model"]
    assert len(events) == 2
    assert events[0]["model_class"] == "fable" and events[0]["by"] == "alice@example.com"
    assert events[0]["text"].startswith("the chat now answers with fable: ")
    assert "iam" in events[0]["text"]
    assert events[1]["model_class"] is None
    assert events[1]["text"].startswith("the chat now answers with sonnet")


# --------------------------------------------------------------------------
# 3. stronger, measured by model_prices
# --------------------------------------------------------------------------

def test_a_stronger_class_is_the_one_with_the_higher_output_price(client, db, objects):
    # haiku listed above sonnet: haiku is now the stronger one.
    _prices(db, claude_haiku_5_5=100.0)
    run = _planned(client, db, objects)
    refused = _set(client, run, "haiku")
    assert refused.status_code == 422 and _code(refused) == "chat_model_not_needed"
    # And opus listed below sonnet is now cheaper.
    _prices(db, claude_haiku_5_5=100.0, claude_opus_5_5=1.0)
    assert _set(client, run, "opus").status_code == 200


def test_an_unpriced_class_needs_the_suggestion(client, db, objects):
    _prices(db, claude_haiku_5_5=None)
    run = _planned(client, db, objects)
    refused = _set(client, run, "haiku")
    assert refused.status_code == 422 and _code(refused) == "chat_model_not_needed"


def test_the_refusal_ships_off_and_off_lets_a_stronger_class_through(
        client, db, objects, monkeypatch):
    assert refusals.SWITCHES["chat_model_not_needed"].default is False
    monkeypatch.setenv(refusals.env_name("chat_model_not_needed"), "off")
    run = _planned(client, db, objects)
    response = _set(client, run, "opus")
    assert response.status_code == 200, response.text
    assert response.json()["plan_chat_model"]["class"] == "opus"


# --------------------------------------------------------------------------
# 4. the usage
# --------------------------------------------------------------------------

def test_plan_review_usage_counts_against_the_caps_read_now(client, db, objects):
    run = _planned(client, db, objects)
    stored = _stored(db, run["id"])
    stored["plan_replans"] = 2
    stored["plan_diagram"] = {"asks": 3, "costs": [1.0, 2.0], "current": None}
    stored[planchat.RUN_FIELD] = {"turns": 4, "costs": [0.5, 0.5, 1.0, 1.0],
                                  "diagram_turns": 1, "current": None}
    usage = _read(client, run["id"])["plan_review_usage"]
    assert usage["replans"] == {"used": 2, "cap": 5}
    assert usage["diagram_asks"] == {"used": 4, "cap": 20}
    assert usage["chat_turns"] == {"used": 4, "cap": 40}
    assert usage["review_usd"] == {"spent": 6.0, "cap": 25.0, "at_least": False}
    assert usage["caps_read_at"]
    # The caps are read now: an admin's change is the next read's cap.
    put = client.put("/v1/admin/settings", headers=auth_header("root"), json={
        "plan_review_caps": {"replans": 3, "diagram_asks": 10, "chat_turns": 12,
                             "review_usd": 9.5}})
    assert put.status_code == 200, put.text
    usage = _read(client, run["id"])["plan_review_usage"]
    assert usage["replans"]["cap"] == 3 and usage["diagram_asks"]["cap"] == 10
    assert usage["chat_turns"]["cap"] == 12 and usage["review_usd"]["cap"] == 9.5


@pytest.mark.parametrize("where", ["chat", "diagram"])
def test_review_usd_is_at_least_when_a_cost_is_unreported(client, db, objects, where):
    run = _planned(client, db, objects)
    stored = _stored(db, run["id"])
    stored["plan_diagram"] = {"asks": 2, "costs": [2.0, None if where == "diagram" else 2.0],
                              "current": None}
    stored[planchat.RUN_FIELD] = {"turns": 2, "costs": [2.0, None if where == "chat" else 2.0],
                                  "current": None}
    review = _read(client, run["id"])["plan_review_usage"]["review_usd"]
    # The unreported one at the median of the reported ones.
    assert review == {"spent": 8.0, "cap": 25.0, "at_least": True}


def test_review_usd_never_counts_a_replans_cost(client, db, objects):
    run = _planned(client, db, objects)
    revision = _sub(db, run["id"], "plan_revisions")["1"]
    revision["cost"] = {"usd": 30.0, "task_id": "task_x", "model_class": "fable",
                        "why_none": None}
    stored = _stored(db, run["id"])
    stored["plan_replans"] = 1
    stored["plan_replan"] = {"replan_id": "rpl_x", "state": "applied", "cost": {"usd": 30.0}}
    stored[planchat.RUN_FIELD] = {"turns": 1, "costs": [1.5], "current": None}
    usage = _read(client, run["id"])["plan_review_usage"]
    assert usage["review_usd"]["spent"] == 1.5
    assert usage["replans"]["used"] == 1


def test_a_run_from_before_the_chat_has_used_nothing(client, db, objects):
    run = _planned(client, db, objects)
    usage = run["plan_review_usage"]
    assert usage["replans"]["used"] == 0 and usage["diagram_asks"]["used"] == 0
    assert usage["chat_turns"]["used"] == 0
    assert usage["review_usd"] == {"spent": 0.0, "cap": 25.0, "at_least": False}
