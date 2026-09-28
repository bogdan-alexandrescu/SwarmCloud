"""A platform pull request takes its title and body from the agent (#214).

WHAT WENT WRONG. The worker wrote every pull request's title as
`[swarm] <task>` and its body itself (`_pull_request_body`), so a platform PR
could never say `Closes #N`, and a reviewer rewrote both by hand before
merging.

WHAT IS PINNED. When the agent leaves `pr-title.txt` and/or `pr-body.md` in
`$SWARM_ARTIFACTS_DIR`, the worker uses them:

* bounded -- one line and at most 256 characters for the title, at most
  64 KiB for the body -- and cut, never refused, for length;
* scrubbed of every registered secret BEFORE it is cut, as every other text
  the worker shows;
* refused, falling back to the generated text, when it is not UTF-8, is empty,
  is a title of more than one line, or carries attribution (the owner's rule:
  none on GitHub);
* read without following a link, like every other file the worker takes out
  of the artifacts folder;
* with the platform's metadata block still in the body, after the agent's.

With neither file the generated body is unchanged, and the generated title
NEVER carries the task id (owner rule, 2026-09-28): the issue input's title
or number, else the prompt's first sentence, else the workflow step. An agent
title carrying a task id is treated as absent, an adopted pull request still
titled `[swarm] task_...` is retitled, and an agent title or body that
@mentions anyone is refused.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.forge import RepoAccess, RepoRef

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    assert_no_attribution_in,
    forge,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

KEY = "sk-ant-supersecret-value-0123456789"


def _publish(
    worker_factory, monkeypatch, origin: Path, *, task_id: str, files: dict, register=(),
    task_input: dict | None = None, step_id: str | None = None, label: str | None = None,
):
    """One direct-pr attempt whose agent edits a file and leaves `files` in
    the artifacts folder (bytes, or a callable that makes the entry)."""
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    metadata: dict = {"dispatch": {"strategy": "direct-pr"}}
    if label is not None:
        metadata["label"] = label
    task = {"task_id": task_id, "metadata": metadata}
    if task_input is not None:
        task["input"] = task_input
    if step_id is not None:
        task["step_id"] = step_id
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    for value in register:
        worker.log.register_secret(value)
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "agent.txt").write_text("the agent's work\n")
    for name, content in files.items():
        target = worker.ws.artifacts / name
        if callable(content):
            content(target)
        else:
            target.write_bytes(content)
    out = worker._harvest_git(publish=True)
    return worker, config, out


def _only_pull(forge) -> dict:
    assert len(forge.pulls) == 1, forge.pulls
    return forge.pulls[0]


def test_the_agents_title_and_body_are_used_and_the_metadata_block_is_kept(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-text",
        files={
            "pr-title.txt": b"The widget refuses a negative size\n",
            "pr-body.md": b"Closes #12\n\nThe widget accepted -1 and drew nothing.\n",
        },
    )

    assert out["published"] is True, out.get("publish_reason")
    pull = _only_pull(forge)
    assert pull["title"] == "The widget refuses a negative size"
    assert pull["body"].startswith("Closes #12\n\nThe widget accepted -1 and drew nothing."), pull["body"]
    # The platform's block follows the agent's text, whole.
    assert f"- task: `{config.task_id}`" in pull["body"]
    assert f"- attempt: `{config.attempt_id}`" in pull["body"]
    assert "Opened by SwarmCloud" in pull["body"]
    assert pull["body"].index("Closes #12") < pull["body"].index("Opened by SwarmCloud")
    assert out["pull_request_text"] == {"title": "agent", "body": "agent"}, out


def test_with_neither_file_the_generated_title_never_carries_the_task_id(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The old fallback was `f"[swarm] {task_id}"` (#214). The owner's
    2026-09-28 rule retired it: no generated title may carry the task id, and
    with no issue input and no prompt to draw from, the last resort names the
    runner profile instead."""
    _, config, out = _publish(worker_factory, monkeypatch, origin, task_id="t-pr-none", files={})

    pull = _only_pull(forge)
    assert config.task_id not in pull["title"], pull["title"]
    assert pull["title"] == "SwarmCloud: work from workflow mock", pull["title"]
    assert pull["body"].startswith("Opened by SwarmCloud"), pull["body"]
    assert out["pull_request_text"] == {"title": "platform", "body": "platform"}, out


def test_either_file_alone_is_used_with_the_other_generated(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, _ = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-body-only",
        files={"pr-body.md": b"Closes #7\n"},
    )
    pull = _only_pull(forge)
    assert config.task_id not in pull["title"], pull["title"]
    assert pull["title"] == "SwarmCloud: work from workflow mock", pull["title"]
    assert pull["body"].startswith("Closes #7"), pull["body"]


def test_a_long_title_and_a_long_body_are_cut_not_refused(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-long",
        files={
            "pr-title.txt": ("T" * 400).encode(),
            "pr-body.md": ("Closes #9\n\n" + "b" * (80 * 1024)).encode(),
        },
    )
    pull = _only_pull(forge)
    assert pull["title"].startswith("TTTT") and len(pull["title"]) <= 256, len(pull["title"])
    agent_part = pull["body"].split("Opened by SwarmCloud")[0]
    assert agent_part.startswith("Closes #9")
    assert len(agent_part.encode("utf-8")) <= 64 * 1024 + 256, len(agent_part.encode("utf-8"))
    assert "Opened by SwarmCloud" in pull["body"], "the cut took the metadata block with it"
    assert out["pull_request_text"] == {"title": "agent", "body": "agent"}, out


def test_a_registered_secret_is_scrubbed_before_the_title_is_cut(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The key straddles character 256. Cut first, a prefix of it would
    survive the scrub; scrubbed first, none of it does."""
    title = "x" * 240 + KEY + "y" * 40
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-secret",
        files={
            "pr-title.txt": title.encode(),
            "pr-body.md": f"Closes #3\n\nused {KEY}\n".encode(),
        },
        register=(KEY,),
    )
    pull = _only_pull(forge)
    assert "sk-ant" not in pull["title"], pull["title"]
    assert KEY not in pull["body"] and "sk-ant" not in pull["body"], pull["body"]
    assert pull["body"].startswith("Closes #3")


@pytest.mark.parametrize(
    "title",
    [
        b"first line\nsecond line\n",
        b"   \n",
        b"\xff\xfe not utf-8",
        b"Fix it \xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)",
    ],
    ids=["two-lines", "blank", "not-utf8", "attribution"],
)
def test_a_title_that_cannot_be_used_falls_back_to_the_generated_one(
    worker_factory, monkeypatch, origin, local_urls, forge, title
):
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-bad-title",
        files={"pr-title.txt": title},
    )
    pull = _only_pull(forge)
    assert config.task_id not in pull["title"], pull["title"]
    assert pull["title"] == "SwarmCloud: work from workflow mock", pull["title"]
    assert out["pull_request_text"]["title"] == "platform", out
    assert out.get("pull_request_text_refused"), out


@pytest.mark.parametrize(
    "body",
    [
        b"Closes #4\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        b"Closes #4\n\n\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)\n",
        b"\xff\xfe not utf-8",
    ],
    ids=["co-author-trailer", "generated-footer", "not-utf8"],
)
def test_a_body_that_cannot_be_used_falls_back_to_the_generated_one(
    worker_factory, monkeypatch, origin, local_urls, forge, body
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-bad-body",
        files={"pr-body.md": body},
    )
    pull = _only_pull(forge)
    assert pull["body"].startswith("Opened by SwarmCloud"), pull["body"]
    assert_no_attribution_in(pull["body"], "pull request body")
    assert out["pull_request_text"]["body"] == "platform", out
    assert out.get("pull_request_text_refused"), out


def test_a_body_that_is_a_link_is_not_followed(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    outside = tmp_path / "outside.md"
    outside.write_text("Closes #99\n\nsomething the agent pointed at\n")

    def link(target: Path) -> None:
        target.symlink_to(outside)

    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-link",
        files={"pr-body.md": link},
    )
    pull = _only_pull(forge)
    assert "Closes #99" not in pull["body"], pull["body"]
    assert out["pull_request_text"]["body"] == "platform", out


# -- the forge: a reused pull request takes the agent's text too --------------


def _access() -> RepoAccess:
    return RepoAccess(
        ref=RepoRef("github.com", "acme", "widgets"),
        default_branch="main",
        can_push=True,
        reason="ok",
    )


def test_an_adopted_pull_request_is_updated_when_asked(monkeypatch):
    """A retried attempt pushes the same branch and adopts the open pull
    request. Asked to (the agent wrote its own text), the worker updates that
    pull request's title and body, so `Closes #N` reaches it."""
    seen = []

    def fake(url, *, token, method="GET", payload=None):
        seen.append((method, url, payload))
        if method == "POST":
            return 422, {"errors": [{"message": "A pull request already exists"}]}
        if method == "PATCH":
            return 200, {"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                         "state": "open"}
        return 200, [{"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                      "state": "open"}]

    monkeypatch.setattr(forge_mod, "_request", fake)
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="New title", body="Closes #12", update_existing=True,
    )
    assert pr.number == 47 and pr.created is False and pr.updated is True
    assert [m for m, _, _ in seen] == ["POST", "GET", "PATCH"], seen
    method, url, payload = seen[-1]
    assert url.endswith("/repos/acme/widgets/pulls/47"), url
    assert payload == {"title": "New title", "body": "Closes #12"}, payload


def test_a_failed_update_still_adopts_the_pull_request(monkeypatch):
    def fake(url, *, token, method="GET", payload=None):
        if method == "POST":
            return 422, {"errors": [{"message": "A pull request already exists"}]}
        if method == "PATCH":
            return 403, {"message": "Resource not accessible by integration"}
        return 200, [{"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                      "state": "open"}]

    monkeypatch.setattr(forge_mod, "_request", fake)
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="New title", body="Closes #12", update_existing=True,
    )
    assert pr.number == 47 and pr.created is False and pr.updated is False


def test_the_worker_asks_for_the_update_only_when_the_agent_wrote_text(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A human may have edited a generated title or body by hand; a retry that
    has nothing of the agent's to say must not overwrite that."""
    _publish(worker_factory, monkeypatch, origin, task_id="t-pr-upd-none", files={})
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-upd-agent",
        files={"pr-body.md": b"Closes #5\n"},
    )
    assert [bool(p.get("update_existing")) for p in forge.pulls] == [False, True], forge.pulls


# -- the generated title never carries the task id (owner rule, 2026-09-28) ---


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (42, "Fixes #42"),
        ("42", "Fixes #42"),
        ({"number": 42, "title": "The widget accepts a negative size"},
         "The widget accepts a negative size (#42)"),
    ],
    ids=["number", "numeric-string", "with-title"],
)
def test_an_issue_input_names_the_issue_in_the_title(
    worker_factory, monkeypatch, origin, local_urls, forge, issue, expected
):
    _, config, _ = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-issue", files={},
        task_input={"prompt": "Do the thing. Then more.", "issue": issue},
    )
    pull = _only_pull(forge)
    assert pull["title"] == expected, pull["title"]
    assert f"- task: `{config.task_id}`" in pull["body"], "the task id left the metadata block"


def test_the_prompts_first_sentence_is_the_title_collapsed_scrubbed_and_capped(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-prompt-a", files={},
        task_input={"prompt": "  Make the\n   widget refuse\tnegatives.  Then more words."},
    )
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-prompt-b", files={},
        task_input={"prompt": f"Rotate {KEY} before the release! It leaked."},
        register=(KEY,),
    )
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-prompt-c", files={},
        task_input={"prompt": "w" * 400 + ". Tail."},
    )
    short, secret, long = (p["title"] for p in forge.pulls)
    assert short == "Make the widget refuse negatives.", short
    assert KEY not in secret and "sk-ant" not in secret, secret
    assert secret.startswith("Rotate ") and secret.endswith("before the release!"), secret
    assert len(long) <= lifecycle.PR_TITLE_MAX_CHARS and long.endswith("..."), (len(long), long[-5:])
    assert long.startswith("wwww"), long[:5]


def test_with_nothing_else_the_title_names_the_workflow_step(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, _ = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-step", files={}, step_id="build",
    )
    pull = _only_pull(forge)
    assert pull["title"] == "SwarmCloud: work from workflow build", pull["title"]
    assert config.task_id not in pull["title"]


@pytest.mark.parametrize(
    "title",
    [
        None,  # this task's own id, filled in below
        b"[SWARM] Task_0123abcd",
        b"[swarm] task_9be4128488d342208947",
    ],
    ids=["own-task-id", "retired-shape-any-case", "another-tasks-old-title"],
)
def test_an_agent_title_carrying_a_task_id_is_treated_as_absent(
    worker_factory, monkeypatch, origin, local_urls, forge, title
):
    task_id = "t-pr-own-id-77"
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id=task_id,
        files={"pr-title.txt": title if title is not None else f"Fix {task_id} now".encode()},
        task_input={"prompt": "Make the widget refuse negatives. More."},
    )
    pull = _only_pull(forge)
    assert pull["title"] == "Make the widget refuse negatives.", pull["title"]
    assert config.task_id not in pull["title"]
    assert out["pull_request_text"]["title"] == "platform", out


def _adopting_forge(existing_title: str, seen: list):
    def fake(url, *, token, method="GET", payload=None):
        seen.append((method, url, payload))
        if method == "POST":
            return 422, {"errors": [{"message": "A pull request already exists"}]}
        if method == "PATCH":
            return 200, {"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                         "state": "open"}
        return 200, [{"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                      "state": "open", "title": existing_title}]
    return fake


def test_an_adopted_pull_request_with_the_retired_title_is_retitled_only(monkeypatch):
    """Nothing of the agent's asked for an update, but the adopted pull
    request still carries `[swarm] task_...`: its TITLE is replaced by the
    same rule, and its body -- which may be a human's -- is left alone."""
    seen: list = []
    monkeypatch.setattr(forge_mod, "_request", _adopting_forge("[Swarm] TASK_1", seen))
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="Make the widget refuse negatives.", body="generated",
        update_existing=False,
        retitle_if=lambda t: "task_" in t.lower(),
    )
    assert pr.updated is True, pr
    assert [m for m, _, _ in seen] == ["POST", "GET", "PATCH"], seen
    assert seen[-1][2] == {"title": "Make the widget refuse negatives."}, seen[-1]


def test_an_adopted_pull_request_with_a_human_title_is_left_alone(monkeypatch):
    seen: list = []
    monkeypatch.setattr(forge_mod, "_request", _adopting_forge("Refuse negative sizes", seen))
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="Make the widget refuse negatives.", body="generated",
        update_existing=False,
        retitle_if=lambda t: "task_" in t.lower(),
    )
    assert pr.updated is False, pr
    assert [m for m, _, _ in seen] == ["POST", "GET"], seen


def test_the_worker_retitles_by_the_task_id_rule(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """What the worker hands the forge is the same rule the agent's title is
    held to: the retired shape in any case, or this task's id."""
    _, config, _ = _publish(worker_factory, monkeypatch, origin, task_id="t-pr-retitle", files={})
    retitle_if = _only_pull(forge)["retitle_if"]
    assert retitle_if(f"[swarm] {config.task_id}") is True
    assert retitle_if("[SWARM] task_other") is True
    assert retitle_if(f"Fixes {config.task_id}") is True
    assert retitle_if("Refuse negative sizes") is False
    assert retitle_if("") is False


# -- an agent's text that @mentions anyone is refused (owner rule, 2026-09-28) --


@pytest.mark.parametrize(
    ("files", "refused"),
    [
        ({"pr-body.md": b"Closes #4\n\n@octocat please review.\n"}, "pr-body.md: mentions"),
        ({"pr-body.md": b"Closes #4\n\ncc @acme/reviewers\n"}, "pr-body.md: mentions"),
        ({"pr-title.txt": b"Refuse negatives (@octocat)\n"}, "pr-title.txt: mentions"),
    ],
    ids=["user-in-body", "team-in-body", "user-in-title"],
)
def test_an_agent_text_with_a_mention_is_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream, files, refused
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-mention", files=files,
    )
    pull = _only_pull(forge)
    assert "@octocat" not in pull["title"] and "@octocat" not in pull["body"], pull
    assert "@acme/reviewers" not in pull["body"], pull["body"]
    assert refused in out.get("pull_request_text_refused", []), out
    which = "body" if refused.startswith("pr-body") else "title"
    assert out["pull_request_text"][which] == "platform", out
    assert f"agent {which} refused: mentions" in log_stream.getvalue()


def test_an_email_address_is_not_a_mention(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: `@` inside an address pages nobody, and is kept."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-email",
        files={"pr-body.md": b"Closes #4\n\nReported by ops@example.com.\n"},
    )
    pull = _only_pull(forge)
    assert pull["body"].startswith("Closes #4"), pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out
