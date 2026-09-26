"""The bridge half of running a Claude Code workflow step in SwarmCloud.

Owner decision, 2026-09-26: build both modes -- a single step dispatched by the
plugin's `sc:remote` agent, and a whole SwarmCloud workflow shown by `/sc:run`
-- so a workflow shows as running in Claude Code while every step executes in
SwarmCloud. Both halves are agents whose only tools are the bridge's, so what
those agents need and the MCP tools lacked is built here and held here:

* `swarm_dispatch` takes a delivery `strategy` (collect | direct-pr) and, when a
  caller names no repository, clones the repository and PUSHED branch of the
  checkout the bridge runs in -- refusing, before anything travels, a branch a
  remote agent could not see (`swarm_mcp.checkout`);
* `swarm_follow` takes an opaque `since` token, can answer as short narrated
  lines, can gather for a window, and hands a finished task's outcome back --
  its answer, the JSON object that answer ends with, its spend and the rest
  (`swarm_mcp.progress`);
* `swarm_workflow` takes a whole `swarm workflow` spec, read by the same
  function the terminal uses;
* the stdio loop answers tool calls concurrently, so a dozen step agents each
  holding a follow open do not queue behind one another.

The review of PR #230 added what those rows need to be SAFE, not just able:
the branch is judged by its own name on its remote, never its upstream (a lane
made from `origin/main` was refused when pushed and told to push to main); a
row stops on a task it can never read or that is another step, instead of
polling it to its turn limit; a spec retyped by a relay is checked against the
digest its script computed, before anything is sent; and an argument a tool
does not declare is refused rather than dropped.

Invariant 10 is asserted alongside: none of it adds a model, image, command or
resource parameter.

`swarm_mcp.checkout` and `swarm_mcp.progress` are imported PER TEST, through
the fixtures below, so the red-first commit failed each of these tests by name
rather than failing the whole collection on one ImportError.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import shutil
import subprocess
import threading
from datetime import timedelta

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmClient, SwarmError

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture()
def checkout():
    return importlib.import_module("swarm_mcp.checkout")


@pytest.fixture()
def progress():
    return importlib.import_module("swarm_mcp.progress")


class _Recorder:
    """The transport, stubbed at `request`, behind the REAL `SwarmClient.dispatch`.

    So the payload asserted below is the one the shipped client builds, not
    one a fake agreed with.
    """

    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        if path == "/v1/workflows":
            steps = [
                {
                    "step_id": s["step_id"],
                    "task_id": f"task_{s['step_id']}",
                    "runner_profile": s["runner_profile"],
                    "depends_on": s.get("depends_on") or [],
                }
                for s in (payload or {}).get("steps", [])
            ]
            return {"workflow": {"workflow_id": "wf_1", "steps": steps}, "dispatch": {}}
        strategy = (payload or {}).get("strategy") or "collect"
        return {
            "task": {
                "id": "task_1",
                "state": "QUEUED",
                "metadata": {"dispatch": {"strategy": strategy, "carrier": "checkpoints"}},
            }
        }


def _dispatch(recorder: _Recorder, **arguments) -> dict:
    return json.loads(server._call(recorder, "swarm_dispatch", {"prompt": "do it", **arguments}))


def _sent_payload(recorder: _Recorder) -> dict:
    (_, _, payload), = recorder.sent
    return payload


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def pushed(tmp_path, monkeypatch):
    """A checkout on `lane/x`, pushed: its remote-tracking ref is its HEAD.

    The remote is an scp-style GitHub URL, the commonest shape on a laptop and
    the one the worker cannot clone as written. Nothing here touches a network:
    "pushed" is the remote-tracking ref, which a real push updates.
    """
    repo = tmp_path / "widgets"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "lane/x")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    _git(repo, "update-ref", "refs/remotes/origin/lane/x", "HEAD")
    _git(repo, "branch", "--quiet", "--set-upstream-to=origin/lane/x")
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(repo))
    return repo


# --------------------------------------------------------------------------
# swarm_dispatch: the repository, inferred -- or refused
# --------------------------------------------------------------------------


@needs_git
def test_an_unnamed_repository_is_the_checkouts_pushed_branch_over_https(pushed):
    recorder = _Recorder()
    reply = _dispatch(recorder)

    payload = _sent_payload(recorder)
    # https, because the worker authenticates a clone with an HTTPS token and
    # runs ssh with no key: `git@github.com:...` as written clones nothing.
    assert payload["repository_url"] == "https://github.com/acme/widgets.git"
    assert payload["repository_ref"] == "lane/x"
    assert reply["repository"]["source"] == "checkout"
    assert reply["repository"]["commit"] == _head(pushed)


@needs_git
def test_a_branch_with_unpushed_commits_is_refused_before_anything_travels(pushed):
    """The remote agent would clone origin's older tip, succeed, and return a
    patch against the wrong base. Refused here, it costs nothing."""
    (pushed / "b.txt").write_text("b\n")
    _git(pushed, "add", "-A")
    _git(pushed, "commit", "--quiet", "-m", "local only")
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder)

    message = str(caught.value)
    assert "1 commit(s)" in message and "not on origin/lane/x" in message, message
    # The fix is always the branch pushed under its own name. A
    # `branch:remote_branch` form is how the refusal came to recommend
    # `git push origin <lane>:main`.
    assert "git push -u origin lane/x" in message, message
    assert "lane/x:" not in message, message
    assert recorder.sent == [], "a refused dispatch reached the API"


@needs_git
def test_a_branch_that_was_never_pushed_is_refused_with_the_push_command(pushed):
    _git(pushed, "checkout", "--quiet", "-b", "lane/y")
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder)

    assert "git push -u origin lane/y" in str(caught.value), str(caught.value)
    assert recorder.sent == []


@needs_git
def test_a_detached_head_is_refused(pushed):
    _git(pushed, "checkout", "--quiet", "--detach")
    with pytest.raises(SwarmError, match="detached HEAD"):
        _dispatch(_Recorder())


@needs_git
def test_uncommitted_changes_are_named_as_invisible_rather_than_refused(pushed):
    (pushed / "a.txt").write_text("edited here\n")
    recorder = _Recorder()

    reply = _dispatch(recorder)

    assert _sent_payload(recorder)["repository_ref"] == "lane/x"
    assert any("NOT visible" in note and "1 uncommitted" in note for note in reply["repository"]["notes"]), (
        reply["repository"]
    )


@needs_git
def test_a_branch_whose_upstream_has_another_name_is_judged_by_its_own_name(pushed):
    """An upstream is where a branch was STARTED from as often as where it is
    pushed -- `git checkout -b mine origin/lane/x` makes lane/x the upstream.
    Sending the upstream's name would clone somebody else's branch; the
    branch's own name is what `git push -u` publishes."""
    _git(pushed, "checkout", "--quiet", "-b", "mine")
    _git(pushed, "branch", "--quiet", "--set-upstream-to=origin/lane/x")
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder)

    message = str(caught.value)
    assert "git push -u origin mine" in message, message
    assert "mine:" not in message, message
    assert "origin/lane/x" in message, "the refusal should say what the upstream is and that it is not sent"
    assert recorder.sent == []

    _git(pushed, "update-ref", "refs/remotes/origin/mine", "HEAD")
    _dispatch(recorder)
    assert _sent_payload(recorder)["repository_ref"] == "mine"


@pytest.fixture()
def lane_from_main(tmp_path, monkeypatch):
    """This repository's own lane recipe: `git checkout -b lane/x origin/main`.

    `--track` is spelled out because it is what `branch.autoSetupMerge=true`
    (git's default) does for a remote-tracking start point, and a CI runner's
    global config must not decide what this fixture is. So the lane's
    UPSTREAM is origin/main, and it has one commit main does not.
    """
    repo = tmp_path / "lane"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "--quiet", "-b", "lane/x", "--track", "origin/main")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the lane's work")
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(repo))
    return repo


@needs_git
def test_a_lane_from_origin_main_pushed_without_u_is_accepted(lane_from_main):
    """`git push origin lane/x` leaves the upstream at origin/main. The lane IS
    pushed -- origin/lane/x is its HEAD -- and it is main that is one commit
    behind. Measured against the upstream it was refused as unpushed."""
    _git(lane_from_main, "update-ref", "refs/remotes/origin/lane/x", "HEAD")
    recorder = _Recorder()

    reply = _dispatch(recorder)

    payload = _sent_payload(recorder)
    assert payload["repository_ref"] == "lane/x", "the lane is cloned by its own name, never as main"
    assert payload["repository_url"] == "https://github.com/acme/widgets.git"
    assert reply["repository"]["commit"] == _head(lane_from_main)
    assert any("upstream is origin/main" in note for note in reply["repository"]["notes"]), reply["repository"]


@needs_git
def test_a_lane_from_origin_main_never_pushed_is_refused_without_a_push_to_main(lane_from_main):
    """The usual state right after the lane recipe. The refusal is read by a
    developer or a Claude session and RUN; `git push origin lane/x:main` would
    land unreviewed commits on the default branch."""
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder)

    message = str(caught.value)
    assert "git push -u origin lane/x" in message, message
    assert ":main" not in message and "lane/x:" not in message, message
    assert recorder.sent == []


@needs_git
def test_a_pushed_lane_with_a_new_local_commit_is_refused_against_its_own_branch(lane_from_main):
    _git(lane_from_main, "update-ref", "refs/remotes/origin/lane/x", "HEAD")
    (lane_from_main / "c.txt").write_text("c\n")
    _git(lane_from_main, "add", "-A")
    _git(lane_from_main, "commit", "--quiet", "-m", "not pushed yet")
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder)

    message = str(caught.value)
    # One commit ahead of ITS OWN branch -- not two, which is main's distance.
    assert "1 commit(s)" in message and "not on origin/lane/x" in message, message
    assert "git push -u origin lane/x" in message, message
    assert recorder.sent == []


@needs_git
def test_a_workflow_from_a_lane_clones_the_lane_not_its_upstream(lane_from_main):
    _git(lane_from_main, "update-ref", "refs/remotes/origin/lane/x", "HEAD")
    recorder = _Recorder()

    server._call(recorder, "swarm_workflow", {"spec": {"steps": [{"step_id": "a", "prompt": "x"}]}})

    assert _sent_payload(recorder)["repository_ref"] == "lane/x"


@needs_git
def test_outside_a_checkout_nothing_is_cloned_and_the_reply_says_why():
    """The conftest points the bridge at an empty directory. A null url with
    no reason would read as a bridge that forgot."""
    recorder = _Recorder()
    reply = _dispatch(recorder)

    payload = _sent_payload(recorder)
    assert "repository_url" not in payload and "repository_ref" not in payload
    assert reply["repository"]["url"] is None
    assert reply["repository"]["source"] == "none"
    assert any("not inside a git checkout" in note for note in reply["repository"]["notes"])


@needs_git
def test_no_repository_clones_nothing_even_inside_a_checkout(pushed):
    recorder = _Recorder()
    reply = _dispatch(recorder, no_repository=True)

    assert "repository_url" not in _sent_payload(recorder)
    assert reply["repository"]["source"] == "none"


@needs_git
def test_the_string_false_is_not_read_as_true(pushed):
    """`bool("false")` is True. A model sends the string as often as the
    boolean, and read loosely it would silently clone nothing."""
    recorder = _Recorder()
    _dispatch(recorder, no_repository="false")
    assert _sent_payload(recorder)["repository_url"] == "https://github.com/acme/widgets.git"


def test_a_named_repository_is_sent_as_given_and_git_is_not_consulted(checkout, monkeypatch):
    def _no_git(*_a, **_k):
        raise AssertionError("git was consulted for a repository the caller named")

    monkeypatch.setattr(checkout, "_git", _no_git)
    recorder = _Recorder()

    _dispatch(recorder, repo="https://github.com/acme/other.git", ref="main")

    payload = _sent_payload(recorder)
    assert payload["repository_url"] == "https://github.com/acme/other.git"
    assert payload["repository_ref"] == "main"


@needs_git
def test_a_credential_in_the_remote_url_never_leaves_this_machine(pushed):
    """A remote URL can carry a token. The dispatch payload is stored on the
    task document and the reply echoes it; neither may hold the token."""
    _git(pushed, "remote", "set-url", "origin", "https://x-access-token:ghp_S3CRET@github.com/acme/widgets.git")
    recorder = _Recorder()

    reply = _dispatch(recorder)

    assert _sent_payload(recorder)["repository_url"] == "https://github.com/acme/widgets.git"
    assert "ghp_S3CRET" not in json.dumps(recorder.sent) + json.dumps(reply)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:acme/widgets.git", "https://github.com/acme/widgets.git"),
        ("ssh://git@github.com:22/acme/widgets.git", "https://github.com/acme/widgets.git"),
        ("https://user:pw@gitlab.example.com/g/p.git", "https://gitlab.example.com/g/p.git"),
        ("https://github.com/acme/widgets", "https://github.com/acme/widgets"),
    ],
)
def test_a_remote_url_is_made_clonable_by_the_worker(checkout, remote, expected):
    assert checkout.https_url(remote) == expected


@pytest.mark.parametrize("remote", ["/srv/git/widgets.git", "file:///srv/git/widgets.git", "http://x.example/r.git"])
def test_a_remote_no_container_can_clone_is_refused(checkout, remote):
    with pytest.raises(SwarmError):
        checkout.https_url(remote)


# --------------------------------------------------------------------------
# swarm_dispatch: the strategy
# --------------------------------------------------------------------------


def test_the_dispatch_sends_the_strategy_it_was_asked_for():
    recorder = _Recorder()
    reply = _dispatch(recorder, strategy="direct-pr", repo="https://github.com/acme/widgets.git")

    assert _sent_payload(recorder)["strategy"] == "direct-pr"
    assert reply["strategy"] == "direct-pr"


def test_no_strategy_sends_none_and_the_reply_names_what_the_api_recorded():
    """Sent only when given, so a caller that names none gets the payload it
    always got; the reply reads the strategy back from the task."""
    recorder = _Recorder()
    reply = _dispatch(recorder)

    assert "strategy" not in _sent_payload(recorder)
    assert reply["strategy"] == "collect"


def test_integrate_is_refused_for_one_task_before_the_round_trip():
    recorder = _Recorder()
    with pytest.raises(SwarmError) as caught:
        _dispatch(recorder, strategy="integrate")
    assert "swarm_workflow" in str(caught.value)
    assert recorder.sent == []


def test_an_unknown_strategy_is_refused_naming_the_real_ones():
    with pytest.raises(SwarmError) as caught:
        _dispatch(_Recorder(), strategy="yolo")
    assert "collect" in str(caught.value) and "direct-pr" in str(caught.value)


def test_direct_pr_with_nothing_to_push_to_is_refused_before_the_round_trip():
    """Outside a checkout there is no repository; a PR strategy with none would
    run to completion and quietly produce nothing."""
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="needs a repository"):
        _dispatch(recorder, strategy="direct-pr")
    assert recorder.sent == []


def test_the_dispatch_schema_gains_no_execution_parameter():
    """Invariant 10. `strategy` is how the work comes BACK; nothing here picks
    what runs. `model` is named because it is the tempting one: the runner
    reads `input.model`, and a caller setting it would choose the model a
    claude-code agent runs."""
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_dispatch")["inputSchema"]
    forbidden = {"image", "command", "args", "cpu", "memory", "resource_class", "backend", "model", "input"}
    assert forbidden.isdisjoint(schema["properties"]), sorted(forbidden & set(schema["properties"]))
    assert schema["properties"]["strategy"]["enum"] == ["collect", "direct-pr"]


# --------------------------------------------------------------------------
# swarm_workflow: a whole spec, the same reader as the terminal
# --------------------------------------------------------------------------


_SPEC = {
    "repository_url": "https://github.com/acme/widgets.git",
    "repository_ref": "main",
    "strategy": "collect",
    "on_step_failure": "continue",
    "label": "scan",
    "steps": [
        {"step_id": "scan-01", "prompt": "scan a", "stage": "Scan"},
        {"step_id": "join", "prompt": "join", "depends_on": ["scan-01"], "stage": "Join"},
    ],
}


def test_a_whole_spec_is_submitted_as_the_terminal_would_submit_it():
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow", {"spec": json.loads(json.dumps(_SPEC))}))

    payload = _sent_payload(recorder)
    assert payload["repository_url"] == "https://github.com/acme/widgets.git"
    assert payload["repository_ref"] == "main"
    assert payload["on_step_failure"] == "continue"
    # `stage` is display-only: WorkflowStepCreate forbids extras, so it is
    # never sent.
    assert all("stage" not in step for step in payload["steps"]), payload["steps"]
    assert reply["workflow_id"] == "wf_1"
    assert [(s["step_id"], s["task_id"], s["depends_on"]) for s in reply["steps"]] == [
        ("scan-01", "task_scan-01", []),
        ("join", "task_join", ["scan-01"]),
    ]


def test_a_spec_beside_the_parameters_it_carries_is_refused():
    with pytest.raises(SwarmError, match="not both"):
        server._call(_Recorder(), "swarm_workflow", {"spec": _SPEC, "label": "other"})


def test_a_spec_key_a_workflow_does_not_have_is_refused_not_dropped():
    """Invariant 10 at the top of a spec, as `build_steps` holds it per step."""
    with pytest.raises(SwarmError) as caught:
        server._call(_Recorder(), "swarm_workflow", {"spec": {**_SPEC, "image": "evil:latest"}})
    assert "image" in str(caught.value)


@needs_git
def test_a_workflow_that_names_no_repository_clones_the_checkouts_branch(pushed):
    recorder = _Recorder()
    spec = {"steps": [{"step_id": "a", "prompt": "x"}]}
    reply = json.loads(server._call(recorder, "swarm_workflow", {"spec": spec}))

    payload = _sent_payload(recorder)
    assert payload["repository_url"] == "https://github.com/acme/widgets.git"
    assert payload["repository_ref"] == "lane/x"
    assert reply["repository"]["source"] == "checkout"


# --------------------------------------------------------------------------
# swarm_follow: the `since` token
# --------------------------------------------------------------------------


def test_since_carries_the_cursor_the_states_what_was_said_and_whose_streams(progress):
    cursor = {"task_a": {"events": 3, "attempt_id": "att_1", "streams": {}}}
    token = progress.encode_since(
        cursor, {"task_a": "RUNNING"}, {"task_a": {"terminal"}}, {"task_a": "runner"}
    )

    assert isinstance(token, str) and "{" not in token
    back_cursor, back_states, back_said, back_groups, note = progress.decode_since(token)
    assert back_cursor == cursor
    assert back_states == {"task_a": "RUNNING"}
    assert back_said == {"task_a": {"terminal"}}
    assert back_groups == {"task_a": "runner"}
    assert note is None


def test_an_unreadable_since_starts_over_and_says_so(progress):
    """Re-reading costs a repeat; skipping ahead loses output silently. Only
    one of those is recoverable, so an unreadable token re-reads -- and says."""
    cursor, states, said, groups, note = progress.decode_since("not-a-token!!")
    assert (cursor, states, said, groups) == ({}, {}, {}, {})
    assert "from the beginning" in note


def test_the_json_follow_hands_back_a_since_as_well(swarm, world):
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "one\n")

    first = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_a"]}))
    world.live("task_a", "att_1", "one\ntwo\n")
    second = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "since": first["since"]}))

    text = [row["text"] for row in second["tasks"][0]["logs"]["streams"] if row["stream"] == "stdout"]
    assert text == ["two\n"], "the call with `since` repeated output the first call delivered"


def test_since_and_cursor_together_are_refused():
    with pytest.raises(SwarmError, match="not both"):
        server._call(_Recorder(), "swarm_follow", {"task_ids": ["t"], "since": "x", "cursor": {"t": {}}})


# --------------------------------------------------------------------------
# swarm_follow format=lines: narration
# --------------------------------------------------------------------------


def test_a_stream_json_line_is_narrated_as_what_it_means(progress):
    """claude-code's stdout is one JSON object per line. Shown raw, a row
    spends its context on braces; narrated, one line says what happened."""
    init = {"type": "system", "subtype": "init", "model": "claude-x", "tools": ["Bash", "Read"]}
    said = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "private"},
        {"type": "text", "text": "I will read the file."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status --short"}},
    ]}}
    answered = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "fatal: not a git repository"},
    ]}}
    done = {"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.2143}

    assert progress.narrate(json.dumps(init)) == ["session started · claude-x · 2 tools"]
    assert progress.narrate(json.dumps(said)) == [
        "thinking…",
        "assistant: I will read the file.",
        "tool Bash: git status --short",
    ]
    assert progress.narrate(json.dumps(answered)) == ["tool error: fatal: not a git repository"]
    assert progress.narrate(json.dumps(done)) == ["finished: success · 7 turns · $0.21"]
    assert "private" not in json.dumps([progress.narrate(json.dumps(said))])


def test_a_line_that_is_not_stream_json_passes_through_capped(progress):
    assert progress.narrate("plain runner output") == ["plain runner output"]
    long = progress.narrate("x" * 5000)[0]
    assert len(long) == progress.MAX_LINE_CHARS and long.endswith("…")
    assert progress.narrate('{"type": "assistant", "message": {"cont') == ['{"type": "assistant", "message": {"cont']


def test_a_waiting_task_says_what_it_waits_for_and_that_it_holds_nothing(progress):
    line = progress.state_line({
        "task_id": "task_0123456789abcdef",
        "step_id": "scan-03",
        "state": "READY",
        "park_reason": "DEPENDENCY_INCOMPLETE",
        "read": "ok",
    })
    assert line.startswith("[scan-03 ")
    assert "waiting · READY" in line
    assert "a step it depends on has not finished" in line
    assert "holds no capacity" in line


# --------------------------------------------------------------------------
# swarm_follow format=lines: the window, against the real routes
# --------------------------------------------------------------------------


class _Time:
    """A clock the test advances, and a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.slept))


def _stream(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def test_the_first_call_returns_at_once_and_says_the_task_is_waiting(swarm, world, progress):
    world.task("task_a", state="READY")
    world.db.docs["tasks/task_a"]["park_reason"] = "DEPENDENCY_INCOMPLETE"
    world.db.docs["tasks/task_a"]["step_id"] = "scan-03"
    clock = _Time()

    got = progress.watch(swarm, ["task_a"], wait_seconds=90, sleep=clock.sleep, clock=clock.clock)

    assert clock.slept == [], "a first call must answer at once so the row shows where it is"
    assert got["polls"] == 1
    assert any("waiting · READY" in line and "holds no capacity" in line for line in got["lines"]), got["lines"]
    assert got["since"] and got["all_finished"] is False
    assert got["stop"] is False, "a waiting task is not a reason to stop following it"


def test_a_window_returns_when_the_task_finishes_with_its_outcome(swarm, world, progress):
    """The whole of `sc:step`'s loop, against the real logs, events and
    attempts routes: narrated progress, then the outcome, in one call that
    gathered until the task finished."""
    world.task("task_a", state="RUNNING")
    world.db.docs["tasks/task_a"]["runner_profile"] = "claude-code"
    world.attempt("task_a", "att_1")
    started = [
        {"type": "system", "subtype": "init", "model": "claude-x", "tools": []},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Scanning."}]}},
    ]
    # The AGENT's stream-json is `agent_stdout`; `stdout` is the runner's own
    # JSON log, which a row must not narrate as the agent's work.
    world.live("task_a", "att_1", _stream(*started), stream="agent_stdout")
    world.live("task_a", "att_1", '{"event": "runner log line", "level": "info"}\n')
    clock = _Time()
    first = progress.watch(swarm, ["task_a"], sleep=clock.sleep, clock=clock.clock)
    assert any("assistant: Scanning." in line for line in first["lines"]), first["lines"]
    assert not any("runner log line" in line for line in first["lines"]), first["lines"]
    assert first["tasks"][0]["streams"] == "agent"

    answer = 'Found two issues.\n```json\n{"issues": 2, "files": ["a.py"]}\n```'
    finished = started + [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
        {"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.21, "result": answer},
    ]

    def _finish(sleeps: int) -> None:
        if sleeps == 2:
            doc = world.db.docs["tasks/task_a"]
            doc["state"] = "SUCCEEDED"
            doc["started_at"] = NOW
            doc["completed_at"] = NOW + timedelta(minutes=4, seconds=12)
            doc["result_summary"] = {
                "runner": {"summary": answer[:20]},
                "artifacts": [{"name": "report.md", "uri": "gs://b/report.md", "bytes": 12}],
                "git": {"commit_count": 1, "pull_request": {"number": 231, "url": "https://github.com/acme/widgets/pull/231"}},
            }
            world.db.docs["attempts/att_1"]["cost_usd"] = 0.21
            world.final("task_a", "att_1", _stream(*finished), stream="agent_stdout")

    clock.on_sleep = _finish
    got = progress.watch(swarm, ["task_a"], since=first["since"], wait_seconds=90, sleep=clock.sleep, clock=clock.clock)

    assert got["all_finished"] is True and got["stop"] is True
    assert got["polls"] == 3, "the window should end on the poll that saw the task finish"
    assert any("tool Read: a.py" in line for line in got["lines"]), got["lines"]
    assert not any("assistant: Scanning." in line for line in got["lines"]), "a line was repeated"
    outcome = got["tasks"][0]["outcome"]
    # The WHOLE answer, from the stream's last `result` event, through the
    # API's answer route -- the runner's summary holds only its first
    # characters, and would have lost the JSON.
    assert outcome["answer"] == answer
    assert outcome["answer_source"] == "agent_result_event"
    assert "answer_truncated" not in outcome
    assert outcome["answer_json"] == {"issues": 2, "files": ["a.py"]}
    assert outcome["cost_usd"] == 0.21
    assert outcome["duration_s"] == 252.0
    assert outcome["pr_url"] == "https://github.com/acme/widgets/pull/231"
    assert outcome["artifacts"] == [{"name": "report.md", "uri": "gs://b/report.md", "bytes": 12}]
    assert outcome["state"] == "SUCCEEDED"


def test_a_window_with_nothing_new_waits_it_out_and_repeats_nothing(swarm, world, progress):
    world.task("task_a", state="RUNNING")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "only line\n")
    clock = _Time()
    first = progress.watch(swarm, ["task_a"], sleep=clock.sleep, clock=clock.clock)

    got = progress.watch(swarm, ["task_a"], since=first["since"], wait_seconds=30, sleep=clock.sleep, clock=clock.clock)

    assert got["lines"] == [], got["lines"]
    assert sum(clock.slept) == 30 and got["polls"] == 7


def test_a_window_longer_than_max_lines_keeps_the_latest_and_says_how_many_it_left_out(swarm, world, progress):
    world.task("task_a", state="RUNNING")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "".join(f"line {i}\n" for i in range(100)))

    got = progress.watch(swarm, ["task_a"], max_lines=10)

    assert len(got["lines"]) == 11
    assert got["lines"][0].startswith("… 91 earlier line(s)"), got["lines"][0]
    assert "uv run swarm tail task_a" in got["lines"][0]
    assert got["lines"][-1].endswith("line 99")
    assert got["truncated"] is True


def test_a_failed_task_hands_back_its_error_and_no_invented_answer(swarm, world, progress):
    world.task("task_a", state="FAILED")
    world.attempt("task_a", "att_1")
    doc = world.db.docs["tasks/task_a"]
    doc["last_error"] = "claude-code exited 1: boom"
    doc["result_summary"] = {}
    world.db.docs["attempts/att_1"].update({"exit_code": 1, "error": "boom"})

    outcome = progress.outcome(swarm, swarm.task("task_a"))

    assert outcome["state"] == "FAILED"
    assert outcome["last_error"] == "claude-code exited 1: boom"
    assert outcome["answer"] is None and outcome["answer_json"] is None
    why = outcome["answer_unavailable_because"]
    assert "absent" in why and "neither a result event" in why, why
    # Not measured is null, never 0.
    assert outcome["cost_usd"] is None and "NOT MEASURED" in outcome["cost_note"]
    assert outcome["failure"]["last_attempt"]["exit_code"] == 1


def test_a_runner_with_no_agent_is_read_from_its_own_streams(swarm, world, progress):
    """The mock starts no agent CLI, so the route answers `not_applicable` for
    `agent_stdout`. The row falls back to the runner's streams in the same
    call, and the token remembers it."""
    world.task("task_a", state="RUNNING")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "mock says hello\n")

    first = progress.watch(swarm, ["task_a"])
    assert any(line.endswith("mock says hello") for line in first["lines"]), first["lines"]
    assert first["tasks"][0]["streams"] == "runner"
    assert progress.decode_since(first["since"])[3] == {"task_a": "runner"}

    world.live("task_a", "att_1", "mock says hello\nand goodbye\n")
    second = progress.watch(swarm, ["task_a"], since=first["since"])
    assert [line for line in second["lines"] if "mock says" in line] == [], "a line was repeated"
    assert any(line.endswith("and goodbye") for line in second["lines"]), second["lines"]


def test_the_stream_names_are_the_apis():
    """`apps/swarm-mcp` depends on swarm-common alone and cannot import
    swarm-api, so the stream names are copies -- held to the API's here, where
    both are importable."""
    from swarm_api import agent_streams, inspect

    from swarm_mcp import follow

    assert follow.STREAMS == inspect.STREAMS
    assert follow.AGENT_STREAMS == agent_streams.AGENT_STREAMS


def test_only_an_object_the_answer_ends_with_is_its_json(progress):
    assert progress.last_json_object('done\n{"a": 1}') == {"a": 1}
    assert progress.last_json_object('done\n```json\n{"a": {"b": [1, 2]}}\n```\n') == {"a": {"b": [1, 2]}}
    assert progress.last_json_object('{"a": 1} and then prose') is None
    assert progress.last_json_object("no json here") is None


def test_the_lines_format_goes_through_the_tool(swarm, world):
    world.task("task_a", state="QUEUED")
    reply = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "format": "lines"}))
    assert set(reply) >= {"since", "lines", "tasks", "all_finished", "stop", "truncated"}
    assert any("waiting · QUEUED" in line for line in reply["lines"]), reply["lines"]
    assert reply["stop"] is False, "a queued task is still going somewhere; the row must keep following"


# --------------------------------------------------------------------------
# swarm_follow format=lines: when a row stops
# --------------------------------------------------------------------------


def test_a_task_the_api_does_not_have_stops_the_row_at_once(swarm, world, progress):
    """A mis-copied id, another deployment's task, another tenant's: 404.
    `follow` rightly reports it as `read: failed`, not an error -- and a row
    that only stopped on errors or on `all_finished` would have polled it for
    its whole turn limit, one 90-second window at a time."""
    clock = _Time()
    first = progress.watch(swarm, ["task_nobody_has"], sleep=clock.sleep, clock=clock.clock)

    assert first["stop"] is True and first["all_finished"] is False
    row = first["tasks"][0]
    assert row["abandoned"] is True and "outcome" not in row
    assert "HTTP 404" in row["abandoned_because"] and "Nothing was cancelled" in row["abandoned_because"]
    assert row["abandoned_because"] in first["stop_because"]
    assert any("stopped following" in line for line in first["lines"]), first["lines"]

    # A later call, with a window, does not wait the window out on a 404.
    again = progress.watch(
        swarm, ["task_nobody_has"], since=first["since"], wait_seconds=90,
        sleep=clock.sleep, clock=clock.clock,
    )
    assert again["stop"] is True and clock.slept == [] and again["polls"] == 1


def test_a_task_that_cannot_be_read_for_three_calls_stops_the_row(swarm, world, progress, monkeypatch):
    """A failure with no HTTP status -- a reset connection, a token that did
    not mint -- may pass. Three calls in a row that read nothing will not be
    followed by a fourth, and the streak rides in `since`."""
    world.task("task_a", state="RUNNING")

    def _unreachable(task_id):  # noqa: ARG001
        raise SwarmError("connection reset by peer")

    monkeypatch.setattr(swarm, "task", _unreachable)
    clock = _Time()
    since = None
    replies = []
    for _ in range(progress.READ_FAILURE_LIMIT):
        reply = progress.watch(swarm, ["task_a"], since=since, wait_seconds=10, sleep=clock.sleep, clock=clock.clock)
        replies.append(reply)
        since = reply["since"]

    assert [r["stop"] for r in replies] == [False] * (progress.READ_FAILURE_LIMIT - 1) + [True]
    assert [r["tasks"][0].get("read_failures") for r in replies] == list(range(1, progress.READ_FAILURE_LIMIT + 1))
    last = replies[-1]["tasks"][0]
    assert last["abandoned"] is True
    assert f"{progress.READ_FAILURE_LIMIT} calls in a row" in last["abandoned_because"]
    assert "UNKNOWN" in last["abandoned_because"]


def test_one_good_read_clears_the_streak(swarm, world, progress, monkeypatch):
    world.task("task_a", state="RUNNING")
    real_task = swarm.task

    def _timed_out(task_id):  # noqa: ARG001
        raise SwarmError("timed out")

    monkeypatch.setattr(swarm, "task", _timed_out)
    first = progress.watch(swarm, ["task_a"])
    assert first["tasks"][0]["read_failures"] == 1

    monkeypatch.setattr(swarm, "task", real_task)
    second = progress.watch(swarm, ["task_a"], since=first["since"])

    assert "read_failures" not in second["tasks"][0]
    assert progress.read_failures(second["since"]) == {}
    assert second["stop"] is False


def test_a_task_that_is_another_step_is_not_followed(swarm, world, progress):
    """/sc:run hands each row its task id through a relay that retypes it. A
    row labelled scan-03 that followed scan-07's task would return scan-07's
    answer, cost and pull request under scan-03's name."""
    world.task("task_a", state="SUCCEEDED")
    world.db.docs["tasks/task_a"]["step_id"] = "scan-07"

    got = progress.watch(swarm, ["task_a"], step_id="scan-03")

    assert got["stop"] is True and got["all_finished"] is False
    row = got["tasks"][0]
    assert row["abandoned"] is True and "outcome" not in row, "another step's outcome must not be handed back"
    assert "'scan-07'" in row["abandoned_because"] and "'scan-03'" in row["abandoned_because"]

    right = progress.watch(swarm, ["task_a"], step_id="scan-07")
    assert right["stop"] is True and right["tasks"][0]["outcome"]["state"] == "SUCCEEDED"


def test_step_id_goes_through_the_tool_and_is_refused_where_it_cannot_be_checked(swarm, world):
    world.task("task_a", state="QUEUED")
    world.db.docs["tasks/task_a"]["step_id"] = "scan-07"

    reply = json.loads(server._call(
        swarm, "swarm_follow", {"task_ids": ["task_a"], "format": "lines", "step_id": "scan-03"},
    ))
    assert reply["stop"] is True and reply["tasks"][0]["abandoned"] is True

    # Asked for and not performed would be worse than not asked for.
    with pytest.raises(SwarmError, match="format"):
        server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "step_id": "scan-07"})
    with pytest.raises(SwarmError, match="ONE task"):
        server._call(
            swarm, "swarm_follow",
            {"task_ids": ["task_a", "task_b"], "format": "lines", "step_id": "scan-07"},
        )


def test_a_finished_task_stops_the_row_with_its_outcome(swarm, world, progress):
    world.task("task_a", state="SUCCEEDED")
    got = progress.watch(swarm, ["task_a"])
    assert got["stop"] is True and got["all_finished"] is True
    assert got["tasks"][0]["outcome"]["state"] == "SUCCEEDED"
    assert "stop_because" not in got


def test_since_carries_the_failed_read_streak(progress):
    token = progress.encode_since({}, {}, {}, {}, {"task_a": 2, "task_b": 0})
    assert progress.read_failures(token) == {"task_a": 2}
    assert progress.read_failures("not-a-token!!") == {}
    assert progress.read_failures(None) == {}


# --------------------------------------------------------------------------
# swarm_workflow: the spec digest
# --------------------------------------------------------------------------


def test_the_digest_is_fnv1a_32_over_utf8():
    """The published FNV-1a test vectors, so the function is the algorithm its
    name says -- run.js implements the same one and must agree with it."""
    from swarm_mcp import workflows

    assert workflows.fnv1a32(b"") == 0x811C9DC5
    assert workflows.fnv1a32(b"a") == 0xE40C292C
    assert workflows.fnv1a32(b"foobar") == 0xBF9CF968


def test_the_digest_is_of_the_spec_not_of_how_it_was_written():
    from swarm_mcp import workflows

    one = {"label": "scan", "steps": [{"step_id": "a", "prompt": "x", "timeout_seconds": 60}]}
    same = json.loads('{"steps": [{"timeout_seconds": 60.0, "prompt": "x", "step_id": "a"}], "label": "scan"}')
    assert workflows.spec_digest(one) == workflows.spec_digest(same)
    assert workflows.spec_digest(one).startswith("fnv1a32:") and len(workflows.spec_digest(one)) == 16

    tidied = {"label": "scan", "steps": [{"step_id": "a", "prompt": "x.", "timeout_seconds": 60}]}
    assert workflows.spec_digest(tidied) != workflows.spec_digest(one)


def test_a_spec_that_arrives_different_from_its_digest_submits_nothing():
    """The relay dropped a step. Checked before the round trip, the changed
    spec is never submitted -- there is nothing to cancel afterwards."""
    from swarm_mcp import workflows

    meant = json.loads(json.dumps(_SPEC))
    arrived = {**meant, "steps": meant["steps"][:1]}
    recorder = _Recorder()

    with pytest.raises(SwarmError) as caught:
        server._call(recorder, "swarm_workflow", {"spec": arrived, "spec_digest": workflows.spec_digest(meant)})

    assert "NOTHING was submitted" in str(caught.value)
    assert workflows.spec_digest(arrived) in str(caught.value)
    assert recorder.sent == []


def test_the_reply_carries_the_digest_of_what_was_received():
    from swarm_mcp import workflows

    spec = json.loads(json.dumps(_SPEC))
    unchecked = json.loads(server._call(_Recorder(), "swarm_workflow", {"spec": spec}))
    assert unchecked["spec_digest"] == workflows.spec_digest(spec)
    assert unchecked["spec_digest_checked"] is False

    checked = json.loads(server._call(
        _Recorder(), "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)},
    ))
    assert checked["spec_digest_checked"] is True


def test_a_spec_digest_without_a_spec_is_refused():
    with pytest.raises(SwarmError, match="checks a whole `spec`"):
        server._call(_Recorder(), "swarm_workflow", {
            "steps": [{"step_id": "a", "prompt": "x"}], "spec_digest": "fnv1a32:00000000",
        })


# --------------------------------------------------------------------------
# Every tool: an argument it does not declare is refused, not dropped
# --------------------------------------------------------------------------


def test_an_argument_a_tool_does_not_declare_is_refused_before_anything_travels():
    """A bridge older than its caller dropped what it did not know: a plugin
    agent's `strategy: direct-pr` became a `collect` task that returned as if
    nothing were wrong. `model` is also invariant 10's argument."""
    recorder = _Recorder()
    with pytest.raises(SwarmError) as caught:
        server._call(recorder, "swarm_dispatch", {"prompt": "x", "model": "opus", "strategee": "direct-pr"})

    message = str(caught.value)
    assert "'model'" in message and "'strategee'" in message and "'strategy'" in message, message
    assert recorder.sent == []


def test_every_argument_a_tool_reads_is_one_its_schema_declares():
    """The refusal reads the schema, so an argument `_call` reads and the
    schema forgot would now be refused on every call. Read `_call`'s source
    for every `args[...]`/`args.get(...)` of each tool and hold it to that
    tool's schema."""
    import ast
    import inspect as pyinspect
    import re
    import textwrap

    source = textwrap.dedent(pyinspect.getsource(server._call))
    tree = ast.parse(source)
    declared = {t["name"]: set(t["inputSchema"].get("properties") or {}) for t in server.TOOLS}
    read: dict[str, set[str]] = {}

    def names_tested(test) -> set[str]:
        found = set()
        for node in ast.walk(test):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name":
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        found.add(comparator.value)
                    if isinstance(comparator, ast.Tuple):
                        found |= {e.value for e in comparator.elts if isinstance(e, ast.Constant)}
                    if isinstance(comparator, ast.Name):
                        # `name in _SC_VIEWS`: the module's own table, read.
                        table = getattr(server, comparator.id, None)
                        assert isinstance(table, (dict, tuple, list, set, frozenset)), comparator.id
                        found |= {key for key in table if isinstance(key, str)}
        return found

    def keys_read(node) -> set[str]:
        keys = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get" \
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "args" \
                    and sub.args and isinstance(sub.args[0], ast.Constant):
                keys.add(sub.args[0].value)
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id == "args" \
                    and isinstance(sub.slice, ast.Constant):
                keys.add(sub.slice.value)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in ("_flag", "_int_arg") \
                    and len(sub.args) >= 2 and isinstance(sub.args[1], ast.Constant):
                keys.add(sub.args[1].value)
        return keys

    function = tree.body[0]
    for statement in function.body:
        if isinstance(statement, ast.If):
            for tool in names_tested(statement.test):
                read.setdefault(tool, set()).update(keys_read(statement))
    # Printed as a count, not trusted as a pass: a walker that matched nothing
    # would report "no undeclared arguments" over no tools at all.
    assert len(read) >= 15, f"only {sorted(read)} were read from _call's branches"
    assert "swarm_trouble" in read and "width" in read["swarm_trouble"], read.get("swarm_trouble")
    undeclared = {
        tool: sorted(keys - declared[tool])
        for tool, keys in read.items()
        if tool in declared and keys - declared[tool]
    }
    assert not undeclared, f"_call reads arguments its schemas do not declare: {undeclared}"
    assert re.search(r"_refuse_unknown_arguments\(name, args\)", source)


# --------------------------------------------------------------------------
# The stdio loop answers concurrently
# --------------------------------------------------------------------------


def _speak(*messages):
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server.serve(stdin=stdin)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_tool_call_that_blocks_does_not_hold_the_next_one(monkeypatch):
    """One `swarm_wait` blocks for up to an hour by design; a workflow's step
    agents each hold a follow open for its window. Answered one at a time,
    every call waits behind all of them."""
    released = threading.Event()

    def _call(client, name, arguments):  # noqa: ARG001
        if name == "hold":
            return "released" if released.wait(timeout=3) else "held the loop"
        released.set()
        return "let the other go"

    monkeypatch.setattr(server, "_call", _call)
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: object())

    replies = _speak(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hold"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "release"}},
    )

    answers = {r["id"]: r["result"]["content"][0]["text"] for r in replies}
    assert answers == {1: "released", 2: "let the other go"}
