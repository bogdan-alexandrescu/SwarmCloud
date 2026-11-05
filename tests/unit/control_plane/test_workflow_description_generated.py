"""A generated description for a workflow submitted without one (BGD, 2026-11-04).

When a workflow arrives with no `metadata.description`, swarm-api writes one
after the submission returns: Claude Haiku on Vertex AI behind
SWARM_WORKFLOW_DESCRIPTION_MODEL=vertex (Terraform sets it; unset is off), else the first
sentence of each masked step brief, marked `description_source: "generated"`
either way. One ledger row per workflow counts the call and caps it at one.

Offline: the real routes over FakeFirestore, and a fake model in place of
Vertex AI.
"""

from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path
from typing import Any

import pytest

from swarm_api import wfdescription
from swarm_api.schemas import DESCRIPTION_MAX_CHARS
from swarm_api.store import TASKS
from swarm_api.wfdescription import (
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    MODEL_SWITCH_ENV,
    USAGE_COLLECTION,
    Summary,
    SummaryRefused,
    VertexSummariser,
    generate,
    summariser_from_env,
    usage_id,
)

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"
MODEL_TEXT = ("Adds a size chart to the product page. Shoppers pick a size without leaving the "
              "page, and the chart reads from the catalogue.")
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


def test_with_the_switch_off_the_first_sentence_of_each_brief_is_the_description(client, db, monkeypatch):
    monkeypatch.delenv(MODEL_SWITCH_ENV, raising=False)
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["description"] == ("Build the size chart component for the product page. "
                                     "Check the chart against the design.")
    assert served["description_source"] == "generated"
    row = _row(db, workflow)
    assert row["outcome"] == "extractive" and row["calls"] == 0 and row["kind"] == "workflow_description"
    assert row["tenant_id"] == workflow["tenant_id"] and row["settled_at"] is not None


def test_with_the_switch_on_the_models_text_is_the_description_and_its_call_is_counted(client, db, model):
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["description"] == MODEL_TEXT
    assert served["description_source"] == "generated"
    assert len(model.prompts) == 1 and "Step 2 brief:" in model.prompts[0]
    row = _row(db, workflow)
    assert {k: row[k] for k in ("calls", "model", "input_tokens", "output_tokens", "outcome")} == {
        "calls": 1, "model": "claude-haiku-fake", "input_tokens": 812, "output_tokens": 41,
        "outcome": "model"}


def test_every_step_task_carries_the_generated_description(client, db, model, api_context):
    workflow = _post(client)
    _read(client, workflow)
    for task in _tasks(api_context, workflow):
        assert task.metadata["description"] == MODEL_TEXT
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
    assert _read(client, workflow)["description"] == MODEL_TEXT


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), SummaryRefused("vertex_http_403")])
def test_a_failed_model_call_falls_back_to_the_extract_and_says_why(client, db, monkeypatch, error):
    failing = FakeModel(error=error)
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: failing)
    workflow = _post(client)
    served = _read(client, workflow)
    assert served["description"].startswith("Build the size chart component")
    assert served["description_source"] == "generated"
    row = _row(db, workflow)
    assert row["calls"] == 1 and row["outcome"].startswith("extractive:model_")


def test_model_text_the_rule_refuses_falls_back_to_the_extract(client, db, monkeypatch):
    marked = FakeModel(text="**Adds** a [chart](https://example.invalid).")
    monkeypatch.setenv(MODEL_SWITCH_ENV, "vertex")
    monkeypatch.setattr(wfdescription, "summariser_from_env", lambda environ, *, project_id: marked)
    workflow = _post(client)
    assert _read(client, workflow)["description"].startswith("Build the size chart component")
    assert _row(db, workflow)["outcome"] == "extractive:model_text_refused"


def test_a_hook_that_cannot_start_still_returns_the_submission(client, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no threads today")

    monkeypatch.setattr(wfdescription.threading, "Thread", broken)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec())
    assert response.status_code == 201, response.text


def test_nothing_to_summarise_leaves_it_empty_and_the_row_says_so(client, db, monkeypatch):
    monkeypatch.delenv(MODEL_SWITCH_ENV, raising=False)
    workflow = _post(client, "```\n```")
    served = _read(client, workflow)
    assert served["description"] is None and served["description_source"] is None
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


def test_the_extract_is_held_to_the_600_character_rule(client, db, held, api_context):
    long_sentences = [f"Step {n} changes {'many things ' * 20}today." for n in range(8)]
    workflow = _post(client, *long_sentences)
    generate(db, _tasks(api_context, workflow), summariser=None)
    served = _read(client, workflow)
    assert 0 < len(served["description"]) <= DESCRIPTION_MAX_CHARS
    assert served["description"].endswith("…")


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


def test_terraform_turns_the_model_on_for_swarm_api():
    """The owner picked Claude Haiku on Vertex AI (#3346): every deployment runs it.

    The switch on swarm-api, the account's role to call it and the API it calls;
    one missing and every workflow falls back to the extract."""
    locals_tf = (TERRAFORM / "infra" / "locals.tf").read_text()
    api_env = locals_tf.split('"swarm-api" = merge(', 1)[1].split('\n    "swarm-', 1)[0]
    assert re.search(rf'^\s*{MODEL_SWITCH_ENV}\s*=\s*"vertex"\s*$', api_env, re.MULTILINE)
    bindings = (TERRAFORM / "modules" / "iam" / "bindings.tf").read_text()
    api_roles = bindings.split('"swarm-api" = concat(', 1)[1].split("])", 1)[0]
    assert '"roles/aiplatform.user"' in api_roles
    services = (TERRAFORM / "infra" / "main.tf").read_text().split('module "project_services"', 1)[1]
    assert '"aiplatform.googleapis.com"' in services.split("]", 1)[0]


# --------------------------------------------------------------------------
# Masked as any read masks it
# --------------------------------------------------------------------------


def test_a_secret_in_a_brief_reaches_neither_the_model_nor_the_description(client, db, model):
    value = secrets.token_hex(12)
    workflow = _post(client, f"Deploy with DB_PASSWORD={value} set. Then check the page.")
    served = _read(client, workflow)
    assert value not in model.prompts[0]
    assert value not in (served["description"] or "")
