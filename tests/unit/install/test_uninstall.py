"""`swarm uninstall` (DAYONE-UNINSTALL-1; docs/design/day-one-plan.md §9.2,
design §10; part of #2688).

On fakes, with no credentials and no terraform binary: the world is a fake
`Teardown` that records every call with the identity it ran as, and each
Terraform root is a fake `exec_` that answers like terraform over a resource
list it keeps (a destroy plan deletes what is in its state; `state rm`
forgets; `apply` destroys).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from swarm_install import remains as remains_mod
from swarm_install import tfvars
from swarm_install import uninstall as un
from swarm_install.api_client import deployer_email
from swarm_install.events import Events
from swarm_install.exitcodes import ExitCode
from swarm_install.state import STATE_OBJECT, InstallState, StateStore, StepRecord
from swarm_install.terraform import IMPERSONATE_ENV, TERRAFORM_VERSION, Completed, Terraform
from swarm_install.verbs import discover

from .test_state_store import FakeBucket, FakeClock

PROJECT = "p-dayone-uninst"
DEPLOYER = deployer_email(PROJECT)
OURS = {"managed-by": "swarm-terraform"}


class Crash(BaseException):
    """A killed process, injected into a fake: nothing in the uninstall catches it."""


class FakeTf:
    """One root's terraform: `resources` maps address -> (type, before).

    It refuses what the real providers refuse (audit 4e F1): a destroy of a
    resource whose `deletion_protection` is true, of a bucket that still holds
    objects without `force_destroy`, and prod inputs that drop the protection
    without `allow_unprotected_prod` (terraform/infra/variables.tf). A
    `deletion_policy = "ABANDON"` resource leaves state and stays in `abandoned`.
    """

    def __init__(self, root: str, log: list[tuple], resources: dict[str, tuple[str, dict]],
                 objects: dict[str, int] | None = None) -> None:
        self.root = root
        self.log = log
        self.resources = dict(resources)
        self.objects = objects if objects is not None else {}
        self.abandoned: list[str] = []
        self.planned: list[dict] = []
        self.crash_on_apply = 0

    @staticmethod
    def _inputs(cwd) -> dict:
        path = Path(cwd) / tfvars.INPUTS_FILE
        return json.loads(path.read_text()) if path.is_file() else {}

    def __call__(self, argv, env, cwd) -> Completed:
        argv = list(argv)
        verb = argv[1]
        self.log.append(("terraform", self.root, verb, env.get(IMPERSONATE_ENV)))
        if verb == "version":
            return Completed(0, json.dumps({"terraform_version": TERRAFORM_VERSION}))
        if verb == "state" and argv[2] == "list":
            return Completed(0, "\n".join(self.resources))
        if verb == "state" and argv[2] == "rm":
            self.resources.pop(argv[3], None)
            return Completed(0, "")
        if verb == "plan":
            inputs = self._inputs(cwd)
            if (inputs.get("environment") == "prod" and inputs.get("deletion_protection") is False
                    and not inputs.get("allow_unprotected_prod")):
                return Completed(1, "", "prod requires deletion_protection = true")
            if "-destroy" in argv:
                self.planned = [{"address": a, "type": t, "change": {"actions": ["delete"], "before": b,
                                                                     "after": None}}
                                for a, (t, b) in self.resources.items()]
            else:
                self.planned = []
                for target in (a.split("=", 1)[1] for a in argv if a.startswith("-target=")):
                    rtype, before = self.resources[target]
                    after = dict(before)
                    if "deletion_protection" in before:
                        after["deletion_protection"] = inputs.get("deletion_protection", True)
                    self.planned.append({"address": target, "type": rtype, "change": {
                        "actions": ["update"] if after != before else ["no-op"], "before": before, "after": after}})
            return Completed(0, "")
        if verb == "show":
            return Completed(0, json.dumps({"resource_changes": self.planned}))
        if verb == "apply":
            if self.crash_on_apply:
                self.crash_on_apply -= 1
                raise Crash(f"killed during {self.root} destroy")
            if any("delete" in c["change"]["actions"] for c in self.planned):
                for address, (rtype, before) in self.resources.items():
                    if before.get("deletion_protection") is True:
                        return Completed(1, "", f"Error: cannot destroy {address}: deletion_protection is set "
                                                "to true")
                    if (rtype == "google_storage_bucket" and not before.get("force_destroy")
                            and self.objects.get(before.get("name"))):
                        return Completed(1, "", f"Error: bucket {before.get('name')} is not empty")
                self.abandoned.extend(a for a, (_, b) in self.resources.items()
                                      if b.get("deletion_policy") == "ABANDON")
                self.resources.clear()
            else:
                for change in self.planned:
                    rtype, _ = self.resources[change["address"]]
                    self.resources[change["address"]] = (rtype, change["change"]["after"])
        return Completed(0, "")


class FakeWorld:
    """`uninstall.Teardown` on lists; every call logged as (name, who)."""

    def __init__(self, tmp_path: Path) -> None:
        self.log: list[tuple] = []
        self.tmp_path = tmp_path
        self.jobs = ["projects/p/locations/r/jobs/swarm-reconciler"]
        self.executions_left = 1
        self.namespaces = ["swarm-eng"]
        self.images = ["projects/p/locations/r/repositories/swarm/packages/swarm-api"]
        self.secrets = ["projects/p/secrets/swarm-platform-key", "projects/p/secrets/swarm-desktop-client"]
        self.missing: list[str] = []
        self.grant = True
        self.deployer_deleted = False
        self.buckets = {f"swarm-tfstate-{PROJECT}", un.logs_bucket(PROJECT)}
        #: Objects in infra's buckets, by bucket name (checkpoints, artifacts).
        self.objects = {f"swarm-artifacts-{PROJECT}": 3}
        self.accounts = [f"swarm-agent-worker-w-3f9a2c@{PROJECT}.iam.gserviceaccount.com"]
        self.member_bindings = {f"serviceAccount:{a}": 2 for a in self.accounts}
        self.firestore = {"swarm"}
        self.budgets = {"billingAccounts/0000-1111/budgets/b1": f"swarm-{PROJECT}",
                        "billingAccounts/0000-1111/budgets/b2": "another-budget"}
        self.made_by_install = False
        self.project_deleted = False
        self.reach_closed = False
        self.tf = {
            "infra": FakeTf("infra", self.log, {
                "google_cloud_run_v2_service.api": ("google_cloud_run_v2_service",
                                                    {"name": "swarm-api", "labels": OURS,
                                                     "deletion_protection": True}),
                "module.gke.google_container_cluster.agents": ("google_container_cluster",
                                                               {"name": "swarm-autopilot",
                                                                "effective_labels": OURS,
                                                                "deletion_protection": True}),
                "google_storage_bucket.artifacts": ("google_storage_bucket",
                                                    {"name": f"swarm-artifacts-{PROJECT}", "labels": OURS,
                                                     "force_destroy": False}),
                "google_firestore_database.swarm": ("google_firestore_database",
                                                    {"name": "swarm", "deletion_policy": "ABANDON",
                                                     "delete_protection_state": "DELETE_PROTECTION_ENABLED"}),
                "google_project_iam_member.api": ("google_project_iam_member",
                                                  {"member": "serviceAccount:swarm-api@x", "role": "r"}),
            }, self.objects),
            "bootstrap": FakeTf("bootstrap", self.log, {
                "google_service_account.deployer": ("google_service_account", {"email": DEPLOYER}),
                "google_project_iam_custom_role.dispatch": ("google_project_iam_custom_role",
                                                            {"role_id": "swarmDispatch_x1"}),
                'google_kms_key_ring.step_spec["dev"]': ("google_kms_key_ring", {
                    "id": f"projects/{PROJECT}/locations/europe-west1/keyRings/swarm-dev-specs"}),
                'google_kms_crypto_key.step_spec["dev"]': ("google_kms_crypto_key", {
                    "id": f"projects/{PROJECT}/locations/europe-west1/keyRings/swarm-dev-specs/cryptoKeys/step-spec",
                    "labels": OURS}),
                "google_storage_bucket.state": ("google_storage_bucket", {"name": f"swarm-tfstate-{PROJECT}",
                                                                          "labels": OURS}),
                "google_storage_bucket.state_logs": ("google_storage_bucket", {"name": un.logs_bucket(PROJECT),
                                                                               "labels": OURS}),
            }),
        }

    def _call(self, name: str, who: str | None = None, *args: Any) -> None:
        if who is not None and self.deployer_deleted:
            raise AssertionError(f"{name} impersonated {who}, which no longer exists")
        self.log.append((name, who, *args))

    def scheduler_jobs(self, who):
        self._call("scheduler_jobs", who)
        return list(self.jobs)

    def pause_job(self, name, who):
        self._call("pause_job", who, name)
        self.jobs.remove(name)

    def running_executions(self, who):
        self._call("running_executions", who)
        if self.executions_left:
            self.executions_left -= 1
            return ["projects/p/locations/r/jobs/j/executions/e1"]
        return []

    def tenant_namespaces(self, who):
        self._call("tenant_namespaces", who)
        if self.reach_closed:
            raise AssertionError("kubectl timed out: the closing pass removed this machine from the control plane")
        return list(self.namespaces)

    def delete_namespace(self, name, who):
        self._call("delete_namespace", who, name)
        self.namespaces.remove(name)

    def delete_policies(self, who):
        self._call("delete_policies", who)
        return None

    def terraform(self, root):
        root_dir = self.tmp_path / root
        root_dir.mkdir(parents=True, exist_ok=True)
        return Terraform(root=root, root_dir=root_dir, state_bucket=f"swarm-tfstate-{PROJECT}",
                         environment="dev", exec_=self.tf[root])

    def registry_images(self, who):
        self._call("registry_images", who)
        return list(self.images)

    def delete_image(self, name, who):
        self._call("delete_image", who, name)
        self.images.remove(name)

    def engine_secrets(self, who):
        self._call("engine_secrets", who)
        return list(self.secrets)

    def delete_secret(self, name, who):
        self._call("delete_secret", who, name)
        self.secrets.remove(name)

    def missing_permissions(self, permissions):
        self._call("missing_permissions")
        return [p for p in permissions if p in self.missing]

    def remove_project_binding(self, role, member):
        self._call("remove_project_binding", None, role, member)
        return True

    def remove_impersonation_grant(self, deployer, member):
        self._call("remove_impersonation_grant", None, deployer, member)
        had, self.grant = self.grant, False
        return had

    def destroy_key_versions(self, key):
        self._call("destroy_key_versions", None, key)
        return 1

    def delete_bucket(self, name):
        self._call("delete_bucket", None, name)
        if name in self.buckets:
            self.buckets.remove(name)
            return True
        return False

    def empty_bucket(self, name, who):
        self._call("empty_bucket", who, name)
        return self.objects.pop(name, 0)

    def workspace_accounts(self):
        self._call("workspace_accounts")
        return list(self.accounts)

    def remove_member_bindings(self, member):
        self._call("remove_member_bindings", None, member)
        return self.member_bindings.pop(member, 0)

    def delete_account(self, email):
        self._call("delete_account", None, email)
        if email in self.accounts:
            self.accounts.remove(email)
            return True
        return False

    def delete_firestore(self, database):
        self._call("delete_firestore", None, database)
        if database in self.firestore:
            self.firestore.remove(database)
            return True
        return False

    def delete_budgets(self, account, display_name):
        self._call("delete_budgets", None, account, display_name)
        gone = [n for n, d in self.budgets.items() if n.startswith(f"billingAccounts/{account}/") and d == display_name]
        for name in gone:
            del self.budgets[name]
        return gone

    def project_made_by_install(self):
        self._call("project_made_by_install")
        return self.made_by_install

    def delete_project(self):
        self._call("delete_project")
        self.project_deleted = True

    def names(self) -> list[str]:
        return [entry[0] if entry[0] != "terraform" else f"terraform {entry[1]} {entry[2]}" for entry in self.log]


INSTALLED = ("foundations.state-bucket", "bootstrap.pass-1", "images.registry", "images.copy", "infra.pass-1",
             "secrets.platform-keys", "edge.desktop-client", "cluster.workloads", "identity.admin-group",
             "verify.end-to-end")


def install_state(*, steps: tuple[str, ...] = INSTALLED, deny: list[str] | None = None,
                  project: str = PROJECT) -> InstallState:
    return InstallState(
        project=project, environment="dev", region="europe-west1", hostname="swarm.example.com",
        release="2026.10.1",
        steps={s: StepRecord(state="done", at=1.0, by="owner@example.com") for s in steps},
        outputs={"installer": "owner@example.com", "desktop_client_id": "1234-desktop.apps.googleusercontent.com",
                 "github_org": "acme-org", "github_app": {"slug": "acme-swarm", "id": 7},
                 "admin_groups": ["swarm-admins@example.com"], "oauth_brand": "projects/42/brands/42",
                 "global_ip": "203.0.113.7", "billing_account": "0000-1111",
                 "engine_iam_bindings": [{"role": "roles/logging.viewer", "member": "group:ops@example.com"}]},
        deny_list=list(deny or []),
    )


def stored_store(tmp_path: Path, state: InstallState) -> tuple[StateStore, FakeBucket]:
    bucket = FakeBucket()
    bucket.write(STATE_OBJECT, state.to_bytes(), if_generation_match=0)
    for root in ("infra", "bootstrap"):
        tfvars.store(bucket, root, {"project_id": state.project, "region": "europe-west1"})
    store = StateStore(project=state.project, environment="dev", local=tmp_path / "local" / f"{state.project}.json",
                       bucket=bucket)
    return store, bucket


def flow(tmp_path: Path, store: StateStore, world: FakeWorld, *, typed: str = PROJECT,
         asked: list[str] | None = None) -> un.Uninstall:
    def confirm(prompt: str) -> str:
        if asked is not None:
            asked.append(prompt)
        return typed

    clock = FakeClock()
    return un.Uninstall(store=store, world=world, confirm=confirm, clock=clock,
                        events=Events(clock.now, out=io.StringIO()), out=io.StringIO(), holder="owner@example.com",
                        remains_dir=tmp_path / "cache")


def test_verb_is_discovered():
    verb = discover()["uninstall"]
    assert verb.NAME == "uninstall"


def test_typed_confirmation_ignores_assume_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    asked: list[str] = []
    # Nothing typed at the terminal (what the real prompt returns with no tty):
    # the environment variable does not stand in for it.
    assert flow(tmp_path, store, world, typed="", asked=asked).run() == ExitCode.REFUSED
    assert len(asked) == 1 and PROJECT in asked[0]
    assert world.log == []
    # "yes" is not the project id either.
    assert flow(tmp_path, store, world, typed="yes").run() == ExitCode.REFUSED
    assert world.log == []
    # The verb's prompt reads /dev/tty; with no terminal it answers nothing, whatever the environment says.
    from swarm_install.steps import bootstrap
    from swarm_install.verbs import uninstall as verb

    def no_tty(*args, **kwargs):
        raise OSError("no terminal")

    monkeypatch.setattr(bootstrap, "open", no_tty, raising=False)
    assert verb.typed("Type the project id: ") == ""


def test_one_confirmation_for_the_whole_uninstall(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    asked: list[str] = []
    assert flow(tmp_path, store, FakeWorld(tmp_path), asked=asked).run() == ExitCode.OK
    assert len(asked) == 1


def test_order_is_section_9_2(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    names = world.names()

    def first(name: str) -> int:
        return names.index(name)

    order = ["pause_job", "running_executions", "delete_namespace", "delete_policies", "terraform infra apply",
             "delete_image", "delete_secret", "remove_project_binding", "remove_impersonation_grant",
             "terraform bootstrap apply", "destroy_key_versions", "delete_bucket"]
    positions = [first(n) for n in order]
    assert positions == sorted(positions), [n for _, n in sorted(zip(positions, order))]
    assert [s.number for s in un.STEPS] == list(range(1, 9))
    # The record is written before the state bucket goes.
    record = remains_mod.record_path(PROJECT, tmp_path / "cache")
    assert record.is_file()
    assert world.buckets == set()
    assert world.namespaces == [] and world.images == [] and world.secrets == [] and world.jobs == []


def test_drain_waits_with_a_bound(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.executions_left = 10**6
    assert flow(tmp_path, store, world).run() == ExitCode.PAUSED
    assert "delete_namespace" not in world.names()
    assert not store.load().done("uninstall.scheduler")


def test_identity_of_each_step(tmp_path):
    assert [s.identity for s in un.STEPS] == [un.DEPLOYER] * 5 + [un.OWNER] * 3
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    deployer_calls = {"scheduler_jobs", "pause_job", "running_executions", "tenant_namespaces", "delete_namespace",
                      "delete_policies", "registry_images", "delete_image", "engine_secrets", "delete_secret"}
    for entry in world.log:
        if entry[0] in deployer_calls:
            assert entry[1] == DEPLOYER, entry
        elif entry[0] == "terraform":
            if entry[2] != "version":  # `terraform version` touches no cloud
                assert entry[3] == (DEPLOYER if entry[1] == "infra" else None), entry
        else:
            assert entry[1] is None, entry
    names = world.names()
    # The impersonation grant goes before bootstrap destroy deletes the account it names.
    assert names.index("remove_impersonation_grant") < names.index("terraform bootstrap apply")
    grant = next(e for e in world.log if e[0] == "remove_impersonation_grant")
    assert grant[2:] == (DEPLOYER, "user:owner@example.com")


def test_resume_after_deployer_deleted_runs_as_owner(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.tf["bootstrap"].crash_on_apply = 1
    with pytest.raises(Crash):
        flow(tmp_path, store, world).run()
    state = store.load()
    assert all(state.done(f"uninstall.{n}") for n in ("scheduler", "kubernetes", "infra", "images", "secrets",
                                                       "iam"))
    assert not state.done("uninstall.bootstrap")
    # The crash came inside step 7: the deployer may be gone. Any impersonation now fails the test.
    world.deployer_deleted = True
    world.log.clear()
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    assert all(entry[1] is None if entry[0] != "terraform" else entry[3] is None for entry in world.log)
    assert "pause_job" not in world.names() and "remove_impersonation_grant" not in world.names()
    assert "terraform bootstrap apply" in world.names()


def test_resume_after_crash_mid_uninstall(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.tf["infra"].crash_on_apply = 1
    with pytest.raises(Crash):
        flow(tmp_path, store, world).run()
    state = store.load()
    assert state.done("uninstall.scheduler") and state.done("uninstall.kubernetes")
    assert not state.done("uninstall.infra")
    world.log.clear()
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    names = world.names()
    assert "pause_job" not in names and "delete_namespace" not in names
    assert names.index("terraform infra apply") < names.index("delete_image")
    # A third run finds nothing left to do and asks nothing.
    asked: list[str] = []
    local = StateStore(project=PROJECT, environment="dev", local=store.local)
    assert flow(tmp_path, local, world, asked=asked).run() == ExitCode.OK
    assert asked == []


def test_steps_follow_the_install_records(tmp_path):
    # An install that stopped after its images: nothing on a cluster, no infra pass applied.
    steps = ("foundations.state-bucket", "bootstrap.pass-1", "images.registry", "images.copy")
    store, _ = stored_store(tmp_path, install_state(steps=steps))
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    names = world.names()
    assert "tenant_namespaces" not in names and "pause_job" not in names and "engine_secrets" not in names
    state = StateStore(project=PROJECT, environment="dev", local=store.local).load()
    assert state.steps["uninstall.kubernetes"].detail.startswith("skipped")
    assert "terraform infra apply" in names  # images.registry is a targeted infra apply


def test_unlabelled_resource_never_deleted(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    foreign = "google_storage_bucket.reports"
    world.tf["infra"].resources[foreign] = ("google_storage_bucket", {"name": "reports", "labels": {"team": "x"}})
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    rm = [e for e in world.log if e[:3] == ("terraform", "infra", "state")]
    assert rm, "the unlabelled bucket was not taken out of state before the destroy"
    names = world.names()
    assert names.index("terraform infra state") < names.index("terraform infra apply")
    record = remains_mod.Record.from_bytes(remains_mod.record_path(PROJECT, tmp_path / "cache").read_bytes())
    assert [r.name for r in record.left] == [f"infra: {foreign}"]
    assert "managed-by" in record.left[0].why


def test_ours_reads_every_label_field():
    assert un.ours({"labels": {"managed-by": "swarm-install"}})
    assert un.ours({"effective_labels": {"managed-by": "swarm-terraform"}, "labels": {}})
    assert un.ours({"member": "user:a@example.com"})  # a type that carries no labels: its state owns it
    assert not un.ours({"labels": {}})
    assert not un.ours({"labels": None})


def test_deny_listed_name_refused(tmp_path):
    store, _ = stored_store(tmp_path, install_state(deny=["agents-staging"]))
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.REFUSED
    assert world.log == []
    # In a destroy plan too: a deny-listed name anywhere refuses the destroy, and nothing is applied.
    tf = world.terraform("infra")
    world.tf["infra"].resources["google_container_cluster.c"] = ("google_container_cluster",
                                                                 {"name": "agents-staging", "labels": OURS})
    with pytest.raises(un.UninstallRefused, match="agents-staging"):
        un.destroy_root(tf, project=PROJECT, who=DEPLOYER, inputs={"project_id": PROJECT},
                        deny_list=["agents-staging"], approved=PROJECT)
    assert "apply" not in [e[2] for e in world.log if e[0] == "terraform"]
    assert un.denied(["serviceAccount:api-service@x.iam", "projects/p/x/agents-staging-vpc"],
                     ["agents-staging-vpc"]) == ["agents-staging-vpc"]


@pytest.mark.parametrize("deny", [[PROJECT], ["agents-staging-vpc"]])
def test_shared_project_refused_before_anything(tmp_path, deny):
    # R4: only the shared project's state carries a deny-list.
    store, _ = stored_store(tmp_path, install_state(deny=deny))
    world = FakeWorld(tmp_path)
    asked: list[str] = []
    out = io.StringIO()
    f = flow(tmp_path, store, world, asked=asked)
    f.out = out
    assert f.run() == ExitCode.REFUSED
    assert asked == [] and world.log == []
    assert "deny-list" in out.getvalue()


def test_destroy_needs_the_typed_project_id(tmp_path):
    world = FakeWorld(tmp_path)
    tf = world.terraform("infra")
    with pytest.raises(un.UninstallRefused, match="not approved"):
        un.destroy_root(tf, project=PROJECT, who=DEPLOYER, inputs={"project_id": PROJECT}, deny_list=[],
                        approved="y")
    assert "apply" not in [e[2] for e in world.log if e[0] == "terraform"]


def test_a_failed_call_is_said_and_the_next_run_continues(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    real = world.delete_image

    def fails(name, who):
        raise RuntimeError("artifact registry said 503")

    world.delete_image = fails
    f = flow(tmp_path, store, world)
    assert f.run() == ExitCode.FAIL
    assert "step 4" in f.out.getvalue() and "503" in f.out.getvalue()
    assert store.load().done("uninstall.infra") and not store.load().done("uninstall.images")
    world.delete_image = real
    assert flow(tmp_path, store, world).run() == ExitCode.OK


def test_owner_missing_permission_pauses_naming_it(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.missing = ["iam.roles.delete"]
    out = io.StringIO()
    f = flow(tmp_path, store, world)
    f.out = out
    assert f.run() == ExitCode.PAUSED
    assert "iam.roles.delete" in out.getvalue()
    assert "remove_impersonation_grant" not in world.names()
    assert store.load().done("uninstall.secrets") and not store.load().done("uninstall.iam")


def test_remains_record_lists_the_eight(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    f = flow(tmp_path, store, world)
    assert f.run() == ExitCode.OK
    record = remains_mod.Record.from_bytes(remains_mod.record_path(PROJECT, tmp_path / "cache").read_bytes())
    assert [r.kind for r in record.remains] == list(remains_mod.KINDS)
    assert len(record.remains) == 8
    by = {r.kind: r for r in record.remains}
    assert all(r.why and r.step for r in record.remains)
    assert by["github-app"].step.startswith(
        "https://github.com/organizations/acme-org/settings/apps/acme-swarm/advanced")
    assert by["desktop-client"].name == "1234-desktop.apps.googleusercontent.com"
    assert by["custom-roles"].name == "swarmDispatch_x1"
    assert by["kms-key-ring"].name.endswith("/keyRings/swarm-dev-specs")
    assert by["dns-record"].name == "swarm.example.com A 203.0.113.7"
    assert by["workspace-groups"].name == "swarm-admins@example.com"
    assert remains_mod.KMS_DELETE_DOCS in by["kms-key-ring"].why
    assert remains_mod.CUSTOM_ROLE_DOCS in by["custom-roles"].why
    # The KMS ring and key, and the state buckets, were never put to a terraform destroy.
    bootstrap_rm = [e for e in world.log if e[:3] == ("terraform", "bootstrap", "state")]
    assert bootstrap_rm
    assert world.tf["bootstrap"].resources == {}
    # Printed, too.
    assert "What remains" in f.out.getvalue()


# -- the verb and the real world, on a fake REST client -------------------------


class FakeApi:
    """`InstallGcp`'s `_call` and `_pages`, answering from `answers` by URL."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any]] = []

    def _call(self, method, url, *, body=None, params=None, missing_ok=False):
        self.calls.append((method, url, body if body is not None else params))
        return self.answers.get((method, url), None if missing_ok else {})

    def _pages(self, url, key, params=None):
        self.calls.append(("GET", url, params))
        return list(self.answers.get(("PAGES", url), []))


def teardown_on(api: FakeApi):
    from swarm_install.verbs.uninstall import GcpTeardown

    world = GcpTeardown(state=install_state())
    world._apis[None] = api
    world._apis[DEPLOYER] = api
    return world


def test_real_world_removes_only_the_installers_token_creator_grant():
    from swarm_install.steps.preflight import IAM

    resource = f"{IAM}/projects/{PROJECT}/serviceAccounts/{DEPLOYER}"
    api = FakeApi({("POST", f"{resource}:getIamPolicy"): {"etag": "e1", "bindings": [
        {"role": un.TOKEN_CREATOR, "members": ["user:owner@example.com", "user:other@example.com"]},
        {"role": "roles/iam.serviceAccountUser", "members": ["user:owner@example.com"]}]}})
    assert teardown_on(api).remove_impersonation_grant(DEPLOYER, "user:owner@example.com") is True
    method, url, body = api.calls[-1]
    assert url == f"{resource}:setIamPolicy"
    assert body["policy"]["etag"] == "e1"
    assert body["policy"]["bindings"] == [
        {"role": un.TOKEN_CREATOR, "members": ["user:other@example.com"]},
        {"role": "roles/iam.serviceAccountUser", "members": ["user:owner@example.com"]}]
    # The account is already gone: nothing to remove, nothing written.
    gone = FakeApi({})
    assert teardown_on(gone).remove_impersonation_grant(DEPLOYER, "user:owner@example.com") is False
    assert [c[1] for c in gone.calls] == [f"{resource}:getIamPolicy"]


def test_real_world_lists_only_engine_secrets_and_destroys_live_key_versions():
    from swarm_install.adopt import KMS
    from swarm_install.verbs.uninstall import ENGINE_SECRET_FILTER, SECRET_MANAGER

    key = f"projects/{PROJECT}/locations/europe-west1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
    api = FakeApi({
        ("PAGES", f"{SECRET_MANAGER}/projects/{PROJECT}/secrets"): [{"name": "projects/1/secrets/a"}],
        ("GET", f"{KMS}/{key}"): {"name": key},
        ("PAGES", f"{KMS}/{key}/cryptoKeyVersions"): [
            {"name": f"{key}/cryptoKeyVersions/1", "state": "DESTROYED"},
            {"name": f"{key}/cryptoKeyVersions/2", "state": "ENABLED"}],
    })
    world = teardown_on(api)
    assert world.engine_secrets(DEPLOYER) == ["projects/1/secrets/a"]
    assert ("GET", f"{SECRET_MANAGER}/projects/{PROJECT}/secrets", {"filter": ENGINE_SECRET_FILTER}) in api.calls
    assert world.destroy_key_versions(key) == 1
    assert ("POST", f"{KMS}/{key}/cryptoKeyVersions/2:destroy", {}) in api.calls


def test_verb_without_install_state_fails_and_touches_nothing(tmp_path, monkeypatch, capsys):
    import argparse

    from swarm_install.verbs import uninstall as verb

    def no_state(project, environment):
        raise LookupError(f"no install state for {project}")

    monkeypatch.setattr(verb, "open_store", no_state)
    monkeypatch.setattr(verb, "build_world", lambda state: pytest.fail("no world without a state"))
    args = argparse.Namespace(project=PROJECT, env="dev", json_events=False)
    assert verb.run(args) == ExitCode.FAIL
    assert "no install state" in capsys.readouterr().err


# -- audit 4e (DAYONE-UNINSTALL-2) ------------------------------------------------


def test_infra_destroy_lifts_deletion_protection_and_empties_buckets(tmp_path):
    # F1: the cluster and the Cloud Run services are protected, and a bucket holds
    # checkpoints; the destroy lifts the one and empties the other before it applies.
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    f = flow(tmp_path, store, world)
    assert f.run() == ExitCode.OK, f.out.getvalue()
    assert world.tf["infra"].resources == {}
    assert world.objects == {}
    infra = [e for e in world.log if e[:2] == ("terraform", "infra")]
    applies = [i for i, e in enumerate(infra) if e[2] == "apply"]
    assert len(applies) == 2  # the unprotect pass, then the destroy
    # Both as the deployer, and the buckets emptied between them.
    assert all(e[3] == DEPLOYER for e in infra if e[2] != "version")
    names = world.names()
    first = names.index("terraform infra apply")
    last = len(names) - 1 - names[::-1].index("terraform infra apply")
    assert first < names.index("empty_bucket") < last
    assert ("empty_bucket", DEPLOYER, f"swarm-artifacts-{PROJECT}") in world.log


def test_unprotect_pass_targets_only_the_protected_and_carries_the_prod_override(tmp_path):
    world = FakeWorld(tmp_path)
    tf = world.terraform("infra")
    inputs = {"project_id": PROJECT, "environment": "prod"}
    done = un.destroy_root(tf, project=PROJECT, who=DEPLOYER, inputs=tfvars.uninstall_inputs("infra", inputs),
                           deny_list=[], approved=PROJECT, empty_bucket=lambda name: world.empty_bucket(name, DEPLOYER))
    assert world.tf["infra"].resources == {}
    assert "google_storage_bucket.artifacts" in done.deleted
    written = json.loads((tf.root_dir / tfvars.INPUTS_FILE).read_text())
    assert written["deletion_protection"] is False and written["allow_unprotected_prod"] is True
    # The stored inputs themselves are never changed: the next install renders its own.
    assert "deletion_protection" not in inputs
    assert tfvars.uninstall_inputs("bootstrap", {"project_id": PROJECT}) == {"project_id": PROJECT}


def test_unprotect_needs_the_typed_project_id(tmp_path):
    world = FakeWorld(tmp_path)
    tf = world.terraform("infra")
    with pytest.raises(un.UninstallRefused, match="not approved"):
        un.destroy_root(tf, project=PROJECT, who=DEPLOYER,
                        inputs=tfvars.uninstall_inputs("infra", {"project_id": PROJECT}), deny_list=[],
                        approved="yes", empty_bucket=lambda name: world.empty_bucket(name, DEPLOYER))
    assert "apply" not in [e[2] for e in world.log if e[0] == "terraform"]
    assert "empty_bucket" not in world.names()


def test_closed_cluster_is_not_waited_on_when_step_3_destroys_it(tmp_path):
    # F2: after infra.pass-close the control plane no longer admits this machine.
    state = install_state()
    state.outputs["tf_applied_infra_close"] = True
    store, _ = stored_store(tmp_path, state)
    world = FakeWorld(tmp_path)
    world.reach_closed = True
    f = flow(tmp_path, store, world)
    assert f.run() == ExitCode.OK, f.out.getvalue()
    assert "tenant_namespaces" not in world.names() and "delete_policies" not in world.names()
    detail = StateStore(project=PROJECT, environment="dev", local=store.local).load().steps["uninstall.kubernetes"]
    assert "step 3" in detail.detail
    assert world.tf["infra"].resources == {}


def test_forge_and_tenant_secrets_are_listed_too():
    # F7: the -git slots swarm-api writes carry managed-by=swarm-api.
    from swarm_install.verbs.uninstall import ENGINE_SECRET_FILTER, SECRET_FILTERS, SECRET_MANAGER

    url = f"{SECRET_MANAGER}/projects/{PROJECT}/secrets"
    assert ENGINE_SECRET_FILTER in SECRET_FILTERS
    assert "labels.managed-by=swarm-api" in SECRET_FILTERS
    assert "name:swarm-tenant-" in SECRET_FILTERS

    class Filtered(FakeApi):
        def _pages(self, url, key, params=None, **kwargs):
            self.calls.append(("GET", url, params))
            return {
                ENGINE_SECRET_FILTER: [{"name": "projects/1/secrets/swarm-platform-key"}],
                "labels.managed-by=swarm-api": [{"name": "projects/1/secrets/swarm-tenant-eng-git"}],
                "name:swarm-tenant-": [{"name": "projects/1/secrets/swarm-tenant-eng-git"},
                                       {"name": "projects/1/secrets/swarm-tenant-eng-claude"}],
            }.get((params or {}).get("filter"), [])

    api = Filtered({})
    assert teardown_on(api).engine_secrets(DEPLOYER) == [
        "projects/1/secrets/swarm-platform-key", "projects/1/secrets/swarm-tenant-eng-git",
        "projects/1/secrets/swarm-tenant-eng-claude"]
    assert [c[2]["filter"] for c in api.calls if c[1] == url] == list(SECRET_FILTERS)
    # Step 5 runs whenever infra ran: swarm-api writes the slots, not an engine step.
    assert "infra." in un.STEPS[4].reverses and "team." in un.STEPS[4].reverses


def test_workspace_accounts_and_their_bindings_are_removed(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    account = world.accounts[0]
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    assert world.accounts == [] and world.member_bindings == {}
    names = world.names()
    assert names.index("remove_member_bindings") < names.index("delete_account") < names.index(
        "terraform bootstrap apply")
    assert ("delete_account", None, account) in world.log


def test_real_world_workspace_accounts_are_the_worker_prefix_only():
    from swarm_install.steps.preflight import IAM

    url = f"{IAM}/projects/{PROJECT}/serviceAccounts"
    worker = f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
    api = FakeApi({("PAGES", url): [{"email": worker}, {"email": f"swarm-api@{PROJECT}.iam.gserviceaccount.com"},
                                    {"email": f"other@{PROJECT}.iam.gserviceaccount.com"}],
                   ("DELETE", f"{url}/{worker}"): {}})
    world = teardown_on(api)
    assert world.workspace_accounts() == [worker]
    assert world.delete_account(worker) is True


def test_real_world_removes_every_binding_of_a_member():
    api = FakeApi({})
    policy = {"etag": "e9", "bindings": [
        {"role": "roles/logging.logWriter", "members": ["serviceAccount:w@x", "user:a@example.com"]},
        {"role": "roles/monitoring.metricWriter", "members": ["serviceAccount:w@x"]}]}

    class Policy:
        set_to: Any = None

        def iam_policy(self, project):
            return policy

        def set_iam_policy(self, project, new):
            Policy.set_to = new

    world = teardown_on(api)
    world._apis[None] = Policy()
    assert world.remove_member_bindings("serviceAccount:w@x") == 2
    assert Policy.set_to == {"etag": "e9", "bindings": [
        {"role": "roles/logging.logWriter", "members": ["user:a@example.com"]}]}


def test_budget_is_deleted_by_its_name(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    assert world.budgets == {"billingAccounts/0000-1111/budgets/b2": "another-budget"}
    assert ("delete_budgets", None, "0000-1111", f"swarm-{PROJECT}") in world.log
    names = world.names()
    assert names.index("delete_budgets") < names.index("delete_bucket")


def test_real_world_budget_delete_names_the_quota_project():
    from swarm_install.steps.preflight import BUDGETS

    calls: list[tuple] = []

    class Budgets(FakeApi):
        def _pages(self, url, key, params=None, **kwargs):
            calls.append(("PAGES", url, kwargs.get("quota_project")))
            return [{"name": "billingAccounts/0000-1111/budgets/b1", "displayName": f"swarm-{PROJECT}"},
                    {"name": "billingAccounts/0000-1111/budgets/b2", "displayName": "theirs"}]

        def _call(self, method, url, *, body=None, params=None, missing_ok=False, quota_project=None):
            calls.append((method, url, quota_project))
            return {}

    world = teardown_on(Budgets({}))
    assert world.delete_budgets("0000-1111", f"swarm-{PROJECT}") == ["billingAccounts/0000-1111/budgets/b1"]
    assert calls == [("PAGES", f"{BUDGETS}/billingAccounts/0000-1111/budgets", PROJECT),
                     ("DELETE", f"{BUDGETS}/billingAccounts/0000-1111/budgets/b1", PROJECT)]


def test_firestore_is_deleted_and_the_record_says_so(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    assert flow(tmp_path, store, world).run() == ExitCode.OK
    # infra abandons the database (deletion_policy ABANDON); the owner deletes it.
    assert world.tf["infra"].abandoned == ["google_firestore_database.swarm"]
    assert world.firestore == set()
    names = world.names()
    assert names.index("delete_firestore") < names.index("destroy_key_versions")
    record = remains_mod.Record.from_bytes(remains_mod.record_path(PROJECT, tmp_path / "cache").read_bytes())
    by = {r.kind: r for r in record.remains}
    assert "was deleted" in by["firestore-database"].why


def test_firestore_kept_is_recorded_as_kept():
    state = install_state()
    kept = {r.kind: r for r in remains_mod.survivors(state, {"firestore_database": "swarm"})}
    assert "kept" in kept["firestore-database"].why and "deleted" not in kept["firestore-database"].why
    assert "/databases/swarm" in kept["firestore-database"].step
    gone = {r.kind: r for r in remains_mod.survivors(state, {"firestore_database": "swarm",
                                                             "firestore_deleted": True})}
    assert "was deleted" in gone["firestore-database"].why


def test_real_world_firestore_delete_lifts_its_protection_first():
    from swarm_install.verbs.uninstall import FIRESTORE

    db = f"{FIRESTORE}/projects/{PROJECT}/databases/swarm"
    api = FakeApi({("GET", db): {"name": "swarm", "deleteProtectionState": "DELETE_PROTECTION_ENABLED"}})
    assert teardown_on(api).delete_firestore("swarm") is True
    assert [(c[0], c[1]) for c in api.calls] == [("GET", db), ("PATCH", db), ("DELETE", db)]
    assert api.calls[1][2] == {"deleteProtectionState": "DELETE_PROTECTION_DISABLED"}
    assert teardown_on(FakeApi({})).delete_firestore("swarm") is False


def _answers(*typed: str):
    asked: list[str] = []
    replies = list(typed)

    def confirm(prompt: str) -> str:
        asked.append(prompt)
        return replies.pop(0)

    return asked, confirm


def test_project_the_install_created_is_deleted_behind_its_own_typed_id(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.made_by_install = True
    f = flow(tmp_path, store, world)
    asked, f.confirm = _answers(PROJECT, PROJECT)
    assert f.run() == ExitCode.OK, f.out.getvalue()
    assert len(asked) == 2 and "delete the project" in asked[1].lower()
    assert world.project_deleted
    names = world.names()
    assert names.index("delete_bucket") < names.index("delete_project")
    record = remains_mod.Record.from_bytes(remains_mod.record_path(PROJECT, tmp_path / "cache").read_bytes())
    assert [r.kind for r in record.left if r.kind == "project"] == ["project"]
    assert "30 days" in next(r for r in record.left if r.kind == "project").why


def test_project_kept_when_its_deletion_is_not_typed(tmp_path):
    store, _ = stored_store(tmp_path, install_state())
    world = FakeWorld(tmp_path)
    world.made_by_install = True
    f = flow(tmp_path, store, world)
    asked, f.confirm = _answers(PROJECT, "")
    assert f.run() == ExitCode.OK
    assert len(asked) == 2 and not world.project_deleted
    record = remains_mod.Record.from_bytes(remains_mod.record_path(PROJECT, tmp_path / "cache").read_bytes())
    project = next(r for r in record.left if r.kind == "project")
    assert "kept" in project.why
    # A project the install did not create is never offered.
    store2, _ = stored_store(tmp_path / "again", install_state())
    world2 = FakeWorld(tmp_path / "again")
    asked2: list[str] = []
    assert flow(tmp_path / "again", store2, world2, asked=asked2).run() == ExitCode.OK
    assert len(asked2) == 1 and "delete_project" not in world2.names()


def test_real_world_project_made_by_install_reads_its_label():
    from swarm_install.steps.preflight import CRM

    url = f"{CRM}/v1/projects/{PROJECT}"
    made = FakeApi({("GET", url): {"labels": {"managed-by": "swarm-install"}}, ("DELETE", url): {}})
    assert teardown_on(made).project_made_by_install() is True
    teardown_on(made).delete_project()
    assert ("DELETE", url, None) in made.calls
    assert teardown_on(FakeApi({("GET", url): {"labels": {}}})).project_made_by_install() is False
