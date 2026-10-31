"""`swarm uninstall`: remove what the install made, in reverse, and record what
it cannot (docs/design/day-one-plan.md §9.2, design §10; lane
DAYONE-UNINSTALL-1, part of #2688).

**One typed confirmation.** The person types the project id, once, after the
steps are printed. `SWARM_ASSUME_YES` never answers it (CLAUDE.md: anything
destructive needs a typed confirmation), and the same typed id is the
approval terraform.py's rule asks of every destroy.

**The order is §9.2's, and each step's identity is fixed by its number:**

| # | Step | Runs as | Reverses the install's |
|---|---|---|---|
| 1 | Scheduler jobs paused, running executions drained (30 min bound) | deployer | infra, verify, hand-off |
| 2 | tenant namespaces, then the cluster policies (skipped once the closing pass shut this machine out: step 3 deletes the cluster with them) | deployer | cluster, team |
| 3 | `terraform destroy` infra, after a targeted pass lifts `deletion_protection` and its buckets are emptied | deployer | registry, infra, GitHub and alert passes |
| 4 | the images in the project's registry | deployer | images |
| 5 | the secrets the engine created (`managed-by=swarm-install`), the Desktop client's included, and the ones swarm-api wrote (`managed-by=swarm-api`, every `swarm-tenant-*`: the teams' forge tokens) | deployer | secrets, Desktop client, GitHub, infra, team |
| 6 | engine-made IAM bindings; the workspace job's worker accounts and their project bindings; then the installer's impersonation grant on the deployer | owner | bootstrap, identity |
| 7 | the Firestore database infra abandons; `terraform destroy` bootstrap; the KMS key versions destroyed | owner | bootstrap |
| 8 | the budget; the "what remains" record written and printed; the state buckets; then, behind its own typed id, the project when stage 1 created it | owner | foundations |

Step 5 is the last that impersonates. Step 6 checks the owner's own rights
first and stops with exit 4 naming what is missing; it removes the
impersonation grant **before** step 7 deletes the account the grant names. A
second run never asks which identity the last one used: a crash inside step 7
resumes as the owner and never tries to impersonate an account that may be
gone (review-fix finding 5).

**From the install state's step records.** A step runs only when the install
recorded at least one of the steps it reverses; otherwise it is recorded as
skipped ("the install never made it"). Each uninstall step's progress is
recorded in the same state, as `uninstall.<name>`, so a second run continues
at the next unremoved step. Every step also checks the world before it acts,
so a step that half ran is finished, not repeated blindly.

**Only what is ours.** A Terraform destroy deletes only what carries
`managed-by=swarm-terraform` or `managed-by=swarm-install` (or a type that
carries no labels, which only its state can own): a labelled resource without
that label is taken out of the state, so it is never deleted, and recorded
in `left`. A name on the deny-list (the state's, R4) anywhere in a destroy, a
namespace, an image, a secret or a bucket refuses the uninstall; and since
only the shared project's state carries a deny-list, a state that carries
one is refused before anything is asked.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from swarm_redaction import redact

from . import remains as remains_mod
from . import tfvars
from .api_client import deployer_email
from .events import Events
from .exitcodes import ExitCode
from .lease import Lease, LeaseHeld, LeaseRefused
from .state import STEP_DONE, InstallState, StateStore, StepRecord, state_bucket
from .terraform import Terraform
from .waits import Clock, WaitGaveUp, WaitSpec, wait_until

#: The labels that mark a resource as the platform's (design §10).
OWNED_BY = ("swarm-terraform", "swarm-install")

#: The rights step 6 checks the owner holds before it runs (the steps 6-8 calls).
OWNER_PERMISSIONS = (
    "resourcemanager.projects.getIamPolicy",
    "resourcemanager.projects.setIamPolicy",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.delete",
    "iam.roles.delete",
    "cloudkms.cryptoKeyVersions.destroy",
    "storage.buckets.delete",
    "storage.objects.delete",
)

TOKEN_CREATOR = "roles/iam.serviceAccountTokenCreator"

#: §9.2 step 1: running executions get 30 minutes to end.
DRAIN = WaitSpec(
    reason="running Cloud Run job executions ending after their Scheduler jobs were paused",
    expected="a few minutes",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="the project's Cloud Run jobs page: an execution still running past 30 min can be cancelled there",
    interval_s=20,
)

#: Terraform addresses a destroy keeps out of its plan, and why. The state
#: buckets hold every root's state (and `prevent_destroy` guards the state
#: bucket), so step 8 deletes them after both destroys; the KMS ring and key
#: are never deleted (design §10), so step 7 destroys their versions instead.
KEEP: dict[str, tuple[str, ...]] = {
    "infra": (),
    "bootstrap": (
        "google_storage_bucket.state",
        "google_storage_bucket.state_logs",
        "google_kms_key_ring.",
        "google_kms_crypto_key.",
    ),
}

PLAN_FILE = "swarm-install.tfplan"
DEPLOYER = "deployer"
OWNER = "owner"


class UninstallRefused(RuntimeError):
    """A rule refused the uninstall; what was refused was not touched."""


class MissingPermission(RuntimeError):
    """The owner lacks a right steps 6-8 need (exit 4)."""


@dataclass(frozen=True)
class Step:
    number: int
    name: str
    title: str
    #: Install step-id prefixes whose records make this step needed.
    reverses: tuple[str, ...]

    @property
    def id(self) -> str:
        return f"uninstall.{self.name}"

    @property
    def identity(self) -> str:
        """Fixed by the number, never by what the last run used (finding 5)."""
        return DEPLOYER if self.number <= 5 else OWNER


STEPS: tuple[Step, ...] = (
    Step(1, "scheduler", "pause the Scheduler jobs and drain running executions (30 min bound)",
         ("infra.", "verify.", "handoff.")),
    Step(2, "kubernetes", "delete the tenant namespaces, then the cluster policies", ("cluster.", "team.")),
    Step(3, "infra", "lift deletion protection, empty the buckets, then terraform destroy infra as the deployer "
         "(only labelled resources)",
         ("images.registry", "infra.", "github.", "alerts.")),
    Step(4, "images", "delete the images in the project's registry", ("images.",)),
    # swarm-api writes each team's -git slot (managed-by=swarm-api) once infra
    # runs it, so step 5 reverses infra and team too (audit 4e F7).
    Step(5, "secrets", "delete the secrets the engine and swarm-api created, the teams' forge tokens included",
         ("secrets.", "edge.desktop-client", "github.", "infra.", "team.")),
    Step(6, "iam", "remove the engine-made IAM bindings and the workspace worker accounts, then your "
         "impersonation grant on the deployer",
         ("bootstrap.", "identity.")),
    Step(7, "bootstrap", "delete the Firestore database; terraform destroy bootstrap as you; destroy the KMS key "
         "versions",
         ("bootstrap.",)),
    Step(8, "state", "delete the budget, write and print what remains, delete the state buckets, then offer the "
         "project's deletion when the install created it",
         ("foundations.",)),
)


def who_for(step: Step, project: str) -> str | None:
    """The account a step impersonates, or None: it runs as the owner."""
    return deployer_email(project) if step.identity == DEPLOYER else None


def budget_name(project: str) -> str:
    """foundations.budget's display name for the project's budget (`BudgetStep._name`)."""
    return f"swarm-{project}"


def logs_bucket(project: str) -> str:
    """bootstrap's access-log bucket for the state bucket (terraform/bootstrap/main.tf)."""
    return f"swarm-tfstate-logs-{project}"


class Teardown(Protocol):
    """The calls the uninstall makes on the world (a fake in tests).

    `who` is the account a call impersonates, or None for the owner.
    """

    def scheduler_jobs(self, who: str | None) -> list[str]: ...

    def pause_job(self, name: str, who: str | None) -> None: ...

    def running_executions(self, who: str | None) -> list[str]: ...

    def tenant_namespaces(self, who: str | None) -> list[str]: ...

    def delete_namespace(self, name: str, who: str | None) -> None: ...

    def delete_policies(self, who: str | None) -> str | None: ...

    def terraform(self, root: str) -> Terraform: ...

    def registry_images(self, who: str | None) -> list[str]: ...

    def delete_image(self, name: str, who: str | None) -> None: ...

    def engine_secrets(self, who: str | None) -> list[str]: ...

    def delete_secret(self, name: str, who: str | None) -> None: ...

    def missing_permissions(self, permissions: Sequence[str]) -> list[str]: ...

    def remove_project_binding(self, role: str, member: str) -> bool: ...

    def remove_impersonation_grant(self, deployer: str, member: str) -> bool: ...

    def destroy_key_versions(self, key: str) -> int: ...

    def delete_bucket(self, name: str) -> bool: ...

    def empty_bucket(self, name: str, who: str | None) -> int: ...

    def workspace_accounts(self) -> list[str]: ...

    def remove_member_bindings(self, member: str) -> int: ...

    def delete_account(self, email: str) -> bool: ...

    def delete_firestore(self, database: str) -> bool: ...

    def delete_budgets(self, account: str, display_name: str) -> list[str]: ...

    def project_made_by_install(self) -> bool: ...

    def delete_project(self) -> None: ...


# -- the rules a destroy keeps ---------------------------------------------------


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def denied(names: Iterable[str], deny_list: Sequence[str]) -> list[str]:
    """Every deny-listed entry among `names` (whole, after a `/`, or after a `:`)."""
    deny = [d for d in deny_list if d]
    hits: list[str] = []
    for name in names:
        for entry in deny:
            if name == entry or name.endswith(f"/{entry}") or name.endswith(f":{entry}"):
                hits.append(entry)
    return sorted(set(hits))


def refuse_denied(what: str, names: Iterable[str], deny_list: Sequence[str]) -> None:
    hits = denied(names, deny_list)
    if hits:
        raise UninstallRefused(f"{what} names {', '.join(hits)}, on the shared deny-list; nothing of it was touched")


def ours(before: Mapping[str, Any] | None) -> bool:
    """A resource is ours when it carries our `managed-by`, or when its type
    carries no labels at all (then only its Terraform state can own it)."""
    before = before or {}
    keys = [k for k in ("effective_labels", "terraform_labels", "labels") if k in before]
    if not keys:
        return True
    return any(str((before.get(k) or {}).get("managed-by", "")) in OWNED_BY for k in keys)


def kept(root: str, address: str) -> bool:
    for keep in KEEP.get(root, ()):
        if keep.endswith(".") and address.startswith(keep):
            return True
        if address == keep or address.startswith(f"{keep}["):
            return True
    return False


@dataclass
class Destroyed:
    """What one root's destroy did and saw."""

    deleted: list[str] = field(default_factory=list)
    left: list[remains_mod.Remain] = field(default_factory=list)
    custom_roles: list[str] = field(default_factory=list)
    kms_keys: list[str] = field(default_factory=list)
    kms_key_ring: str | None = None


def _plan_destroy(tf: Terraform, who: str | None) -> dict[str, Any]:
    tf.run("plan", "-destroy", "-input=false", f"-out={PLAN_FILE}", impersonate=who)
    return json.loads(tf.run("show", "-json", PLAN_FILE, impersonate=who).stdout or "{}")


def _deletes(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in plan.get("resource_changes") or ()
            if "delete" in ((c.get("change") or {}).get("actions") or ())]


def _protected(plan: Mapping[str, Any]) -> list[str]:
    """The planned deletes Google refuses while their `deletion_protection` is on."""
    return [str(c.get("address")) for c in _deletes(plan)
            if ((c.get("change") or {}).get("before") or {}).get("deletion_protection") is True]


def _buckets(plan: Mapping[str, Any]) -> list[str]:
    """The planned bucket deletes Storage refuses while they hold objects."""
    found: list[str] = []
    for change in _deletes(plan):
        before = (change.get("change") or {}).get("before") or {}
        if change.get("type") == "google_storage_bucket" and before.get("name") and not before.get("force_destroy"):
            found.append(str(before["name"]))
    return found


def _unprotect(tf: Terraform, targets: Sequence[str], who: str | None,
               announce: Callable[[str], None]) -> None:
    """Audit 4e F1: one infra pass, targeted at the protected resources alone,
    that applies the uninstall's inputs (`tfvars.UNINSTALL_OVERLAY`), so the
    destroy that follows is not refused. Targeted, so the pass changes nothing
    but those resources' protection; a plan that would delete or replace
    anything is refused before it applies."""
    plan = tf.plan(targets=targets, impersonate=who)
    moved = [str(c.get("address")) for c in plan.get("resource_changes") or ()
             if set((c.get("change") or {}).get("actions") or ()) - {"update", "no-op", "read"}]
    if moved:
        raise UninstallRefused(f"{tf.root}'s unprotect pass would also change {', '.join(moved)}; nothing was "
                               "applied")
    announce(f"terraform apply: {tf.root}, deletion protection lifted on {len(targets)} resource(s)"
             + (f" as {who}" if who else " as you"))
    tf.apply(impersonate=who)


def destroy_root(tf: Terraform, *, project: str, who: str | None, inputs: Mapping[str, Any] | None,
                 deny_list: Sequence[str], approved: str,
                 announce: Callable[[str], None] = lambda line: None,
                 empty_bucket: Callable[[str], Any] | None = None) -> Destroyed:
    """`terraform destroy` one root, under terraform.py's rules and this module's.

    The same typed project id that approved the uninstall approves the destroy
    (terraform.py: a destroy needs the project id typed, never
    SWARM_ASSUME_YES). Nothing is applied unless every rule holds.

    `inputs` are the uninstall's (`tfvars.uninstall_inputs`). Before the
    destroy, the resources whose `deletion_protection` is on get a targeted
    pass that lifts it, and each bucket the destroy deletes is emptied through
    `empty_bucket` (audit 4e F1: Google refuses both otherwise, and the next
    run would fail identically).
    """
    done = Destroyed()
    tf.check_version()
    if inputs is not None:
        tfvars.write_local(tf.root_dir, inputs)
    tf.init(impersonate=who)
    addresses = tf.state_list(impersonate=who)
    if not addresses:
        return done
    if inputs is None:
        raise UninstallRefused(f"{tf.root} has state but no generated inputs in gs://{tf.state_bucket}/"
                               f"{tfvars.object_name(tf.root)}, so its destroy cannot be planned")
    plan = _plan_destroy(tf, who)
    for change in _deletes(plan):
        before = (change.get("change") or {}).get("before") or {}
        refuse_denied(f"{tf.root}'s destroy of {change.get('address')}", _strings(before), deny_list)
    keep = [a for a in addresses if kept(tf.root, a)]
    unowned = [str(c.get("address")) for c in _deletes(plan)
               if not ours((c.get("change") or {}).get("before")) and not kept(tf.root, str(c.get("address")))]
    for change in _deletes(plan):
        before = (change.get("change") or {}).get("before") or {}
        rtype = str(change.get("type", ""))
        if rtype == "google_project_iam_custom_role" and before.get("role_id"):
            done.custom_roles.append(str(before["role_id"]))
        if rtype == "google_kms_key_ring" and before.get("id"):
            done.kms_key_ring = str(before["id"])
        if rtype == "google_kms_crypto_key" and before.get("id"):
            done.kms_keys.append(str(before["id"]))
    for address in unowned:
        done.left.append(remains_mod.Remain(
            kind="unlabelled", name=f"{tf.root}: {address}",
            why=f"it carries no managed-by label of {', '.join(OWNED_BY)}, so it is not the install's to delete; "
                "it was taken out of Terraform state and left in place",
            step=f"https://console.cloud.google.com/home/dashboard?project={project}: delete it by hand if "
                 "it is yours"))
    if keep or unowned:
        for address in [*keep, *unowned]:
            tf.run("state", "rm", address, impersonate=who)
        plan = _plan_destroy(tf, who)
    deletes = [str(c.get("address")) for c in _deletes(plan)]
    if not deletes:
        return done
    if approved.strip() != project:
        raise UninstallRefused(f"{tf.root}'s destroy was not approved with the project id; nothing was applied")
    protected = _protected(plan)
    if protected:
        _unprotect(tf, protected, who, announce)
    buckets = _buckets(plan)
    if buckets and empty_bucket is not None:
        for name in buckets:
            empty_bucket(name)
    if protected or buckets:
        plan = _plan_destroy(tf, who)
        deletes = [str(c.get("address")) for c in _deletes(plan)]
    announce(f"terraform destroy: {tf.root} ({len(deletes)} resources)" + (f" as {who}" if who else " as you"))
    tf.apply(impersonate=who)
    done.deleted = deletes
    return done


# -- the run ------------------------------------------------------------------------


@dataclass
class Uninstall:
    """One `swarm uninstall` of `store.project`."""

    store: StateStore
    world: Teardown
    confirm: Callable[[str], str]
    clock: Clock
    events: Events
    out: TextIO
    holder: str
    lease: Lease | None = None
    remains_dir: Path | None = None
    state: InstallState = field(init=False)
    approved: str = field(default="", init=False)

    @property
    def project(self) -> str:
        return self.store.project

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    # -- the record --------------------------------------------------------------

    def _record(self, step: Step, detail: str) -> None:
        record = StepRecord(state=STEP_DONE, at=self.clock.now(), by=self.holder, detail=detail)
        self.state = self.store.update(lambda s: s.steps.__setitem__(step.id, record))

    def _remember(self, **outputs: Any) -> None:
        def change(s: Any) -> None:
            found = dict(s.outputs.get("uninstall") or {})
            for key, value in outputs.items():
                if isinstance(value, list):
                    merged = list(found.get(key) or [])
                    merged.extend(v for v in value if v not in merged)
                    found[key] = merged
                elif value is not None:
                    found[key] = value
            s.outputs["uninstall"] = found

        self.state = self.store.update(change)

    def _found(self) -> dict[str, Any]:
        return dict(self.state.outputs.get("uninstall") or {})

    def _installed(self, step: Step) -> bool:
        return any(sid.startswith(prefix) for sid in self.state.steps for prefix in step.reverses)

    # -- refusals before anything is asked ------------------------------------------

    def refusal(self) -> str | None:
        # R4: the state carries a deny-list only in the shared project (CLAUDE.md
        # rule 2), so any deny-list at all refuses the whole uninstall. No
        # project id is written here: a live-deployment literal is refused in
        # apps/ (tests/unit/scripts/test_no_live_literals.py).
        deny = list(self.state.deny_list)
        if self.project in deny:
            return f"{self.project} is on the shared deny-list; swarm uninstall never touches it"
        if deny:
            return (f"the install state of {self.project} carries the shared deny-list, which only the shared "
                    "project's does; swarm uninstall never touches it")
        return None

    def prompt(self) -> str:
        lines = [f"swarm uninstall removes the SwarmCloud install in {self.project}, in this order:"]
        for step in STEPS:
            mark = "done" if self.state.done(step.id) else ("as the deployer" if step.identity == DEPLOYER
                                                             else "as you")
            lines.append(f"  {step.number}. {step.title} [{mark}]")
        lines.append("What cannot be removed is recorded and printed at the end.")
        lines.append(f"This deletes the deployment and its data. Type the project id ({self.project}) to "
                     "approve, anything else refuses: ")
        return "\n".join(lines)

    def run(self) -> ExitCode:
        self.state = self.store.load()
        refused = self.refusal()
        if refused is not None:
            self.say(f"swarm uninstall: refused: {refused}")
            return ExitCode.REFUSED
        if all(self.state.done(s.id) for s in STEPS):
            self.say(f"swarm uninstall: {self.project} is already uninstalled; what remains is in "
                     f"{remains_mod.record_path(self.project, self.remains_dir)}")
            return ExitCode.OK
        # SWARM_ASSUME_YES is never read: the person types the project id.
        typed = self.confirm(self.prompt()).strip()
        if typed != self.project:
            self.say("swarm uninstall: not approved (the project id was not typed); nothing was removed")
            return ExitCode.REFUSED
        self.approved = typed
        step = STEPS[0]
        try:
            if self.lease is not None:
                self.lease.acquire(self.confirm)
            for step in STEPS:
                if self.state.done(step.id):
                    continue
                self.events.emit("step_check", step=step.id, number=step.number, identity=step.identity,
                                 detail=step.title)
                if not self._installed(step):
                    detail = "skipped: the install never made what this step removes"
                else:
                    detail = self._run(step)
                self._record(step, detail)
                self.events.emit("step_done", step=step.id, number=step.number, detail=detail)
        except WaitGaveUp as exc:
            self.say(str(exc).replace("swarm install", "swarm uninstall"))
            return ExitCode.PAUSED
        except MissingPermission as exc:
            self.say(f"swarm uninstall: {exc}")
            return ExitCode.PAUSED
        except (UninstallRefused, LeaseHeld, LeaseRefused) as exc:
            self.say(f"swarm uninstall: refused: {exc}")
            return ExitCode.REFUSED
        except Exception as exc:  # a failed call: said (redacted), and the next run continues at this step
            message = redact(f"{type(exc).__name__}: {exc}").text
            self.events.emit("step_failed", step=step.id, detail=message)
            self.say(f"swarm uninstall: step {step.number} ({step.title}) failed: {message}. "
                     "Run swarm uninstall again to continue from this step.")
            return ExitCode.FAIL
        finally:
            if self.lease is not None:
                try:
                    self.lease.release()
                except Exception as exc:  # the lease expires by itself; say so, never mask the outcome
                    self.say(f"swarm uninstall: the lease was not released ({type(exc).__name__}); it expires")
        self.events.emit("run_end", outcome="ok", exit_code=int(ExitCode.OK), message="uninstalled")
        return ExitCode.OK

    def _run(self, step: Step) -> str:
        who = who_for(step, self.project)
        return getattr(self, f"_step_{step.name}")(who)

    # -- the eight steps ------------------------------------------------------------

    def _step_scheduler(self, who: str | None) -> str:
        jobs = self.world.scheduler_jobs(who)
        refuse_denied("a Scheduler job", jobs, self.state.deny_list)
        for job in jobs:
            self.world.pause_job(job, who)

        def drained() -> str:
            return "met" if not self.world.running_executions(who) else "waiting"

        wait_until("uninstall.scheduler", DRAIN, drained, clock=self.clock, events=self.events)
        return f"{len(jobs)} job(s) paused; no execution running"

    def _step_kubernetes(self, who: str | None) -> str:
        from .steps.infra import reach_closed

        # Audit 4e F2: after infra.pass-close the control plane admits no
        # address of this machine, so kubectl only waits out CONTROL_PLANE. The
        # namespaces and the cluster-scoped policies live in the cluster step 3
        # destroys, so they go with it; reopening would be one more infra apply
        # (and one more way to stop) for objects deleted a minute later.
        if reach_closed(self.state.outputs) and not self.state.done(STEPS[2].id) and self._installed(STEPS[2]):
            return ("skipped: the closing pass removed this machine from the control plane's authorised "
                    "networks, and step 3 destroys the cluster with its namespaces and policies")
        names = self.world.tenant_namespaces(who)
        refuse_denied("a tenant namespace", names, self.state.deny_list)
        for name in names:
            self.world.delete_namespace(name, who)
        policies = self.world.delete_policies(who)
        if policies:
            self._remember(left=[{"kind": "cluster-policies", "name": "the cluster's policies", "why": policies,
                                  "step": "nothing to do: step 3 deletes the cluster that holds them"}])
        return f"{len(names)} tenant namespace(s) deleted; " + ("policies: " + policies if policies
                                                                  else "cluster policies deleted")

    def _destroy(self, root: str, who: str | None) -> Destroyed:
        bucket = self.store.bucket
        inputs = tfvars.stored(bucket, root) if bucket is not None else None
        tf = self.world.terraform(root)
        done = destroy_root(tf, project=self.project, who=who,
                            inputs=None if inputs is None else tfvars.uninstall_inputs(root, inputs),
                            deny_list=self.state.deny_list, approved=self.approved,
                            announce=lambda line: self.events.emit("step_apply", step=f"uninstall.{root}",
                                                                   message=line),
                            empty_bucket=lambda name: self.world.empty_bucket(name, who))
        if root == "infra" and inputs and inputs.get("firestore_database"):
            self._remember(firestore_database=str(inputs["firestore_database"]))
        self._remember(left=[vars(r) for r in done.left], custom_roles=done.custom_roles,
                       kms_keys=done.kms_keys, kms_key_ring=done.kms_key_ring)
        return done

    def _step_infra(self, who: str | None) -> str:
        done = self._destroy("infra", who)
        return f"{len(done.deleted)} resource(s) destroyed, {len(done.left)} unlabelled left in place"

    def _step_images(self, who: str | None) -> str:
        images = self.world.registry_images(who)
        refuse_denied("a registry image", images, self.state.deny_list)
        for image in images:
            self.world.delete_image(image, who)
        return f"{len(images)} image(s) deleted"

    def _step_secrets(self, who: str | None) -> str:
        secrets = self.world.engine_secrets(who)
        refuse_denied("a secret", secrets, self.state.deny_list)
        for secret in secrets:
            self.world.delete_secret(secret, who)
        return f"{len(secrets)} secret(s) deleted"

    def _step_iam(self, who: str | None) -> str:
        missing = self.world.missing_permissions(OWNER_PERMISSIONS)
        if missing:
            raise MissingPermission(
                f"steps 6-8 run as you, and you lack {', '.join(missing)} on {self.project}: grant them "
                "(roles/owner holds every one), then run swarm uninstall again to continue")
        removed = 0
        for binding in self.state.outputs.get("engine_iam_bindings") or ():
            role, member = str(binding.get("role")), str(binding.get("member"))
            refuse_denied("an engine-made IAM binding", [member], self.state.deny_list)
            removed += int(self.world.remove_project_binding(role, member))
        # Audit 4e F7: the workspace job (register-tenant.sh --workspace) makes
        # each worker account and its project bindings outside Terraform, so no
        # destroy removes them. Bindings first: a deleted account's binding
        # lingers as `deleted:serviceAccount:...`.
        accounts = self.world.workspace_accounts()
        refuse_denied("a workspace worker account", accounts, self.state.deny_list)
        bindings = 0
        for email in accounts:
            bindings += self.world.remove_member_bindings(f"serviceAccount:{email}")
            self.world.delete_account(email)
        installer = str(self.state.outputs.get("installer") or self.holder)
        grant = self.world.remove_impersonation_grant(deployer_email(self.project), f"user:{installer}")
        return (f"{removed} engine-made binding(s) removed; {len(accounts)} workspace account(s) and their "
                f"{bindings} project binding(s) removed; impersonation grant "
                + ("removed" if grant else "already gone"))

    def _step_bootstrap(self, who: str | None) -> str:
        # Audit 4e F7: infra's database is `deletion_policy = "ABANDON"`, so
        # step 3's destroy leaves it, with its data. The owner deletes it here,
        # before the KMS key versions go.
        database = ""
        if self._installed(STEPS[2]):
            database = str(self._found().get("firestore_database") or remains_mod.DEFAULT_FIRESTORE_DATABASE)
            refuse_denied("the Firestore database", [database], self.state.deny_list)
            self._remember(firestore_database=database,
                           firestore_deleted=bool(self.world.delete_firestore(database)) or None)
        done = self._destroy("bootstrap", who)
        keys = list(self._found().get("kms_keys") or ())
        if not keys:
            region = self.state.region or ""
            keys = [f"projects/{self.project}/locations/{region}/keyRings/swarm-{self.state.environment}-specs/"
                    "cryptoKeys/step-spec"]
        destroyed = sum(self.world.destroy_key_versions(key) for key in keys)
        return (f"{len(done.deleted)} resource(s) destroyed; {destroyed} KMS key version(s) destroyed"
                + (f"; Firestore database {database} "
                   + ("deleted" if self._found().get("firestore_deleted") else "not found") if database else ""))

    def _budget(self) -> remains_mod.Remain | None:
        """Audit 4e F7: the budget lives on the billing account and outlives the
        project; it is found by foundations.budget's name. None when it is gone;
        what to do by hand when the billing account refuses."""
        account = str(self.state.outputs.get("billing_account") or "")
        if not account:
            return None
        name = budget_name(self.project)
        page = f"https://console.cloud.google.com/billing/{account}/budgets"
        try:
            self.world.delete_budgets(account, name)
        except Exception as exc:  # a refusal on the billing account is a remain, not a stop
            return remains_mod.Remain(
                kind="budget", name=f"billingAccounts/{account}: {name}",
                why=redact(f"the billing account refused its deletion ({type(exc).__name__}: {exc})").text[:400],
                step=f"{page}: delete the budget named {name}")
        return None

    def _project(self) -> tuple[bool, remains_mod.Remain | None]:
        """Audit 4e F7: when stage 1 created the project (its `managed-by=swarm-install`
        label), offer its deletion behind its own typed id; (approved, the record's line)."""
        if not self.world.project_made_by_install():
            return False, None
        typed = self.confirm(
            f"swarm install created the project {self.project}. Type its id again to delete the project too "
            "(Google shuts it down now and deletes it after 30 days; its id is never reusable), anything else "
            "keeps it: ").strip()
        if typed == self.project:
            return True, remains_mod.Remain(
                kind="project", name=self.project,
                why="deleted: Google shuts the project down now, deletes it after 30 days, and never reissues "
                    "its id",
                step=f"nothing to do; to restore it within 30 days: https://console.cloud.google.com/"
                     f"cloud-resource-manager?pendingDeletion=true ({self.project})")
        return False, remains_mod.Remain(
            kind="project", name=self.project,
            why="kept: its deletion was not typed; swarm install created it, and it holds nothing of the "
                "install's but what this record lists",
            step=f"https://console.cloud.google.com/iam-admin/settings?project={self.project}: Shut down")

    def _step_state(self, who: str | None) -> str:
        budget = self._budget()
        delete_project, project = self._project()
        fresh = [vars(r) for r in (budget, project) if r is not None]

        def relist(st: Any) -> None:
            # A rerun of this step answers again: its lines replace the last run's.
            found = dict(st.outputs.get("uninstall") or {})
            found["left"] = [i for i in found.get("left") or () if i.get("kind") not in ("budget", "project")] + fresh
            st.outputs["uninstall"] = found

        self.state = self.store.update(relist)
        found = self._found()
        record = remains_mod.Record(
            project=self.project, environment=self.state.environment, written_at=self.clock.now(),
            remains=remains_mod.survivors(self.state, found),
            left=remains_mod.left_from(found.get("left") or ()))
        path = remains_mod.write(record, self.remains_dir)
        self.say(record.render())
        self.say(f"(written to {path})")
        buckets = [state_bucket(self.project), logs_bucket(self.project)]
        refuse_denied("a state bucket", buckets, self.state.deny_list)
        if self.lease is not None and self.store.bucket is not None:
            self.lease.release()
            self.lease = None
        # From here the local file is the state: the bucket is about to go.
        self.store.bucket = None
        gone = [name for name in buckets if self.world.delete_bucket(name)]
        if delete_project:
            self.world.delete_project()
        return (f"what remains written to {path}; bucket(s) deleted: {', '.join(gone) or 'already gone'}; "
                f"budget {'left: ' + budget.step if budget else 'deleted'}; project "
                + ("deleted" if delete_project else "kept"))
