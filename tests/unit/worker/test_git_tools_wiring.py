"""Runner git tools, wired (docs/runner-git-tools.md §1.3, §1.4, §7 lane G6).

The relay lives inside `ChildPath`, which the lifecycle already prepares and
ticks, and the CLI runners name the `swarm-git` shim. What this file holds:

  * claude-code's argv carries `--mcp-config` naming `agent_worker.gitmcp`
    when the attempt is offered git tools, and carries none when it is not;
    on the live browser the shim and Playwright MCP share ONE config;
  * codex is given the same server through its own MCP configuration;
  * the configured command really starts the shim and lists the five tools;
  * the attempt key is registered separately from the child path being
    offered: a CHILD task gets git tools but not `SWARM_CHILDREN`;
  * a git request is relayed signed with the attempt key, and a fenced git
    call ends the attempt through the fenced exit without releasing the lease
    (invariant 5);
  * the runner's environment holds no variable whose name or value matches
    `token`, case-insensitive (#1225 acceptance: `env | grep -i token` empty).

Real git against a bare repository on disk; only swarm-api is faked, at the
`ChildApi.call` boundary, with `swarm_api.childkey`'s own verifier.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.gitmcp import server as shim
from scheduler import dispatch as scheduler_dispatch
from swarm_api import childkey as api_childkey
from swarm_api import gittools as api_gittools
from swarm_common.profiles import RUNNER_PROFILES

from worker_seeds import TENANT, seed_attempt, seed_tenant
from test_strategy_end_to_end import forge, local_urls, origin  # noqa: F401 -- fixtures

NONCE = "a1b2c3d4-test-nonce-not-a-secret"
_TOKEN = re.compile("token", re.IGNORECASE)


# --------------------------------------------------------------------------
# The worker's restatements, held to the scheduler's and swarm-api's
# --------------------------------------------------------------------------


def test_the_offer_rule_is_the_schedulers():
    """The scheduler issues the nonce a child needs exactly when the worker
    offers it git tools; a drift would leave a child keyed and toolless, or
    offered tools it can never sign for."""
    assert children_mod.GIT_TOOL_RUNNERS == scheduler_dispatch.GIT_TOOL_RUNNERS
    for name in RUNNER_PROFILES:
        assert children_mod.offers_git_tools(name) == scheduler_dispatch.offers_git_tools(name), name
    assert children_mod.offers_git_tools("claude-code") and children_mod.offers_git_tools("codex")
    assert not children_mod.offers_git_tools("mock")  # the control
    assert not children_mod.offers_git_tools("no-such-profile")


_BLOCKS: list[dict[str, Any]] = [
    {},
    {"strategy": "collect"},
    {"strategy": "collect", "carrier": "branches"},
    {"strategy": "direct-pr"},
    {"strategy": "integrate"},
    {"strategy": "no-such-strategy"},
    {"strategy": "direct-pr", "continues": "task_" + "a" * 20},
    {"strategy": "single-pr", "pr_role": "author"},
    {"strategy": "single-pr", "pr_role": "reader"},
    {"strategy": "single-pr", "pr_role": "none"},
    {"strategy": "single-pr", "pr_role": "amender", "pr_author": "task_" + "b" * 20},
    {"strategy": "single-pr", "pr_role": "no-such-role"},
]


@pytest.mark.parametrize("block", _BLOCKS, ids=lambda b: json.dumps(b, sort_keys=True))
def test_the_worker_derives_the_branch_swarm_api_will_accept(block):
    """The worker's fast answer and swarm-api's gate read one signed block the same way."""
    task = {"metadata": {"dispatch": block}} if block else {"metadata": {}}
    policy = api_gittools.push_policy(task, "task_own")
    pushes, pushable = children_mod.git_push_policy(task, task_id="task_own",
                                                    branch_prefix="swarm/")
    assert pushes == policy.pushes
    assert pushable == ((policy.branch,) if policy.pushes else ())


def test_a_branch_no_task_id_names_is_derived_as_none():
    """swarm-api refuses it; the worker offers no branch rather than guess one."""
    task = {"metadata": {"dispatch": {"strategy": "direct-pr", "continues": "main"}}}
    assert children_mod.git_push_policy(task, task_id="task_own", branch_prefix="swarm/") \
        == (True, ())


# --------------------------------------------------------------------------
# The runners name the shim
# --------------------------------------------------------------------------


def _ctx(tmp_path: Path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Rebase onto main and push."},
    )


def _run_body(module, monkeypatch, tmp_path, *, spool: str | None,
              browsers: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        return {}

    monkeypatch.setattr(module, "run_cli_agent", fake_run)
    for name, value in ((shim.SPOOL_ENV, spool), ("PLAYWRIGHT_BROWSERS_PATH", browsers),
                        ("SWARM_CDP_ENDPOINT", endpoint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    module.body(_ctx(tmp_path))
    return seen


def _mcp_config(seen: dict[str, Any]) -> dict[str, Any] | None:
    args = seen["args"]
    if "--mcp-config" not in args:
        return None
    assert args.count("--mcp-config") == 1, "one config file, every server in it"
    return json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))


def _shim_server(spool: str) -> dict[str, Any]:
    return {"type": "stdio", "command": sys.executable,
            "args": ["-I", "-m", "agent_worker.gitmcp", "--spool", spool]}


def test_claude_code_names_the_shim_when_the_attempt_is_offered_git_tools(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    spool = str(tmp_path / ".swarm-git")
    seen = _run_body(claude_code, monkeypatch, tmp_path, spool=spool)
    config = _mcp_config(seen)
    assert config == {"mcpServers": {shim.SERVER_NAME: _shim_server(spool)}}, \
        "claude-code's argv names no swarm-git server for an attempt offered git tools"
    path = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
    assert path.parent == tmp_path / claude_code.SETTINGS_DIR_NAME
    assert path.stat().st_mode & 0o077 == 0
    # The CLI-form shim (`python -m agent_worker.gitmcp <tool>`) reads it too.
    assert seen["env"][shim.SPOOL_ENV] == spool


def test_claude_code_names_no_mcp_config_without_git_tools(monkeypatch, tmp_path):
    """The control: no spool, no server, no flag -- as before this lane."""
    from agent_worker.runners import claude_code

    seen = _run_body(claude_code, monkeypatch, tmp_path, spool=None)
    assert _mcp_config(seen) is None
    assert shim.SPOOL_ENV not in seen["env"]


def test_the_live_browser_and_the_git_tools_share_one_config(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    spool = str(tmp_path / ".swarm-git")
    endpoint = "http://127.0.0.1:9222"
    seen = _run_body(claude_code, monkeypatch, tmp_path, spool=spool,
                     browsers="/opt/playwright", endpoint=endpoint)
    assert _mcp_config(seen) == {"mcpServers": {
        claude_code.MCP_SERVER_NAME: {"type": "stdio", "command": claude_code.PLAYWRIGHT_MCP_BIN,
                                      "args": ["--cdp-endpoint", f"{endpoint}/client/playwright",
                                               "--image-responses", "omit",
                                               "--output-dir", str(tmp_path / "artifacts" / "screenshots")]},
        shim.SERVER_NAME: _shim_server(spool),
    }}
    # Without the git tools the live browser's config is exactly what it was.
    seen = _run_body(claude_code, monkeypatch, tmp_path, spool=None,
                     browsers="/opt/playwright", endpoint=endpoint)
    assert set(_mcp_config(seen)["mcpServers"]) == {claude_code.MCP_SERVER_NAME}


def test_codex_is_given_the_shim_through_its_mcp_configuration(monkeypatch, tmp_path):
    from agent_worker.runners import codex

    spool = str(tmp_path / ".swarm-git")
    seen = _run_body(codex, monkeypatch, tmp_path, spool=spool)
    args = seen["args"]
    overrides = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
    server = _shim_server(spool)
    assert f"mcp_servers.{shim.SERVER_NAME}.command={json.dumps(server['command'])}" in overrides
    assert f"mcp_servers.{shim.SERVER_NAME}.args={json.dumps(server['args'])}" in overrides
    assert seen["env"][shim.SPOOL_ENV] == spool
    # The control.
    seen = _run_body(codex, monkeypatch, tmp_path, spool=None)
    assert "-c" not in seen["args"] and shim.SPOOL_ENV not in seen["env"]


def test_the_configured_command_starts_the_shim(tmp_path):
    """Not only a name in a file: the command the CLI will run answers MCP."""
    spool = tmp_path / ".swarm-git"
    server = _shim_server(str(spool))
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    done = subprocess.run(
        [server["command"], *server["args"]],
        input="".join(json.dumps(line) + "\n" for line in lines),
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert done.returncode == 0, done.stderr
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert answers[0]["result"]["serverInfo"]["name"] == shim.SERVER_NAME
    assert sorted(t["name"] for t in answers[1]["result"]["tools"]) == sorted(shim.TOOL_NAMES)


# --------------------------------------------------------------------------
# The relay inside ChildPath
# --------------------------------------------------------------------------


class FakeRoutes:
    """swarm-api's worker-only routes: the key registration and the git tools."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.fence_git = False
        self.registered_state: str | None = None
        self.db: Any = None

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            payload = json.loads(body)
            self.public_key = payload.get("public_key")
            if self.db is not None:
                self.registered_state = self.db.doc(f"tasks/{payload['task_id']}")["state"]
            return 201, {"registered": "key" if self.public_key else "tombstone"}
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        message = api_childkey.request_message(method, path, body or b"", stamp)
        if not (self.public_key and proof
                and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(403, "git_tool_unproven", "", retryable=False)
        if "/git/" in path:
            if self.fence_git:
                raise children_mod.ChildApiError(409, "git_fenced", "superseded", retryable=False)
            return 200, {"pr": 7, "head_sha": "c" * 40, "state": "green", "rows": []}
        if path.endswith("/children"):
            return 201, {"task": {"id": "task_child1"}, "created": True}
        raise AssertionError(f"unexpected route {method} {path}")

    def git_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "/git/" in c["path"]]


class _Log:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _clone(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _path(tmp_path: Path, routes: FakeRoutes, *, profile: str = "claude-code"):
    from agent_worker.hardening import PROTECTED, MemoryProtection

    cfg = SimpleNamespace(
        child_submit_retry_seconds=5, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id=TENANT, attempt_id="att_1",
        lease_id="lease_1", generation=1, task_id="task_1", swarm_api_url=None,
        swarm_api_audience=None, child_nonce=NONCE, runner_profile=profile,
        git_branch_prefix="swarm/", git_author_name="swarmcloud agent",
        git_author_email="swarmcloud-agent@users.noreply.github.com",
        git_clone_timeout_seconds=60,
    )
    path = children_mod.ChildPath(cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
                                  api=routes)
    path.register(lambda _note: None)
    assert path._key is not None
    return path


def _prepare(path, tmp_path: Path, task: dict[str, Any]) -> tuple[dict[str, str], Path]:
    repo, base = _clone(tmp_path)
    work = repo.parent
    private = tmp_path / "private"
    logs = tmp_path / "logs"
    private.mkdir()
    logs.mkdir()
    env = path.prepare(work, task=task, checkout=repo,
                       repository_url="https://github.com/acme/widgets.git",
                       clone_base=base, private_dir=private, logs_dir=logs, store=None)
    return env, work


_TASK = {"id": "task_1", "metadata": {"dispatch": {"strategy": "direct-pr"}}}


def test_a_parent_task_gets_both_spools(tmp_path):
    """The control for the child test below."""
    path = _path(tmp_path, FakeRoutes())
    env, work = _prepare(path, tmp_path, dict(_TASK))
    assert env[children_mod.CHILDREN_ENV] == str(work / children_mod.SPOOL_DIR_NAME)
    assert env[shim.SPOOL_ENV] == str(work / shim.SPOOL_DIR_NAME)
    assert path.offered


def test_a_child_task_gets_git_tools_but_not_the_child_path(tmp_path):
    """A child is keyed (G2) for its git tools; it still may not submit children."""
    routes = FakeRoutes()
    path = _path(tmp_path, routes)
    env, work = _prepare(path, tmp_path, {**_TASK, "parent_task_id": "task_parent"})
    assert shim.SPOOL_ENV in env, "a child offered git tools was given no git spool"
    assert children_mod.CHILDREN_ENV not in env, "a child was offered the child path"
    assert not path.offered
    # A child request the agent writes anyway is never relayed.
    requests = children_mod.ChildPath.spool(work) / "requests"
    requests.mkdir(parents=True)
    (requests / "one.json").write_text(json.dumps(
        {"request_id": "one", "runner_profile": "mock", "input": {"prompt": "x"}}))
    assert not path.has_requests(work)
    assert path.tick(work) == 0
    assert [c for c in routes.calls if c["path"].endswith("/children")] == []


@pytest.mark.parametrize("why", ["mock profile", "no repository", "no key"])
def test_no_git_spool_when_the_attempt_is_not_offered_git_tools(tmp_path, why):
    routes = FakeRoutes()
    path = _path(tmp_path, routes, profile="mock" if why == "mock profile" else "claude-code")
    if why == "no key":
        path._key = None
        path.unavailable = "no_nonce"
    if why == "no repository":
        env = path.prepare(tmp_path / "work", task=dict(_TASK), checkout=None,
                           repository_url=None, clone_base=None,
                           private_dir=tmp_path, logs_dir=tmp_path, store=None)
    else:
        env, _ = _prepare(path, tmp_path, dict(_TASK))
    assert shim.SPOOL_ENV not in env
    assert path.git_unavailable


def test_a_git_request_is_relayed_signed_on_the_tick(tmp_path):
    routes = FakeRoutes()
    path = _path(tmp_path, routes)
    env, work = _prepare(path, tmp_path, dict(_TASK))
    spool = Path(env[shim.SPOOL_ENV])
    shim.write_request(spool, "r1", "pr_checks", {})
    assert path.has_requests(work)
    assert path.tick(work) == 1
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer == {"tool": "pr_checks", "result": {"pr": 7, "head_sha": "c" * 40,
                                                      "state": "green", "rows": []}}
    (call,) = routes.git_calls()
    assert call["path"] == "/v1/attempts/att_1/git/pr_checks"
    sent = json.loads(call["body"])
    assert (sent["attempt_id"], sent["lease_id"], sent["generation"]) == ("att_1", "lease_1", 1)
    assert not path.has_requests(work)


def test_a_fenced_git_call_raises_the_error_the_lifecycle_exits_on(tmp_path):
    routes = FakeRoutes()
    routes.fence_git = True
    path = _path(tmp_path, routes)
    env, work = _prepare(path, tmp_path, dict(_TASK))
    spool = Path(env[shim.SPOOL_ENV])
    shim.write_request(spool, "r1", "pr_checks", {})
    with pytest.raises(children_mod.ChildSubmitFenced):
        path.tick(work)
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer["refused"]["code"] == "fenced"


def test_preparing_again_keeps_the_relay(tmp_path):
    """The credential reload rebuilds the runner's environment mid-run; the
    relay -- its rate window and what it learnt origin has -- survives it."""
    path = _path(tmp_path, FakeRoutes())
    env, work = _prepare(path, tmp_path, dict(_TASK))
    relay = path._git
    again = path.prepare(work, task=dict(_TASK), checkout=work / "repo",
                         repository_url="https://github.com/acme/widgets.git",
                         clone_base="0" * 40, private_dir=tmp_path / "private",
                         logs_dir=tmp_path / "logs", store=None)
    assert again[shim.SPOOL_ENV] == env[shim.SPOOL_ENV]
    assert path._git is relay


# --------------------------------------------------------------------------
# The agent's own commits are its own (GITTOOLS-AUTHOR, part of #1225)
# --------------------------------------------------------------------------
#
# Measured on every release from 2026-10-18 14:51Z: acceptance submits as a
# service account, so its agent commits as `BOT_GIT_IDENTITY` -- what
# `git_identity_env` hands the runner -- while the relay was configured with
# the worker's own `git_author_*`, and refused every mid-task push
# foreign_author. These commit the way the runner does (no `user.*`, the
# runner's GIT_AUTHOR_*/GIT_COMMITTER_* from the same task document) and push
# through the relay `ChildPath` builds.

_SUBMITTER = "ada@example.invalid"
_ROBOT = "swarm-accept@project.iam.gserviceaccount.com"

_IDENTITY_TASKS = {
    "display name": {"submitted_by": _SUBMITTER, "metadata": {"dispatch": {
        "strategy": "direct-pr",
        "git_identity": {"name": "Ada Lovelace", "email": _SUBMITTER}}}},
    "bare submitter": {"submitted_by": _SUBMITTER,
                       "metadata": {"dispatch": {"strategy": "direct-pr"}}},
    "service account": {"submitted_by": _ROBOT,
                        "metadata": {"dispatch": {"strategy": "direct-pr"}}},
    "service account with the bot's record": {"submitted_by": _ROBOT, "metadata": {"dispatch": {
        "strategy": "direct-pr",
        "git_identity": {"name": "SwarmCloud", "email": "swarmcloud@users.noreply.github.com"}}}},
    "no submitter": dict(_TASK),
}


def _commit_as(repo: Path, env: dict[str, str], name: str) -> None:
    """`git commit` in the agent's clone with `env` and no identity config."""
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
    clean.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **env})
    (repo / name).write_text(f"{name}\n")
    for argv in (["add", "-A"], ["commit", "--quiet", "-m", f"add {name}"]):
        subprocess.run(["git", *argv], cwd=str(repo), env=clean, check=True,
                       capture_output=True, text=True)


def _push_answer(path, work: Path) -> dict[str, Any]:
    """What the relay answers a push of HEAD: prepared (and discarded), or refused."""
    from agent_worker import gittools

    try:
        path._git.prepare_push(path._git.cfg.pushable[0], request_id="r1").discard()
    except gittools.GitToolRefused as exc:
        return {"refused": exc.code, "message": exc.message}
    return {"prepared": True}


@pytest.mark.parametrize("case", list(_IDENTITY_TASKS))
def test_a_commit_made_the_way_the_runner_makes_it_is_the_tasks_own(tmp_path, case):
    from agent_worker.gitidentity import git_identity_env

    task = _IDENTITY_TASKS[case]
    path = _path(tmp_path, FakeRoutes())
    _, work = _prepare(path, tmp_path, task)
    _commit_as(work / "repo", git_identity_env(task), "work.txt")
    assert _push_answer(path, work) == {"prepared": True}


@pytest.mark.parametrize("case", list(_IDENTITY_TASKS))
def test_a_foreign_commit_is_still_refused_foreign_author(tmp_path, case):
    task = _IDENTITY_TASKS[case]
    path = _path(tmp_path, FakeRoutes())
    _, work = _prepare(path, tmp_path, task)
    mallory = {"GIT_AUTHOR_NAME": "Mallory", "GIT_AUTHOR_EMAIL": "mallory@example.invalid",
               "GIT_COMMITTER_NAME": "Mallory", "GIT_COMMITTER_EMAIL": "mallory@example.invalid"}
    _commit_as(work / "repo", mallory, "foreign.txt")
    assert _push_answer(path, work)["refused"] == "foreign_author"


def test_a_persons_task_does_not_accept_a_commit_as_the_bot(tmp_path):
    """Accepting the bot's identity for a task that names nobody must not let
    it through on a task that names a person: there it is someone else."""
    from agent_worker.gitidentity import BOT_GIT_IDENTITY

    path = _path(tmp_path, FakeRoutes())
    _, work = _prepare(path, tmp_path, _IDENTITY_TASKS["display name"])
    name, email = BOT_GIT_IDENTITY
    _commit_as(work / "repo", {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email},
               "bot.txt")
    assert _push_answer(path, work)["refused"] == "foreign_author"


def test_a_commit_naming_the_person_under_the_bots_committer_is_refused(tmp_path):
    """One identity's author with another's committer is a pair nothing writes."""
    from agent_worker.gitidentity import BOT_GIT_IDENTITY

    path = _path(tmp_path, FakeRoutes())
    _, work = _prepare(path, tmp_path, _IDENTITY_TASKS["display name"])
    name, email = BOT_GIT_IDENTITY
    _commit_as(work / "repo", {"GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": _SUBMITTER,
                               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email},
               "mixed.txt")
    assert _push_answer(path, work)["refused"] == "foreign_author"


# --------------------------------------------------------------------------
# A whole attempt
# --------------------------------------------------------------------------

AGENT = r'''
import json, os, sys, time, pathlib
plan = json.load(open(sys.argv[1]))
spool = os.environ.get("SWARM_GIT")
seen = {"git": spool, "children": os.environ.get("SWARM_CHILDREN"), "env": dict(os.environ)}
if spool and plan.get("call"):
    root = pathlib.Path(spool)
    tmp = root / "requests" / "r1.tmp"
    tmp.write_text(json.dumps({"tool": "pr_checks", "arguments": {}}))
    os.replace(tmp, root / "requests" / "r1.json")
    deadline = time.time() + float(plan.get("wait", 8))
    answer = root / "responses" / "r1.json"
    while time.time() < deadline and not answer.exists():
        time.sleep(0.1)
    seen["answer"] = json.loads(answer.read_text()) if answer.exists() else None
with open(plan["record"], "a") as handle:
    handle.write(json.dumps(seen) + "\n")
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None, "metrics": {},
     "artifacts": []}))
'''


@pytest.fixture
def agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    plan_path = tmp_path / "plan.json"
    record = tmp_path / "record.jsonl"
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda cfg: [sys.executable, str(script), str(plan_path)])

    def write_plan(**plan: Any) -> None:
        plan_path.write_text(json.dumps({"record": str(record), **plan}))

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in record.read_text().splitlines()] \
            if record.exists() else []

    write_plan()
    return write_plan, records


@pytest.fixture
def attempt(db, worker_factory, monkeypatch, origin, local_urls, forge):  # noqa: F811
    """A claude-code attempt on a real clone, keyed, with the git routes faked."""
    routes = FakeRoutes()
    routes.db = db

    def build(*, parent: str | None = None):
        seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "push it"})
        # The tenant's provider secret in its API-key form, a JSON object
        # naming ANTHROPIC_API_KEY. A SUBSCRIPTION is `CLAUDE_CODE_OAUTH_TOKEN`
        # by the CLI's own name, and a bare secret is exported under every
        # name the profile declares (`secrets.resolve_credentials`), so those
        # two forms put that name in the runner's environment whatever this
        # lane does: the owner's question in the PR. What #1225 keeps out is a
        # FORGE token, and nothing the git tools add may match either.
        seed_tenant(db, credentials=["anthropic"])
        task = db.doc("tasks/task_1")
        # `collect`: nothing is published at the end, so the attempt's own
        # outcome is the wiring's. The git tools are offered all the same; only
        # a push would be `strategy_pushes_nothing`.
        task["metadata"] = {"dispatch": {"strategy": "collect"}}
        task["repository_url"] = f"file://{origin}"
        if parent:
            task["parent_task_id"] = parent
        worker, _, _ = worker_factory(
            runner_profile="claude-code", repository_url=f"file://{origin}",
            child_api=routes, child_nonce=NONCE, swarm_api_url="https://swarm-api.example",
            secret_client=_ApiKeySecrets(),
        )
        monkeypatch.setattr(worker, "_git_token", lambda: None)
        return worker

    return build, routes


class _ApiKeySecrets:
    """Secret Manager holding the tenant's API key as a JSON secret. The value
    is built at runtime (CLAUDE.md: nothing added may look like a credential)."""

    def access(self, secret_name: str, version: str = "latest") -> str:
        return json.dumps({"ANTHROPIC_API_KEY": "fake-" + "q" * 32 + secret_name[-4:]})


def _no_token(env: dict[str, str]) -> list[str]:
    return sorted(name for name, value in env.items()
                  if _TOKEN.search(name) or _TOKEN.search(str(value)))


def test_an_attempt_offered_git_tools_relays_a_call_and_its_env_holds_no_token(
    db, attempt, agent
):
    build, routes = attempt
    write_plan, records = agent
    write_plan(call=True)
    worker = build()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    (seen,) = records()
    assert seen["git"] and seen["git"].endswith(shim.SPOOL_DIR_NAME)
    assert seen["answer"] == {"tool": "pr_checks", "result": {
        "pr": 7, "head_sha": "c" * 40, "state": "green", "rows": []}}
    assert len(routes.git_calls()) == 1
    # #1225 acceptance: `env | grep -i token` is empty in the runner.
    assert _no_token(seen["env"]) == [], "the runner's environment names a token"
    assert NONCE not in "\n".join(seen["env"].values())


def test_a_child_attempt_gets_git_tools_but_not_swarm_children(db, attempt, agent):
    build, _ = attempt
    _, records = agent
    worker = build(parent="task_parent")
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    (seen,) = records()
    assert seen["git"], "a child offered git tools was given no SWARM_GIT"
    assert seen["children"] is None, "a child was given SWARM_CHILDREN"


def test_a_fenced_git_call_ends_the_attempt_fenced_and_leaves_the_lease(
    db, attempt, agent
):
    """Invariant 5: a superseded worker ends as a fenced one does, lease untouched."""
    build, routes = attempt
    write_plan, _ = agent
    write_plan(call=True, wait=6)
    routes.fence_git = True
    worker = build()
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None
    assert len(routes.git_calls()) == 1
