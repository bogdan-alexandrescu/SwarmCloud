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
  is a title of more than one line, or is a title carrying attribution (the
  owner's rule: none on GitHub);
* a body carrying attribution has those lines removed and the rest published
  (#735); only a body with nothing left falls back to the generated text;
* read without following a link, like every other file the worker takes out
  of the artifacts folder;
* with the platform's metadata block still in the body, after the agent's.

With no `pr-body.md` the generated body is unchanged. NO TITLE IS INVENTED
(owner decisions, 2026-09-28): the only title the platform writes is the
step's `issue` input, as "<issue title> (part of #N)" or "Work on issue #N
(part of #N)" -- never a closing keyword, which a
squash merge would carry into the base branch and close the issue with
(review of #545) -- with a zero-width joiner after every `@` in the issue's
title. With no usable `pr-title.txt` and
no `issue` input the branch is pushed and no pull request is opened, and
`_title_owed` makes `pr-title.txt` an expected output so the attempt fails
retryably before it gets that far. An agent title carrying a task id is
treated as absent, an adopted pull request still titled `[swarm] task_...`
is retitled, and every `@` that could start a mention in the agent's title or
body -- in code or not -- gets a U+200D ZERO WIDTH JOINER after it, so nothing
pages anyone and no text is refused for a mention (owner decision, 2026-09-29).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.forge import RepoAccess, RepoRef
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

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
        b"\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)\n",
        b"\n---\n\n\xf0\x9f\xa4\x96\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        b"\xff\xfe not utf-8",
    ],
    ids=["only-a-footer", "only-attribution-and-a-rule", "not-utf8"],
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


@pytest.mark.parametrize(
    "body",
    [
        b"Closes #322\n\nThe last item, 9c.\n\n"
        b"\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)\n",
        b"Closes #322\n\nThe last item, 9c.\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        b"Closes #322\n\nThe last item, 9c.\n\n"
        b"\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
        b"Co-Authored-By: Claude Opus <noreply@anthropic.com>\n",
    ],
    ids=["generated-footer", "co-author-trailer", "both"],
)
def test_an_attributed_body_loses_the_attribution_and_keeps_the_rest(
    worker_factory, monkeypatch, origin, local_urls, forge, body
):
    """#735: the attribution lines are removed and the agent's body, its
    `Closes #N` included, is published -- not swapped for the fallback."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-attributed",
        files={"pr-body.md": body},
    )
    pull = _only_pull(forge)
    assert pull["body"].startswith("Closes #322\n\nThe last item, 9c.\n\n"), pull["body"]
    assert_no_attribution_in(pull["body"], "pull request body")
    assert "\U0001f916" not in pull["body"], pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out
    assert not out.get("pull_request_text_refused"), out


def test_strip_attribution_keeps_every_other_line():
    text = (
        "Closes #322\n\nWhat changed.\n\n"
        "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
    )
    assert lifecycle.strip_attribution(text) == "Closes #322\n\nWhat changed."


def test_strip_attribution_removes_a_bare_robot_line():
    assert lifecycle.strip_attribution("Closes #9\n\n\U0001f916\n") == "Closes #9"


def test_strip_attribution_leaves_nothing_of_a_body_that_is_only_a_footer():
    footer = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
    assert lifecycle.strip_attribution(footer) == ""
    assert lifecycle.strip_attribution("---\n\n" + footer) == ""


@pytest.mark.parametrize(
    "text",
    [
        "Closes #12\n\nThe widget accepted -1.\n",
        "  leading space\n\n\n\ntrailing blank lines\n\n\n",
        "a claude-code runner profile, `@decorator`, \u00e9\r\nCRLF\n---\n",
        "",
    ],
    ids=["plain", "whitespace", "mixed", "empty"],
)
def test_strip_attribution_returns_a_body_without_attribution_byte_for_byte(text):
    assert lifecycle.strip_attribution(text) is text


def test_a_body_without_attribution_is_published_byte_for_byte(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    agent_body = "Closes #12\n\n\n\nTwo blank lines above, `x  ` and a rule:\n\n---\n\ndone."
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-plain",
        files={"pr-body.md": (agent_body + "\n").encode()},
    )
    pull = _only_pull(forge)
    # The agent's text, exactly, then the platform's separator.
    assert pull["body"].startswith(agent_body + "\n\n---\n\n"), pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out


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


def test_an_adopted_pull_request_gains_the_amendment_and_keeps_its_body(monkeypatch):
    """A retried attempt pushes the same branch and adopts the open pull
    request. Its body is KEPT and the attempt's section appended (#807), so a
    new `Closes #N` reaches it without the old ones being lost; its title is
    kept unless `retitle_if` says it is the worker's own."""
    seen = []

    def fake(url, *, token, method="GET", payload=None):
        seen.append((method, url, payload))
        if method == "POST":
            return 422, {"errors": [{"message": "A pull request already exists"}]}
        if method == "PATCH":
            return 200, {"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                         "state": "open"}
        return 200, [{"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                      "state": "open", "title": "Old title", "body": "Closes #3"}]

    monkeypatch.setattr(forge_mod, "_request", fake)
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="New title", body="Closes #12", amendment="## Republished (attempt 2): `t`\n\nCloses #12",
        retitle_if=lambda t: False,
    )
    assert pr.number == 47 and pr.created is False and pr.updated is True and pr.appended is True
    assert [m for m, _, _ in seen] == ["POST", "GET", "PATCH"], seen
    method, url, payload = seen[-1]
    assert url.endswith("/repos/acme/widgets/pulls/47"), url
    assert payload == {"body": "Closes #3\n\n## Republished (attempt 2): `t`\n\nCloses #12\n"}, payload


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
        title="New title", body="Closes #12", amendment="## Republished (attempt 2): `t`",
    )
    assert pr.number == 47 and pr.created is False and pr.updated is False


def test_the_worker_always_offers_an_amendment_never_a_replacement(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A human may have edited the body by hand, and it holds the closing
    keywords: an adopting publish appends to it whether or not the agent
    wrote text (#807), and the agent's body is inside the section when it did."""
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-upd-none", files={},
        task_input={"issue": 5}, with_title=False,
    )
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-upd-agent",
        files={"pr-body.md": b"Closes #5\n"},
    )
    assert all("update_existing" not in p for p in forge.pulls), forge.pulls
    none, agent = (p["amendment"] for p in forge.pulls)
    assert none.startswith("## Republished (attempt `att-t-pr-upd-none`): `t-pr-upd-none`"), none
    assert "Closes #5" not in none
    assert "Closes #5" in agent.splitlines(), agent


# -- the generated title never carries the task id (owner rule, 2026-09-28) ---


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (42, "Work on issue #42 (part of #42)"),
        ("42", "Work on issue #42 (part of #42)"),
        ({"number": 42, "title": "The widget accepts a negative size"},
         "The widget accepts a negative size (part of #42)"),
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
def test_a_mention_in_an_issue_title_is_neutralised(
    worker_factory, monkeypatch, origin, local_urls, forge, mentioned
):
    """An issue's title is anyone's text; a title the platform writes from it
    must page no one. The `@` stays and a zero-width joiner follows it, the
    same transform as the agent's own text."""
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-issue-at", files={},
        task_input={"issue": {"number": 8, "title": mentioned}}, with_title=False,
    )
    pull = _only_pull(forge)
    assert pull["title"] == f"{mentioned.replace('@', '@' + chr(0x200D))} (part of #8)", pull["title"]


#: A closing keyword as GitHub reads one in a squash-merge commit subject.
_CLOSING_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*#\d", re.IGNORECASE)


@pytest.mark.parametrize(
    "issue", [42, "42", {"number": 42}, {"number": 42, "title": "Widgets cannot be sorted"}],
    ids=["number", "numeric-string", "mapping-without-title", "mapping-with-title"],
)
def test_a_platform_title_never_carries_a_closing_keyword(
    worker_factory, monkeypatch, origin, local_urls, forge, issue
):
    """The worker does not decide that a pull request closes its issue. A
    "Fixes #N" title closed the issue on a squash merge whatever the review
    found, and for an issue run one failed GitHub write left it there (review
    of #545). The title says `part of #N`; the body's block says the rest."""
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-no-close", files={},
        task_input={"issue": issue}, with_title=False,
    )
    title = _only_pull(forge)["title"]
    assert _CLOSING_RE.search(title) is None, title
    assert title.endswith("(part of #42)"), title


def test_without_the_issue_title_the_number_names_the_work_whatever_the_label(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    # `metadata.label` is not covered by the step-spec signature, so the
    # worker does not read it for the title.
    _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-label", files={},
        task_input={"issue": 42}, label="Sort the widget list", with_title=False,
    )
    assert _only_pull(forge)["title"] == "Work on issue #42 (part of #42)"


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
    assert pull["title"] == "Work on issue #77 (part of #77)", pull["title"]
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


# -- every @mention in the agent's text is neutralised (owner decision, 2026-09-29) --
#
# Three reviews found a Markdown code/prose parser that decided which `@name`
# GitHub would notify for, and each found a case it got wrong: an escaped
# backtick, an unterminated fence, an HTML block. It is gone. Every `@` that
# could start a mention gets a U+200D ZERO WIDTH JOINER right after it, in
# code or not, so nothing the agent writes can page anyone and no body or
# title is refused for a mention.

ZWJ = "\u200d"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thanks @octocat for the report.", f"Thanks @{ZWJ}octocat for the report."),
        ("cc @acme/reviewers", f"cc @{ZWJ}acme/reviewers"),
        ("@octocat please review", f"@{ZWJ}octocat please review"),
        ("(@octocat)", f"(@{ZWJ}octocat)"),
        ("line one\n@octocat\n", f"line one\n@{ZWJ}octocat\n"),
        ("Use `@pytest.fixture` here.", f"Use `@{ZWJ}pytest.fixture` here."),
        ("Use ``@octocat`` please.", f"Use ``@{ZWJ}octocat`` please."),
        (
            "```python\n@pytest.fixture\ndef widget():\n    return 1\n```\n",
            f"```python\n@{ZWJ}pytest.fixture\ndef widget():\n    return 1\n```\n",
        ),
        ("~~~\ncc @acme/reviewers\n~~~\n", f"~~~\ncc @{ZWJ}acme/reviewers\n~~~\n"),
        (
            "<details>\n<summary>Log</summary>\n\n@octocat\n</details>\n",
            f"<details>\n<summary>Log</summary>\n\n@{ZWJ}octocat\n</details>\n",
        ),
        ("<div>`@octocat`</div>", f"<div>`@{ZWJ}octocat`</div>"),
        ("Thanks \\`@octocat\\` for this.", f"Thanks \\`@{ZWJ}octocat\\` for this."),
        ("Thanks \\@octocat.", f"Thanks \\@{ZWJ}octocat."),
        ("Ping café@octocat about this.", f"Ping café@{ZWJ}octocat about this."),
        ("@@octocat", f"@@{ZWJ}octocat"),
        ("@-octocat and @_octocat", f"@{ZWJ}-octocat and @{ZWJ}_octocat"),
    ],
    ids=[
        "prose", "team", "start-of-text", "after-punctuation", "own-line", "code-span",
        "double-backtick-span", "backtick-fence", "tilde-fence", "html-block", "html-inline",
        "escaped-backticks", "escaped-at", "non-ascii-letter-before", "double-at",
        "hyphen-and-underscore",
    ],
)
def test_every_mention_gets_a_zero_width_joiner_after_its_at(text, expected):
    assert lifecycle._neutralise_mentions(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thanks &#64;octocat.", f"Thanks &#64;{ZWJ}octocat."),
        ("Thanks &#064;octocat.", f"Thanks &#064;{ZWJ}octocat."),
        ("Thanks &#x40;octocat.", f"Thanks &#x40;{ZWJ}octocat."),
        ("Thanks &#X40;octocat.", f"Thanks &#X40;{ZWJ}octocat."),
        ("Thanks &commat;octocat.", f"Thanks &commat;{ZWJ}octocat."),
        ("<div>&#64octocat</div>", f"<div>&#64{ZWJ}octocat</div>"),
        ("Thanks @&#111;ctocat.", f"Thanks @{ZWJ}&#111;ctocat."),
    ],
    ids=["decimal", "zero-padded", "hex", "hex-upper", "named", "no-semicolon", "entity-name"],
)
def test_an_at_written_as_a_character_reference_is_neutralised_too(text, expected):
    """GitHub decodes character references before it looks for mentions, so
    `&#64;octocat` renders as `@octocat` and pages. The entity is kept as the
    agent wrote it and the joiner goes right after it; a name that starts
    with a reference of its own is neutralised as well."""
    assert lifecycle._neutralise_mentions(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Reported by ops@example.com.",
        "mail first.last_name@example.co.uk now",
        "&#640; is not an at sign, nor is &#x40a;",
        "an @ on its own, and @ followed by a space",
        "no at sign at all",
        "",
    ],
    ids=["email", "email-dotted", "longer-entities", "bare-at", "none", "empty"],
)
def test_text_with_nothing_that_could_mention_is_unchanged(text):
    """An address is left intact: GitHub reads a mention only where the `@`
    follows the start of the text or a character outside `[A-Za-z0-9_]`, so
    `ops@example.com` pages no one (see `_MENTION_AT_RE`)."""
    assert lifecycle._neutralise_mentions(text) == text


CORPUS = [
    "Closes #4\n\nThanks @octocat and @acme/reviewers.\n\n```\n@pytest.fixture\n```\n",
    "cc &#64;octocat, ops@example.com, café@octocat, @@x, \\@y",
    "<details>\n`@a` @b\n</details>\n",
]


@pytest.mark.parametrize("text", CORPUS)
def test_neutralising_is_idempotent(text):
    once = lifecycle._neutralise_mentions(text)
    assert lifecycle._neutralise_mentions(once) == once


@pytest.mark.parametrize("text", CORPUS)
def test_nothing_but_the_joiners_changes(text):
    """Taking the joiners back out gives the agent's text byte for byte, and
    every joiner sits right after an `@` or an `@` written as a reference."""
    out = lifecycle._neutralise_mentions(text)
    assert out.replace(ZWJ, "") == text
    assert out.count(ZWJ) > 0
    for index, char in enumerate(out):
        if char == ZWJ:
            before = out[:index]
            assert before.endswith(("@", "&#64;", "&#x40;")), repr(before[-8:])


@pytest.mark.parametrize(
    ("files", "which", "needle"),
    [
        ({"pr-body.md": b"Closes #4\n\n@octocat please review.\n"}, "body", "@octocat"),
        ({"pr-body.md": b"Closes #4\n\ncc @acme/reviewers\n"}, "body", "@acme/reviewers"),
        ({"pr-title.txt": b"Refuse negatives (@octocat)\n"}, "title", "@octocat"),
    ],
    ids=["user-in-body", "team-in-body", "user-in-title"],
)
def test_an_agent_text_with_a_mention_is_used_neutralised(
    worker_factory, monkeypatch, origin, local_urls, forge, files, which, needle
):
    """No body or title is refused for a mention any more: the agent's text is
    used, with the joiner after each `@`."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-mention", files=files,
        task_input={"issue": 4},
    )
    pull = _only_pull(forge)
    assert needle not in pull["title"] and needle not in pull["body"], pull
    assert needle.replace("@", f"@{ZWJ}") in pull[which], pull[which]
    assert out["pull_request_text"][which] == "agent", out
    assert not out.get("pull_request_text_refused"), out


def test_an_email_address_in_the_body_is_kept(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: `@` inside an address pages nobody, and is kept as written."""
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-email",
        files={"pr-body.md": b"Closes #4\n\nReported by ops@example.com.\n"},
    )
    pull = _only_pull(forge)
    assert pull["body"].startswith("Closes #4\n\nReported by ops@example.com."), pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out


def test_a_decorator_in_a_fence_is_kept_neutralised(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Code is not exempt: the fence reaches the pull request with the joiner
    after its `@`, and nothing else in it changes."""
    body = b"Closes #4\n\n```python\n@pytest.fixture\ndef widget():\n    return 1\n```\n\nDone.\n"
    _, _, out = _publish(
        worker_factory, monkeypatch, origin, task_id="t-pr-code-at", files={"pr-body.md": body},
    )
    pull = _only_pull(forge)
    expected = body.decode().strip().replace("@pytest", f"@{ZWJ}pytest")
    assert pull["body"].startswith(expected), pull["body"]
    assert out["pull_request_text"]["body"] == "agent", out


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

    # The harvest leaves its publish for after the missing-output check
    # (#165), as a real one does; the publish records what it was asked.
    def harvest(**_kwargs):
        worker._deferred_publish = {"repo": None, "work_head": None, "publish_repo": None}
        return {"base": "a" * 40}

    def publish_git(*, publish: bool, withheld: str = "", **_kwargs):
        published.append(publish)
        return {"published": publish, "publish_reason": withheld}

    monkeypatch.setattr(worker, "_harvest_git", harvest)
    monkeypatch.setattr(worker, "_publish_git", publish_git)
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
        ("Fix it \U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n",
         "carries attribution"),
    ],
    ids=["attribution"],
)
def test_every_refusal_fails_the_attempt_with_its_reason(
    db, worker_factory, monkeypatch, title, why
):
    worker, published = _run_writing_title(db, worker_factory, monkeypatch, title)

    assert worker.run() == ExitCode.FAILED
    assert published == [False]
    assert f"pr-title.txt refused: {why}" in db.doc("tasks/task_1")["last_error"]


def test_a_title_with_a_mention_is_not_refused(db, worker_factory, monkeypatch):
    """A mention no longer refuses a title (owner decision, 2026-09-29): it
    is neutralised, and the attempt publishes and succeeds."""
    worker, published = _run_writing_title(
        db, worker_factory, monkeypatch, "Refuse negatives, thanks @octocat\n"
    )

    assert worker.run() == ExitCode.OK
    assert published == [True]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_a_usable_title_publishes_and_succeeds(db, worker_factory, monkeypatch):
    """The control: the same attempt with a fact-style title publishes."""
    worker, published = _run_writing_title(
        db, worker_factory, monkeypatch, "The widget refuses a negative size\n"
    )

    assert worker.run() == ExitCode.OK
    assert published == [True]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
