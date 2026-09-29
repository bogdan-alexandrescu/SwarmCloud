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

With no `pr-body.md` the generated body is unchanged. NO TITLE IS INVENTED
(owner decisions, 2026-09-28): the only title the platform writes is the
step's `issue` input, as "<issue title> (#N)" or "Fixes #N", with the `@`
taken off any mention in the issue's title. With no usable `pr-title.txt` and
no `issue` input the branch is pushed and no pull request is opened, and
`_title_owed` makes `pr-title.txt` an expected output so the attempt fails
retryably before it gets that far. An agent title carrying a task id is
treated as absent, an adopted pull request still titled `[swarm] task_...`
is retitled, and an agent title or body that @mentions anyone in prose is
refused -- a mention inside a code span or a fenced block is not a mention.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.forge import RepoAccess, RepoRef
from swarm_common.states import TaskState

from conftest import seed_attempt

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    assert_no_attribution_in,
    forge,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

KEY = "sk-ant-supersecret-value-0123456789"

DEFAULT_TITLE = b"The widget refuses a negative size\n"


def _publish(
    worker_factory, monkeypatch, origin: Path, *, task_id: str, files: dict, register=(),
    task_input: dict | None = None, step_id: str | None = None, label: str | None = None,
    with_title: bool = True,
):
    """One direct-pr attempt whose agent edits a file and leaves `files` in
    the artifacts folder (bytes, or a callable that makes the entry).

    `with_title` writes `DEFAULT_TITLE` as `pr-title.txt` unless `files`
    already names one: the agent's title is required, so a test about
    something else must still write one to get a pull request at all."""
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
    files = dict(files)
    if with_title and "pr-title.txt" not in files:
        files["pr-title.txt"] = DEFAULT_TITLE
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


def _assert_no_pull_and_title_asked_for(forge, out: dict) -> None:
    """NO TITLE IS INVENTED: the branch is pushed, nothing is opened, and the
    reason tells the agent which file to write."""
    assert forge.pulls == [], forge.pulls
    assert out["published"] is True, out
    assert "pr-title.txt" in out["publish_reason"], out["publish_reason"]
    assert "no pull request was opened" in out["publish_reason"], out["publish_reason"]


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


def test_with_neither_file_and_no_issue_no_pull_request_is_opened(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The old fallback was `f"[swarm] {task_id}"` (#214), and the one after
    it made a title up from the prompt. Neither described the change (owner
    decision 2, 2026-09-28): with no agent title and no issue, no pull
    request is opened at all."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-none", files={}, with_title=False,
    )
    _assert_no_pull_and_title_asked_for(forge, out)


def test_a_body_alone_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-body-only",
        files={"pr-body.md": b"Closes #7\n"}, with_title=False,
    )
    _assert_no_pull_and_title_asked_for(forge, out)


def test_a_title_alone_is_used_with_the_generated_body(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-title-only", files={},
    )
    pull = _only_pull(forge)
    assert pull["title"] == "The widget refuses a negative size", pull["title"]
    assert config.task_id not in pull["title"], pull["title"]
    assert pull["body"].startswith("Opened by SwarmCloud"), pull["body"]
    assert out["pull_request_text"] == {"title": "agent", "body": "platform"}, out


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
def test_a_title_that_cannot_be_used_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge, title
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-bad-title",
        files={"pr-title.txt": title},
    )
    _assert_no_pull_and_title_asked_for(forge, out)
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
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-upd-none", files={},
        task_input={"issue": 5}, with_title=False,
    )
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
        task_input={"prompt": "Do the thing. Then more.", "issue": issue}, with_title=False,
    )
    pull = _only_pull(forge)
    assert pull["title"] == expected, pull["title"]
    assert f"- task: `{config.task_id}`" in pull["body"], "the task id left the metadata block"


@pytest.mark.parametrize(
    "mentioned",
    ["Ping @octocat about the widget", "The @acme/team widget accepts -1"],
    ids=["user", "team"],
)
def test_a_mention_in_an_issue_title_loses_its_at_sign(
    worker_factory, monkeypatch, origin, local_urls, forge, mentioned
):
    """An issue's title is anyone's text; a title the platform writes from it
    must page no one."""
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-issue-at", files={},
        task_input={"issue": {"number": 8, "title": mentioned}}, with_title=False,
    )
    pull = _only_pull(forge)
    assert "@" not in pull["title"], pull["title"]
    assert pull["title"] == f"{mentioned.replace('@', '')} (#8)", pull["title"]


def test_the_prompt_alone_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The prompt's first sentence used to be the title. It described the
    request, not the change, and is no longer used."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-prompt", files={},
        task_input={"prompt": "Make the widget refuse negatives. Then more words."},
        with_title=False,
    )
    _assert_no_pull_and_title_asked_for(forge, out)


def test_the_step_alone_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-step", files={}, step_id="build",
        label="Build the widget", with_title=False,
    )
    _assert_no_pull_and_title_asked_for(forge, out)


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
        task_input={"prompt": "Make the widget refuse negatives. More.", "issue": 77},
    )
    pull = _only_pull(forge)
    assert pull["title"] == "Fixes #77", pull["title"]
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
        task_input={"issue": 4},
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


@pytest.mark.parametrize(
    "body",
    [
        b"Closes #4\n\nThe fixture is now `@pytest.fixture(scope=\"module\")`.\n",
        b"Closes #4\n\nUse ``@octocat`` as the example login.\n",
        b"Closes #4\n\n```python\n@pytest.fixture\ndef widget():\n    return 1\n```\n\nDone.\n",
        b"Closes #4\n\n~~~\ncc @acme/reviewers\n~~~\n",
    ],
    ids=["code-span", "double-backtick-span", "backtick-fence", "tilde-fence"],
)
def test_a_mention_inside_code_is_kept(
    worker_factory, monkeypatch, origin, local_urls, forge, body
):
    """GitHub notifies no one for `@name` inside a code span or a fenced
    block (owner decision 3, 2026-09-28), so a body quoting a decorator is
    the agent's, not refused."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-code-at", files={"pr-body.md": body},
    )
    pull = _only_pull(forge)
    assert pull["body"].startswith("Closes #4"), pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out
    assert not any(
        r.startswith("pr-body.md") for r in out.get("pull_request_text_refused", [])
    ), out


def test_a_mention_after_a_closed_fence_still_refuses_the_body(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: code is skipped only while it lasts."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-code-then-at",
        files={"pr-body.md": b"Closes #4\n\n```\n@pytest.fixture\n```\n\n@octocat please review.\n"},
    )
    assert "pr-body.md: mentions" in out.get("pull_request_text_refused", []), out
    assert out["pull_request_text"]["body"] == "platform", out


# -- two CommonMark bypasses in the code/fence skip (#259 review, M3) --


def test_a_code_span_cannot_hide_a_mention_across_a_blank_line():
    """CommonMark: an inline code span cannot cross a blank line (a paragraph
    break) -- a lone backtick before one, and another after it, are just two
    backticks, not an opening and a closing delimiter. The previous pattern
    used `re.DOTALL`, which let `.` cross a blank line too, so a stray
    backtick, a blank line, a real `@mention` in its own paragraph, and a
    later backtick all read as "one code span" and the mention inside it was
    never seen."""
    text = "Closes #4\n\nnote `\n\n@octocat please review\n`\n"
    assert lifecycle._carries_mention(text) is True


def test_a_backtick_fences_info_string_cannot_hold_a_backtick():
    """CommonMark: a backtick-fenced block's info string must not itself
    contain a backtick -- a line like "``` `python" is not a valid fence at
    all, so GitHub would read what follows as ordinary prose (and notify for
    a mention in it). The previous pattern accepted any non-newline info
    string on a backtick fence, so it read this as a real fence and hid a
    mention inside it. A tilde fence has no such restriction and is
    unaffected."""
    text = "Closes #4\n\n``` `python\n@octocat please review\n```\n"
    assert lifecycle._carries_mention(text) is True


def test_a_real_code_span_and_fence_still_hide_a_decorator():
    """The control: fixing the two bypasses above must not stop a genuine
    code span or a valid fence from hiding `@pytest.fixture`."""
    assert lifecycle._carries_mention("Use `@pytest.fixture` here.\n") is False
    assert lifecycle._carries_mention("Use ``@octocat`` please.\n") is False
    assert lifecycle._carries_mention("```python\n@pytest.fixture\n```\n") is False
    assert lifecycle._carries_mention("~~~\ncc @acme/reviewers\n~~~\n") is False


# -- the mention lookbehind is ASCII-only (#259 review, M3) --


def test_a_non_ascii_letter_before_at_does_not_hide_a_mention():
    """Python's `\\w` on a `str` pattern matches any Unicode letter, not just
    `[A-Za-z0-9_]`; GitHub's own mention boundary is ASCII. A non-ASCII letter
    immediately before `@` must not stop the mention after it from being
    seen."""
    assert lifecycle._carries_mention("Ping café@octocat about this.\n") is True


def test_an_ascii_letter_before_at_still_hides_an_email():
    """The control for the ASCII fix: an ASCII letter before `@` is still
    excluded, so an address like `ops@example.com` remains not a mention."""
    assert lifecycle._carries_mention("Reported by ops@example.com.\n") is False


# -- the title is owed as an expected output only when a pull request needs it --


def _owing_worker(worker_factory, *, dispatch: dict, task_input: dict | None = None):
    worker, _, _ = worker_factory(
        task_id="t-pr-owed", attempt_id="att-t-pr-owed", lease_id="lease-t-pr-owed",
        repository_url="https://github.com/acme/widgets.git",
    )
    task: dict = {"task_id": "t-pr-owed", "metadata": {"dispatch": dispatch}}
    if task_input is not None:
        task["input"] = task_input
    worker._task = task
    return worker, task


@pytest.mark.parametrize(
    ("dispatch", "task_input", "owed"),
    [
        ({"strategy": "direct-pr"}, None, True),
        ({"strategy": "direct-pr"}, {"prompt": "Do it."}, True),
        ({"strategy": "integrate", "role": "integrator"}, None, True),
        ({"strategy": "collect"}, None, False),
        ({"strategy": "integrate", "role": "contributor"}, None, False),
        ({"strategy": "direct-pr"}, {"issue": 12}, False),
        ({"strategy": "integrate", "role": "integrator"}, {"issue": {"number": 12}}, False),
    ],
    ids=[
        "direct-pr", "direct-pr-prompt-only", "integrator", "collect", "contributor",
        "direct-pr-with-issue", "integrator-with-issue",
    ],
)
def test_the_title_is_owed_only_when_a_pull_request_has_nothing_else_to_title_it(
    worker_factory, dispatch, task_input, owed
):
    worker, task = _owing_worker(worker_factory, dispatch=dispatch, task_input=task_input)
    assert worker._title_owed(task) is owed


# -- a refused title fails the attempt retryably, before anything is pushed ----
#
# Owner decision, 2026-09-28: a `pr-title.txt` that EXISTS but is refused is
# treated like a missing one. The finish check tests that the title is usable,
# not only that the file is there, so the retry is told what to fix and no
# branch is pushed without the pull request it was for.


def _run_writing_title(db, worker_factory, monkeypatch, title: str):
    seed_attempt(
        db,
        task_input={
            "prompt": "fix the widget",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": "pr-title.txt",
            "artifact_text": title,
        },
    )
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    # Owed as though the step had a repository to open a pull request on; the
    # repository itself is `_publish`'s concern above, not this one's.
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: True)
    worker, _config, _exporter = worker_factory()
    published: list[bool] = []

    def harvest(*, publish: bool, **_kwargs):
        published.append(publish)
        return None

    monkeypatch.setattr(worker, "_harvest_git", harvest)
    return worker, published


def test_a_refused_title_fails_the_attempt_retryably_and_pushes_nothing(
    db, worker_factory, monkeypatch
):
    worker, published = _run_writing_title(db, worker_factory, monkeypatch, "Fix task_1 now\n")

    assert worker.run() == ExitCode.FAILED
    assert published == [False], "a refused title still published"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert "pr-title.txt refused: names a task id; write a fact-style title" in task["last_error"], (
        task["last_error"]
    )
    retrying = [e for e in db.events("task_1") if e["type"] == "retrying"]
    assert len(retrying) == 1, db.event_types("task_1")
    assert retrying[0]["detail"]["cause"] == "pull_request_title_refused"
    assert "succeeded" not in db.event_types("task_1")


@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("Refuse negatives, thanks @octocat\n", "mentions"),
        ("Fix it \U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n",
         "carries attribution"),
    ],
    ids=["mention", "attribution"],
)
def test_every_refusal_fails_the_attempt_with_its_reason(
    db, worker_factory, monkeypatch, title, why
):
    worker, published = _run_writing_title(db, worker_factory, monkeypatch, title)

    assert worker.run() == ExitCode.FAILED
    assert published == [False]
    assert f"pr-title.txt refused: {why}" in db.doc("tasks/task_1")["last_error"]


def test_a_usable_title_publishes_and_succeeds(db, worker_factory, monkeypatch):
    """The control: the same attempt with a fact-style title publishes."""
    worker, published = _run_writing_title(
        db, worker_factory, monkeypatch, "The widget refuses a negative size\n"
    )

    assert worker.run() == ExitCode.OK
    assert published == [True]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
