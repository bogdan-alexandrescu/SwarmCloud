"""Graph snapshot staging and the stored-setting gate (lane CTXQ-S).

docs/design/context-quality.md §4.4, §4.7 and §7 (CTXQ-S's row): staged only
for a format >= 3 graph whose commit is an ancestor of the step's base;
`swarm-graph` is named in the agent's MCP configuration only when staged; the
A/B arm is deterministic; a staging failure never fails the attempt.

Offline: a LocalObjectStore, the worker's FakeFirestore and a local git repo.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import graphstage, indexrun, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.graphmcp import server as graph_server
from agent_worker.graphmcp.freshness import Freshness
from agent_worker.graphmcp.snapshot import Snapshot, load_freshness_record

from worker_seeds import TENANT, seed_attempt

URL = "https://github.com/acme/widgets"
REPO_ID = indexrun.repo_id_for(TENANT, "acme", "widgets")
ROOT = f"tenants/{TENANT}/repos/{REPO_ID}/graph"


# --------------------------------------------------------------------------
# fixtures: a clone, a promoted graph, a registration
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                               "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo)})
    return done.stdout.decode().strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str, str]:
    """work/repo with two commits: (checkout, first, second)."""
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    first = _commit(repo, "a.py", "def f():\n    return 1\n")
    second = _commit(repo, "b.py", "def g():\n    return 2\n")
    return repo, first, second


def _promote(db: Any, store: Any, sha: str, *, fmt: int = 3, current: bool = True,
             tenant: str = TENANT, relation: dict | None = None,
             registration: dict | None = None) -> str:
    """Upload a graph of `sha` as KG2 writes one and record its version; its digest."""
    row = {"id": "a.py#f", "path": "a.py", "name": "f", "kind": "function", "line": 1}
    blob = gzip.compress(json.dumps(row, sort_keys=True).encode() + b"\n", mtime=0)
    hexdigest = hashlib.sha256(blob).hexdigest()
    manifest = {
        "schema": "swarm.repo-graph-manifest/v1", "tenant_id": TENANT, "repo_id": REPO_ID,
        "commit_sha": sha, "format_version": fmt, "compression": "gzip",
        "blob_suffix": ".jsonl.gz", "term_stats": {},
        "shards": {"symbols": {".": {"blob": f"sha256:{hexdigest}"}}},
        "index_shards": {},
    }
    data = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    store.upload_bytes(f"{ROOT}/{sha}/manifest.json", data)
    store.upload_bytes(f"{ROOT}/blobs/{hexdigest}.jsonl.gz", blob)
    index: dict[str, Any] = {"current_sha": sha if current else None}
    if relation is not None:
        index["head_relation"] = relation
    db.seed(f"repositories/{REPO_ID}", {"tenant_id": tenant, "repo_id": REPO_ID,
                                         "index": index, **(registration or {})})
    db.seed(f"repositories/{REPO_ID}/index_versions/{sha}", {
        "commit_sha": sha, "graph_digest": digest, "built_at": "2026-10-14T00:00:00Z"})
    return digest


def _stage(db: Any, store: Any, checkout: Path, base: str, *,
           task_id: str = "task_b") -> graphstage.StageResult:
    return graphstage.stage(db=db, store=store, tenant_id=TENANT, repository_url=URL,
                            task_id=task_id, checkout=checkout, base_sha=base,
                            context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME)


def _arm_task(arm: str) -> str:
    return next(f"task_{i}" for i in range(1000) if graphstage.assign_arm(f"task_{i}") == arm)


def _code_intelligence(db: Any, **fields: Any) -> None:
    """Merge `fields` into the workspace's stored `code_intelligence`, in
    `workspace_settings`, where swarm-api's Code intelligence screen writes
    `context`, `context_ab` and `graph_delivery` alike."""
    path = f"workspace_settings/{TENANT}"
    doc = dict(db.documents.get(path) or {})
    doc["code_intelligence"] = {**(doc.get("code_intelligence") or {}), **fields}
    db.seed(path, doc)


def _no_ab(db: Any) -> None:
    _code_intelligence(db, context_ab="off")


# --------------------------------------------------------------------------
# staged only for format >= 3 and an ancestor sha
# --------------------------------------------------------------------------


def test_a_format_3_graph_of_an_ancestor_is_staged_beside_the_checkout(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    digest = _promote(db, store, first)

    result = _stage(db, store, checkout, second)

    assert result.staged, result.reason
    snapshot = checkout.parent / graphstage.CONTEXT_DIR_NAME / graphstage.SNAPSHOT_DIR_NAME
    assert result.snapshot == snapshot
    # Outside the checkout: nothing staged reaches the harvested patch.
    assert checkout not in snapshot.parents
    record = load_freshness_record(snapshot)
    assert record == {"index_sha": first, "base_sha": second, "behind_by": 1,
                      "manifest_digest": digest, "basis": "git", "changed": ["b.py"]}
    # What swarm-graph opens, it can read: the manifest checked against the
    # digest, the blob against its name.
    graph = Snapshot(snapshot, manifest_digest=record["manifest_digest"])
    assert graph.format_version == 3
    assert graph.symbol("a.py#f")["name"] == "f"
    fresh = Freshness.establish(graph.commit_sha, workdir=checkout, record=record)
    assert fresh.basis == "staged" and fresh.behind_by == 1


def test_a_format_2_graph_is_not_staged(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first, fmt=2)

    result = _stage(db, store, checkout, second)

    assert not result.staged
    assert "format 2" in result.reason
    assert not (checkout.parent / graphstage.CONTEXT_DIR_NAME / "snapshot").exists()


def test_a_graph_of_a_commit_that_is_not_an_ancestor_is_never_staged(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _git(checkout, "checkout", "-q", "-b", "rewritten", first)
    other = _commit(checkout, "c.py", "x = 3\n")
    _git(checkout, "checkout", "-q", "main")
    _promote(db, store, other)

    result = _stage(db, store, checkout, second)

    assert not result.staged
    assert "not an ancestor" in result.reason


def test_ancestry_that_cannot_be_shown_is_not_staged_and_a_recorded_compare_shows_it(
        db, store, clone):
    """A depth-1 lane clone lacks the index commit: the compare swarm-api
    recorded for exactly this index and this base is the proof, and nothing
    less is."""
    checkout, _first, second = clone
    _no_ab(db)
    absent = "e" * 40
    _promote(db, store, absent)
    result = _stage(db, store, checkout, second)
    assert not result.staged and "cannot be shown" in result.reason

    _promote(db, store, absent, relation={"base": absent, "head": second, "status": "ahead",
                                          "ahead_by": 4, "files": ["b.py"]})
    result = _stage(db, store, checkout, second)
    assert result.staged, result.reason
    assert result.facts["basis"] == "recorded" and result.facts["behind_by"] == 4

    _promote(db, store, absent, relation={"base": absent, "head": second,
                                          "status": "diverged", "ahead_by": 4, "files": []})
    result = _stage(db, store, checkout, second)
    assert not result.staged and "not an ancestor" in result.reason


def test_the_base_commit_itself_is_staged_with_nothing_changed(db, store, clone):
    checkout, _first, second = clone
    _no_ab(db)
    _promote(db, store, second)
    result = _stage(db, store, checkout, second)
    assert result.staged, result.reason
    assert load_freshness_record(result.snapshot)["changed"] == []


@pytest.mark.parametrize("damage", ["manifest", "blob"])
def test_a_rewritten_manifest_or_blob_is_refused_not_staged(db, store, clone, damage):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    if damage == "manifest":
        key = f"{ROOT}/{first}/manifest.json"
        store.upload_bytes(key, store.download_bytes(key).replace(b'"gzip"', b'"gzip" '))
    else:
        (key,) = [k for k in store.list_keys(f"{ROOT}/blobs/")]
        store.upload_bytes(key, gzip.compress(b'{"id": "other"}\n'))

    result = _stage(db, store, checkout, second)

    assert not result.staged
    assert "match" in result.reason
    assert not (checkout.parent / graphstage.CONTEXT_DIR_NAME / "snapshot").exists()


def test_another_tenants_registration_is_never_read_from(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first, tenant="other")
    result = _stage(db, store, checkout, second)
    assert not result.staged and "not this tenant's" in result.reason


# --------------------------------------------------------------------------
# the stored setting (§4.7, owner decision Q1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("override,workspace,expected,source", [
    (None, None, "on", "default"),
    ("inherit", None, "on", "default"),
    (None, "off", "off", "workspace"),
    ("inherit", "off", "off", "workspace"),
    ("off", "on", "off", "repository"),
    ("on", "off", "on", "repository"),
    ("sometimes", "maybe", "on", "default"),
])
def test_the_repository_override_wins_over_the_workspace_default(
        override, workspace, expected, source):
    settings = {"code_intelligence": {"context": workspace, "context_ab": "off"}}
    decision = graphstage.decide(repository_override=override, workspace_settings=settings,
                                 task_id="task_x")
    assert (decision.context, decision.source) == (expected, source)
    assert decision.stage is (expected == "on")
    assert decision.arm is None


def test_a_repository_turned_off_stages_nothing_and_says_nothing(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first, registration={"context": "off"})
    env = graphstage.prepare(checkout.parent, profile_name="claude-code",
                             runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                             db=db, store=store, tenant_id=TENANT, repository_url=URL,
                             task_id="task_b", checkout=checkout, base_sha=second)
    assert env == {}
    record = json.loads((checkout.parent / graphstage.CONTEXT_DIR_NAME /
                         graphstage.STAGE_RECORD_NAME).read_text())
    assert record["staged"] is False and record["decision"]["source"] == "repository"


def test_nothing_in_the_task_input_is_read_by_the_decision():
    """Invariant 10: `decide` takes the two stored documents, the task id and
    the SIGNED dispatch block's on/off switch (the owner's decision,
    2026-11-01): never the task's input, and `prepare` reads the switch only
    through `switch_of`, which accepts nothing but the two words."""
    import inspect

    assert set(inspect.signature(graphstage.decide).parameters) == {
        "repository_override", "workspace_settings", "task_id", "switch"}
    assert graphstage.switch_of({"code_intelligence": "always"}, "code_intelligence") is None


# --------------------------------------------------------------------------
# the A/B arm (§6.2)
# --------------------------------------------------------------------------


def test_arm_assignment_is_deterministic_and_splits_the_tasks():
    ids = [f"task_{i:04d}" for i in range(400)]
    first = [graphstage.assign_arm(i) for i in ids]
    assert first == [graphstage.assign_arm(i) for i in ids]
    # sha256(task_id) mod 2, computed independently.
    assert first == ["B" if int(hashlib.sha256(i.encode()).hexdigest(), 16) % 2 else "A"
                     for i in ids]
    assert 150 < first.count("A") < 250


def test_under_the_ab_arm_a_stages_nothing_and_arm_b_stages(db, store, clone):
    checkout, first, second = clone
    _code_intelligence(db, context_ab="ab")
    _promote(db, store, first)
    argv = ["python", "-m", "agent_worker.runners.claude_code"]

    def prepare(task_id: str) -> tuple[dict[str, str], dict]:
        env = graphstage.prepare(checkout.parent, profile_name="claude-code",
                                 runner_argv=argv, db=db, store=store, tenant_id=TENANT,
                                 repository_url=URL, task_id=task_id, checkout=checkout,
                                 base_sha=second)
        stage = json.loads((checkout.parent / graphstage.CONTEXT_DIR_NAME /
                            graphstage.STAGE_RECORD_NAME).read_text())
        return env, stage

    env, record = prepare(_arm_task("A"))
    # Arm A is today's runner: no server, and no line saying one is missing.
    assert env == {}
    assert record["decision"]["arm"] == "A" and record["staged"] is False
    assert not (checkout.parent / graphstage.CONTEXT_DIR_NAME / "snapshot").exists()

    env, record = prepare(_arm_task("B"))
    assert env[graphstage.SNAPSHOT_ENV].endswith("/.swarm-context/snapshot")
    assert record["decision"]["arm"] == "B" and record["staged"] is True


def test_the_default_runs_the_ab_until_the_thresholds_pass():
    decision = graphstage.decide(repository_override=None, workspace_settings=None,
                                 task_id=_arm_task("A"))
    assert graphstage.DEFAULT_CONTEXT == "on" and graphstage.DEFAULT_AB == "ab"
    assert decision.arm == "A" and decision.stage is False


# --------------------------------------------------------------------------
# a staging failure never fails the attempt
# --------------------------------------------------------------------------


class _Unreachable:
    def collection(self, name: str) -> Any:
        raise RuntimeError("firestore unavailable")


class _BrokenStore:
    def download_bytes(self, key: str) -> bytes:
        raise OSError("gcs unavailable")


def test_an_unreachable_firestore_stages_nothing_and_raises_nothing(clone, store):
    checkout, _first, second = clone
    env = graphstage.prepare(checkout.parent, profile_name="claude-code",
                             runner_argv=["python", "-m", "agent_worker.runners.codex"],
                             db=_Unreachable(), store=store, tenant_id=TENANT,
                             repository_url=URL, task_id="task_b", checkout=checkout,
                             base_sha=second)
    assert env == {}


def test_an_unreadable_graph_says_so_in_one_prompt_line(db, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, _store_for_seed(checkout), first)
    env = graphstage.prepare(checkout.parent, profile_name="claude-code",
                             runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                             db=db, store=_BrokenStore(), tenant_id=TENANT,
                             repository_url=URL, task_id="task_b", checkout=checkout,
                             base_sha=second)
    assert set(env) == {graphstage.UNSTAGED_ENV}
    note = graphstage.unstaged_note(env)
    assert note is not None and "OSError" in note and "\n" not in note


def _store_for_seed(checkout: Path) -> Any:
    from agent_worker.objectstore import LocalObjectStore

    return LocalObjectStore(checkout.parent.parent / "seed-gcs")


@pytest.mark.parametrize("profile,argv,checkout", [
    ("indexer", ["python", "-m", "agent_worker.runners.claude_code"], True),
    ("mock", ["python", "-m", "agent_worker.runners.mock"], True),
    ("claude-code", ["python", "-m", "agent_worker.runners.claude_code"], False),
])
def test_an_index_run_another_runner_or_no_checkout_stages_nothing(
        db, store, clone, profile, argv, checkout):
    repo, first, second = clone
    _promote(db, store, first)
    env = graphstage.prepare(repo.parent, profile_name=profile, runner_argv=argv, db=db,
                             store=store, tenant_id=TENANT, repository_url=URL,
                             task_id="task_b", checkout=repo if checkout else None,
                             base_sha=second)
    assert env == {}
    assert not (repo.parent / graphstage.CONTEXT_DIR_NAME).exists()


def test_a_staging_error_in_the_lifecycle_does_not_fail_the_attempt(
        db, worker_factory, monkeypatch):
    calls: list[str] = []

    def explode(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        calls.append("prepare")
        raise RuntimeError("staging exploded")

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", explode)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert calls == ["prepare"], "the lifecycle never asked to stage"


# --------------------------------------------------------------------------
# the runners name swarm-graph only when staged
# --------------------------------------------------------------------------


def _ctx(work: Path, repo: Path | None = None):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=work, artifacts_dir=work / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Implement it."}, repo_dir=repo)


def _run_body(module: Any, monkeypatch: pytest.MonkeyPatch, work: Path,
              env: dict[str, str]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["prompt"] = ctx.payload.get("prompt")
        return {}

    monkeypatch.setattr(module, "run_cli_agent", fake_run)
    for name in ("SWARM_GIT", "SWARM_CHILDREN", "SWARM_SEARCH", "PLAYWRIGHT_BROWSERS_PATH",
                 "SWARM_CDP_ENDPOINT", graphstage.SNAPSHOT_ENV, graphstage.CONTEXT_ENV, graphstage.UNSTAGED_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    repo = work / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    module.body(_ctx(work, repo))
    return seen


def _mcp_servers(seen: dict[str, Any]) -> dict[str, Any] | None:
    args = seen["args"]
    if "--mcp-config" not in args:
        return None
    path = Path(args[args.index("--mcp-config") + 1])
    return json.loads(path.read_text())["mcpServers"]


def test_claude_code_names_swarm_graph_only_when_staged(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    snapshot = str(work / ".swarm-context" / "snapshot")
    seen = _run_body(claude_code, monkeypatch, work, {
        graphstage.SNAPSHOT_ENV: snapshot, graphstage.CONTEXT_ENV: str(work / ".swarm-context")})
    servers = _mcp_servers(seen)
    assert servers == {graphstage.SERVER_NAME: {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", "-m", "agent_worker.graphmcp", "--snapshot", snapshot,
                 "--workdir", str(work / "repo")]}}
    assert seen["env"][graphstage.CONTEXT_ENV] == str(work / ".swarm-context")
    # Its one line on using the graph first (LANE CAPS, part of #2524), and nothing else.
    assert seen["prompt"] == ("Implement it.\n\n"
                              + claude_code.exploration_note(graph=True, children=""))

    # The control: nothing staged, no server and no config at all.
    seen = _run_body(claude_code, monkeypatch, work, {})
    assert _mcp_servers(seen) is None
    assert graphstage.CONTEXT_ENV not in seen["env"]


def test_claude_code_says_in_one_line_when_the_setting_asked_and_nothing_was_staged(
        monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(claude_code, monkeypatch, tmp_path / "work",
                     {graphstage.UNSTAGED_ENV: "the graph is format 2"})
    assert _mcp_servers(seen) is None
    assert seen["prompt"].startswith("Implement it.\n\nNo repository graph was staged")
    assert "the graph is format 2" in seen["prompt"]


def test_codex_names_swarm_graph_only_when_staged(monkeypatch, tmp_path):
    from agent_worker.runners import codex

    work = tmp_path / "work"
    snapshot = str(work / ".swarm-context" / "snapshot")
    seen = _run_body(codex, monkeypatch, work, {graphstage.SNAPSHOT_ENV: snapshot})
    overrides = [seen["args"][i + 1] for i, a in enumerate(seen["args"]) if a == "-c"]
    argv = ["-I", "-m", "agent_worker.graphmcp", "--snapshot", snapshot,
            "--workdir", str(work / "repo")]
    assert overrides == [
        f"mcp_servers.{graphstage.SERVER_NAME}.command={json.dumps(sys.executable)}",
        f"mcp_servers.{graphstage.SERVER_NAME}.args={json.dumps(argv)}"]

    seen = _run_body(codex, monkeypatch, work, {})
    assert seen["args"] == [] and seen["prompt"] == "Implement it."

    seen = _run_body(codex, monkeypatch, work, {graphstage.UNSTAGED_ENV: "no index"})
    assert seen["args"] == [] and "staged for this step (no index)" in seen["prompt"]


def test_the_server_name_is_the_servers_own():
    assert graphstage.SERVER_NAME == graph_server.SERVER_NAME


def test_the_configured_command_answers_mcp_over_the_staged_snapshot(db, store, clone):
    """Not only a name in a file: the argv the CLI runs serves the staged graph."""
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    result = _stage(db, store, checkout, second)
    assert result.staged, result.reason
    argv = graphstage.server_argv(str(result.snapshot), str(checkout))
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "context", "arguments": {"symbol": "a.py#f"}}})
    done = subprocess.run(argv, input=(request + "\n").encode(), capture_output=True,
                          timeout=60, check=False, cwd=str(checkout),
                          env={"PYTHONPATH": ":".join(sys.path), "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr.decode()
    answer = json.loads(done.stdout.decode().splitlines()[0])
    text = answer["result"]["content"][0]["text"]
    assert first in text


# --------------------------------------------------------------------------
# the timer and the facts the `graph_ready` mark carries (lane GS-MEASURE,
# docs/design/graph-service.md §9.1 and §9.3)
# --------------------------------------------------------------------------


class _Clock:
    """A monotonic clock that advances `step` seconds per reading."""

    def __init__(self, start: float = 100.0, step: float = 2.5) -> None:
        self.now, self.step, self.readings = start, step, 0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        self.readings += 1
        return value


def test_staging_is_timed_around_the_whole_decision(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    clock = _Clock(step=2.5)

    result = graphstage.stage(db=db, store=store, tenant_id=TENANT, repository_url=URL,
                              task_id="task_b", checkout=checkout, base_sha=second,
                              context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME,
                              clock=clock)

    assert result.staged, result.reason
    # One reading before, one after: the seconds are exactly their difference.
    assert clock.readings == 2
    assert result.seconds == 2.5
    assert result.delivery == graphstage.DELIVERY_STAGED == "staged"


def test_the_facts_count_every_byte_and_blob_downloaded(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)

    result = _stage(db, store, checkout, second)

    assert result.staged, result.reason
    snapshot = checkout.parent / graphstage.CONTEXT_DIR_NAME / graphstage.SNAPSHOT_DIR_NAME
    on_disk = (snapshot / "manifest.json").stat().st_size + sum(
        p.stat().st_size for p in (snapshot / "blobs").iterdir())
    assert result.facts["blobs"] == 1
    assert result.facts["bytes"] == on_disk
    assert isinstance(result.seconds, float) and result.seconds >= 0.0


def test_stage_json_records_the_delivery_and_the_seconds(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    graphstage.prepare(checkout.parent, profile_name="claude-code",
                       runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                       db=db, store=store, tenant_id=TENANT, repository_url=URL,
                       task_id="task_b", checkout=checkout, base_sha=second)
    record = json.loads((checkout.parent / graphstage.CONTEXT_DIR_NAME /
                         graphstage.STAGE_RECORD_NAME).read_text())
    assert record["delivery"] == "staged"
    assert isinstance(record["seconds"], float) and record["seconds"] >= 0.0
    assert record["blobs"] == 1 and record["bytes"] > 0


def test_a_staging_that_delivered_nothing_is_timed_and_says_none(db, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, _store_for_seed(checkout), first)
    clock = _Clock(step=0.75)

    result = graphstage.stage(db=db, store=_BrokenStore(), tenant_id=TENANT,
                              repository_url=URL, task_id="task_b", checkout=checkout,
                              base_sha=second,
                              context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME,
                              clock=clock)

    assert not result.staged and "OSError" in result.reason
    assert result.seconds == 0.75
    assert result.delivery == graphstage.DELIVERY_NONE == "none"
    assert result.record()["delivery"] == "none" and result.record()["seconds"] == 0.75


def test_a_failure_outside_the_decision_is_timed_too(clone):
    checkout, _first, second = clone
    clock = _Clock(step=1.25)
    result = graphstage.stage(db=_Unreachable(), store=None, tenant_id=TENANT,
                              repository_url=URL, task_id="task_b", checkout=checkout,
                              base_sha=second,
                              context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME,
                              clock=clock)
    assert not result.staged and result.reason == "staging failed (RuntimeError)"
    assert result.seconds == 1.25 and result.delivery == "none"


# --------------------------------------------------------------------------
# GS-WORKER: pin, ticket, probe, and the fallback to staging
# (docs/design/graph-service.md §4.1, §8; lane GS-WORKER, part of #1384)
# --------------------------------------------------------------------------

CLAUDE = ["python", "-m", "agent_worker.runners.claude_code"]
GKE, CLOUD_RUN_JOB = "GKE_AUTOPILOT", "CLOUD_RUN_JOB"
SERVICE_URL = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"


class _FakeTicket:
    """`graph_ticket.GraphTicket`'s two calls graphstage makes, recorded."""

    def __init__(self, outcome: str | None = None, error: Exception | None = None) -> None:
        self.outcome, self.error = outcome, error
        self.started: list[str] = []
        self.discarded = 0
        self.path: Path | None = None

    def start(self, private_dir: Path, index_sha: str) -> str | None:
        self.started.append(index_sha)
        if self.error is not None:
            raise self.error
        if self.outcome is None:
            self.path = Path(private_dir) / "graph-ticket"
            self.path.write_text("t." + "x" * 8)
        return self.outcome

    def discard(self) -> None:
        self.discarded += 1
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def _deliver(db: Any, value: Any) -> None:
    """The workspace's stored `graph_delivery`, where swarm-api writes it."""
    _code_intelligence(db, graph_delivery=value)


def _prepare(db: Any, store: Any, checkout: Path, base: str, *, ticket: Any,
             probe: Any = None, backend: str = GKE, mark: Any = None) -> dict[str, str]:
    private = checkout.parent.parent / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    return graphstage.prepare(
        checkout.parent, profile_name="claude-code", runner_argv=CLAUDE, db=db, store=store,
        tenant_id=TENANT, repository_url=URL, task_id="task_b", checkout=checkout,
        base_sha=base, graph_ticket=ticket, private_dir=private, backend=backend,
        service_url=SERVICE_URL, probe=probe or (lambda url, path: None), mark=mark)


def _context(checkout: Path) -> Path:
    return checkout.parent / graphstage.CONTEXT_DIR_NAME


def _record(checkout: Path) -> dict[str, Any]:
    return json.loads((_context(checkout) / graphstage.STAGE_RECORD_NAME).read_text())


def test_the_names_are_the_ones_swarm_api_and_the_pack_use():
    from agent_worker import contextpack
    from agent_worker.contextpack import build as pack_build
    from agent_worker.graphmcp import shim
    from swarm_api import codeintel

    assert graphstage.DELIVERY_FIELD == codeintel.DELIVERY_FIELD
    assert graphstage.DEFAULT_GRAPH_DELIVERY == codeintel.DEFAULT_GRAPH_DELIVERY == "staged"
    assert (graphstage.DELIVERY_STAGED, graphstage.DELIVERY_SERVICE) == codeintel.DELIVERIES
    assert graphstage.DELIVERY_SETTINGS_COLLECTION == codeintel.COLLECTION
    assert graphstage.PIN_NAME == contextpack.PIN_FILE
    assert graphstage.PIN_MANIFEST_NAME == contextpack.PIN_MANIFEST_FILE
    assert graphstage.DELIVERY_SERVICE == contextpack.DELIVERY_SERVICE
    assert graphstage.TICKET_FILE_ENV == pack_build.TICKET_FILE_ENV
    assert graphstage.URL_ENV == pack_build.SERVICE_URL_ENV == shim.URL_ENV


@pytest.mark.parametrize("stored", [None, "staged", "inherit", "SERVICE ", 7])
def test_the_default_stages_exactly_as_before_and_never_asks_for_a_ticket(
        db, store, clone, stored):
    """THE GUARD: `graph_delivery` unset, `staged` or malformed is today's path."""
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    if stored is not None:
        _deliver(db, stored)
    ticket = _FakeTicket()
    probes: list[Any] = []

    env = _prepare(db, store, checkout, second, ticket=ticket,
                   probe=lambda *a: probes.append(a))

    context = _context(checkout)
    assert env == {graphstage.CONTEXT_ENV: str(context),
                   graphstage.SNAPSHOT_ENV: str(context / graphstage.SNAPSHOT_DIR_NAME)}
    assert ticket.started == [] and probes == []
    assert not (context / graphstage.PIN_NAME).exists()
    assert list((checkout.parent.parent / "private").iterdir()) == []
    record = _record(checkout)
    assert record["delivery"] == "staged" and record["staged"] is True
    assert "fallback" not in record


def test_the_default_record_is_the_one_staging_wrote_before_this_lane(db, store, clone):
    """Byte for byte, the default's stage.json and env are those of a prepare
    that was never handed a ticket, a backend or a URL."""
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    before = graphstage.prepare(checkout.parent, profile_name="claude-code", runner_argv=CLAUDE,
                                db=db, store=store, tenant_id=TENANT, repository_url=URL,
                                task_id="task_b", checkout=checkout, base_sha=second)
    old = _record(checkout)
    after = _prepare(db, store, checkout, second, ticket=_FakeTicket())
    new = _record(checkout)
    old.pop("seconds"), new.pop("seconds")
    assert before == after and old == new


def test_service_pins_instead_of_copying_and_hands_the_runner_the_shims_variables(
        db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    digest = _promote(db, store, first)
    _deliver(db, "service")
    ticket = _FakeTicket()
    probes: list[tuple[str, Path]] = []
    marks: list[dict[str, Any]] = []

    env = _prepare(db, store, checkout, second, ticket=ticket,
                   probe=lambda url, path: probes.append((url, path)), mark=marks.append)

    context = _context(checkout)
    pin = context / graphstage.PIN_NAME
    assert env == {graphstage.CONTEXT_ENV: str(context), graphstage.PIN_ENV: str(pin),
                   graphstage.TICKET_FILE_ENV: str(ticket.path),
                   graphstage.URL_ENV: SERVICE_URL}
    assert graphstage.SNAPSHOT_ENV not in env
    assert ticket.started == [first]
    assert probes == [(SERVICE_URL, ticket.path)]
    # Nothing downloaded but the manifest: no snapshot, no blob.
    assert not (context / graphstage.SNAPSHOT_DIR_NAME).exists()
    record = json.loads(pin.read_text())
    assert record == {"index_sha": first, "base_sha": second, "behind_by": 1,
                      "manifest_digest": digest, "basis": "git", "changed": ["b.py"]}
    manifest = (context / graphstage.PIN_MANIFEST_NAME).read_bytes()
    assert "sha256:" + hashlib.sha256(manifest).hexdigest() == digest
    # The pin holds no credential: the ticket is in private/, never in work/.
    assert checkout.parent not in ticket.path.parents
    stage = _record(checkout)
    assert stage["delivery"] == "service" and stage["pin"] == str(pin)
    assert stage["blobs"] == 0 and stage["bytes"] == len(manifest)
    assert marks == [{"graph": {"delivery": "service", "seconds": stage["seconds"],
                                "bytes": len(manifest), "blobs": 0,
                                "reason": stage["reason"]}}]


def test_the_pin_is_what_the_shim_and_the_pack_open(db, store, clone):
    from agent_worker.contextpack.build import _open
    from agent_worker.graphmcp import shim

    checkout, first, second = clone
    _no_ab(db)
    digest = _promote(db, store, first)
    _deliver(db, "service")
    _prepare(db, store, checkout, second, ticket=_FakeTicket())
    context = _context(checkout)
    assert shim.load_pin(context / graphstage.PIN_NAME)["index_sha"] == first
    snap, pin, _source, delivery = _open(context, _record(checkout),
                                         lambda message: {"jsonrpc": "2.0", "id": 1})
    assert delivery == "service" and pin["manifest_digest"] == digest
    assert snap.commit_sha == first


@pytest.mark.parametrize("why", ["HTTP 503", "timed out"])
def test_a_failed_probe_stages_exactly_as_today_and_says_why(db, store, clone, why):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    ticket = _FakeTicket()
    marks: list[dict[str, Any]] = []

    env = _prepare(db, store, checkout, second, ticket=ticket,
                   probe=lambda url, path: why, mark=marks.append)

    context = _context(checkout)
    assert env == {graphstage.CONTEXT_ENV: str(context),
                   graphstage.SNAPSHOT_ENV: str(context / graphstage.SNAPSHOT_DIR_NAME)}
    # No pin left for the pack to prefer, no ticket left to renew.
    assert not (context / graphstage.PIN_NAME).exists()
    assert not (context / graphstage.PIN_MANIFEST_NAME).exists()
    assert ticket.discarded == 1 and not ticket.path.exists()
    record = _record(checkout)
    assert record["delivery"] == "staged" and record["staged"] is True
    assert why in record["fallback"] and why in record["reason"]
    assert marks[0]["graph"]["delivery"] == "staged" and why in marks[0]["graph"]["reason"]


def test_a_mint_that_fails_stages_without_a_probe(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    probes: list[Any] = []
    env = _prepare(db, store, checkout, second, ticket=_FakeTicket("graph_ticket_unavailable"),
                   probe=lambda *a: probes.append(a))
    assert graphstage.SNAPSHOT_ENV in env and probes == []
    assert "graph_ticket_unavailable" in _record(checkout)["fallback"]


def test_a_cloud_run_job_step_keeps_staging_and_mints_nothing(db, store, clone):
    """Owner Q2: a CLOUD_RUN_JOB step cannot reach the headless in-cluster Service."""
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    ticket = _FakeTicket()
    env = _prepare(db, store, checkout, second, ticket=ticket, backend=CLOUD_RUN_JOB)
    assert graphstage.SNAPSHOT_ENV in env and graphstage.PIN_ENV not in env
    assert ticket.started == []
    record = _record(checkout)
    assert record["delivery"] == "staged" and "CLOUD_RUN_JOB" in record["fallback"]


def test_without_an_upkeep_or_a_url_the_service_is_not_tried(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    env = _prepare(db, store, checkout, second, ticket=None)
    assert graphstage.SNAPSHOT_ENV in env
    assert _record(checkout)["fallback"]


def test_a_forbidden_ticket_registers_no_server_and_stages_nothing(db, store, clone):
    """§10 'context disabled': swarm-api's 403 is the stored setting saying no;
    staging behind its back would hand the agent what the setting refused."""
    from agent_worker import graph_ticket

    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    ticket = _FakeTicket(error=graph_ticket.GraphTicketForbidden("refused (forbidden)"))
    env = _prepare(db, store, checkout, second, ticket=ticket)
    assert set(env) == {graphstage.UNSTAGED_ENV}
    assert "forbidden" in env[graphstage.UNSTAGED_ENV]
    context = _context(checkout)
    assert not (context / graphstage.SNAPSHOT_DIR_NAME).exists()
    assert not (context / graphstage.PIN_NAME).exists()
    assert _record(checkout)["delivery"] == "none"


def test_a_fenced_mint_is_raised_through_prepare_for_the_lifecycle(db, store, clone):
    """Invariant 5: every other failure is a reason; a 409 is the fence."""
    from agent_worker import graph_ticket

    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    ticket = _FakeTicket(error=graph_ticket.GraphTicketFenced("graph ticket: 409 conflict"))
    with pytest.raises(graph_ticket.GraphTicketFenced):
        _prepare(db, store, checkout, second, ticket=ticket)


def test_a_restored_pin_is_not_this_attempts_when_it_stages(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    context = _context(checkout)
    context.mkdir()
    (context / graphstage.PIN_NAME).write_text("{}")
    (context / graphstage.PIN_MANIFEST_NAME).write_text("{}")
    _prepare(db, store, checkout, second, ticket=_FakeTicket())
    assert not (context / graphstage.PIN_NAME).exists()
    assert not (context / graphstage.PIN_MANIFEST_NAME).exists()


def test_a_restored_snapshot_is_not_this_attempts_when_it_is_served(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    stale = _context(checkout) / graphstage.SNAPSHOT_DIR_NAME
    stale.mkdir(parents=True)
    (stale / "manifest.json").write_text("{}")
    env = _prepare(db, store, checkout, second, ticket=_FakeTicket())
    assert graphstage.PIN_ENV in env and not stale.exists()


def test_a_rewritten_manifest_is_never_pinned_and_falls_back_to_a_refusal(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)
    _deliver(db, "service")
    store.upload_bytes(f"{ROOT}/{first}/manifest.json", b'{"rewritten": true}\n')
    ticket = _FakeTicket()
    env = _prepare(db, store, checkout, second, ticket=ticket)
    # The same check staging makes refuses the manifest before any ticket is asked.
    assert ticket.started == []
    assert set(env) == {graphstage.UNSTAGED_ENV}
    assert "no longer matches" in env[graphstage.UNSTAGED_ENV]


def test_an_unreadable_settings_document_stages_nothing_and_says_why(db, store, clone):
    """`context`, `context_ab` and `graph_delivery` are one document's now, so
    an unreadable one is a staging failure with its reason, as an unreadable
    `tenant_settings` was before 2026-11-01, never a quiet default."""
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)

    class _Settings:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def collection(self, name: str) -> Any:
            if name == graphstage.SETTINGS_COLLECTION:
                raise RuntimeError("unreachable")
            return self.inner.collection(name)

    ticket = _FakeTicket()
    env = _prepare(_Settings(db), store, checkout, second, ticket=ticket)
    assert graphstage.SNAPSHOT_ENV not in env and ticket.started == []
    assert _record(checkout)["reason"] == "staging failed (RuntimeError)"
