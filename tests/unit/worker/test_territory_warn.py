"""A pull request whose diff leaves the step's declared territory opens with a warning.

Lane TERRITORY-WARN, part of #1468. Owner decision, 2026-10-19: publish with a
loud warning, not a refusal. When a step declares `inputs.territory` and its
diff touches a path outside it, the pull request still opens; its body STARTS
with "⚠ Outside this lane's territory:" and the list of those paths, and it
carries the label `out-of-territory`. The territory is the task's stored
input, never anything the agent wrote. A step that declares none is unchanged.

WHAT IS PINNED
  * the real publish path (`_harvest_git` -> `open_pull_request`): a diff
    inside the territory, one path outside it, and no territory declared;
  * the worker's stored-input recheck takes `territory` beside `prompt`, so a
    task that declares one is not refused CANNOT_START;
  * what an entry matches;
  * PARITY: the worker's rule and swarm-api's 422 (`swarm_api.laneterritory`)
    give the same answer for every entry of one corpus.

The constants are spelled out rather than imported, so the red run fails on
what the pull request says, not on an import.
"""

from __future__ import annotations

import shutil

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
)

HEADER = "⚠ Outside this lane's territory:"
LABEL = "out-of-territory"

_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _publish(worker_factory, monkeypatch, origin, forge, *, files, task_input):
    """One attempt that writes `files` and publishes; the pull request asked for."""
    task_id = "task_territory"
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    if task_input is not None:
        task["input"] = task_input
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"the agent's {name}\n")
    (worker.ws.artifacts / "pr-title.txt").write_bytes(b"The widget refuses a negative size\n")
    (worker.ws.artifacts / "pr-body.md").write_bytes(b"Closes #12\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")
    assert len(forge.pulls) == 1, forge.pulls
    return out, forge.pulls[0]


@_needs_git
def test_a_diff_inside_the_territory_leaves_the_body_and_labels_unchanged(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    out, pull = _publish(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "apps/widget/size.py"],
        task_input={"prompt": "fix it", "territory": ["a.py", "apps/widget/"]},
    )
    assert pull["body"].startswith("Closes #12"), pull["body"]
    assert HEADER not in pull["body"]
    assert not pull.get("labels"), pull.get("labels")
    assert "out_of_territory" not in out


@_needs_git
def test_a_diff_with_one_path_outside_names_exactly_that_path_and_sets_the_label(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    out, pull = _publish(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "b.py"],
        task_input={"prompt": "fix it", "territory": ["a.py"]},
    )
    body = pull["body"]
    assert body.startswith(HEADER + "\n"), body
    header = body.split("\n---\n", 1)[0]
    listed = [line for line in header.splitlines() if line.startswith("- ")]
    assert listed == ["- `b.py`"], header
    assert "a.py" not in header
    # The agent's own text follows, whole.
    assert "Closes #12" in body
    assert list(pull.get("labels") or []) == [LABEL], pull.get("labels")
    assert out["out_of_territory"]["paths"] == ["b.py"], out.get("out_of_territory")


@_needs_git
def test_no_territory_declared_publishes_exactly_as_before(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    out, pull = _publish(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "b.py", "docs/anything.md"],
        task_input={"prompt": "fix it"},
    )
    assert pull["body"].startswith("Closes #12"), pull["body"]
    assert HEADER not in pull["body"]
    assert "labels" not in pull, "a step with no territory asked the forge for labels"
    assert "out_of_territory" not in out
    assert "territory_unchecked" not in out


@_needs_git
def test_an_agent_written_territory_file_is_never_read(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The territory is the task's input; nothing in the workspace widens or names one."""
    task_id = "task_territory"
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}},
            "input": {"prompt": "fix it", "territory": ["a.py"]}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "b.py").write_text("outside\n")
    (worker.ws.artifacts / "input.json").write_text('{"territory": ["b.py"]}')
    (worker.ws.artifacts / "pr-title.txt").write_bytes(b"The widget refuses a negative size\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")
    assert forge.pulls[0]["body"].startswith(HEADER)
    assert list(forge.pulls[0].get("labels") or []) == [LABEL]


def test_the_worker_recheck_takes_a_territory_beside_the_prompt():
    stored = {"prompt": "fix it", "territory": ["apps/agent-worker/", "docs/*.md"]}
    checked = lifecycle._recheck_runner_input("claude-code", stored)
    assert checked == stored


# ---------------------------------------------------------------------------
# What an entry holds
# ---------------------------------------------------------------------------

HOLDS = [
    ("a.py", "a.py", True),
    ("a.py", "b.py", False),
    ("a.py", "a.pyc", False),
    ("apps/x", "apps/x/y.py", True),
    ("apps/x/", "apps/x/deep/y.py", True),
    ("apps/x/", "apps/xy/z.py", False),
    ("apps/*.py", "apps/a.py", True),
    ("apps/*.py", "apps/sub/a.py", False),
    ("apps/**/test_*.py", "apps/test_a.py", True),
    ("apps/**/test_*.py", "apps/a/b/test_c.py", True),
    ("apps/**/test_*.py", "apps/a/b/c.py", False),
    ("**/README.md", "README.md", True),
    ("**/README.md", "docs/x/README.md", True),
    ("docs/**", "docs/a/b.md", True),
    ("docs/**", "docsx/a.md", False),
    ("file?.txt", "file1.txt", True),
    ("file?.txt", "file/.txt", False),
    ("v[0-9].md", "v3.md", True),
    ("v[!0-9].md", "v3.md", False),
    ("v[!0-9].md", "vx.md", True),
    ("a+b(c).py", "a+b(c).py", True),
]


@pytest.mark.parametrize(("entry", "path", "held"), HOLDS)
def test_what_an_entry_holds(entry, path, held):
    from agent_worker import territory

    rules = territory.parse([entry])
    assert (territory.outside([path], rules) == []) is held


#: Entries both rules must accept or refuse alike.
CORPUS = [
    "a.py", "apps/x/", "apps/**", "**/x.py", "a/**/b", "v[0-9].md", "v[!a].md", "[]]x",
    "a+b(c).py", "*", "**",
    # refused
    "", " a.py", "a.py ", "/abs/a.py", "../up.py", "a/../b", "./a.py", "a//b", "a\\b",
    "a/**b/c", "x**", "[abc", "a]b", "v[z-a].md", "{a,b}.py", "a\tb", "x" * 401,
]


@pytest.mark.parametrize("entry", CORPUS, ids=[repr(e)[:40] for e in CORPUS])
def test_the_worker_and_the_api_agree_on_every_entry(entry):
    from agent_worker import territory as worker_rule
    from swarm_api import laneterritory as api_rule

    def answer(rule):
        try:
            rule.parse([entry])
        except rule.TerritoryError:
            return "refused"
        return "accepted"

    assert answer(worker_rule) == answer(api_rule), entry


@pytest.mark.parametrize("raw", [None, "a.py", [], [3], {"a.py": True}, ["a.py"] * 201])
def test_a_declaration_that_is_not_a_list_of_entries_is_refused_by_both(raw):
    from agent_worker import territory as worker_rule
    from swarm_api import laneterritory as api_rule

    for rule in (worker_rule, api_rule):
        with pytest.raises(rule.TerritoryError):
            rule.parse(raw)


# ---------------------------------------------------------------------------
# The forge: the label is added, and a refused label never fails the publish
# ---------------------------------------------------------------------------


def _forge_answers(monkeypatch, labels_status):
    from agent_worker import forge

    asked: list[tuple[str, str, dict | None]] = []

    def request(url, *, token, method="GET", payload=None):
        asked.append((method, url, payload))
        if url.endswith("/pulls") and method == "POST":
            return 201, {"number": 7, "html_url": "https://github.com/acme/widgets/pull/7"}
        if url.endswith("/issues/7/labels"):
            if labels_status == 200:
                return 200, [{"name": name} for name in payload["labels"]]
            return labels_status, {"message": "Resource not accessible by integration"}
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(forge, "_request", request)
    access = forge.RepoAccess(
        ref=forge.RepoRef(host="github.com", owner="acme", name="widgets"),
        default_branch="main", can_push=True, reason="write",
    )
    return forge, access, asked


def test_open_pull_request_adds_the_labels_it_is_given(monkeypatch):
    forge, access, asked = _forge_answers(monkeypatch, 200)
    pr = forge.open_pull_request(
        access=access, token="t", head="swarm/x", base="main", title="T", body="B",
        labels=[LABEL],
    )
    assert pr.number == 7 and pr.created
    assert pr.labelled == (LABEL,)
    assert pr.label_error == ""
    assert asked[-1][0] == "POST" and asked[-1][2] == {"labels": [LABEL]}


def test_a_refused_label_leaves_the_pull_request_open_and_says_why(monkeypatch):
    forge, access, _ = _forge_answers(monkeypatch, 403)
    pr = forge.open_pull_request(
        access=access, token="t", head="swarm/x", base="main", title="T", body="B",
        labels=[LABEL],
    )
    assert pr.number == 7 and pr.created
    assert pr.labelled == ()
    assert "403" in pr.label_error


def test_no_labels_asks_the_forge_for_none(monkeypatch):
    forge, access, asked = _forge_answers(monkeypatch, 200)
    forge.open_pull_request(access=access, token="t", head="swarm/x", base="main",
                            title="T", body="B")
    assert [url for _, url, _ in asked if "/labels" in url] == []


# ---------------------------------------------------------------------------
# A CI-fix round's push onto the lane's open pull request (CIFIX-TERRITORY,
# part of #1499): `cifix.stamp` gives the round its lane's territory, and the
# forge's adopt path carries the warning the worker hands it.
# ---------------------------------------------------------------------------


def test_the_api_names_the_label_the_worker_sets():
    """The round's prompt (`laneterritory.round_brief`) names the label; the two
    images cannot share the constant, so it is held equal here."""
    from agent_worker import territory as worker_rule
    from swarm_api import laneterritory as api_rule

    # MUTATION: rename the label on one side only -- the fixer is told of a
    # label its pull request never gets.
    assert api_rule.OUT_OF_TERRITORY_LABEL == worker_rule.OUT_OF_TERRITORY_LABEL == LABEL


def test_a_push_onto_an_open_pull_request_labels_it_and_appends_the_paths(monkeypatch):
    """A round never opens the pull request: its 422 adopts the lane's. The
    label still goes on, and the paths arrive in the appended section, the
    lane's own body kept verbatim above it."""
    from agent_worker import forge, territory as worker_rule

    asked: list[tuple[str, str, dict | None]] = []
    lane_body = "The lane's own text.\n\nPart of #1499\n"

    def request(url, *, token, method="GET", payload=None):
        asked.append((method, url, payload))
        if url.endswith("/pulls") and method == "POST":
            return 422, {"message": "Validation Failed",
                         "errors": [{"message": "A pull request already exists"}]}
        if "/pulls?head=" in url:
            return 200, [{"number": 7, "html_url": "https://github.com/acme/widgets/pull/7",
                          "state": "open", "title": "The lane's title", "body": lane_body}]
        if url.endswith("/pulls/7") and method == "PATCH":
            return 200, {}
        if url.endswith("/issues/7/labels"):
            return 200, [{"name": name} for name in payload["labels"]]
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(forge, "_request", request)
    access = forge.RepoAccess(
        ref=forge.RepoRef(host="github.com", owner="acme", name="widgets"),
        default_branch="main", can_push=True, reason="write",
    )
    header = worker_rule.warning_header(["apps/swarm-mcp/swarm_mcp/sc.py"])
    section = "## Continuation (attempt 1): `task_round`\n\n- task: `task_round`\n\n" + header

    pr = forge.open_pull_request(
        access=access, token="t", head="swarm/task_lane", base="main", title="T",
        body="never sent to an adopted pull request", amendment=section, labels=[LABEL],
    )

    assert pr.number == 7 and not pr.created
    # MUTATION: add labels only to a pull request this call created -- a
    # round's out-of-territory push onto the lane's pull request is unlabelled.
    assert pr.labelled == (LABEL,), pr.label_error
    (patch,) = [payload for method, _, payload in asked if method == "PATCH"]
    # MUTATION: replace the adopted body -- the lane's `Part of #1499` is lost.
    assert patch["body"].startswith(lane_body.rstrip())
    # MUTATION: drop the header from the amendment -- the label says "outside"
    # and nothing on the pull request says which path.
    assert header in patch["body"]
    assert "- `apps/swarm-mcp/swarm_mcp/sc.py`" in patch["body"]


# ---------------------------------------------------------------------------
# A push onto an open pull request that leaves the territory is said in ONE
# comment (CIFIX-TERRITORY-2, part of #1499). CLD, 2026-10-23: a section at
# the bottom of a long body is easy to miss. Still a warning, never a refusal
# (#1468): the label goes on as before and the publish succeeds.
# ---------------------------------------------------------------------------

CONTINUATION_SAYS = "This push onto an open pull request was published anyway"
OPENED_SAYS = "The pull request opened anyway"
MARKER = "<!-- swarm:out-of-territory"


class _OpenPullRequest:
    """The lane's pull request, already open: every publish adopts it. Its
    comments live here, read and posted through `forge`'s two calls."""

    def __init__(self, *, post_fails: bool = False) -> None:
        self.pulls: list[dict] = []
        self.comments: list[str] = []
        self.posts = 0
        self.post_fails = post_fails

    def open_pull_request(self, **kwargs):
        self.pulls.append(kwargs)
        labels = tuple(kwargs.get("labels") or ())
        return lifecycle.PullRequest(
            number=7, url="https://github.com/acme/widgets/pull/7", state="open",
            created=False, updated=True, appended=True, labelled=labels,
        )

    def list_comments(self, *, ref, token, number):
        assert number == 7
        return list(self.comments)

    def post_comment(self, *, ref, token, number, body):
        assert number == 7
        self.posts += 1
        if self.post_fails:
            raise lifecycle.ForgeError("could not comment on issue #7 (403): Forbidden")
        self.comments.append(body)
        return 1000 + len(self.comments)


def _continue(worker_factory, monkeypatch, origin, forge, *, files, territory,
              post_fails=False, publishes=1):
    """A round that writes `files` and publishes onto the lane's open pull
    request `publishes` times, as one attempt; what it asked of the forge."""
    from agent_worker import forge as forge_mod

    lane = _OpenPullRequest(post_fails=post_fails)
    monkeypatch.setattr(lifecycle, "open_pull_request", lane.open_pull_request)
    monkeypatch.setattr(forge_mod, "list_issue_comments", lane.list_comments)
    monkeypatch.setattr(forge_mod, "create_issue_comment", lane.post_comment)
    task_id = "task_round"
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    warned: list[tuple[str, dict]] = []
    real_warning = worker.log.warning

    def warning(message, **fields):
        warned.append((message, fields))
        return real_warning(message, **fields)

    monkeypatch.setattr(worker.log, "warning", warning)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}},
            "input": {"prompt": "fix CI", "territory": territory}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"the round's {name}\n")
    (worker.ws.artifacts / "pr-title.txt").write_bytes(b"CI is green again\n")
    outs = []
    for _ in range(publishes):
        out = worker._harvest_git(publish=True)
        # MUTATION: let a comment the forge refused fail the publish -- a
        # warning becomes a refusal, against #1468.
        assert out["published"] is True, out.get("publish_reason")
        outs.append(out)
    return outs, lane, warned


@_needs_git
def test_a_continuation_outside_the_territory_posts_one_comment_and_labels_it(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    from agent_worker import territory as worker_rule

    (out,), lane, _ = _continue(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "b.py"], territory=["a.py"],
    )
    # MUTATION: post nothing -- the warning is only the section at the bottom
    # of a long body, which is what CLD found easy to miss.
    assert len(lane.comments) == 1, lane.comments
    (comment,) = lane.comments
    # MUTATION: post the opened-PR wording -- a round's push did not open it.
    assert comment.startswith(worker_rule.warning_header(["b.py"], continuation=True)), comment
    assert comment.startswith(HEADER + "\n"), comment
    assert CONTINUATION_SAYS in comment and OPENED_SAYS not in comment, comment
    # MUTATION: drop the path list -- the comment says "outside" and not where.
    assert [ln for ln in comment.splitlines() if ln.startswith("- ")] == ["- `b.py`"], comment
    # MUTATION: drop the marker -- a retry of the attempt cannot find its comment.
    assert MARKER in comment and "att-task_round" in comment, comment
    (pull,) = lane.pulls
    # MUTATION: stop asking for the label once the comment is posted.
    assert list(pull.get("labels") or []) == [LABEL], pull.get("labels")
    amendment = pull["amendment"]
    # MUTATION: keep the header in the appended section -- the warning is said twice.
    assert HEADER not in amendment and "- `b.py`" not in amendment, amendment
    # MUTATION: drop the pointer line -- the body no longer says where the warning went.
    assert "out-of-territory warning was posted as a comment" in amendment, amendment
    assert out["out_of_territory"]["comment"] == 1001, out["out_of_territory"]


@_needs_git
def test_the_same_attempt_publishing_twice_comments_once(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    outs, lane, _ = _continue(
        worker_factory, monkeypatch, origin, forge,
        files=["b.py"], territory=["a.py"], publishes=2,
    )
    # MUTATION: skip the marker lookup -- every retry of the attempt posts again.
    assert len(lane.comments) == 1, lane.comments
    assert lane.posts == 1
    assert outs[1]["out_of_territory"]["comment"] == "already posted", outs[1]["out_of_territory"]


@_needs_git
def test_a_continuation_inside_the_territory_posts_no_comment_and_no_label(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    (out,), lane, _ = _continue(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "apps/widget/size.py"], territory=["a.py", "apps/widget/"],
    )
    # MUTATION: comment on every continuation -- a round inside its territory is warned.
    assert lane.comments == [] and lane.posts == 0, lane.comments
    (pull,) = lane.pulls
    # MUTATION: label every continuation.
    assert not pull.get("labels"), pull.get("labels")
    assert "out-of-territory" not in pull["amendment"]
    assert "out_of_territory" not in out


@_needs_git
def test_a_newly_opened_pull_request_keeps_the_opened_wording(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    from agent_worker import territory as worker_rule

    _, pull = _publish(
        worker_factory, monkeypatch, origin, forge,
        files=["a.py", "b.py"],
        task_input={"prompt": "fix it", "territory": ["a.py"]},
    )
    # MUTATION: give a new pull request the continuation wording.
    assert pull["body"].startswith(worker_rule.warning_header(["b.py"])), pull["body"]
    assert OPENED_SAYS in pull["body"].split("\n---\n", 1)[0]
    assert CONTINUATION_SAYS not in pull["body"]


def test_the_opened_wording_is_unchanged():
    """Pinned verbatim: a newly opened pull request says what it said before."""
    from agent_worker import territory as worker_rule

    # MUTATION: reword the default form while adding the continuation one.
    assert worker_rule.warning_header(["b.py"]) == (
        HEADER + "\n\n- `b.py`\n\nThis step declared its territory in "
        "`inputs.territory`, and the path above matches none of its entries. "
        "The pull request opened anyway (owner decision, 2026-10-19); merging "
        "still needs the review verdict and green CI."
    )


@_needs_git
def test_a_comment_the_forge_refuses_leaves_the_publish_successful_and_is_logged(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    (out,), lane, warned = _continue(
        worker_factory, monkeypatch, origin, forge,
        files=["b.py"], territory=["a.py"], post_fails=True,
    )
    assert lane.posts == 1
    # MUTATION: swallow the failure silently -- nobody learns the warning is missing.
    assert any(m == "the out-of-territory comment was not posted" for m, _ in warned), warned
    assert "403" in out["out_of_territory"]["comment_error"], out["out_of_territory"]
    # MUTATION: drop the label when the comment fails.
    assert list(lane.pulls[0].get("labels") or []) == [LABEL]
