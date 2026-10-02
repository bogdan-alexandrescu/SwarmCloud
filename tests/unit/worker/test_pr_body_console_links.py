"""A pull request the worker opens carries the console links -- only when switched on.

Owner decision, 2026-10-01: the workflow and agent console links are appended
to the body of a pull request the worker opens ONLY when the platform switch
(SWARM_PR_CONSOLE_LINKS, from the scheduler's own settings) is on AND a console
origin (SWARM_CONSOLE_URL) is configured. Off by default, until the owner has
seen it on a real pull request.

WHAT IS PINNED
  * off: the body is byte-for-byte what it was;
  * on: exactly the block `CONSOLE_LINKS_BLOCK` documents is appended -- the
    workflow link only when the task belongs to a workflow, then the agent link;
  * on with no origin (empty, blank): nothing is appended;
  * PARITY: the worker cannot import swarm_api, so it spells the URLs itself;
    they must equal `swarm_api.codec.agent_console_url` / `workflow_console_url`
    for the same origin and ids, so a route changed in one place fails here;
  * `WorkerConfig.from_env` reads both names, default off and None;
  * the real publish path (`_harvest_git` -> `open_pull_request`) applies it.
"""

from __future__ import annotations

import shutil

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.config import WorkerConfig
from agent_worker.lifecycle import pr_body_with_console_links
from swarm_api.codec import agent_console_url, workflow_console_url

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    forge,
    local_urls,
    origin,
)

CONSOLE = "https://swarm.example.test"
BODY = "Closes #12\n\nThe widget accepted -1.\n\n---\n\nOpened by SwarmCloud  é"


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------


def test_pr_body_console_links_off_leaves_the_body_byte_for_byte_unchanged():
    out = pr_body_with_console_links(
        BODY, origin=CONSOLE, task_id="task_1", workflow_id="wf_1", enabled=False
    )
    assert out == BODY
    assert out.encode("utf-8") == BODY.encode("utf-8")


def test_pr_body_console_links_on_appends_exactly_the_two_links():
    out = pr_body_with_console_links(
        BODY, origin=CONSOLE + "/", task_id="task_1", workflow_id="wf_1", enabled=True
    )
    assert out == (
        BODY
        + "\n\n---\n\n"
        + "Console:\n"
        + f"- workflow: {CONSOLE}/workflows/wf_1\n"
        + f"- agent: {CONSOLE}/agents/live/task_1\n"
    )


def test_pr_body_console_links_on_without_a_workflow_appends_only_the_agent_link():
    for workflow_id in (None, ""):
        out = pr_body_with_console_links(
            BODY, origin=CONSOLE, task_id="task_1", workflow_id=workflow_id, enabled=True
        )
        assert out == BODY + f"\n\n---\n\nConsole:\n- agent: {CONSOLE}/agents/live/task_1\n"


@pytest.mark.parametrize("empty_origin", [None, "", "   ", "/"])
def test_pr_body_console_links_on_with_no_origin_appends_nothing(empty_origin):
    out = pr_body_with_console_links(
        BODY, origin=empty_origin, task_id="task_1", workflow_id="wf_1", enabled=True
    )
    assert out == BODY


@pytest.mark.parametrize("origin_value", [CONSOLE, CONSOLE + "/", " " + CONSOLE + "// "])
@pytest.mark.parametrize("task_id", ["task_1f699ef4cdbb4cc79c16", "t-pr-text"])
@pytest.mark.parametrize(
    "workflow_id", ["wf_abc123", "wf with space", "a/b?c#d", "it's(!*)~_.-", "été"]
)
def test_console_link_urls_match_the_apis_codec(origin_value, task_id, workflow_id):
    out = pr_body_with_console_links(
        "", origin=origin_value, task_id=task_id, workflow_id=workflow_id, enabled=True
    )
    assert out == (
        "\n\n---\n\nConsole:\n"
        f"- workflow: {workflow_console_url(origin_value, workflow_id)}\n"
        f"- agent: {agent_console_url(origin_value, task_id)}\n"
    )


# ---------------------------------------------------------------------------
# The worker's configuration
# ---------------------------------------------------------------------------

_IDENTITY = {
    "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
    "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
    "PROJECT_ID": "swarm-test",
}


def _env(monkeypatch, **extra: str) -> None:
    for name in ("SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER", "SPEC_VERIFY_KEYS",
                 "SPEC_SIGNING_KEY", "TASK_TIMEOUT_SECONDS", "CLOUD_RUN_JOB",
                 "RUNNER_JOB_NAME", "SWARM_CONSOLE_URL", "SWARM_PR_CONSOLE_LINKS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**_IDENTITY, **extra}.items():
        monkeypatch.setenv(name, value)


def test_worker_config_console_links_default_off(monkeypatch):
    _env(monkeypatch)
    cfg = WorkerConfig.from_env()
    assert cfg.pr_console_links is False
    assert cfg.console_url is None


@pytest.mark.parametrize(
    ("raw", "expected"), [("true", True), ("1", True), ("false", False), ("off", False)]
)
def test_worker_config_reads_the_console_link_settings(monkeypatch, raw, expected):
    _env(monkeypatch, SWARM_CONSOLE_URL=f" {CONSOLE} ", SWARM_PR_CONSOLE_LINKS=raw)
    cfg = WorkerConfig.from_env()
    assert cfg.pr_console_links is expected
    assert cfg.console_url == CONSOLE


# ---------------------------------------------------------------------------
# The real publish path
# ---------------------------------------------------------------------------


def _open_one_pull(worker_factory, monkeypatch, origin, forge, *, workflow_id=None, **cfg):
    task_id = "task_prlinks"
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
        **cfg,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    if workflow_id is not None:
        task["workflow_id"] = workflow_id
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "agent.txt").write_text("the agent's work\n")
    (worker.ws.artifacts / "pr-title.txt").write_bytes(b"The widget refuses a negative size\n")
    (worker.ws.artifacts / "pr-body.md").write_bytes(b"Closes #12\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")
    assert len(forge.pulls) == 1, forge.pulls
    return forge.pulls[0]["body"]


_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@_needs_git
def test_pr_body_opened_with_console_links_on_ends_with_the_links(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    body = _open_one_pull(
        worker_factory, monkeypatch, origin, forge, workflow_id="wf_links",
        console_url=CONSOLE, pr_console_links=True,
    )
    assert body.startswith("Closes #12")
    assert body.endswith(
        "\n\n---\n\nConsole:\n"
        f"- workflow: {CONSOLE}/workflows/wf_links\n"
        f"- agent: {CONSOLE}/agents/live/task_prlinks\n"
    ), body


@_needs_git
def test_pr_body_opened_with_console_links_off_carries_no_link(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    body = _open_one_pull(
        worker_factory, monkeypatch, origin, forge, workflow_id="wf_links",
        console_url=CONSOLE, pr_console_links=False,
    )
    assert CONSOLE not in body
    assert "Console:" not in body
