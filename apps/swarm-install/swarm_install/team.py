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
| `team.git-link` | the team's `-git` slot is filled through a one-time console link, never the terminal | CLAUDE.md |
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
asks swarm-api to mint the link and prints it. It never reads stdin.

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

    def team_ids(self) -> Sequence[str]: ...

    def create_team(self, group: str, *, display_name: str | None,
                    first_admin: str | None) -> Mapping[str, Any]: ...

    def workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

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
    clock: Clock
    events: Events
    out: TextIO

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

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
        self.events.emit("step_apply", step="team.create", detail=group)
        answer = self.api.create_team(group, display_name=display_name, first_admin=first_admin)
        team = str(answer.get("team") or "")
        workspace = answer.get("workspace") or {}
        workspace_id = str(workspace.get("workspace_id") or "") if isinstance(workspace, Mapping) else ""
        if not team or not workspace_id:
            raise TeamError("swarm-api's team route answered without the team or its workspace")
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
                raise TeamError(
                    f"swarm-workspace-apply stopped the team's workspace {workspace_id} at {state}"
                    + (f", step {step}" if step else "") + (f", code {code}" if code else "")
                    + ". Admin › People shows it; an admin's retry runs it again.")
            return WAITING

        wait_until("team.workspace", WORKSPACE_WAIT, check, clock=self.clock, events=self.events)
        self.events.emit("step_done", step="team.workspace", state=MET)

    def namespace(self, team: str) -> None:
        applied = ensure_verify_keys(self.cluster(), tenant=team, settings=self.spec_settings())
        self.events.emit("step_done", step="team.namespace",
                         detail=f"{namespace_of(team)}: verify keys " + ("applied" if applied else "present"))

    def git_link(self, team: str, owner: str) -> None:
        link = self.api.team_git_link(team, owner)
        where = link.get("url") or link.get("path")
        if not where:
            raise TeamError("swarm-api minted the team's git link but answered no address for it")
        self.say(f"The team's GitHub credential goes in through a one-time link, for {owner} only, "
                 f"until {link.get('expires_at')}:")
        self.say(f"  {where}")
        self.say("Open it in a browser signed in as that person. The value is never typed here.")
        self.events.emit("step_done", step="team.git-link", detail=f"link minted for {owner}")

    def await_pool_account(self, team: str, *, first_team: bool) -> None:
        if first_team:
            how = ("this is the first team, so its accounts are stage 12's: add them in "
                   "Admin › Setup › Claude pool accounts (they are owned by this team)")
        else:
            how = "lend one of the pool's accounts to this team in Admin › People"
        self.say(f"The smoke agent starts once a Claude pool account is owned by or lent to {team}; {how}.")

        def check() -> str:
            accounts = self.api.pool_accounts(team)
            usable = [a for a in accounts if str(a.get("state") or "").upper() == "AVAILABLE"]
            return MET if usable else WAITING

        spec = WaitSpec(reason=f"{POOL_ACCOUNT_WAIT.reason} ({how})", expected=POOL_ACCOUNT_WAIT.expected,
                        expected_s=POOL_ACCOUNT_WAIT.expected_s, bound_s=POOL_ACCOUNT_WAIT.bound_s,
                        look_at=POOL_ACCOUNT_WAIT.look_at, interval_s=POOL_ACCOUNT_WAIT.interval_s)
        wait_until("team.pool-account", spec, check, clock=self.clock, events=self.events)
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

        wait_until("team.smoke", SMOKE_WAIT, check_until_terminal, clock=self.clock, events=self.events)
        self.events.emit("step_done", step="team.smoke", state=MET, detail=f"task {task_id} SUCCEEDED")
        return task_id

    # -- the whole -------------------------------------------------------------

    def run(self, group: str, *, display_name: str | None = None, first_admin: str | None = None,
            git_owner: str | None = None) -> dict[str, Any]:
        self.probe(group)
        # The first team owns stage 12's pool accounts; a later one is lent one.
        first_team = not self.api.team_ids()
        team, workspace_id = self.create(group, display_name, first_admin)
        self.await_workspace(workspace_id)
        self.namespace(team)
        owner = git_owner or first_admin
        if owner:
            self.git_link(team, owner)
        else:
            self.say("No --git-owner or --first-admin: mint the team's git link later in Admin › Setup.")
        self.await_pool_account(team, first_team=first_team)
        task_id = self.smoke(team)
        self.say(f"Team {team} is ready: namespace {namespace_of(team)}, quota, verify keys, worker identity, "
                 f"slots, and a green smoke agent ({task_id}).")
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
        for env in container.get("env") or ():
            if env.get("name") in SPEC_SETTINGS and env.get("value"):
                out[str(env["name"])] = str(env["value"])
    return out


__all__ = [
    "Cluster", "KubeCluster", "SwarmApi", "TeamAdd", "TeamApi", "TeamError", "ensure_verify_keys",
    "namespace_of", "scheduler_spec_settings", "verify_keys_manifest",
]
