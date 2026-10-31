"""`swarm team add <group>`: one team, end to end (docs/design/day-one-plan.md,
lane DAYONE-TEAM-1, part of #2688; design §5.3, §5.7; manifest stage 11).

The CLI and the console's team creation drive ONE path: swarm-api's team
route (`POST /v1/admin/setup/teams`, SETUP-API-1) writes the group workspace
record through the existing workspace path, and `swarm-workspace-apply`
(scripts/workspace-apply.sh -> scripts/register-tenant.sh --workspace) makes
the worker identity, the slots, the namespace and its quota from the record
(lesson 32). This module drives that path and proves it ended well:

| Step | What it proves | Lesson |
|---|---|---|
| `team.probe` | swarm-api's own group identity reads groups, BEFORE anything is registered | 20 |
| `team.create` | the team route answered with the team's workspace | |
| `team.workspace` | the job ran to `ready` (identity, slots, namespace, quota, its own A9 check) | 32 |
| `team.namespace` | the namespace exists and holds `swarm-spec-verify-keys` | 31 |
| `team.git-link` | the team's `-git` slot has an enabled version, filled through a one-time console link, never the terminal | CLAUDE.md |
| `team.pool-account` | a Claude pool account is owned by or lent to the team | review-fix 1 |
| `team.smoke` | a smoke agent ran on the team and SUCCEEDED | |

**The verify keys are the installer's to apply.** The job's identity holds no
ConfigMap grant (kubernetes/rbac/provisioner-rbac.yaml: "No ConfigMap"), so a
namespace the job makes has none (lesson 31, #1590). The scheduler hands every
Job the same settings as environment (`scheduler.dispatch.spec_job_env`), and
a worker reads the mount only without them; the ConfigMap is the fallback a
GKE pod still mounts. `ensure_verify_keys` renders it with the bundle's own
`render.py` rule from the keys the scheduler service carries, so the two can
never differ, and applies it on the install cluster as the installer.

**No forge token ever passes through here.** The team's `-git` value is
written by swarm-api's credential writer when the person named on the link
opens it in the console (`POST /v1/setup/links/redeem`); this module only
asks swarm-api to mint the link, opens it (the owner is the caller unless
`--git-owner` or `--first-admin` names another), and polls Secret Manager for
the secret's enabled version: the team is not ready until it has one (audit 4e
F12). For another owner it prints the link for them, since it redeems for
them alone, and opens Admin › Setup, where the team's row shows the link. A
value already there mints no link. It never reads stdin.

**A re-run resumes** (audit 4e F13). The team route answers a workspace already
standing as it is; a failed or `needs_owner` one is retried through
`POST /v1/admin/workspaces/{id}/retry`, the console's own retry, and every
wait's give-up names `swarm team add`, not `swarm install`.

**The smoke waits for a pool account** (review-fix finding 1): the smoke agent
is a `claude-code` task, which needs a Claude pool account the team may run
on. For the first team those are stage 12's accounts, added in Admin › Setup
and owned by the first team; for any later team, an account lent to it in
Admin › People. The wait says which.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

from . import steps
from .events import Events
from .exitcodes import MET, UNMET, WAITING
from .waits import Clock, WaitSpec, wait_until

#: The namespace the job makes for a tenant (register-tenant.sh, `ws_expect namespace`).
NAMESPACE_PREFIX = "swarm-tenant-"
VERIFY_KEYS_CONFIG_MAP = "swarm-spec-verify-keys"
#: The settings the scheduler hands a Job (scheduler.dispatch.spec_job_env),
#: and the ones render.py's ConfigMap carries.
SPEC_SETTINGS = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY", "SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER")
SPEC_REQUIRED = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY")

#: The workspace states (swarm_api.workspaces) the job's run ends in.
READY = "ready"
STOPPED = ("failed", "needs_owner", "denied")
#: The ones `People.retry` starts again (swarm_api.people.RETRYABLE); `denied` is an admin's decision.
RETRYABLE = ("failed", "needs_owner")

#: The verb a person runs again to resume; the verb passes the whole command.
RESUME = "swarm team add"

#: The console's pages a wait sends the person to (apps/swarm-ui App.tsx routes).
SETUP_PAGE = "/admin/setup"
PEOPLE_PAGE = "/admin/people"

#: The task states a smoke run ends in.
SUCCEEDED = "SUCCEEDED"
TASK_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")

#: The smoke agent: the cheapest class, no repository, a short bound.
SMOKE_PROFILE = "claude-code"
SMOKE_MODEL_CLASS = "haiku"
SMOKE_TIMEOUT_S = 900
SMOKE_PROMPT = ("This is the SwarmCloud team smoke test. Reply with the single word READY and stop; "
                "change no file.")

WORKSPACE_WAIT = WaitSpec(
    reason="swarm-workspace-apply making the team's identity, slots and namespace",
    expected="2 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="Admin › People shows the workspace's step and failure; the job's execution log",
    interval_s=15,
)
GIT_WAIT = WaitSpec(
    reason="the team's GitHub credential to be stored",
    expected="as long as the link's owner takes to open it",
    expected_s=15 * 60,
    bound_s=60 * 60,
    look_at="Admin › Setup shows the team's git link, its owner and when it expires; mint a fresh one there",
    interval_s=15,
)
POOL_ACCOUNT_WAIT = WaitSpec(
    reason="a Claude pool account owned by or lent to the team",
    expected="as long as the sign-in takes",
    expected_s=15 * 60,
    bound_s=60 * 60,
    look_at="Admin › Setup › Claude pool accounts (the first team), or Admin › People › lend (any later team)",
    interval_s=15,
)
SMOKE_WAIT = WaitSpec(
    reason="the team's smoke agent to finish",
    expected="1 to 5 min",
    expected_s=5 * 60,
    bound_s=20 * 60,
    look_at="the task's page in the console; swarm status <task id>",
    interval_s=10,
)


class TeamError(RuntimeError):
    """The team cannot go on; the message says why and what to do."""


class TeamApi(Protocol):
    """swarm-api, called as the person running the verb (a platform admin)."""

    def groups_probe(self) -> Mapping[str, Any]: ...

    def caller(self) -> str: ...

    def team_ids(self) -> Sequence[str]: ...

    def create_team(self, group: str, *, display_name: str | None,
                    first_admin: str | None) -> Mapping[str, Any]: ...

    def workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def team_git_link(self, team: str, owner: str) -> Mapping[str, Any]: ...

    def pool_accounts(self, team: str) -> Sequence[Mapping[str, Any]]: ...

    def start_smoke(self, team: str) -> str: ...

    def task_state(self, team: str, task_id: str) -> str: ...


class Cluster(Protocol):
    """The install cluster, through the engine's own context (lesson 37)."""

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None: ...

    def apply(self, manifest: str) -> None: ...


def namespace_of(team: str) -> str:
    return f"{NAMESPACE_PREFIX}{team}"


def git_secret_of(team: str) -> str:
    """The team's git slot (swarm_api.setup_links: `swarm-tenant-<tenant>-git`)."""
    return f"swarm-tenant-{team}-git"


def git_secret_ready(gcp: Any, project: str, team: str) -> bool:
    """True once the team's git slot exists and holds an enabled version: what
    the redeemed link writes, and what a task's forge broker reads."""
    name = git_secret_of(team)
    return gcp.secret(project, name) is not None and gcp.enabled_version(project, name) is not None


def verify_keys_manifest(tenant: str, settings: Mapping[str, str]) -> str:
    """The `swarm-spec-verify-keys` ConfigMap for the tenant's namespace, in
    kubernetes/render.py's shape (`render_spec_verify_keys`): every value a
    JSON string, so a PEM can never open a second YAML document."""
    missing = [k for k in SPEC_REQUIRED if not settings.get(k)]
    if missing:
        raise TeamError(
            f"the scheduler carries no {', '.join(missing)}, so there are no step-spec keys to put in "
            f"{namespace_of(tenant)}: every signed task would end CANNOT_START (lesson 31). "
            "Run swarm install again to finish infra pass 2, then swarm team add again.")
    try:
        keys = json.loads(settings["SPEC_VERIFY_KEYS"])
    except ValueError:
        raise TeamError("the scheduler's SPEC_VERIFY_KEYS is not JSON; swarm doctor names the fix") from None
    if not isinstance(keys, dict) or not keys:
        raise TeamError("the scheduler's SPEC_VERIFY_KEYS is empty; it would verify nothing")
    namespace = namespace_of(tenant)
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {VERIFY_KEYS_CONFIG_MAP}",
        f"  namespace: {namespace}",
        "  labels:",
        "    managed-by: swarm-terraform",
        f"    swarm-tenant: {tenant}",
        "    app.kubernetes.io/part-of: swarm",
        "data:",
    ]
    lines += [f"  {key}: {json.dumps(settings[key])}" for key in SPEC_SETTINGS if settings.get(key)]
    return "\n".join(lines) + "\n"


def ensure_verify_keys(cluster: Cluster, *, tenant: str, settings: Mapping[str, str]) -> bool:
    """Apply the namespace's verify keys when they are absent or differ from
    the scheduler's; True when it applied. Raises TeamError when the keys
    cannot be put in place or are still absent afterwards (lesson 31)."""
    namespace = namespace_of(tenant)
    if cluster.get("Namespace", namespace) is None:
        raise TeamError(f"namespace {namespace} does not exist: swarm-workspace-apply did not make it")
    manifest = verify_keys_manifest(tenant, settings)
    want = {k: settings[k] for k in SPEC_SETTINGS if settings.get(k)}
    found = cluster.get("ConfigMap", VERIFY_KEYS_CONFIG_MAP, namespace)
    if found is not None and dict(found.get("data") or {}) == want:
        return False
    cluster.apply(manifest)
    if cluster.get("ConfigMap", VERIFY_KEYS_CONFIG_MAP, namespace) is None:
        raise TeamError(f"{VERIFY_KEYS_CONFIG_MAP} is still absent from {namespace} after the apply")
    return True


def probe_explanation(result: Mapping[str, Any]) -> str:
    """Lesson 20's two causes, told apart."""
    reason = str(result.get("reason") or "")
    return (f"swarm-api cannot read {result.get('group')} ({reason or 'unreadable'}). A signJwt 403 is a "
            "missing GCP grant on swarm-api's group identity; `unauthorized_client` is the Admin console's "
            "domain-wide delegation. The Workspace Groups Reader admin role avoids both (design §8). "
            "swarm doctor names which.")


@dataclass
class TeamAdd:
    api: TeamApi
    cluster: Callable[[], Cluster]
    spec_settings: Callable[[], Mapping[str, str]]
    #: True once `swarm-tenant-<team>-git` has an enabled version (`git_secret_ready`).
    git_secret: Callable[[str], bool]
    #: The console's origin, `https://<hostname>`.
    console: str
    clock: Clock
    events: Events
    out: TextIO
    #: The command a give-up tells the person to run again.
    resume: str = RESUME
    open_page: Callable[..., None] = steps.open_page

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def page(self, path: str, why: str) -> None:
        self.open_page(f"{self.console}{path}", why=why, say=self.say)

    def wait(self, step: str, spec: WaitSpec, check: Callable[[], str]) -> None:
        wait_until(step, spec, check, clock=self.clock, events=self.events, resume=self.resume)

    # -- the steps ---------------------------------------------------------

    def probe(self, group: str) -> None:
        """Lesson 20: before anything is registered, swarm-api's own group
        identity must read groups. The answer is met/unmet per group swarm-api
        knows, never a member list."""
        self.events.emit("step_check", step="team.probe", detail=group)
        answer = self.api.groups_probe()
        results = [r for r in answer.get("groups") or () if isinstance(r, Mapping)]
        if not results:
            raise TeamError("swarm-api's groups probe named no group, so it proved nothing; "
                            "stage 8 configures the admin group. Run swarm install --status.")
        unmet = [r for r in results if r.get("state") != MET]
        if unmet:
            raise TeamError(probe_explanation(unmet[0]) + " Nothing was registered.")
        self.events.emit("step_done", step="team.probe", state=MET)

    def create(self, group: str, display_name: str | None, first_admin: str | None) -> tuple[str, str]:
        """The team route; on a re-run it answers the workspace already
        standing, and a stopped one is retried here, as the console's retry
        does, so the re-run resumes instead of stopping on the same failure."""
        self.events.emit("step_apply", step="team.create", detail=group)
        answer = self.api.create_team(group, display_name=display_name, first_admin=first_admin)
        team = str(answer.get("team") or "")
        workspace = answer.get("workspace") or {}
        workspace_id = str(workspace.get("workspace_id") or "") if isinstance(workspace, Mapping) else ""
        if not team or not workspace_id:
            raise TeamError("swarm-api's team route answered without the team or its workspace")
        state = str(workspace.get("state") or "")
        if state in RETRYABLE:
            self.events.emit("step_apply", step="team.create", detail=f"retry workspace {workspace_id} ({state})")
            self.api.retry_workspace(workspace_id)
            self.say(f"The team's workspace {workspace_id} had stopped at {state}; it is being retried.")
        self.events.emit("step_done", step="team.create", detail=f"team {team}, workspace {workspace_id}")
        return team, workspace_id

    def await_workspace(self, workspace_id: str) -> None:
        def check() -> str:
            view = self.api.workspace(workspace_id)
            state = str(view.get("state") or "")
            if state == READY:
                return MET
            if state in STOPPED:
                failure = view.get("failure") or {}
                code = failure.get("code") if isinstance(failure, Mapping) else None
                step = (failure.get("step") if isinstance(failure, Mapping) else None) or view.get(
                    "needs_owner_step")
                then = (f" Fix what it names, then run {self.resume} again: it retries the workspace."
                        if state in RETRYABLE else " An admin denied it in Admin › People; nothing retries it.")
                raise TeamError(
                    f"swarm-workspace-apply stopped the team's workspace {workspace_id} at {state}"
                    + (f", step {step}" if step else "") + (f", code {code}" if code else "")
                    + ". Admin › People shows it." + then)
            return WAITING

        self.wait("team.workspace", WORKSPACE_WAIT, check)
        self.events.emit("step_done", step="team.workspace", state=MET)

    def namespace(self, team: str) -> None:
        applied = ensure_verify_keys(self.cluster(), tenant=team, settings=self.spec_settings())
        self.events.emit("step_done", step="team.namespace",
                         detail=f"{namespace_of(team)}: verify keys " + ("applied" if applied else "present"))

    def git_link(self, team: str, owner: str, *, caller: str) -> None:
        """The team is not ready until its `-git` slot holds a value: mint
        the link, open it (or Admin › Setup, when it is another person's),
        and poll Secret Manager for the enabled version."""
        secret = git_secret_of(team)
        if self.git_secret(team):
            self.events.emit("step_done", step="team.git-link", state=MET, detail=f"{secret} has a value")
            return
        link = self.api.team_git_link(team, owner)
        path = str(link.get("path") or "")
        where = str(link.get("url") or (f"{self.console}{path}" if path.startswith("/") else ""))
        if not where:
            raise TeamError("swarm-api minted the team's git link but answered no address for it")
        self.events.emit("step_apply", step="team.git-link", detail=f"link minted for {owner}")
        until = link.get("expires_at")
        if owner == caller:
            self.open_page(where, why=(
                f"The team's GitHub credential goes in through a one-time link, for you only, until {until}. "
                "Opening it in your browser; the value is never typed here."), say=self.say)
        else:
            self.say(f"The team's GitHub credential goes in through a one-time link, for {owner} only, "
                     f"until {until}. Send it to them to open in a browser signed in as themselves:")
            self.say(f"  {where}")
            self.say("The value is never typed here.")
            self.page(SETUP_PAGE, f"Admin › Setup shows the team's git link and whether {owner} has used it.")

        def check() -> str:
            return MET if self.git_secret(team) else WAITING

        spec = WaitSpec(reason=f"{GIT_WAIT.reason} ({secret}, through {owner}'s link)", expected=GIT_WAIT.expected,
                        expected_s=GIT_WAIT.expected_s, bound_s=GIT_WAIT.bound_s, look_at=GIT_WAIT.look_at,
                        interval_s=GIT_WAIT.interval_s)
        self.wait("team.git-link", spec, check)
        self.events.emit("step_done", step="team.git-link", state=MET, detail=f"{secret} has a value")

    def await_pool_account(self, team: str, *, first_team: bool) -> None:
        if first_team:
            how = ("this is the first team, so its accounts are stage 12's: add them in "
                   "Admin › Setup › Claude pool accounts (they are owned by this team)")
            where = SETUP_PAGE
        else:
            how = "lend one of the pool's accounts to this team in Admin › People"
            where = PEOPLE_PAGE

        opened: list[bool] = []

        def check() -> str:
            accounts = self.api.pool_accounts(team)
            if [a for a in accounts if str(a.get("state") or "").upper() == "AVAILABLE"]:
                return MET
            if not opened:
                # Only when there is none yet: the page is where the person adds or lends one.
                opened.append(True)
                self.page(where, f"The smoke agent starts once a Claude pool account is owned by or lent to "
                                 f"{team}; {how}.")
            return WAITING

        spec = WaitSpec(reason=f"{POOL_ACCOUNT_WAIT.reason} ({how})", expected=POOL_ACCOUNT_WAIT.expected,
                        expected_s=POOL_ACCOUNT_WAIT.expected_s, bound_s=POOL_ACCOUNT_WAIT.bound_s,
                        look_at=POOL_ACCOUNT_WAIT.look_at, interval_s=POOL_ACCOUNT_WAIT.interval_s)
        self.wait("team.pool-account", spec, check)
        self.events.emit("step_done", step="team.pool-account", state=MET)

    def smoke(self, team: str) -> str:
        task_id = self.api.start_smoke(team)
        if not task_id:
            raise TeamError("swarm-api accepted the smoke agent but answered no task id")
        self.events.emit("step_apply", step="team.smoke", detail=f"task {task_id}")
        final: list[str] = []

        def check() -> str:
            state = self.api.task_state(team, task_id).upper()
            if state in TASK_TERMINAL:
                final.append(state)
                return MET if state == SUCCEEDED else UNMET
            return WAITING

        def check_until_terminal() -> str:
            status = check()
            if status == UNMET:
                raise TeamError(f"the smoke agent {task_id} ended {final[-1]}; its page in the console says why")
            return status

        self.wait("team.smoke", SMOKE_WAIT, check_until_terminal)
        self.events.emit("step_done", step="team.smoke", state=MET, detail=f"task {task_id} SUCCEEDED")
        return task_id

    # -- the whole -------------------------------------------------------------

    def run(self, group: str, *, display_name: str | None = None, first_admin: str | None = None,
            git_owner: str | None = None) -> dict[str, Any]:
        self.probe(group)
        caller = self.api.caller().strip().lower()
        if not caller:
            raise TeamError("swarm-api did not say who is signed in; run swarm login, then swarm team add again")
        before = list(self.api.team_ids())
        team, workspace_id = self.create(group, display_name, first_admin)
        # The first team owns stage 12's pool accounts; a later one is lent one.
        # On a re-run the team itself is already listed, and is still the first.
        first_team = not [t for t in before if t != team]
        self.await_workspace(workspace_id)
        self.namespace(team)
        self.git_link(team, (git_owner or first_admin or caller).strip().lower(), caller=caller)
        self.await_pool_account(team, first_team=first_team)
        task_id = self.smoke(team)
        self.say(f"Team {team} is ready: namespace {namespace_of(team)}, quota, verify keys, worker identity, "
                 f"its GitHub credential ({git_secret_of(team)}), slots, and a green smoke agent ({task_id}).")
        return {"team": team, "workspace": workspace_id, "smoke_task": task_id}


# ---------------------------------------------------------------------------
# The real seams: swarm-api as the person, the install cluster, the scheduler
# ---------------------------------------------------------------------------


@dataclass
class SwarmApi:
    """swarm-api through the `swarm` CLI's own client (`swarm_mcp.client`),
    signed in as the person with the deployment's Desktop client (lesson 18);
    every `/v1/admin/setup/*` route is a platform admin's (plan §5.2). The
    teammate wheel is imported only here, at run time."""

    context: str | None = None
    _clients: dict[str, Any] = field(default_factory=dict)

    def _client(self, tenant: str | None = None) -> Any:
        key = tenant or ""
        if key not in self._clients:
            from swarm_mcp.client import SwarmClient  # the swarm_mcp wheel install.sh installs beside this one

            self._clients[key] = SwarmClient(context=self.context, tenant=tenant)
        return self._clients[key]

    def groups_probe(self) -> Mapping[str, Any]:
        return self._client().request("POST", "/v1/admin/setup/health/groups-probe", payload={}) or {}

    def caller(self) -> str:
        found = self._client().request("GET", "/v1/tenants/me") or {}
        principal = found.get("principal") or {}
        return str(principal.get("email") or "") if isinstance(principal, Mapping) else ""

    def team_ids(self) -> Sequence[str]:
        found = self._client().request("GET", "/v1/admin/setup") or {}
        return [str(t.get("id")) for t in found.get("teams") or () if isinstance(t, Mapping) and t.get("id")]

    def create_team(self, group: str, *, display_name: str | None, first_admin: str | None) -> Mapping[str, Any]:
        body: dict[str, Any] = {"group": group}
        if display_name:
            body["display_name"] = display_name
        if first_admin:
            body["first_admin"] = first_admin
        return self._client().request("POST", "/v1/admin/setup/teams", payload=body) or {}

    def workspace(self, workspace_id: str) -> Mapping[str, Any]:
        return self._client().request("GET", f"/v1/admin/workspaces/{workspace_id}") or {}

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        return self._client().request("POST", f"/v1/admin/workspaces/{workspace_id}/retry", payload={}) or {}

    def team_git_link(self, team: str, owner: str) -> Mapping[str, Any]:
        return self._client().request("POST", "/v1/admin/setup/links",
                                      payload={"kind": "team-git", "label": team, "owner": owner}) or {}

    def pool_accounts(self, team: str) -> Sequence[Mapping[str, Any]]:
        found = self._client(team).request("GET", "/v1/accounts") or {}
        return [a for a in found.get("accounts") or () if isinstance(a, Mapping)]

    def start_smoke(self, team: str) -> str:
        task = self._client(team).dispatch(prompt=SMOKE_PROMPT, runner_profile=SMOKE_PROFILE,
                                           model_class=SMOKE_MODEL_CLASS, timeout_seconds=SMOKE_TIMEOUT_S,
                                           metadata={"source": "swarm team add", "smoke": True})
        return str(task.get("id") or task.get("task_id") or "")

    def task_state(self, team: str, task_id: str) -> str:
        return str((self._client(team).task(task_id) or {}).get("state") or "")


@dataclass
class KubeCluster:
    """The engine's `Kube` (steps/cluster.py): its own kubeconfig, the install
    cluster's context only, kubectl through `prefer_local_bin`'s rule."""

    kube: Any

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None:
        args = ["get", kind.lower(), name, "-o", "json"]
        if namespace:
            args[1:1] = ["-n", namespace]
        code, out, err = self.kube(*args)
        if code != 0:
            if "NotFound" in err or "not found" in err:
                return None
            raise TeamError(f"kubectl get {kind.lower()} {name} failed: {err.strip()[:300]}")
        return json.loads(out or "{}")

    def apply(self, manifest: str) -> None:
        code, _out, err = self.kube("apply", "-f", "-", stdin=manifest)
        if code != 0:
            raise TeamError(f"kubectl apply of {VERIFY_KEYS_CONFIG_MAP} failed: {err.strip()[:300]}")


def scheduler_spec_settings(gcp: Any, project: str, region: str) -> dict[str, str]:
    """The step-spec settings the `swarm-scheduler` service carries, which it
    hands every Job it creates (`scheduler.dispatch.spec_job_env`)."""
    service = gcp.run_service(project, region, "swarm-scheduler") or {}
    out: dict[str, str] = {}
    for container in (service.get("template") or {}).get("containers") or ():
        for var in container.get("env") or ():
            if var.get("name") in SPEC_SETTINGS and var.get("value"):
                out[str(var["name"])] = str(var["value"])
    return out


__all__ = [
    "Cluster", "KubeCluster", "SwarmApi", "TeamAdd", "TeamApi", "TeamError", "ensure_verify_keys",
    "git_secret_of", "git_secret_ready", "namespace_of", "scheduler_spec_settings", "verify_keys_manifest",
]
