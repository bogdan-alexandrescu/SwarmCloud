"""`swarm upgrade --to V` (docs/design/day-one-plan.md §9.3, lane DAYONE-UPGRADE-1,
part of #2688): downgrade refused, the IAM gate, the apply order, App
permissions, every tenant's namespace, the metric split, roll back and resume.

Everything outside `swarm_install.upgrade` is a fake `UpgradeWorld`; the
install state is the real `StateStore` over a temporary file, so no test needs
credentials or an emulator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from swarm_install import migrations as mig
from swarm_install import upgrade as up
from swarm_install.events import Events
from swarm_install.exitcodes import ExitCode
from swarm_install.state import InstallState, StateStore
from swarm_install.verbs import discover
from swarm_install.verbs import upgrade as verb

PROJECT = "acme-swarm-1"
OLD, NEW = "2026.10.1", "2026.11.1"


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


POLICY_OLD = """\
kind: ValidatingAdmissionPolicy
metadata: {name: worker-sizes}
spec: {max_cpu: 4}
---
kind: ValidatingAdmissionPolicy
metadata: {name: retired}
spec: {}
"""
POLICY_NEW = """\
kind: ValidatingAdmissionPolicy
metadata: {name: worker-sizes}
spec: {max_cpu: 8}
---
kind: ValidatingAdmissionPolicy
metadata: {name: hardening}
spec: {}
"""


def iam_change(address: str, actions: list[str]) -> dict[str, Any]:
    return {"address": address, "type": "google_project_iam_member",
            "change": {"actions": actions, "after": {"role": "roles/viewer", "member": "serviceAccount:x"}}}


class FakeWorld:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.plans: dict[str, list[dict[str, Any]]] = {"bootstrap": [{}], "infra": [{}]}
        self.fail: dict[str, Exception] = {}
        self.check_results = [("digests", "met", "ok")]
        self.app_now: dict[str, str] | None = {"contents": "write"}
        self.app_release: dict[str, str] | None = {"contents": "write"}
        self.app_owner, self.app_owner_is_org = "acme", True
        self.installed: dict[str, str] | None = {"contents": "write"}
        #: Per read of the App (or the installation), what the person has saved by then.
        self.app_after: list[dict[str, str]] = []
        self.installed_after: list[dict[str, str]] = []
        self.tenant_ids = ["eng", "ops", "research"]
        self.docs = FakeDocuments()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail.pop(name)

    def fetch(self, version: str, name: str) -> bytes:
        assert name == "images.json"
        return json.dumps({"swarm-api": digest(f"api-{version}"), "scheduler": digest("scheduler")}).encode()

    def plan_root(self, root: str, version: str) -> dict[str, Any]:
        queue = self.plans[root]
        found = queue.pop(0) if len(queue) > 1 else queue[0]
        self.calls.append(("plan", root, version))
        return found

    def apply_root(self, root: str, version: str) -> None:
        self.calls.append(("apply_root", root, version))
        self._maybe_fail(f"apply_root:{root}:{version}")

    def patch_log_metric(self, address: str, after: Mapping[str, Any]) -> None:
        self.calls.append(("patch_log_metric", address))

    def copy_images(self, version: str, images: Mapping[str, str]) -> None:
        self.calls.append(("copy_images", version, dict(images)))

    def policies(self, version: str) -> str:
        return POLICY_OLD if version == OLD else POLICY_NEW

    def apply_policies(self, rendered: str) -> None:
        import yaml

        names = sorted(d["metadata"]["name"] for d in yaml.safe_load_all(rendered) if d)
        self.calls.append(("apply_policies", tuple(names)))

    def delete_policies(self, documents: Sequence[tuple[str, str]]) -> None:
        self.calls.append(("delete_policies", tuple(n for _, n in documents)))

    def app(self) -> up.GithubApp | None:
        self.calls.append(("read_app",))
        if self.app_now is None:
            return None
        if self.app_after:
            self.app_now = self.app_after.pop(0)
        return up.GithubApp(slug="acme-swarm", owner=self.app_owner, owner_is_org=self.app_owner_is_org,
                            permissions=dict(self.app_now))

    def installation(self) -> up.AppInstallation | None:
        self.calls.append(("read_installation",))
        if self.installed is None:
            return None
        if self.installed_after:
            self.installed = self.installed_after.pop(0)
        return up.AppInstallation(id=4242, account=self.app_owner, account_is_org=self.app_owner_is_org,
                                  permissions=dict(self.installed))

    def release_app_permissions(self, version: str) -> dict[str, str] | None:
        return self.app_release

    def tenants(self) -> list[str]:
        return list(self.tenant_ids)

    def apply_namespace(self, tenant: str) -> None:
        self.calls.append(("apply_namespace", tenant))
        self._maybe_fail(f"apply_namespace:{tenant}")

    def pause_dispatch(self, reason: str) -> None:
        self.calls.append(("pause_dispatch",))

    def resume_dispatch(self, reason: str) -> None:
        self.calls.append(("resume_dispatch",))

    def running(self) -> int:
        return 0

    def checks(self) -> list[tuple[str, str, str]]:
        self.calls.append(("checks",))
        self._maybe_fail("checks")
        return list(self.check_results)

    def documents(self) -> mig.Documents:
        return self.docs


class FakeDocuments:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.times: dict[str, int] = {}

    def list(self, collection: str):
        for path in sorted(self.data):
            if path.count("/") == 1 and path.startswith(collection + "/"):
                yield path, json.loads(json.dumps(self.data[path])), self.times[path]

    def get(self, path: str):
        return (dict(self.data[path]), self.times[path]) if path in self.data else None

    def update(self, path: str, fields: dict[str, Any], *, update_time: Any) -> None:
        raise AssertionError("no migration writes in these tests")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Typed:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def make(tmp_path: Path, world: FakeWorld, *, answer: str = "", release: str = OLD) -> tuple[up.Upgrade, StateStore,
                                                                                             list[str]]:
    store = StateStore(project=PROJECT, environment="prod", local=tmp_path / "state.json")
    store.save(InstallState(project=PROJECT, environment="prod", release=release, region="us-central1"))
    lines: list[str] = []
    clock = Clock()
    flow = up.Upgrade(project=PROJECT, world=world, store=store, events=Events(clock.now, out=_Sink()),
                      clock=clock, confirm=Typed(answer), out=lines.append, migrations=[],
                      open_page=Pages(world, lines))
    return flow, store, lines


class Pages:
    """`steps.open_page`'s stand-in: records the page in the world's calls, in order."""

    def __init__(self, world: FakeWorld, lines: list[str]) -> None:
        self.world = world
        self.lines = lines

    def __call__(self, url: str, *, why: str, say: Any = print) -> None:
        self.world.calls.append(("open_page", url))
        say(f"{why}\n  {url}")


class _Sink:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def steps_of(world: FakeWorld) -> list[str]:
    names = {"copy_images": up.COPY_IMAGES, "pause_dispatch": up.DRAIN, "checks": up.VERIFY}
    order = []
    for call in world.calls:
        if call[0] == "apply_root":
            order.append(f"{call[1]}:{call[2]}")
        elif call[0] in ("apply_policies", "delete_policies", "apply_namespace"):
            order.append(call[0])
        elif call[0] in names:
            order.append(names[call[0]])
    return order


# -- red first ----------------------------------------------------------------------


def test_upgrade_is_a_discovered_verb() -> None:
    assert "upgrade" in discover()
    parser = argparse.ArgumentParser()
    verb.add_arguments(parser)
    args = parser.parse_args(["--to", NEW, "--project", PROJECT])
    assert args.to == NEW and args.allow_downgrade is False


def test_downgrade_refused_exit_5(tmp_path: Path) -> None:
    world = FakeWorld()
    flow, store, _ = make(tmp_path, world, release=NEW)
    with pytest.raises(up.UpgradeRefused, match="--allow-downgrade"):
        flow.run(OLD)
    assert world.calls == [] and store.load().release == NEW
    assert verb.exit_code_for(up.UpgradeRefused("x")) == ExitCode.REFUSED == 5
    assert up.version_key("2026.11.1") > up.version_key("2026.11.1-rc.2") > up.version_key("2026.11.1-rc.1") \
        > up.version_key("2026.10.9")


def test_iam_change_waits_for_typed_project_id(tmp_path: Path) -> None:
    world = FakeWorld()
    world.plans["infra"] = [{"resource_changes": [iam_change("google_project_iam_member.api_viewer", ["create"])]}]
    flow, store, lines = make(tmp_path, world, answer="yes")
    with pytest.raises(up.UpgradeRefused, match="not approved"):
        flow.run(NEW)
    prompt = flow.confirm.prompts[0]
    assert "block the upgrade to " + NEW in prompt and PROJECT in prompt
    assert any("google_project_iam_member.api_viewer" in line and "create" in line for line in lines)
    assert not [c for c in world.calls if c[0] not in ("plan", "read_app")], "nothing is applied before the IAM approval"
    assert store.load().release == OLD

    world2 = FakeWorld()
    world2.plans["infra"] = [{"resource_changes": [iam_change("google_project_iam_member.api_viewer", ["create"])]}]
    flow2, store2, _ = make(tmp_path / "typed", world2, answer=PROJECT)
    assert flow2.run(NEW) == ExitCode.OK
    assert store2.load().release == NEW


def test_destroy_first_iam_swap_refused(tmp_path: Path) -> None:
    world = FakeWorld()
    world.plans["bootstrap"] = [{"resource_changes": [iam_change("google_project_iam_member.x", ["delete", "create"])]}]
    flow, _, _ = make(tmp_path, world, answer=PROJECT)
    with pytest.raises(up.UpgradeRefused, match="destroy-first"):
        flow.run(NEW)


def test_apply_order_is_section_9_3(tmp_path: Path) -> None:
    world = FakeWorld()
    flow, store, _ = make(tmp_path, world)
    assert flow.run(NEW) == ExitCode.OK
    assert steps_of(world) == [
        up.DRAIN, up.COPY_IMAGES, f"bootstrap:{NEW}", "delete_policies", "apply_policies", f"infra:{NEW}",
        "apply_policies", "apply_namespace", "apply_namespace", "apply_namespace", up.VERIFY,
    ]
    admit = [c for c in world.calls if c[0] in ("apply_policies", "delete_policies")]
    # changed before the new code, removed before it, added only after it (lesson 33)
    assert admit == [("delete_policies", ("retired",)), ("apply_policies", ("worker-sizes",)),
                     ("apply_policies", ("hardening", "worker-sizes"))]
    assert world.calls[-1] == ("resume_dispatch",)
    state = store.load()
    assert state.release == NEW and state.outputs[up.PREVIOUS_KEY] == OLD
    assert state.outputs[up.UPGRADE_KEY]["done"] == list(up.ORDER)


def _pages_and_drain(world: FakeWorld) -> list[tuple[Any, ...]]:
    return [c for c in world.calls if c[0] in ("open_page", "pause_dispatch")]


def test_app_permission_added_opens_the_definition_then_the_installation_and_waits(tmp_path: Path) -> None:
    """Audit 4e F5: V's new permission goes into the App's definition first,
    then is accepted on the installation; each page is opened and polled, and
    both come before the drain, so nothing new runs before they are in force."""
    world = FakeWorld()
    world.app_release = {"contents": "write", "workflows": "write"}
    # The owner saves the definition on the third read; the installation is accepted on its third.
    world.app_after = [{"contents": "write"}, {"contents": "write"}, {"contents": "write", "workflows": "write"}]
    world.installed_after = [{"contents": "write"}, {"contents": "write"}, {"contents": "write", "workflows": "write"}]
    flow, store, lines = make(tmp_path, world)
    assert flow.run(NEW) == ExitCode.OK
    assert _pages_and_drain(world) == [
        ("open_page", "https://github.com/organizations/acme/settings/apps/acme-swarm/permissions"),
        ("open_page", "https://github.com/organizations/acme/settings/installations/4242"),
        ("pause_dispatch",),
    ]
    text = "\n".join(lines)
    assert "workflows: write" in text
    assert not world.app_after and not world.installed_after, "the definition and the installation were polled"
    assert store.load().outputs[up.UPGRADE_KEY]["done"][0] == up.APP_PERMISSIONS


def test_app_permission_not_yet_accepted_never_drains(tmp_path: Path) -> None:
    world = FakeWorld()
    world.app_release = {"contents": "write", "workflows": "write"}
    world.app_now = {"contents": "write", "workflows": "write"}  # the definition already holds it
    flow, store, _ = make(tmp_path, world)
    with pytest.raises(up.WaitGaveUp):
        flow.run(NEW)
    assert _pages_and_drain(world) == [
        ("open_page", "https://github.com/organizations/acme/settings/installations/4242")]
    assert ("resume_dispatch",) not in world.calls, "dispatch was never paused"
    assert store.load().outputs[up.UPGRADE_KEY]["failed"] == up.APP_PERMISSIONS


def test_app_on_a_personal_account_uses_the_account_pages(tmp_path: Path) -> None:
    world = FakeWorld()
    world.app_owner, world.app_owner_is_org = "ada", False
    world.app_release = {"contents": "write", "workflows": "write"}
    world.app_after = [{"contents": "write"}, {"contents": "write"}, {"contents": "write", "workflows": "write"}]
    world.installed_after = [{"contents": "write"}, {"contents": "write", "workflows": "write"}]
    flow, _, _ = make(tmp_path, world)
    assert flow.run(NEW) == ExitCode.OK
    assert [c[1] for c in world.calls if c[0] == "open_page"] == [
        "https://github.com/settings/apps/acme-swarm/permissions",
        "https://github.com/settings/installations/4242",
    ]


def test_a_stronger_level_already_holds_the_permission(tmp_path: Path) -> None:
    assert up.missing_permissions({"contents": "admin"}, {"contents": "write"}) == {}
    assert up.missing_permissions({"contents": "read"}, {"contents": "write"}) == {"contents": "write"}
    world = FakeWorld()
    world.app_now = world.installed = {"contents": "admin"}
    flow, _, _ = make(tmp_path, world)
    assert flow.run(NEW) == ExitCode.OK
    assert not [c for c in world.calls if c[0] == "open_page"]


def test_without_to_the_plan_waits_for_the_typed_project_id(tmp_path: Path) -> None:
    world = FakeWorld()
    flow, store, _ = make(tmp_path, world, answer="yes")
    flow.confirm_plan = True
    with pytest.raises(up.UpgradeRefused, match="not approved"):
        flow.run(NEW)
    assert PROJECT in flow.confirm.prompts[0] and store.load().release == OLD
    assert not [c for c in world.calls if c[0] not in ("plan", "read_app")]


def test_namespaces_applied_for_every_tenant(tmp_path: Path) -> None:
    world = FakeWorld()
    flow, _, _ = make(tmp_path, world)
    flow.run(NEW)
    assert [c[1] for c in world.calls if c[0] == "apply_namespace"] == ["eng", "ops", "research"]


def test_metric_label_change_split_live_first(tmp_path: Path) -> None:
    world = FakeWorld()
    replace = {"address": "module.monitoring.google_logging_metric.dispatch", "type": "google_logging_metric",
               "change": {"actions": ["delete", "create"], "after": {"name": "dispatch"}}}
    world.plans["infra"] = [{"resource_changes": [replace]}, {"resource_changes": [replace]}, {}]
    flow, _, lines = make(tmp_path, world)
    assert flow.run(NEW) == ExitCode.OK
    patched = world.calls.index(("patch_log_metric", "module.monitoring.google_logging_metric.dispatch"))
    applied = world.calls.index(("apply_root", "infra", NEW))
    assert patched < applied
    assert any("lesson 14" in line for line in lines)


def test_rollback_reapplies_previous_digests(tmp_path: Path) -> None:
    world = FakeWorld()
    world.check_results = [("digests", "unmet", "swarm-api serves an old revision")]
    flow, store, _ = make(tmp_path, world)
    with pytest.raises(up.UpgradeFailed, match="rolled back"):
        flow.run(NEW)
    copies = [c for c in world.calls if c[0] == "copy_images"]
    assert copies[-1][1] == OLD and copies[-1][2]["swarm-api"] == digest(f"api-{OLD}")
    assert world.calls.index(("apply_root", "infra", OLD)) > world.calls.index(("apply_root", "infra", NEW))
    state = store.load()
    assert state.release == OLD
    progress = state.outputs[up.UPGRADE_KEY]
    assert progress["failed"] == up.VERIFY and progress["rolled_back_to"] == OLD
    assert up.INFRA not in progress["done"]
    assert world.calls[-1] == ("resume_dispatch",)


def test_resume_after_rollback_drains_first(tmp_path: Path) -> None:
    world = FakeWorld()
    world.check_results = [("digests", "unmet", "swarm-api serves an old revision")]
    flow, store, _ = make(tmp_path, world)
    with pytest.raises(up.UpgradeFailed, match="rolled back"):
        flow.run(NEW)
    assert up.DRAIN not in store.load().outputs[up.UPGRADE_KEY]["done"], "the rollback resumed dispatch"
    world.check_results = [("digests", "met", "ok")]
    world.calls.clear()
    assert flow.run(NEW) == ExitCode.OK
    applied = steps_of(world)
    assert applied[0] == up.DRAIN
    assert applied.index(up.DRAIN) < applied.index(f"infra:{NEW}")


def test_resume_at_failed_step(tmp_path: Path) -> None:
    world = FakeWorld()
    world.fail["apply_namespace:ops"] = RuntimeError("kubectl apply failed")
    flow, store, _ = make(tmp_path, world)
    with pytest.raises(up.UpgradeFailed, match="namespaces failed"):
        flow.run(NEW)
    assert store.load().outputs[up.UPGRADE_KEY]["failed"] == up.NAMESPACES
    assert ("resume_dispatch",) in world.calls, "a stopped upgrade gives dispatch back"
    world.calls.clear()
    assert flow.run(NEW) == ExitCode.OK
    applied = steps_of(world)
    assert up.COPY_IMAGES not in applied and f"infra:{NEW}" not in applied and f"bootstrap:{NEW}" not in applied
    assert applied == [up.DRAIN, "apply_namespace", "apply_namespace", "apply_namespace", up.VERIFY]
    assert store.load().outputs[up.UPGRADE_KEY]["finished"] == NEW


def test_same_release_is_nothing_to_do(tmp_path: Path) -> None:
    world = FakeWorld()
    flow, _, lines = make(tmp_path, world, release=NEW)
    assert flow.run(NEW) == ExitCode.OK
    assert world.calls == [] and "nothing to do" in lines[-1]


# -- finding 16: after the closing pass, the upgrade closes what it reopened --------


def _live_world(monkeypatch: pytest.MonkeyPatch, ctx: Any) -> Any:
    world = object.__new__(verb.LiveWorld)
    world.deps = object()
    monkeypatch.setattr(verb.LiveWorld, "ctx", lambda self, release=None: ctx)
    return world


def test_infra_apply_after_the_close_is_recorded_for_the_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """infra-4's inputs carry the installer's address (decision 2): once the close
    had removed it, the upgrade's infra apply records the reopen, so the
    upgrade's close (or the next swarm install) removes it again. It is never
    infra-2, whose inputs switch the GitHub App off (N12)."""
    from swarm_install.steps import bootstrap, infra

    from .test_terraform_runner import FakeCtx

    ctx = FakeCtx()
    ctx.state.outputs[bootstrap.applied_key("infra-close")] = "digest"
    applied: list[str] = []
    monkeypatch.setattr(bootstrap, "apply_pass", lambda c, deps, pass_name, **kw: applied.append(pass_name))
    monkeypatch.setattr(infra.REACH, "address", lambda c: None)
    world = _live_world(monkeypatch, ctx)
    world.apply_root("bootstrap", "v1.1.0")
    assert not ctx.outputs.get(infra.REOPENED), "bootstrap never touches the control plane"
    world.apply_root("infra", "v1.1.0")
    assert applied == ["bootstrap-2", infra.REOPEN_PASS]
    assert ctx.outputs[infra.REOPENED] == ctx.outputs["installer_cidr"]

    closed: list[tuple[Any, str]] = []
    monkeypatch.setattr(infra.REACH, "close_after", lambda c, name: closed.append((c, name)))
    world.close_reach()
    assert closed == [(ctx, "swarm upgrade")]


def test_infra_apply_after_the_close_carries_this_machines_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the close, an upgrade from another network refreshes installer_cidr
    before infra-4, so the control plane admits this machine's /32, not the one
    recorded at install (finding 16)."""
    from swarm_install.steps import bootstrap, infra

    from .test_infra_steps import SecretMetadata, every_key
    from .test_terraform_runner import FakeCtx

    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    ctx.state.outputs[bootstrap.applied_key("infra-close")] = "digest"
    recorded = ctx.state.outputs["installer_cidr"]
    now = "203.0.113.77/32"
    assert now != recorded
    monkeypatch.setattr(infra.REACH, "address", lambda c: c.state.outputs.update(installer_cidr=now))
    seen: list[Any] = []
    monkeypatch.setattr(bootstrap, "apply_pass", lambda c, deps, pass_name, **kw: seen.append(
        bootstrap.generate(c, pass_name, gcp)["gke_master_authorized_cidrs"]))
    _live_world(monkeypatch, ctx).apply_root("infra", "v1.1.0")
    assert seen == [[{"cidr_block": now, "display_name": "swarm-install"}]]
    assert ctx.outputs[infra.REOPENED] == now


def test_infra_apply_before_the_close_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_install.steps import bootstrap, infra

    from .test_terraform_runner import FakeCtx

    ctx = FakeCtx()
    monkeypatch.setattr(bootstrap, "apply_pass", lambda c, deps, pass_name, **kw: None)
    _live_world(monkeypatch, ctx).apply_root("infra", "v1.1.0")
    assert not ctx.outputs.get(infra.REOPENED)


@pytest.mark.parametrize(("passes", "expected"), [
    (("infra-1", "infra-2"), "infra-2"),
    (("infra-1", "infra-2", "infra-3"), "infra-3"),
    (("infra-1", "infra-2", "infra-3", "infra-4"), "infra-4"),
])
def test_upgrade_applies_the_latest_infra_pass_never_infra_2(monkeypatch: pytest.MonkeyPatch,
                                                             passes: tuple[str, ...], expected: str) -> None:
    """An upgrade re-applies the latest pass the install reached: infra-2's inputs
    have the GitHub App, the forge refresh and the hooks off, so applying it
    after pass 3 asked to destroy the hooks (N12)."""
    from swarm_install.steps import bootstrap, infra

    from .test_infra_steps import SecretMetadata, every_key
    from .test_terraform_runner import FakeCtx

    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    ctx.state.outputs["github_app"] = {"id": "1234", "slug": "swarm"}
    for pass_name in passes:
        ctx.state.outputs[bootstrap.applied_key(pass_name)] = "digest"
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(bootstrap, "apply_pass", lambda c, deps, pass_name, **kw: seen.append(
        (pass_name, bootstrap.generate(c, pass_name, gcp)["enable_github_app"])))
    _live_world(monkeypatch, ctx).apply_root("infra", "v1.1.0")
    assert seen == [(expected, expected != "infra-2")]
    assert not ctx.outputs.get(infra.REOPENED)


# -- A12: upgrade opens the install's own Firestore database, never (default) -------


class _Clients:
    """Stands in for `google.cloud.firestore.Client`; records the database each client names."""

    def __init__(self) -> None:
        self.databases: list[str] = []

    def __call__(self, *, project: str, database: str) -> Any:
        self.databases.append(database)
        return _TasksClient()


class _TasksClient:
    def collection(self, name: str) -> Any:
        assert name == "tasks"
        return self

    def where(self, *, filter: Any) -> Any:  # noqa: A002 -- the Firestore keyword
        return self

    def stream(self) -> list[Any]:
        return [object(), object()]


def test_upgrade_migrations_and_running_name_the_install_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a shared project (default) belongs to someone else: the drain count and
    every numbered migration act on the install's database, record.DATABASE."""
    from google.cloud import firestore

    from swarm_install import record

    clients = _Clients()
    monkeypatch.setattr(firestore, "Client", clients)
    world = verb.LiveWorld("proj", "prod", object(), object(), object(), object(), "holder", None)
    assert world.running() == 2
    world.documents()._client = None
    _ = world.documents().client
    assert clients.databases == [record.DATABASE, record.DATABASE]


def test_migration_documents_default_to_the_install_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import firestore

    from swarm_install import record

    clients = _Clients()
    monkeypatch.setattr(firestore, "Client", clients)
    _ = mig.FirestoreMigrationDocuments("proj").client
    assert clients.databases == [record.DATABASE]


def test_a_drain_that_gives_up_names_swarm_upgrade(tmp_path: Path) -> None:
    """Audit 4e F13: `swarm install` does not resume an upgrade's drain."""
    from swarm_install.waits import WaitGaveUp

    world = FakeWorld()
    world.running = lambda: 3  # type: ignore[method-assign]
    flow, _store, _ = make(tmp_path, world)
    with pytest.raises(WaitGaveUp) as gave_up:
        flow.run(NEW)
    assert gave_up.value.step == "upgrade.drain"
    assert "Run swarm upgrade" in str(gave_up.value) and "swarm install" not in str(gave_up.value)
