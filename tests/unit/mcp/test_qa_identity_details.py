"""A step that names `qa_identity` says so in its DETAILS line (QI-MCP, part of #1649).

docs/design/qa-identity.md §4.7: "The step details line gains `QA identity:
console` beside `Browser tool:`". §6 row QI-MCP: "a step naming `qa_identity`
shows `QA identity: console`; one without does not".

`qa_identity` is a `claude-code-browser` input (contract request 106,
QI-CONTRACT). A task that names it has its live browser signed in to the
console as the deployment's QA service account, so the person reading the
expanded row in Claude Code is told whose session the browser holds.

UNLIKE THE SIX FIXED FIELDS, this one is shown only when a step names it.
The six say `not reported` rather than vanish, so a missing fact reads as
missing; here absence IS the fact -- the browser is signed in to nothing --
and a `QA identity: not reported` on every plain claude-code row would read
as a sign-in nobody asked for.

THE MUTATIONS THESE CATCH: never add the field (the naming tests go red);
add it to every line (the without tests go red); put it after `Session:` or
before `Browser tool:` (the order test goes red); read it on a profile that
does not declare it (the other-profile test goes red); show the value
unchecked (the credential-shaped test goes red).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_mcp import details, launch

from test_plugin_agents_and_workflows import _PLUGIN

FIELD = "QA identity"
BROWSER = "claude-code-browser"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-qa")
    monkeypatch.delenv("SWARM_SC_SESSION_ID", raising=False)
    launch.forget_attached()


class _Api:
    """Serves the two reads `launch.for_spec` makes before a submission."""

    def request(self, method, path, **_):
        if path == "/v1/tenants/me":
            return {"tenant": {"tenant_id": "eng", "display_name": "Engineering"},
                    "principal": {"email": "dev@example.com", "groups": []}}
        if path == "/v1/accounts":
            return {"accounts": [], "tenant_id": "eng"}
        raise AssertionError(path)

    def tenants_mine(self):
        return [{"tenant_id": "eng", "display_name": "Engineering"}]


def _step(step_id: str, profile: str, inputs: dict | None = None) -> dict:
    step = {"step_id": step_id, "prompt": "p", "runner_profile": profile}
    if inputs is not None:
        step["inputs"] = inputs
    return step


def _task(profile: str, task_input: dict) -> dict:
    return {"id": "t_a", "tenant_id": "eng", "step_id": "verify", "state": "RUNNING",
            "runner_profile": profile, "submitted_by": "dev@example.com", "input": task_input}


def _line(facts) -> str:
    return details.line("Engineering (eng)", facts, sid="sess-qa")


def _description(script: str) -> str:
    return json.loads(Path(script).read_text(encoding="utf-8").split("\n")[2][len("  description: "):-1])


def test_a_spec_step_naming_qa_identity_shows_it():
    facts = details.for_spec({"steps": [{"runner_profile": BROWSER,
                                         "input": {"qa_identity": "console"}}]}, "dev@example.com")
    assert "QA identity: console" in _line(facts)


def test_a_spec_step_without_qa_identity_does_not_show_the_field():
    """The control: the same step without the input, and a plain claude-code
    step, carry no `QA identity` field at all -- not `not reported`."""
    for step in ({"runner_profile": BROWSER, "input": {"browser_tool": "jev"}},
                 {"runner_profile": BROWSER},
                 {"runner_profile": "claude-code", "input": {"prompt": "p"}}):
        text = _line(details.for_spec({"steps": [step]}, "dev@example.com"))
        assert FIELD not in text, (step, text)


def test_the_field_sits_beside_browser_tool_and_before_session():
    facts = details.for_spec({"steps": [{"runner_profile": BROWSER,
                                         "input": {"browser_tool": "playwright", "qa_identity": "console"}}]},
                             "dev@example.com")
    text = _line(facts)
    assert " · Browser tool: playwright · QA identity: console · Session: " in text, text


def test_a_task_document_naming_qa_identity_shows_it_and_one_without_does_not():
    assert "QA identity: console" in _line(details.for_task(_task(BROWSER, {"prompt": "p", "qa_identity": "console"})))
    assert FIELD not in _line(details.for_task(_task(BROWSER, {"prompt": "p"})))


def test_only_the_workflow_step_naming_it_shows_it_on_its_row():
    tasks = [
        {**_task("claude-code", {"prompt": "p"}), "id": "t_impl", "step_id": "implement"},
        {**_task(BROWSER, {"prompt": "p", "qa_identity": "console"}), "id": "t_ver", "step_id": "verify"},
    ]
    envelope = {"workflow": {"workflow_id": "wf_a", "submitted_by": "dev@example.com",
                             "steps": [{"step_id": t["step_id"], "runner_profile": t["runner_profile"],
                                        "input": t["input"]} for t in tasks]},
                "tasks": tasks}
    rows = {step_id: _line(facts) for step_id, facts in details.per_step(envelope).items()}
    assert "QA identity: console" in rows["verify"]
    assert FIELD not in rows["implement"], rows["implement"]
    assert "QA identity: console" in _line(details.for_workflow(envelope))


def test_a_submitted_spec_carries_it_in_the_runs_details_and_the_naming_rows_details():
    """End to end through `launch.for_spec`, the way `/sc:swarmcloud` builds a
    run: the spec's `inputs` are checked against the catalogue and become the
    step's `input`, and the field follows them. The run's whole line is
    `out["details"]`; the script's description is that line clipped to
    `launch.DESCRIPTION_CHARS`, so it is checked only to start with it."""
    spec = {"label": "qa", "steps": [
        _step("implement", "claude-code"),
        _step("verify", BROWSER, {"qa_identity": "console"}),
    ]}
    out = launch.for_spec(spec, _Api())
    assert "QA identity: console" in out["details"]
    assert _description(out["script_path"]).startswith("Workspace: ")
    rows = out["row_details"]["steps"]
    assert "QA identity: console" in rows["verify"]
    assert FIELD not in rows["implement"], rows["implement"]


def test_a_profile_that_does_not_declare_it_never_shows_it():
    """Only `claude-code-browser` declares the input (contract request 106); a
    document of another profile carrying the key is not read for it."""
    text = _line(details.for_task(_task("claude-code", {"prompt": "p", "qa_identity": "console"})))
    assert FIELD not in text, text


def test_a_credential_shaped_value_is_withheld():
    shaped = "ey" + "J" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    text = _line(details.for_task(_task(BROWSER, {"prompt": "p", "qa_identity": shaped})))
    assert f"QA identity: {details.WITHHELD}" in text, text
    assert shaped not in text
