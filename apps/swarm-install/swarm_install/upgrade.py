"""`swarm upgrade --to V`: plan, IAM gate, ordered apply, platform checks,
roll back (docs/design/day-one-plan.md §9.3, lane DAYONE-UPGRADE-1, part of
#2688).

1. **Refuse a downgrade** (exit 5) unless `--allow-downgrade`.
2. **Fetch and verify release V**: its `images.json` read through the
   release's `SHA256SUMS`, which cosign verifies with the key pinned in this
   wheel (as stage 4 does, `steps/images.py`).
3. **Plan everything and show it**: both roots' Terraform plans, the image
   digests that change, the Firestore migrations still to run, the cluster
   policy diff, and new GitHub App permissions with the page where the owner
   accepts them on the installation (lesson 44: a changed permission is not in
   force until accepted).
4. **IAM gate** (lesson 26): an IAM resource created, changed or deleted in
   either plan is shown row by row and waits for the project id TYPED;
   anything else refuses (exit 5) and nothing is applied. The prompt says what
   it blocks. A destroy-first IAM swap or a condition the linter refuses is
   refused outright (lesson 10).
5. **Drain**: dispatch paused (the platform's own switch,
   `POST /v1/admin/dispatch/pause`), running work left to checkpoint and park
   (invariant 4), bounded at 30 min with a countdown.
6. **Apply in this order** (`ORDER`; lesson 33): copy images, `additive`
   migrations, bootstrap, the policies that admit more, infra, the policies
   that tighten, `needs-new-code` migrations, the namespaces and verify keys
   of EVERY tenant (lesson 31). Infra's plan replacing a log-based metric (a
   new label forces it, and GCP will not delete a metric an alert uses) is
   split: the label is added to the live metric first, then infra applies in
   place (lesson 14).
7. **Verify**: the platform checks (stage 14's quick checks), then
   `removes-data` migrations, then dispatch resumed.
8. **Roll back** a failed image roll (infra or the checks): the previous
   release's digests and generated inputs are applied again and the install
   state records the previous release. Data-removing migrations never ran.

**Resume.** Progress is in the install state (`outputs["upgrade"]`): the
target, the release it started from, the steps done and the one that
failed. `swarm upgrade --to V` again resumes at the failed step; a roll back
drops the drain, infra and every step after it from `done`, so they run again.

**The policy diff.** Each release's rendered policies are compared by
`(kind, name)`: a policy only the previous release has is deleted, and one
whose content changed is applied, in the admit slot (before the new code); a
policy only V has is applied in the tighten slot, after the new code is in.

Everything outside this module goes through `UpgradeWorld`, so the tests run
with no credentials; `verbs/upgrade.py` builds the real one from the
install's own steps.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml

from . import migrations as mig
from .exitcodes import ExitCode
from .terraform import _IAM_TYPE, review_plan
from .events import Events
from .waits import Clock, WaitGaveUp, WaitSpec, wait_until

#: §9.3 step 6, then 7: the order the upgrade applies in.
DRAIN = "drain"
COPY_IMAGES = "copy-images"
MIGRATIONS_ADDITIVE = "migrations-additive"
BOOTSTRAP = "bootstrap"
POLICIES_ADMIT = "policies-admit"
INFRA = "infra"
POLICIES_TIGHTEN = "policies-tighten"
MIGRATIONS_NEW_CODE = "migrations-new-code"
NAMESPACES = "namespaces"
VERIFY = "verify"
MIGRATIONS_CLEANUP = "migrations-cleanup"
RESUME = "resume-dispatch"
ORDER = (DRAIN, COPY_IMAGES, MIGRATIONS_ADDITIVE, BOOTSTRAP, POLICIES_ADMIT, INFRA, POLICIES_TIGHTEN,
         MIGRATIONS_NEW_CODE, NAMESPACES, VERIFY, MIGRATIONS_CLEANUP, RESUME)

#: A failure of these is a failed image roll, and is rolled back (§9.3 step 8).
ROLLED_BACK = (INFRA, VERIFY)

#: What the upgrade records in `InstallState.outputs`.
UPGRADE_KEY = "upgrade"
PREVIOUS_KEY = "previous_release"

DRAIN_WAIT = WaitSpec(
    reason="running work to checkpoint and park before the new release rolls out",
    expected="a few minutes",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="the console's Tasks page: a task still RUNNING past 30 min is not checkpointing",
    interval_s=15,
)

_LOG_METRIC = "google_logging_metric"
_CUSTOM_ROLE = re.compile(r"^google_.*_iam_custom_role$")


class UpgradeRefused(RuntimeError):
    """Exit 5: a downgrade, an unapproved IAM change, a plan that cannot be applied safely."""


class UpgradeFailed(RuntimeError):
    """Exit 1: a step failed; the state names it and the next run resumes there."""


# -- versions -------------------------------------------------------------------


def version_key(version: str) -> tuple[Any, ...]:
    """Order releases: `2026.11.1` > `2026.11.1-rc.2` > `2026.11.1-rc.1` > `2026.10.3`."""
    main, _, pre = str(version).lstrip("v").partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in main.split("."))
    if not pre:
        return (numbers, 1, ())
    return (numbers, 0, tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", pre)))


# -- the world --------------------------------------------------------------------


class UpgradeWorld(Protocol):
    """Everything the upgrade does outside this module, each a fake in tests."""

    def fetch(self, version: str, name: str) -> bytes:
        """A file of release `version`, verified against its cosign-verified SHA256SUMS."""

    def plan_root(self, root: str, version: str) -> dict[str, Any]:
        """`terraform show -json` of the root's plan with `version`'s generated inputs."""

    def apply_root(self, root: str, version: str) -> None:
        """The root's latest install pass, with `version`'s generated inputs."""

    def patch_log_metric(self, address: str, after: Mapping[str, Any]) -> None:
        """Add the planned labels to the live log-based metric (lesson 14)."""

    def copy_images(self, version: str, images: Mapping[str, str]) -> None:
        """Verify every image with the pinned key and copy it by digest."""

    def policies(self, version: str) -> str:
        """`version`'s rendered cluster policies (multi-document YAML)."""

    def apply_policies(self, rendered: str) -> None: ...

    def delete_policies(self, documents: Sequence[tuple[str, str]]) -> None: ...

    def app_permissions(self) -> dict[str, str] | None:
        """The GitHub App's permissions as they are now, or None with no App yet."""

    def release_app_permissions(self, version: str) -> dict[str, str] | None:
        """The permissions release `version` asks the App for, or None if it names none."""

    def app_review_url(self) -> str: ...

    def tenants(self) -> list[str]: ...

    def apply_namespace(self, tenant: str) -> None:
        """The tenant's namespace objects and verify keys for the release now running (lesson 31)."""

    def pause_dispatch(self, reason: str) -> None: ...

    def resume_dispatch(self, reason: str) -> None: ...

    def running(self) -> int:
        """Tasks that hold infrastructure (`LEASED` .. `RUNNING`, invariant 1)."""

    def checks(self) -> list[tuple[str, str, str]]:
        """The platform checks: `(name, status, detail)`; only `met` is green."""

    def documents(self) -> mig.Documents: ...


class StateAccess(Protocol):
    def load(self) -> Any: ...

    def update(self, change: Callable[[Any], None]) -> Any: ...


# -- the plan ---------------------------------------------------------------------


@dataclass(frozen=True)
class IamRow:
    root: str
    action: str
    address: str
    role: str
    member: str

    def line(self) -> str:
        who = f" {self.member}" if self.member else ""
        return f"  {self.action:<7} {self.root}: {self.address} ({self.role or 'role'}{who})"


@dataclass
class UpgradePlan:
    previous: str
    target: str
    terraform: dict[str, dict[str, Any]]
    images: dict[str, tuple[str | None, str]]
    migrations: list[str]
    policy_admit: list[tuple[str, str]]
    policy_delete: list[tuple[str, str]]
    policy_tighten: list[tuple[str, str]]
    iam: list[IamRow]
    app_added: dict[str, str]
    replaced_metrics: list[str] = field(default_factory=list)


def _actions(change: Mapping[str, Any]) -> list[str]:
    return list((change.get("change") or {}).get("actions") or ())


def _action_word(actions: Sequence[str]) -> str | None:
    if not actions or actions in (["no-op"], ["read"]):
        return None
    if "create" in actions and "delete" in actions:
        return "replace"
    return actions[0]


def iam_rows(root: str, plan: Mapping[str, Any]) -> list[IamRow]:
    """Every IAM grant, binding, policy or custom role the plan creates, changes or deletes."""
    rows = []
    for change in plan.get("resource_changes") or ():
        rtype = str(change.get("type", ""))
        if not (_IAM_TYPE.match(rtype) or _CUSTOM_ROLE.match(rtype)):
            continue
        word = _action_word(_actions(change))
        if word is None:
            continue
        body = (change.get("change") or {}).get("after") or (change.get("change") or {}).get("before") or {}
        member = body.get("member") or ", ".join(body.get("members") or ())
        rows.append(IamRow(root, word, str(change.get("address")), str(body.get("role") or body.get("role_id")
                                                                          or ""), str(member or "")))
    return rows


def replaced_metrics(plan: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(str(c.get("address")), dict((c.get("change") or {}).get("after") or {}))
            for c in plan.get("resource_changes") or ()
            if str(c.get("type")) == _LOG_METRIC and _action_word(_actions(c)) == "replace"]


def _documents(rendered: str) -> dict[tuple[str, str], Any]:
    found = {}
    for doc in yaml.safe_load_all(rendered or ""):
        if isinstance(doc, dict):
            found[(str(doc.get("kind")), str((doc.get("metadata") or {}).get("name")))] = doc
    return found


def policy_diff(previous: str, target: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]],
                                                      list[tuple[str, str]]]:
    """(changed: admit slot, removed: deleted in the admit slot, added: tighten slot)."""
    old, new = _documents(previous), _documents(target)
    changed = sorted(k for k in new if k in old and new[k] != old[k])
    removed = sorted(k for k in old if k not in new)
    added = sorted(k for k in new if k not in old)
    return changed, removed, added


def _subset(rendered: str, wanted: Sequence[tuple[str, str]]) -> str:
    docs = _documents(rendered)
    return yaml.safe_dump_all([docs[k] for k in wanted], sort_keys=False)


def parse_images_json(data: bytes) -> dict[str, str]:
    from .steps.images import parse_images

    return parse_images(data)


# -- the upgrade ------------------------------------------------------------------


@dataclass
class Upgrade:
    project: str
    world: UpgradeWorld
    store: StateAccess
    events: Events
    clock: Clock
    confirm: Callable[[str], str]
    out: Callable[[str], None] = print
    migrations: Sequence[mig.Migration] = field(default_factory=mig.discover)

    # -- state ----------------------------------------------------------------

    def _progress(self) -> dict[str, Any]:
        return dict(self.store.load().outputs.get(UPGRADE_KEY) or {})

    def _save_progress(self, **fields: Any) -> None:
        def change(state: Any) -> None:
            progress = dict(state.outputs.get(UPGRADE_KEY) or {})
            progress.update(fields)
            state.outputs[UPGRADE_KEY] = progress

        self.store.update(change)

    def running_release(self) -> str:
        state = self.store.load()
        progress = dict(state.outputs.get(UPGRADE_KEY) or {})
        if progress.get("to") and progress.get("to") != progress.get("finished"):
            return str(progress.get("from") or state.release or "")
        return str(state.release or "")

    # -- plan -----------------------------------------------------------------

    def plan(self, target: str, previous: str) -> UpgradePlan:
        images_new = parse_images_json(self.world.fetch(target, "images.json"))
        try:
            images_old = parse_images_json(self.world.fetch(previous, "images.json"))
        except Exception:  # noqa: BLE001 -- the previous release's files are informative only here
            images_old = {}
        images = {name: (images_old.get(name), digest) for name, digest in sorted(images_new.items())
                  if images_old.get(name) != digest}
        terraform = {root: self.world.plan_root(root, target) for root in ("bootstrap", "infra")}
        iam: list[IamRow] = []
        for root, found in terraform.items():
            review = review_plan(found)
            if review.iam_destroy_first:
                raise UpgradeRefused(
                    f"{root} would replace {', '.join(review.iam_destroy_first)} destroy-first; an IAM grant is "
                    "created before the old one is destroyed (lesson 10). Nothing was applied")
            if review.problems:
                raise UpgradeRefused(f"{root}'s plan cannot be applied: " + "; ".join(review.problems))
            iam.extend(iam_rows(root, found))
        changed, removed, added = policy_diff(self.world.policies(previous), self.world.policies(target))
        done = mig.applied(self.store.load().outputs)
        pending = [f"{mig.key(m.number)} {m.name} ({m.phase})" for m in self.migrations
                   if mig.key(m.number) not in done]
        current_app = self.world.app_permissions()
        wanted = self.world.release_app_permissions(target) or {}
        added_perms = {name: level for name, level in sorted(wanted.items())
                       if current_app is not None and current_app.get(name) != level}
        return UpgradePlan(previous=previous, target=target, terraform=terraform, images=images,
                           migrations=pending, policy_admit=changed, policy_delete=removed, policy_tighten=added,
                           iam=iam, app_added=added_perms,
                           replaced_metrics=[a for a, _ in replaced_metrics(terraform["infra"])])

    def show(self, plan: UpgradePlan) -> None:
        say = self.out
        say(f"swarm upgrade: {plan.previous} -> {plan.target} in {self.project}")
        for root, found in plan.terraform.items():
            counts: dict[str, int] = {}
            for change in found.get("resource_changes") or ():
                word = _action_word(_actions(change))
                if word:
                    counts[word] = counts.get(word, 0) + 1
            summary = ", ".join(f"{n} to {w}" for w, n in sorted(counts.items())) or "no changes"
            say(f"  terraform {root}: {summary}")
        say(f"  images: {len(plan.images)} change digest")
        for name, (old, new) in plan.images.items():
            say(f"    {name}: {old or '(new)'} -> {new}")
        say("  firestore migrations: " + (", ".join(plan.migrations) if plan.migrations else "none to run"))
        say(f"  cluster policies: {len(plan.policy_admit)} changed and {len(plan.policy_delete)} removed before "
            f"the new code, {len(plan.policy_tighten)} added after it")
        for address in plan.replaced_metrics:
            say(f"  {address}: a new label would replace it; the label is added to the live metric first (lesson 14)")
        if plan.app_added:
            say("  GitHub App permissions this release adds: "
                + ", ".join(f"{k}: {v}" for k, v in plan.app_added.items()))
            say("    They are not in force until the organisation owner accepts them on the installation (lesson "
                f"44). Review and accept them at: {self.world.app_review_url()}")
        if plan.iam:
            say(f"  IAM changes ({len(plan.iam)}):")
            for row in plan.iam:
                say(row.line())

    def gate(self, plan: UpgradePlan) -> None:
        if not plan.iam:
            return
        prompt = (f"These {len(plan.iam)} IAM changes block the upgrade to {plan.target}: nothing is applied, and "
                  f"the platform keeps running {plan.previous}, until they are approved. Type the project id "
                  f"({self.project}) to approve, anything else refuses: ")
        if self.confirm(prompt).strip() != self.project:
            raise UpgradeRefused(f"the IAM changes were not approved; nothing was applied and {plan.previous} "
                                 "keeps running")

    # -- apply ----------------------------------------------------------------

    def _record_migration(self, target: str) -> Callable[[mig.Migration, mig.Outcome], None]:
        def record(migration: mig.Migration, outcome: mig.Outcome) -> None:
            at = self.clock.now()
            self.store.update(lambda s: mig.record_into(s.outputs, migration, outcome, at=at, release=target))

        return record

    def _migrate(self, phase: str, target: str) -> None:
        mig.run_phase(self.world.documents(), self.migrations, phase, outputs=self.store.load().outputs,
                      record=self._record_migration(target), say=self.out)

    def _drain(self) -> None:
        self.world.pause_dispatch("swarm upgrade: draining before the new release rolls out")

        def check() -> str:
            return "met" if self.world.running() == 0 else "waiting"

        wait_until("upgrade.drain", DRAIN_WAIT, check, clock=self.clock, events=self.events)

    def _infra(self, target: str) -> None:
        found = self.world.plan_root("infra", target)
        metrics = replaced_metrics(found)
        if metrics:
            for address, after in metrics:
                self.world.patch_log_metric(address, after)
                self.out(f"  {address}: label added to the live metric first (lesson 14)")
            still = replaced_metrics(self.world.plan_root("infra", target))
            if still:
                raise UpgradeFailed(f"infra would still replace {', '.join(a for a, _ in still)} after the live "
                                    "label change; GCP refuses to delete a metric an alert uses")
        self.world.apply_root("infra", target)

    def _verify(self) -> None:
        results = self.world.checks()
        bad = [f"{name}: {status} ({detail})" for name, status, detail in results if status != "met"]
        if not results or bad:
            raise UpgradeFailed("the platform checks are not green: " + ("; ".join(bad) or "none ran"))

    def _step(self, name: str, plan: UpgradePlan) -> None:
        target = plan.target
        if name == DRAIN:
            self._drain()
        elif name == COPY_IMAGES:
            self.world.copy_images(target, parse_images_json(self.world.fetch(target, "images.json")))
        elif name == MIGRATIONS_ADDITIVE:
            self._migrate(mig.ADDITIVE, target)
        elif name == BOOTSTRAP:
            self.world.apply_root("bootstrap", target)
        elif name == POLICIES_ADMIT:
            rendered = self.world.policies(target)
            if plan.policy_delete:
                self.world.delete_policies(plan.policy_delete)
            if plan.policy_admit:
                self.world.apply_policies(_subset(rendered, plan.policy_admit))
        elif name == INFRA:
            self._infra(target)
        elif name == POLICIES_TIGHTEN:
            self.world.apply_policies(self.world.policies(target))
        elif name == MIGRATIONS_NEW_CODE:
            self._migrate(mig.NEEDS_NEW_CODE, target)
        elif name == NAMESPACES:
            for tenant in self.world.tenants():
                self.world.apply_namespace(tenant)
        elif name == VERIFY:
            self._verify()
        elif name == MIGRATIONS_CLEANUP:
            self._migrate(mig.REMOVES_DATA, target)
        elif name == RESUME:
            self.world.resume_dispatch(f"swarm upgrade: {target} is verified")
        else:
            raise ValueError(f"no upgrade step {name!r}")

    def _rollback(self, plan: UpgradePlan, step: str, why: str) -> None:
        self.out(f"swarm upgrade: {step} failed ({why}); rolling back to {plan.previous}")
        self.world.copy_images(plan.previous, parse_images_json(self.world.fetch(plan.previous, "images.json")))
        self.world.apply_root("infra", plan.previous)
        progress = self._progress()
        # Dispatch resumes below, so the next run drains again, as after _stopped.
        done = [s for s in progress.get("done") or () if ORDER.index(s) < ORDER.index(INFRA) and s != DRAIN]

        def change(state: Any) -> None:
            state.release = plan.previous
            current = dict(state.outputs.get(UPGRADE_KEY) or {})
            current.update(done=done, failed=step, rolled_back_to=plan.previous)
            state.outputs[UPGRADE_KEY] = current

        self.store.update(change)
        self.world.resume_dispatch(f"swarm upgrade: rolled back to {plan.previous}")

    def _stopped(self, step: str, done: list[str]) -> None:
        """A stop that is not rolled back: dispatch resumes on what runs now, and
        the next run drains again before it goes on at `step`."""
        self._save_progress(failed=step, done=[s for s in done if s != DRAIN])
        if DRAIN in done or step == DRAIN:
            self.world.resume_dispatch(f"swarm upgrade stopped at {step}")

    def run(self, target: str, *, allow_downgrade: bool = False) -> ExitCode:
        previous = self.running_release()
        if not previous:
            raise UpgradeRefused("the install state records no running release: run swarm install first")
        progress = self._progress()
        resuming = progress.get("to") == target and progress.get("finished") != target
        if not resuming and target == previous:
            self.out(f"swarm upgrade: {self.project} already runs {target}; nothing to do")
            return ExitCode.OK
        if version_key(target) < version_key(previous) and not allow_downgrade:
            raise UpgradeRefused(f"{target} is older than the running {previous}; a downgrade needs "
                                 "--allow-downgrade")
        plan = self.plan(target, previous)
        self.show(plan)
        self.gate(plan)
        if not resuming:
            def start(state: Any) -> None:
                state.outputs[PREVIOUS_KEY] = previous
                state.outputs[UPGRADE_KEY] = {"from": previous, "to": target, "done": [], "failed": None,
                                              "started_at": self.clock.now()}
                state.release = target

            self.store.update(start)
        else:
            self.store.update(lambda s: setattr(s, "release", target))
            self.out(f"swarm upgrade: resuming the upgrade to {target} at "
                     f"{progress.get('failed') or 'the next step'}")
        done = list(self._progress().get("done") or ())
        for name in ORDER:
            if name in done:
                continue
            self.events.emit("step_apply", step=f"upgrade.{name}", message=f"upgrade to {target}")
            try:
                self._step(name, plan)
            except (WaitGaveUp, UpgradeRefused, KeyboardInterrupt):
                self._stopped(name, done)
                raise
            except Exception as exc:  # noqa: BLE001 -- every failure is recorded, then reported
                why = str(exc)[:500]
                if name in ROLLED_BACK:
                    self._rollback(plan, name, why)
                    raise UpgradeFailed(f"{name} failed and the deployment was rolled back to {previous}: {why}. "
                                        f"Run swarm upgrade --to {target} again once it is fixed") from exc
                self._stopped(name, done)
                raise UpgradeFailed(f"{name} failed: {why}. Run swarm upgrade --to {target} again to resume "
                                    "there") from exc
            done.append(name)
            self._save_progress(done=list(done), failed=None)
            self.events.emit("step_done", step=f"upgrade.{name}", state="met")
        self._save_progress(finished=target, finished_at=self.clock.now())
        self.out(f"swarm upgrade: {self.project} runs {target}")
        return ExitCode.OK


__all__ = [
    "DRAIN_WAIT", "IamRow", "ORDER", "ROLLED_BACK", "Upgrade", "UpgradeFailed", "UpgradePlan", "UpgradeRefused",
    "UpgradeWorld", "iam_rows", "policy_diff", "replaced_metrics", "version_key",
]
