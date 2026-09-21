"""The bridge: dispatching to SwarmCloud the way you dispatch a local subagent.

Offline by construction. Every test here uses a fake control plane -- no
gcloud, no GCS, no network -- because the thing being checked is the LOGIC
around the API, not the API. Real git does run, for the same reason it runs in
test_harvest: the conflict behaviour is what matters and a fixture of git's
output would pin what I believe git does.

Two properties get the most attention:

* a missing patch is never silently an empty one. Six things cause "no patch"
  and they need six different responses, so `explain_absence` is checked for
  each rather than allowed to collapse into one message;
* `integrate` refuses a dirty tree. Applying agents' work on top of the
  operator's uncommitted edits would mix the two and leave no way to tell which
  conflict came from where.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest

from swarm_mcp import patches, server
from swarm_mcp.client import SwarmError
from swarm_mcp.cli import build_parser
from swarm_mcp.patches import apply_patch, explain_absence, integrate, parse_gs_uri, patch_uri

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class FakeClient:
    """A control plane that answers from a dict. No credentials, no network.

    It answers in the shape `SwarmClient` HANDS BACK -- the task document, not
    the `{"task": {...}}` envelope the route sends, which the client unwraps.
    That distinction is not this file's to check; `test_against_the_real_api`
    drives the real application through the real client for exactly that, and
    it exists because this fake once named the id field `task_id` while the
    API named it `id`, so every consumer here agreed with the fake and none of
    them worked.
    """

    def __init__(self, tasks=None, blobs=None):
        self.tasks = tasks or {}
        self.blobs = blobs or {}
        self.dispatched = []
        self.cancelled = []

    def task(self, task_id):
        if task_id not in self.tasks:
            raise SwarmError(f"no such task: {task_id}")
        return self.tasks[task_id]

    def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        # `id`, the name `codec.task_to_api` uses. Not `task_id`.
        return {"id": f"task_{len(self.dispatched)}", "state": "QUEUED"}

    def cancel(self, task_id):
        self.cancelled.append(task_id)

    def access_token(self):
        return "fake"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any test that reaches for GCS must go through the fake, not urllib."""
    monkeypatch.setattr(
        patches, "download", lambda client, uri, timeout=120: client.blobs[uri]
    )


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True, capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    (path / "types.ts").write_text("export interface A {\n  x: number\n}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "base")
    return path


def _patch_for(repo, mutate):
    """Produce a real patch the way a worker would, via a commit.

    COMMITTED, not diffed from the working tree, and the difference is not
    stylistic. git decides a file is unchanged from its stat information, so a
    file written twice inside one filesystem timestamp tick is "racily clean"
    and `git diff` reports nothing. Generating the fixture by writing, diffing
    and restoring hit that about one run in five: the patch came back empty,
    `git apply` succeeded on it, and the assertion failed somewhere unrelated
    to what was being tested. Commits are content-addressed, so no stat is
    consulted and the fixture is the same every time.

    `add --intent-to-add` is still needed inside the commit, for the same
    reason `summarize_work` needs it: without it a diff omits every file the
    agent CREATED, which is most of what an agent creates.
    """
    mutate(repo)
    _git(repo, "add", "--all", "--", ".")
    _git(repo, "commit", "--quiet", "-m", "scratch fixture")
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "HEAD~1", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout
    _git(repo, "reset", "--hard", "--quiet", "HEAD~1")
    return out


def _task(task_id="task_a", *, state="SUCCEEDED", git=None, artifacts=None, summary=True):
    body = {"id": task_id, "tenant_id": "u-test", "state": state}
    if summary:
        body["result_summary"] = {"artifacts": artifacts or [], "git": git}
    return body


# -- locating the patch ----------------------------------------------------


def test_a_gs_uri_splits_into_bucket_and_key():
    assert parse_gs_uri("gs://b/tenants/u/x.patch") == ("b", "tenants/u/x.patch")


@pytest.mark.parametrize("bad", ["", "https://x/y", "gs://onlybucket"])
def test_anything_that_is_not_a_complete_gs_uri_is_refused(bad):
    with pytest.raises(SwarmError):
        parse_gs_uri(bad)


def test_the_patch_uri_is_matched_from_the_artifact_list_not_minted():
    """`git.patch` holds a NAME and `artifacts[]` holds the uri. Building the
    uri from the name instead would hand back a link to an object that failed
    to upload."""
    task = _task(
        git={"patch": "swarm-work.patch"},
        artifacts=[{"name": "swarm-work.patch", "uri": "gs://b/k.patch", "bytes": 10}],
    )
    assert patch_uri(task) == "gs://b/k.patch"


def test_a_recorded_patch_whose_upload_failed_yields_no_uri():
    task = _task(git={"patch": "swarm-work.patch"}, artifacts=[])
    assert patch_uri(task) is None


# -- why there is no patch -------------------------------------------------


def test_each_cause_of_a_missing_patch_reads_differently():
    """The property this whole panel exists for. Collapsing six causes into
    'no patch' would send an operator to grant write scope when the real
    problem was that the agent changed nothing."""
    cases = {
        "cloned no repository": _task(git=None),
        "could not be read": _task(git={"error": "git rev-parse failed"}),
        "over the cap": _task(git={"patch_omitted": True, "patch_bytes": 99}),
        "changed nothing": _task(git={"commits": [], "dirty": []}),
        "no write permission": _task(
            git={"commits": [{"sha": "a"}], "publish_reason": "no write permission"}
        ),
    }
    messages = {}
    for fragment, task in cases.items():
        message = explain_absence(task)
        assert fragment in message, f"{fragment!r} not in {message!r}"
        messages[fragment] = message
    assert len(set(messages.values())) == len(messages), "two causes produced one message"


def test_a_task_with_no_summary_at_all_says_so_rather_than_guessing():
    message = explain_absence(_task(state="PARKED", summary=False))
    assert "PARKED" in message and "no result summary" in message


# -- applying --------------------------------------------------------------


def test_a_clean_patch_applies_and_reports_no_conflict(repo):
    patch = _patch_for(repo, lambda r: (r / "types.ts").write_text(
        "export interface A {\n  x: number\n  y: string\n}\n"
    ))
    result = apply_patch(patch, repo, task_id="task_a")
    assert result.clean
    assert "y: string" in (repo / "types.ts").read_text()


def test_a_conflict_leaves_markers_rather_than_failing(repo):
    """The design point. A plain `git apply` fails atomically and says little;
    `--3way` leaves ordinary markers, which is what lets whoever is reading
    finish the integration with the whole repository in front of them."""
    patch = _patch_for(repo, lambda r: (r / "types.ts").write_text(
        "export interface A {\n  x: string\n}\n"
    ))
    # The local tree moves the same line a different way.
    (repo / "types.ts").write_text("export interface A {\n  x: boolean\n}\n")
    _git(repo, "commit", "--quiet", "-am", "local change")

    result = apply_patch(patch, repo, task_id="task_a")

    assert result.applied is True, "a conflict must not throw away the content"
    assert result.conflicted == ["types.ts"]
    assert "<<<<<<<" in (repo / "types.ts").read_text()


def test_the_temporary_patch_file_never_survives_the_apply(repo):
    patch = _patch_for(repo, lambda r: (r / "types.ts").write_text("export interface A {}\n"))
    apply_patch(patch, repo)
    assert not (repo / ".git" / "swarm-incoming.patch").exists()


# -- integrating -----------------------------------------------------------


def test_integrating_onto_a_dirty_tree_is_refused(repo):
    """Applying on top of the operator's uncommitted edits would mix their work
    with the agents' and leave no way to tell which conflict came from where."""
    (repo / "types.ts").write_text("locally edited\n")
    with pytest.raises(SwarmError, match="uncommitted changes"):
        integrate(FakeClient(), ["task_a"], repo, branch="swarm/batch-1")


def test_several_agents_land_on_one_branch_in_the_order_given(repo):
    patch_a = _patch_for(repo, lambda r: (r / "a.txt").write_text("from a\n"))
    patch_b = _patch_for(repo, lambda r: (r / "b.txt").write_text("from b\n"))
    for stray in ("a.txt", "b.txt"):
        (repo / stray).unlink(missing_ok=True)

    client = FakeClient(
        tasks={
            "task_a": _task("task_a", git={"patch": "p"},
                            artifacts=[{"name": "p", "uri": "gs://b/a"}]),
            "task_b": _task("task_b", git={"patch": "p"},
                            artifacts=[{"name": "p", "uri": "gs://b/b"}]),
        },
        blobs={"gs://b/a": patch_a, "gs://b/b": patch_b},
    )

    result = integrate(client, ["task_a", "task_b"], repo, branch="swarm/batch-1")

    assert [r.task_id for r in result.results] == ["task_a", "task_b"]
    assert all(r.clean for r in result.results)
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()
    branch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "swarm/batch-1"


def test_a_task_with_no_patch_is_skipped_with_its_reason_not_silently_dropped(repo):
    """A batch where one agent produced nothing must SAY so. Silently
    integrating four of five and reporting success is the failure this is
    written against."""
    patch_a = _patch_for(repo, lambda r: (r / "a.txt").write_text("from a\n"))
    (repo / "a.txt").unlink(missing_ok=True)
    client = FakeClient(
        tasks={
            "task_a": _task("task_a", git={"patch": "p"},
                            artifacts=[{"name": "p", "uri": "gs://b/a"}]),
            "task_b": _task("task_b", git={"commits": [], "dirty": []}),
        },
        blobs={"gs://b/a": patch_a},
    )

    result = integrate(client, ["task_a", "task_b"], repo, branch="swarm/batch-2")

    assert len(result.results) == 1
    assert result.skipped == [("task_b", "the agent changed nothing in the repository")]
    assert "skip  task_b" in result.render()


# -- the MCP protocol ------------------------------------------------------


def _speak(*messages):
    """Drive the stdio server with a script of JSON-RPC messages."""
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server.serve(stdin=stdin)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_initialize_answers_with_a_protocol_version_and_tool_capability():
    replies = _speak({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert replies[0]["result"]["protocolVersion"] == server.PROTOCOL_VERSION
    assert "tools" in replies[0]["result"]["capabilities"]


def test_a_notification_gets_no_reply():
    """`notifications/initialized` has no id. Answering it would put an
    unmatched response on the wire and some hosts drop the session for it."""
    replies = _speak({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert replies == []


def test_listing_tools_needs_no_credentials():
    """A host lists tools at startup. If that shelled out to gcloud, merely
    opening a session would trigger an auth prompt."""
    replies = _speak({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in replies[0]["result"]["tools"]]
    assert "swarm_dispatch" in names and "swarm_integrate" in names


def test_every_advertised_tool_has_a_schema_whose_required_fields_exist():
    for tool in server.TOOLS:
        schema = tool["inputSchema"]
        properties = set(schema.get("properties", {}))
        missing = set(schema.get("required", [])) - properties
        assert not missing, f"{tool['name']} requires undeclared {missing}"
        assert tool["description"].strip(), f"{tool['name']} has no description"


def test_an_unknown_method_is_a_jsonrpc_error():
    replies = _speak({"jsonrpc": "2.0", "id": 3, "method": "nonsense/thing"})
    assert replies[0]["error"]["code"] == -32601


def test_a_failing_tool_call_is_isError_and_not_a_transport_error(monkeypatch):
    """A host that saw a JSON-RPC error would drop the session instead of
    showing the operator what went wrong."""
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: FakeClient())
    replies = _speak(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "swarm_result", "arguments": {"task_id": "missing"}}}
    )
    assert replies[0]["result"]["isError"] is True
    assert "no such task" in replies[0]["result"]["content"][0]["text"]
    assert "error" not in replies[0]


def test_dispatch_returns_the_command_that_follows_it_live(monkeypatch):
    """An MCP tool cannot stream. Rather than pretend, the dispatch reply
    hands back the terminal command that can."""
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: FakeClient())
    replies = _speak(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "swarm_dispatch", "arguments": {"prompt": "fix the gate"}}}
    )
    body = json.loads(replies[0]["result"]["content"][0]["text"])
    assert body["follow_live_with"] == "swarm tail task_1"


def test_a_wait_that_times_out_names_what_is_still_running(monkeypatch):
    """A caller reading a timeout as "everything finished" would integrate a
    partial set and call it the whole batch."""
    client = FakeClient(tasks={"task_a": _task("task_a", state="RUNNING")})
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: client)
    replies = _speak(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "swarm_wait",
                    "arguments": {"task_ids": ["task_a"], "timeout_seconds": 0}}}
    )
    body = json.loads(replies[0]["result"]["content"][0]["text"])
    assert body["still_running"] == ["task_a"]


def test_dispatch_cannot_be_told_which_image_to_run():
    """Invariant 10 at the client. The catalogue is keyed by profile NAME;
    there is no parameter here that could carry an image, a command or a
    resource spec, so a caller cannot try."""
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_dispatch")["inputSchema"]
    forbidden = {"image", "command", "args", "cpu", "memory", "resource_class", "backend"}
    assert forbidden.isdisjoint(schema["properties"])


# -- the CLI ---------------------------------------------------------------


def test_the_cli_exposes_every_operation_the_mcp_server_does():
    """Two implementations of "what does integrate mean" is how a CLI and a
    tool quietly start disagreeing."""
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)
    assert {"dispatch", "tail", "result", "apply", "integrate", "cancel"} <= commands


def test_tail_takes_several_tasks_so_one_background_shell_follows_a_whole_batch():
    args = build_parser().parse_args(["tail", "task_a", "task_b", "--interval", "1"])
    assert args.task_ids == ["task_a", "task_b"]
    assert args.interval == 1
