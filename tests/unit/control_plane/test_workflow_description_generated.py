"""A generated description for a workflow submitted without one (BGD, 2026-11-04).

When a workflow arrives with no `metadata.description` or `metadata.about`,
swarm-api writes the three parts (purpose, goals, expected output; BGD,
2026-11-06) after the submission returns: Claude Haiku on Vertex AI behind
SWARM_WORKFLOW_DESCRIPTION_MODEL=vertex (Terraform ships it "off"), else an
extract of the masked step briefs and what the strategy publishes, marked
`description_source: "generated"` either way. Unset, it writes nothing. One
ledger row per workflow counts the call and caps it at one.

Offline: the real routes over FakeFirestore, and a fake model in place of
Vertex AI.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path
from typing import Any

import pytest

from swarm_api import wfdescription
from swarm_api.schemas import ABOUT_GOAL_MAX_CHARS, ABOUT_PURPOSE_MAX_CHARS
from swarm_api.store import TASKS
from swarm_api.wfdescription import (
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    MODEL_SWITCH_ENV,
    USAGE_COLLECTION,
    Summary,
    StepFacts,
    SummaryRefused,
    VertexSummariser,
    after_submission,
    generate,
    output_of,
    summariser_from_env,
    usage_id,
)

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"
MODEL_ABOUT = {
    "purpose": "Adds a size chart to the product page so shoppers pick a size without leaving it.",
    "goals": ["The chart reads sizes from the catalogue", "The chart matches the design"],
    "output": "Each step's result on the workflow page.",
}
MODEL_TEXT = json.dumps(MODEL_ABOUT)
#: What the extract writes for BRIEFS under the default `collect` strategy.
EXTRACT_ABOUT = {
    "purpose": "Build the size chart component for the product page.",
    "goals": ["s0: Build the size chart component for the product page.",
              "s1: Check the chart against the design."],
    "output": "Each step's result and artifacts on this page; no pull request.",
}
BRIEFS = ("Build the size chart component for the product page. It reads sizes from the catalogue.",
          "## Review\nCheck the chart against the design. Then approve.")


def _spec(*briefs: str, **metadata: Any) -> dict[str, Any]:
    steps = [{"step_id": f"s{n}", "runner_profile": "mock", "input": {"prompt": brief}}
             for n, brief in enumerate(briefs or BRIEFS)]
    return {"repository_url": REPO, "strategy": "collect", "steps": steps, "metadata": metadata}


def _post(client, *briefs: str, **metadata: Any) -> dict[str, Any]:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec(*briefs, **metadata))
    assert response.status_code == 201, response.text
    return response.json()["workflow"]


def _read(client, workflow: dict[str, Any]) -> dict[str, Any]:
    assert wfdescription.wait_pending(10), "the description thread did not finish"
    response = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["workflow"]


def _row(db, workflow: dict[str, Any]) -> dict[str, Any] | None:
    return db.docs.get(f"{USAGE_COLLECTION}/{usage_id(workflow['tenant_id'], workflow['workflow_id'])}")


def _tasks(api_context, workflow: dict[str, Any]) -> list[Any]:
    return [api_context.store.get_task(workflow["tenant_id"], step["task_id"], submitted_by=None)
            for step in workflow["steps"]]


class FakeModel:
    model = "claude-haiku-fake"

    def __init__(self, text: str = MODEL_TEXT, *, error: Exception | None = None,
                 gate: threading.Event | None = None) -> None:
        self.text, self.error, self.gate = text, error, gate
        self.prompts: list[str] = []

    def summarise(self, briefs: str) -> Summary:
        self.prompts.append(briefs)
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return Summary(text=self.text, input_tokens=812, output_tokens=41)


@pytest.fixture(autouse=True)
def switched_off(monkeypatch):
    """The switch as Terraform ships it: the extract writes."""
    monkeypatch.setenv(MODEL_SWITCH_ENV, "off")


@pytest.fixture
def model(monkeypatch) -> FakeModel:
    """The switch on, with a fake model where Vertex AI would be."""
    fake = FakeModel()
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: fake)
    return fake


@pytest.fixture
def held(monkeypatch):
    """Submit without describing, so a test drives `generate` itself."""
    monkeypatch.setattr(wfdescription, "after_submission", lambda *a, **k: None)


# --------------------------------------------------------------------------
# Generated when absent, marked generated
# --------------------------------------------------------------------------


def test_with_the_switch_off_the_briefs_give_the_three_parts(client, db):
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["about"] == EXTRACT_ABOUT
    assert served["description"] == EXTRACT_ABOUT["purpose"]
    assert served["description_source"] == "generated"
    row = _row(db, workflow)
    assert row["outcome"] == "extractive" and row["calls"] == 0 and row["kind"] == "workflow_description"
    assert row["tenant_id"] == workflow["tenant_id"] and row["settled_at"] is not None


def test_unset_writes_nothing_and_the_page_shows_the_title_alone(client, db, monkeypatch):
    monkeypatch.delenv(MODEL_SWITCH_ENV, raising=False)
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["about"] is None and served["description"] is None
    assert served["description_source"] is None and _row(db, workflow) is None


def test_with_the_switch_on_the_models_parts_are_the_about_and_its_call_is_counted(client, db, model):
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["about"] == MODEL_ABOUT
    assert served["description"] == MODEL_ABOUT["purpose"]
    assert served["description_source"] == "generated"
    assert len(model.prompts) == 1 and "Step 2 brief:" in model.prompts[0]
    # The facts beside the briefs: the step ids and what the strategy publishes.
    assert "Steps: s0, s1" in model.prompts[0]
    assert "Expected output: Each step's result and artifacts on this page" in model.prompts[0]
    row = _row(db, workflow)
    assert {k: row[k] for k in ("calls", "model", "input_tokens", "output_tokens", "outcome")} == {
        "calls": 1, "model": "claude-haiku-fake", "input_tokens": 812, "output_tokens": 41,
        "outcome": "model"}


def test_every_step_task_carries_the_generated_description(client, db, model, api_context):
    workflow = _post(client)
    _read(client, workflow)
    for task in _tasks(api_context, workflow):
        assert task.metadata["about"] == MODEL_ABOUT
        assert task.metadata["description"] == MODEL_ABOUT["purpose"]
        assert task.metadata["description_source"] == "generated"


# --------------------------------------------------------------------------
# Never when present: the submitter wins
# --------------------------------------------------------------------------


def test_a_workflow_submitted_with_a_description_is_never_summarised(client, db, model):
    workflow = _post(client, description="The submitter's own two sentences. They stay.")
    served = _read(client, workflow)
    assert served["description"] == "The submitter's own two sentences. They stay."
    assert served["description_source"] == "submitter"
    assert model.prompts == [] and _row(db, workflow) is None


def test_a_workflow_submitted_with_an_about_is_never_summarised(client, db, model):
    about = {"purpose": "Ships the size chart.", "goals": ["Reads the catalogue"], "output": "A pull request."}
    workflow = _post(client, about=about)
    served = _read(client, workflow)
    assert served["about"] == {**about} and served["description_source"] == "submitter"
    assert model.prompts == [] and _row(db, workflow) is None


def test_a_description_written_meanwhile_is_never_overwritten(client, db, held, api_context):
    workflow = _post(client)
    tasks = _tasks(api_context, workflow)
    for task in tasks:
        db.docs[f"{TASKS}/{task.id}"]["metadata"].update(
            description="Written by the submitter after all.", description_source="submitter")
    assert generate(db, tasks, summariser=FakeModel()) == "skipped:described_meanwhile"
    served = _read(client, workflow)
    assert served["description"] == "Written by the submitter after all."
    assert served["description_source"] == "submitter"


# --------------------------------------------------------------------------
# Never blocks submission
# --------------------------------------------------------------------------


def test_the_submission_returns_while_the_model_is_still_answering(client, db, monkeypatch):
    gate = threading.Event()
    slow = FakeModel(gate=gate)
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: slow)
    try:
        workflow = _post(client)
        assert _row(db, workflow)["outcome"] == "started"
    finally:
        gate.set()
    assert _read(client, workflow)["about"] == MODEL_ABOUT


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), SummaryRefused("vertex_http_403")])
def test_a_failed_model_call_falls_back_to_the_extract_and_says_why(client, db, monkeypatch, error):
    failing = FakeModel(error=error)
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: failing)
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["about"] == EXTRACT_ABOUT
    assert served["description_source"] == "generated"
    row = _row(db, workflow)
    assert row["calls"] == 1 and row["outcome"].startswith("extractive:model_")


@pytest.mark.parametrize("text", [
    "**Adds** a [chart](https://example.invalid).",
    json.dumps({"purpose": "Adds a [chart](https://example.invalid)."}),
    json.dumps({"purpose": "Adds a chart.", "goals": ["a", "b", "c", "d"]}),
    json.dumps({"purpose": "x" * (ABOUT_PURPOSE_MAX_CHARS + 1)}),
    json.dumps({"summary": "Adds a chart."}),
])
def test_model_text_the_rule_refuses_falls_back_to_the_extract(client, db, monkeypatch, text):
    marked = FakeModel(text=text)
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: marked)
    workflow = _post(client)
    assert _read(client, workflow)["about"] == EXTRACT_ABOUT
    assert _row(db, workflow)["outcome"] == "extractive:model_text_refused"


def test_a_hook_that_cannot_start_still_returns_the_submission(client, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no threads today")

    monkeypatch.setattr(wfdescription.threading, "Thread", broken)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec())
    assert response.status_code == 201, response.text


def test_nothing_to_summarise_leaves_it_empty_and_the_row_says_so(client, db):
    workflow = _post(client, "```\n```")
    served = _read(client, workflow)
    assert served["description"] is None and served["description_source"] is None
    assert served["about"] is None
    assert _row(db, workflow)["outcome"] == "failed:no_text"


# --------------------------------------------------------------------------
# Counted and capped
# --------------------------------------------------------------------------


def test_one_call_per_workflow(client, db, held, api_context):
    workflow = _post(client)
    tasks = _tasks(api_context, workflow)
    fake = FakeModel()
    assert generate(db, tasks, summariser=fake) == "model"
    assert generate(db, tasks, summariser=fake) == "skipped:already_claimed"
    assert len(fake.prompts) == 1 and _row(db, workflow)["calls"] == 1


def test_the_models_input_is_capped(client, db, held, api_context):
    workflow = _post(client, *(["word " * 3000] * 10))
    fake = FakeModel()
    generate(db, _tasks(api_context, workflow), summariser=fake)
    assert len(fake.prompts[0]) <= MAX_PROMPT_CHARS


def test_the_extract_is_held_to_each_parts_cap(client, db, held, api_context):
    long_sentences = [f"Step {n} changes {'many things ' * 40}today." for n in range(8)]
    workflow = _post(client, *long_sentences)
    generate(db, _tasks(api_context, workflow), summariser=None)
    about = _read(client, workflow)["about"]
    assert 0 < len(about["purpose"]) <= ABOUT_PURPOSE_MAX_CHARS and about["purpose"].endswith("…")
    assert len(about["goals"]) == 3
    assert all(len(goal) <= ABOUT_GOAL_MAX_CHARS and goal.endswith("…") for goal in about["goals"])


def _facts(**over: Any) -> StepFacts:
    base = dict(title=None, step_ids=("build",), labels=(), strategy="direct-pr", merge=None,
                merge_step=False, repository="octo-org/widget-shop", agent_steps=1)
    return StepFacts(**{**base, **over})


def test_the_expected_output_says_what_the_strategy_and_the_merge_setting_publish():
    assert output_of(_facts()) == "A pull request on octo-org/widget-shop."
    assert output_of(_facts(merge_step=True)) == (
        "A pull request on octo-org/widget-shop, merged by its merge step once its checks pass.")
    assert output_of(_facts(merge="off")) == "A pull request on octo-org/widget-shop, left open for review."
    assert output_of(_facts(agent_steps=3)) == "A pull request per step on octo-org/widget-shop."
    assert output_of(_facts(strategy="integrate", agent_steps=3)) == "A pull request on octo-org/widget-shop."
    assert output_of(_facts(strategy="collect")) == "Each step's result and artifacts on this page; no pull request."


def test_a_pull_request_workflow_says_it_publishes_one(client, db):
    spec = {**_spec(BRIEFS[0]), "strategy": "direct-pr"}
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    about = _read(client, response.json()["workflow"])["about"]
    assert about["output"].startswith("A pull request on octo-org/widget-shop")
    assert "goals" not in about or about["goals"] == []


def test_after_submission_starts_nothing_while_the_switch_is_unset(client, api_context, held):
    workflow = _post(client)
    started: list[Any] = []
    monkey_thread = type("T", (), {"__init__": lambda self, **k: started.append(k), "start": lambda self: None,
                                   "is_alive": lambda self: False})
    real = wfdescription.threading.Thread
    wfdescription.threading.Thread = monkey_thread
    try:
        after_submission(object(), _tasks(api_context, workflow), settings=None, environ={})
        after_submission(object(), _tasks(api_context, workflow), settings=None, environ={MODEL_SWITCH_ENV: " "})
        assert started == []
        after_submission(object(), _tasks(api_context, workflow), settings=None, environ={MODEL_SWITCH_ENV: "off"})
        assert len(started) == 1
    finally:
        wfdescription.threading.Thread = real


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code, self._body = status_code, body

    def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append((url, json, timeout))
        return self.response


def test_the_vertex_call_asks_for_at_most_the_token_ceiling():
    session = FakeSession(FakeResponse(200, {
        "content": [{"type": "text", "text": MODEL_TEXT}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 500, "output_tokens": 40}}))
    vertex = VertexSummariser(project_id="proj-1", session=session)
    summary = vertex.summarise("Step 1 brief:\nBuild it.")
    assert summary == Summary(text=MODEL_TEXT, input_tokens=500, output_tokens=40)
    ((url, body, timeout),) = session.calls
    assert url == ("https://aiplatform.googleapis.com/v1/projects/proj-1/locations/global"
                   "/publishers/anthropic/models/claude-haiku-4-5@20251001:rawPredict")
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS and body["anthropic_version"] == "vertex-2023-10-16"
    assert timeout > 0


def test_a_refused_vertex_answer_still_counts_its_tokens(client, db, held, api_context):
    session = FakeSession(FakeResponse(200, {
        "content": [], "stop_reason": "refusal", "usage": {"input_tokens": 300, "output_tokens": 2}}))
    workflow = _post(client)
    outcome = generate(db, _tasks(api_context, workflow),
                       summariser=VertexSummariser(project_id="proj-1", session=session))
    assert outcome == "extractive:model_stop_refusal"
    row = _row(db, workflow)
    assert (row["calls"], row["input_tokens"], row["output_tokens"]) == (1, 300, 2)


def test_the_switch_is_off_unless_it_says_vertex():
    assert summariser_from_env({}, project_id="proj-1") is None
    assert summariser_from_env({MODEL_SWITCH_ENV: "on"}, project_id="proj-1") is None
    assert isinstance(summariser_from_env({MODEL_SWITCH_ENV: "vertex"}, project_id="proj-1"), VertexSummariser)


TERRAFORM = Path(__file__).resolve().parents[3] / "terraform"


def test_terraform_ships_the_switch_off_for_swarm_api():
    """The owner has not yet granted Vertex AI: every deployment writes the extract.

    Terraform sets the switch to "off" on swarm-api, and nothing grants the
    account a role to call the model or enables the API it calls; the patch
    that turns it to "vertex" brings all three."""
    locals_tf = (TERRAFORM / "infra" / "locals.tf").read_text()
    api_env = locals_tf.split('"swarm-api" = merge(', 1)[1].split('\n    "swarm-', 1)[0]
    assert re.search(rf'^\s*{MODEL_SWITCH_ENV}\s*=\s*"off"\s*$', api_env, re.MULTILINE)
    assert summariser_from_env({MODEL_SWITCH_ENV: "off"}, project_id="proj-1") is None
    bindings = (TERRAFORM / "modules" / "iam" / "bindings.tf").read_text()
    api_roles = bindings.split('"swarm-api" = concat(', 1)[1].split("])", 1)[0]
    assert '"roles/aiplatform.user"' not in api_roles
    services = (TERRAFORM / "infra" / "main.tf").read_text().split('module "project_services"', 1)[1]
    assert '"aiplatform.googleapis.com"' not in services.split("]", 1)[0]


# --------------------------------------------------------------------------
# Masked as any read masks it
# --------------------------------------------------------------------------


def test_a_secret_in_a_brief_reaches_neither_the_model_nor_the_description(client, db, model):
    value = secrets.token_hex(12)
    workflow = _post(client, f"Deploy with DB_PASSWORD={value} set. Then check the page.")
    served = _read(client, workflow)
    assert value not in model.prompts[0]
    assert value not in (served["description"] or "") and value not in json.dumps(served["about"])


def test_a_secret_in_a_brief_never_reaches_the_extract(client, db):
    value = secrets.token_hex(12)
    workflow = _post(client, f"Deploy with DB_PASSWORD={value} set. Then check the page.", BRIEFS[1])
    served = _read(client, workflow)
    assert served["description_source"] == "generated"
    assert value not in json.dumps(served["about"])
