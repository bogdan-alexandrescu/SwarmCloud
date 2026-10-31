"""`swarm doctor`'s platform tier: every manifest probe, run against a live
deployment (docs/design/day-one-install.md §2.1-§2.2; plan §1.5 DAYONE-DOCTOR-1).

The manifest's entries carry typed `probe:`s (DAYONE-MANIFEST-STEPS). This
module runs each against the deployment the install state names and says, per
entry, `met`, `unmet` or `unknown`, with the fix in words and the one command
that repairs it: `swarm install --repair <id>`, which re-runs the entry's
`step:` with that step's own approvals (`swarm_install.repair`). A per-tenant
entry (`expand: per_tenant`) gets no such command: each team's namespace is
written by the platform's swarm-workspace-apply job when the team is made, and
re-running an install step remakes none of it (`PLATFORM_MADE`). One the
tenant render holds (its `refs.kubernetes` names a file) is repaired per team:
the finding names each team whose namespace fails, with `swarm team repair
<team> --project <project>`, which has that job apply the team's render again
(`swarm_install.team.TeamRepair`).

**It never writes.** Every call is a GET, except two read-only POSTs: the
Policy Troubleshooter (which explains a 403; it changes nothing) and swarm-api's
groups probe (swarm-api reads the groups with its own identity; the doctor
holds no Groups role). The repair lives in `install`, never here: this module
imports no step, no engine and no Terraform runner, and a test holds that.

**An unknown is never fine** (`exitcodes`): a probe that could not run is
`unknown` and exits 3, the same as an unmet one. The one exception is
`skipped`, which says WHY nothing was asked and who asks instead: off Google's
runtime, after the closing infra pass took the installer's address out of the
control plane's authorised networks (decision 2), the cluster admits only the
platform, so a laptop's `kubernetes_can_i` is not tried (it would time out
after 60 s, every run, on a healthy deployment). The hourly doctor runs the
same probe on the platform, at the private endpoint, and posts it to Admin ›
Setup › Health. A skipped finding counts toward neither green nor red. A 403 is asked of the Policy
Troubleshooter: when the grant exists the finding says IAM is still
propagating (45 s to 10 min, lesson 19) and to run the doctor again; when it
does not, it names the missing grant (lesson 48). Neither is "fine".

The same code runs on a laptop (`swarm doctor`, the caller's gcloud
credentials) and hourly in the `swarm-doctor` job (`swarm_install.
scheduled_doctor`, the metadata server's token), so the module's imports are
the standard library and the manifest model alone: the job's image carries no
Google client library.

Probes, by `type`:

| type | what it reads |
|---|---|
| `service_enabled` | Service Usage: the API's state is `ENABLED` |
| `secret_exists` | Secret Manager: the secret's container exists (never a value) |
| `iap_programmatic_clients` | every IAP-enabled frontend backend admits the recorded Desktop client (lesson 18) |
| `groups_probe_via_api` | swarm-api re-probes every registered group with its own identity |
| `cloud_run_job_count` | the region's Cloud Run Jobs against the 1,000-Job quota; finished Jobs nobody deletes (lesson 34) |
| `gke_authorized_networks` | the control plane's authorised networks: on, no 0.0.0.0/0, the installer's entry removed (lesson 37) |
| `kubernetes_can_i` | the subject's RBAC in every tenant namespace, by email and by uniqueId (lesson 30) |
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .exitcodes import MET, UNKNOWN, UNMET, ExitCode, for_statuses
from .manifest import Entry, Manifest

#: The swarm-doctor job's name (terraform/infra/doctor.tf `doctor_job_name`).
DOCTOR_JOB = "swarm-doctor"

#: Cloud Run's documented limit on Jobs per region per project (#2661).
CLOUD_RUN_JOB_LIMIT = 1000
#: The share of that limit past which the count is a finding: the platform's
#: fallback and workspace-apply Jobs need headroom to dispatch at all.
JOB_COUNT_WARN = 0.8
#: A finished Job kept longer than this is one nothing is deleting (the tenant
#: namespaces keep a finished Job for an hour, docs/scaling.md).
JOB_TTL_SECONDS = 3600

#: The label every resource the install makes carries (CLAUDE.md, Terraform);
#: a cluster without it is not this deployment's, and the doctor never reads it.
MANAGED_BY = ("managed-by", "swarm-terraform")

#: How long IAM may take to propagate a new grant (lesson 19, design H19).
PROPAGATION = "45 s to 10 min"

#: A finding the doctor deliberately did not ask from where it runs, with why
#: and who asks instead; `exit_code` leaves it out (it is neither met nor not).
SKIPPED = "skipped"

#: The display name of the installer's entry in the control plane's authorised
#: networks (`tfvars.CLUSTER_REACH_NAME`, decision 2; a test holds the two
#: equal, since this module imports no install path). While it is there the
#: installer's machine reaches the cluster; `infra-close` removes it.
INSTALLER_NETWORK = "swarm-install"

#: Why a laptop's doctor skips a cluster probe once the install has closed.
CLOSED_CONTROL_PLANE = (
    "skipped: the control plane admits only the platform (closed after install); "
    "the hourly doctor checks this from inside and posts it to Admin › Setup › Health"
)

#: Per-tenant entries: the platform's swarm-workspace-apply job renders them
#: (kubernetes/render.py TENANT_FILES) into each team's namespace when the team
#: is made. No install step makes them, so no `swarm install --repair` is
#: printed for them: re-running the manifest's `step:` (the console hand-off)
#: only sees Admin › Setup finished again and changes nothing (audit 4c M2).
PLATFORM_MADE = "per_tenant"

#: The verb that re-applies one team's tenant render (verbs/team.py `repair`;
#: a test holds its command equal to `team_repair_command`).
TEAM_REPAIR = "swarm team repair"


def team_repairable(entry: Entry) -> bool:
    """A per-tenant entry the tenant render holds, so `swarm team repair`
    re-applies it: the manifest names its file under `refs.kubernetes`."""
    return entry.expand == PLATFORM_MADE and bool(entry.refs.kubernetes)


def team_repair_command(team: str, project: str) -> str:
    return f"{TEAM_REPAIR} {team} --project {project}"


# -- what the doctor reports ---------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The deployment a run is about, from its install state."""

    project: str
    environment: str
    region: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    #: The identity the probes run as (the gcloud account, or the job's
    #: service account): the principal the Policy Troubleshooter is asked about.
    principal: str = ""


@dataclass(frozen=True)
class Finding:
    id: str
    status: str
    detail: str
    step: str | None = None
    fix: str = ""
    #: Why a probe saw a 403: "propagating" (the grant exists) or
    #: "missing_grant"; empty otherwise.
    cause: str = ""
    #: False where no install step makes the entry (`PLATFORM_MADE`), so
    #: re-running its `step:` would repair nothing.
    repairable: bool = True
    #: A per-team entry's repair: `swarm team repair` for each team whose
    #: namespace fails (`team_repairable`).
    team_repairs: tuple[str, ...] = ()

    @property
    def repair(self) -> str | None:
        if self.team_repairs:
            return "; ".join(self.team_repairs)
        return f"swarm install --repair {self.id}" if self.step and self.repairable else None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "step": self.step,
            "fix": self.fix,
            "cause": self.cause,
            "repair": self.repair,
        }


# -- the seams: Google's REST APIs, swarm-api, the cluster ---------------------


class NotFound(LookupError):
    """The API answered 404: the thing is absent (an answer, not a failure)."""


class Denied(PermissionError):
    """The API answered 403 for `permission` on `resource`."""

    def __init__(self, permission: str, resource: str, detail: str = "") -> None:
        super().__init__(f"403 for {permission} on {resource}{': ' + detail if detail else ''}")
        self.permission = permission
        self.resource = resource


class Gcp(Protocol):
    def get(self, url: str, *, permission: str, resource: str) -> Mapping[str, Any]: ...

    def post(self, url: str, body: Mapping[str, Any], *, permission: str,
             resource: str) -> Mapping[str, Any]: ...


class Api(Protocol):
    def groups_probe(self) -> Sequence[Mapping[str, Any]]:
        """swarm-api's answer: `{group, state, reason}` per registered group."""
        ...


class Kube(Protocol):
    def get(self, path: str) -> Mapping[str, Any]:
        """A Kubernetes API GET on this deployment's cluster; NotFound on 404."""
        ...


@dataclass
class Deps:
    gcp: Gcp
    api: Api | None = None
    #: Builds the cluster client from the cluster object the authorised-
    #: networks read found (its endpoint and CA); None where it cannot be built.
    kube: Callable[[Mapping[str, Any]], Kube] | None = None
    #: One read per run of what several entries share.
    cache: dict[str, Any] = field(default_factory=dict)
    #: Whether the probes run on Google's runtime (the hourly job), where
    #: `kube_for` reaches the cluster's private endpoint; None asks
    #: `on_google_runtime()`. Off it, a closed control plane is skipped.
    on_google: bool | None = None

    def inside_google(self) -> bool:
        return on_google_runtime() if self.on_google is None else self.on_google


# -- substitution ----------------------------------------------------------------


def _sub(value: Any, target: Target) -> str:
    return (
        str(value)
        .replace("${project}", target.project)
        .replace("$P", target.project)
        .replace("$R", target.region)
        .replace("$E", target.environment)
    )


# -- the probes ------------------------------------------------------------------

#: (status, detail), or (status, detail, teams): a per-tenant probe names the
#: tenants whose namespace failed, so the finding can name each one's repair.
Result = tuple[str, str] | tuple[str, str, tuple[str, ...]]
ProbeFn = Callable[[Mapping[str, Any], Target, Deps], Result]


def _service_enabled(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    name = _sub(args["name"], t)
    found = deps.gcp.get(
        f"https://serviceusage.googleapis.com/v1/projects/{t.project}/services/{name}",
        permission="serviceusage.services.get",
        resource=f"//serviceusage.googleapis.com/projects/{t.project}/services/{name}",
    )
    state = str(found.get("state") or "")
    if state == "ENABLED":
        return MET, f"{name} is enabled"
    return UNMET, f"{name} is {state or 'not enabled'}"


def _secret_exists(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    secret = _sub(args["secret_id"], t)
    name = f"projects/{t.project}/secrets/{secret}"
    try:
        deps.gcp.get(
            f"https://secretmanager.googleapis.com/v1/{name}",
            permission="secretmanager.secrets.get",
            resource=f"//secretmanager.googleapis.com/{name}",
        )
    except NotFound:
        return UNMET, f"the secret {secret} does not exist"
    return MET, f"the secret {secret} exists"


def _project_number(t: Target, deps: Deps) -> str:
    key = "project_number"
    if key not in deps.cache:
        found = deps.gcp.get(
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{t.project}",
            permission="resourcemanager.projects.get",
            resource=f"//cloudresourcemanager.googleapis.com/projects/{t.project}",
        )
        deps.cache[key] = str(found.get("projectNumber") or "")
    if not deps.cache[key]:
        raise LookupError(f"Resource Manager returned no number for {t.project}")
    return str(deps.cache[key])


def _frontend_backends(t: Target, deps: Deps) -> list[str]:
    """This deployment's IAP-enabled backends: the recorded list
    (`frontend_iap_backends`), else every `swarm-*` backend with IAP on. Never
    another team's backend in a shared project."""
    recorded = t.outputs.get("frontend_iap_backends")
    found = deps.gcp.get(
        f"https://compute.googleapis.com/compute/v1/projects/{t.project}/global/backendServices",
        permission="compute.backendServices.list",
        resource=f"//compute.googleapis.com/projects/{t.project}",
    )
    names = []
    for item in found.get("items", ()) or ():
        name = str(item.get("name") or "")
        if not (item.get("iap") or {}).get("enabled"):
            continue
        if recorded:
            if name in recorded:
                names.append(name)
        elif name.startswith("swarm-"):
            names.append(name)
    return names


def _iap_programmatic_clients(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    output = str(args.get("client_output") or "desktop_client_id")
    client = t.outputs.get(output)
    if not client:
        return UNKNOWN, f"no {output} is recorded in the install state (stage 2's edge.desktop-client records it)"
    backends = _frontend_backends(t, deps)
    if not backends:
        return UNMET, "no frontend backend has IAP on"
    number = _project_number(t, deps)
    missing = []
    for backend in backends:
        path = f"projects/{number}/iap_web/compute/services/{backend}"
        settings = deps.gcp.get(
            f"https://iap.googleapis.com/v1/{path}:iapSettings",
            permission="iap.webServices.getSettings",
            resource=f"//iap.googleapis.com/{path}",
        )
        oauth = ((settings.get("accessSettings") or {}).get("oauthSettings") or {})
        if client not in (oauth.get("programmaticClients") or ()):
            missing.append(backend)
    if missing:
        return UNMET, (
            f"the Desktop client {client} is not in the programmatic allowlist of "
            f"{', '.join(missing)}: `sc login` tokens are refused there (lesson 18)"
        )
    return MET, f"the Desktop client is admitted programmatically on {', '.join(backends)}"


def _groups_probe_via_api(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    if deps.api is None:
        return UNKNOWN, "swarm-api could not be reached to re-probe the groups"
    if "groups" not in deps.cache:
        deps.cache["groups"] = list(deps.api.groups_probe())
    results = deps.cache["groups"]
    if not results:
        return UNMET, "swarm-api reports no registered group"
    unread = [f"{r.get('group')} ({r.get('reason') or r.get('state')})"
              for r in results if r.get("state") != MET]
    if unread:
        return UNMET, "swarm-api cannot read " + ", ".join(unread)
    return MET, f"swarm-api reads all {len(results)} registered groups"


def _list_jobs(t: Target, region: str, deps: Deps) -> list[Mapping[str, Any]]:
    jobs: list[Mapping[str, Any]] = []
    token = ""
    parent = f"projects/{t.project}/locations/{region}"
    while True:
        query = "?pageSize=500" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
        page = deps.gcp.get(
            f"https://run.googleapis.com/v2/{parent}/jobs{query}",
            permission="run.jobs.list",
            resource=f"//run.googleapis.com/{parent}",
        )
        jobs.extend(page.get("jobs", ()) or ())
        token = str(page.get("nextPageToken") or "")
        if not token:
            return jobs


def _age_seconds(stamp: str, now: float) -> float | None:
    from datetime import datetime

    try:
        return now - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cloud_run_job_count(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    import time

    region = _sub(args.get("region") or t.region, t)
    jobs = _list_jobs(t, region, deps)
    count = len(jobs)
    limit = int(args.get("limit") or CLOUD_RUN_JOB_LIMIT)
    detail = f"{count} of {limit} Cloud Run Jobs in {region}"
    stale = 0
    if args.get("checks_ttl"):
        now = time.time()
        for job in jobs:
            done = str(((job.get("latestCreatedExecution") or {}).get("completionTime")) or "")
            age = _age_seconds(done, now) if done else None
            if age is not None and age > JOB_TTL_SECONDS:
                stale += 1
        detail += f"; {stale} finished more than {JOB_TTL_SECONDS // 60} min ago and are still kept"
    if count >= JOB_COUNT_WARN * limit:
        return UNMET, detail + " (past the headroom new dispatches need)"
    if stale and stale >= limit // 2:
        return UNMET, detail + ": nothing is deleting finished Jobs"
    return MET, detail


def _our_clusters(t: Target, location: str, deps: Deps) -> list[Mapping[str, Any]]:
    key = f"clusters:{location}"
    if key not in deps.cache:
        parent = f"projects/{t.project}/locations/{location}"
        found = deps.gcp.get(
            f"https://container.googleapis.com/v1/{parent}/clusters",
            permission="container.clusters.list",
            resource=f"//container.googleapis.com/{parent}",
        )
        label, value = MANAGED_BY
        deps.cache[key] = [
            c for c in found.get("clusters", ()) or ()
            if (c.get("resourceLabels") or {}).get(label) == value
        ]
    return list(deps.cache[key])


def _gke_authorized_networks(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    location = _sub(args.get("location") or t.region, t)
    clusters = _our_clusters(t, location, deps)
    if not clusters:
        return UNMET, f"no cluster labelled managed-by=swarm-terraform in {location}"
    absent = str(args.get("absent_display_name") or "")
    problems: list[str] = []
    shown: list[str] = []
    for cluster in clusters:
        name = str(cluster.get("name"))
        config = cluster.get("masterAuthorizedNetworksConfig") or {}
        blocks = config.get("cidrBlocks") or ()
        shown.append(f"{name}: " + (", ".join(
            f"{b.get('displayName') or '(unnamed)'} {b.get('cidrBlock')}" for b in blocks) or "none"))
        if not config.get("enabled"):
            problems.append(f"{name}'s control plane has no authorised networks: it is open to every address")
            continue
        for block in blocks:
            if block.get("cidrBlock") == "0.0.0.0/0":
                problems.append(f"{name} authorises 0.0.0.0/0")
            if absent and block.get("displayName") == absent:
                problems.append(
                    f"{name} still authorises the installer's address {block.get('cidrBlock')} "
                    f"({absent}); the closing infra pass removes it"
                )
    detail = "authorised networks: " + "; ".join(shown)
    if problems:
        return UNMET, "; ".join(problems) + f" ({detail})"
    return MET, detail


def _admits_installer(cluster: Mapping[str, Any]) -> bool:
    """Whether the control plane can admit a machine off Google's network: its
    authorised networks are off (open to all; `gke_authorized_networks` says so)
    or still hold the installer's entry (the install, or `swarm team add`, has
    it open)."""
    config = cluster.get("masterAuthorizedNetworksConfig") or {}
    if not config.get("enabled"):
        return True
    return any(b.get("displayName") == INSTALLER_NETWORK for b in config.get("cidrBlocks") or ())


def _rule_allows(rule: Mapping[str, Any], verb: str, group: str, resource: str) -> bool:
    def has(values: Iterable[str] | None, wanted: str) -> bool:
        values = list(values or ())
        return "*" in values or wanted in values

    return (
        has(rule.get("verbs"), verb)
        and has(rule.get("apiGroups"), group)
        and has(rule.get("resources"), resource)
    )


def _can_in(kube: Kube, namespace: str, who: str, verb: str, group: str, resource: str) -> bool:
    bindings = kube.get(f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings")
    for binding in bindings.get("items", ()) or ():
        names = {str(s.get("name")) for s in binding.get("subjects", ()) or () if s.get("kind") == "User"}
        if who not in names:
            continue
        ref = binding.get("roleRef") or {}
        if ref.get("kind") == "ClusterRole":
            path = f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{ref.get('name')}"
        else:
            path = f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{ref.get('name')}"
        try:
            role = kube.get(path)
        except NotFound:
            continue
        if any(_rule_allows(r, verb, group, resource) for r in role.get("rules", ()) or ()):
            return True
    return False


def _kubernetes_can_i(args: Mapping[str, Any], t: Target, deps: Deps) -> Result:
    subject = str(args["subject"])
    verb = str(args["verb"])
    resource, _, group = str(args["resource"]).partition(".")
    prefix = _sub(str(args["namespace"]), t).split("$T", 1)[0]
    if deps.kube is None:
        return UNKNOWN, "no cluster client here"
    clusters = _our_clusters(t, t.region, deps)
    if not clusters:
        return UNMET, f"no cluster labelled managed-by=swarm-terraform in {t.region}"
    if not deps.inside_google() and not _admits_installer(clusters[0]):
        return SKIPPED, CLOSED_CONTROL_PLANE
    email = f"{subject}@{t.project}.iam.gserviceaccount.com"
    account = deps.gcp.get(
        f"https://iam.googleapis.com/v1/projects/{t.project}/serviceAccounts/{email}",
        permission="iam.serviceAccounts.get",
        resource=f"//iam.googleapis.com/projects/{t.project}/serviceAccounts/{email}",
    )
    uid = str(account.get("uniqueId") or "")
    kube = deps.kube(clusters[0])
    found = kube.get("/api/v1/namespaces")
    namespaces = sorted(
        str((n.get("metadata") or {}).get("name"))
        for n in found.get("items", ()) or ()
        if str((n.get("metadata") or {}).get("name")).startswith(prefix)
    )
    if not namespaces:
        return UNMET, f"no {prefix}* namespace exists yet (the first team's, Admin › Setup)"
    refused = [
        (ns, who)
        for ns in namespaces
        for who in (email, uid)
        if not (who and _can_in(kube, ns, who, verb, group, resource))
    ]
    if refused:
        teams = tuple(dict.fromkeys(ns[len(prefix):] for ns, _ in refused))
        return (UNMET, f"{subject} may not {verb} {args['resource']} in "
                + "; ".join(f"{ns} as {who}" for ns, who in refused), teams)
    return MET, f"{subject} may {verb} {args['resource']} in {len(namespaces)} namespaces, by email and uniqueId"


PROBES: dict[str, ProbeFn] = {
    "service_enabled": _service_enabled,
    "secret_exists": _secret_exists,
    "iap_programmatic_clients": _iap_programmatic_clients,
    "groups_probe_via_api": _groups_probe_via_api,
    "cloud_run_job_count": _cloud_run_job_count,
    "gke_authorized_networks": _gke_authorized_networks,
    "kubernetes_can_i": _kubernetes_can_i,
}


# -- a 403, explained --------------------------------------------------------------


def troubleshoot(gcp: Gcp, principal: str, permission: str, resource: str) -> str:
    """The Policy Troubleshooter's verdict on (principal, permission, resource):
    "granted", "not_granted" or "unknown". Read-only."""
    # The API takes the bare email, not IAM's `user:`/`serviceAccount:` member form.
    email = principal.split(":", 1)[-1]
    if "@" not in email:
        return "unknown"
    try:
        found = gcp.post(
            "https://policytroubleshooter.googleapis.com/v3/iam:troubleshoot",
            {"accessTuple": {"principal": email, "fullResourceName": resource,
                             "permission": permission}},
            permission="policytroubleshooter.iam.troubleshoot",
            resource=resource,
        )
    except Exception:  # noqa: BLE001 - an unexplained 403 stays unknown
        return "unknown"
    state = str(found.get("overallAccessState") or "")
    if state == "CAN_ACCESS":
        return "granted"
    if state == "CANNOT_ACCESS":
        return "not_granted"
    return "unknown"


# -- the run -----------------------------------------------------------------------


def fix_words(entry: Entry) -> str:
    """The fix, in words: what the entry is for and what repairs it."""
    why = (entry.explain or "").strip()
    if len(why) > 240:
        why = why[:237].rstrip() + "..."
    if team_repairable(entry):
        how = (
            "the platform's swarm-workspace-apply job writes it into each team's namespace when the "
            f"team is made; {TEAM_REPAIR} <team> --project <project> has that job apply one team's "
            "tenant render again (no install step remakes it)"
        )
    elif entry.expand == PLATFORM_MADE:
        how = (
            "the platform's swarm-workspace-apply job writes it into each team's namespace when the "
            "team is made (Admin › Setup's first team, or a team added later); no install step makes "
            "it, so swarm install --repair has nothing to re-run"
        )
    elif entry.step is None:
        how = "no install step makes this; see the entry's check: " + (entry.check or entry.id)
    elif entry.provider == "engine":
        how = f"the install's step {entry.step} makes it: swarm install --repair {entry.id}"
    else:
        how = (
            f"step {entry.step} needs a person ({entry.provider}): swarm install --repair {entry.id} "
            "opens its page and waits for the change"
        )
    return f"{why} Fix: {how}." if why else f"Fix: {how}."


def check_entry(entry: Entry, target: Target, deps: Deps) -> Finding:
    assert entry.probe is not None
    args = entry.probe.model_dump()
    probe = PROBES.get(entry.probe.type)
    fix = fix_words(entry)
    repairable = entry.expand != PLATFORM_MADE
    if probe is None:
        return Finding(entry.id, UNKNOWN, f"this doctor has no probe of type {entry.probe.type!r}",
                       entry.step, fix, repairable=repairable)
    try:
        status, detail, *rest = probe(args, target, deps)
    except Denied as denied:
        verdict = troubleshoot(deps.gcp, target.principal, denied.permission, denied.resource)
        if verdict == "granted":
            return Finding(
                entry.id, UNKNOWN,
                f"{target.principal} holds {denied.permission} but was refused: IAM is still "
                f"propagating the grant ({PROPAGATION}); run swarm doctor again",
                entry.step, fix, repairable=repairable, cause="propagating",
            )
        if verdict == "not_granted":
            return Finding(
                entry.id, UNKNOWN,
                f"missing grant: {target.principal or 'this identity'} lacks {denied.permission} on "
                f"{denied.resource}, so this could not be checked",
                entry.step, fix, repairable=repairable, cause="missing_grant",
            )
        return Finding(entry.id, UNKNOWN, f"{denied} (the Policy Troubleshooter could not say why)",
                       entry.step, fix, repairable=repairable)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run is unknown, never fine
        return Finding(entry.id, UNKNOWN, f"{type(exc).__name__}: {exc}", entry.step, fix, repairable=repairable)
    teams: tuple[str, ...] = tuple(rest[0]) if rest and status != MET and team_repairable(entry) else ()
    repairs = tuple(team_repair_command(team, target.project) for team in teams)
    if repairs:
        fix = f"{fix} Repair: " + "; ".join(repairs) + "."
    return Finding(entry.id, status, detail, entry.step, fix, repairable=repairable, team_repairs=repairs)


def probed(manifest: Manifest) -> list[Entry]:
    """The entries the doctor checks: every one with a probe, core first."""
    return [e for e in manifest.entries if e.probe is not None]


def run(manifest: Manifest, target: Target, deps: Deps) -> list[Finding]:
    return [check_entry(entry, target, deps) for entry in probed(manifest)]


def exit_code(findings: Sequence[Finding]) -> ExitCode:
    """0 only when at least one finding was asked and every one asked is met;
    a skipped finding (`SKIPPED`) is left out, and all-skipped is not 0."""
    return for_statuses(f.status for f in findings if f.status != SKIPPED)


def hourly_doctor(target: Target, deps: Deps) -> str:
    """One line on the scheduled doctor: on (its job exists) or off."""
    if not target.region:
        return "the hourly doctor's job could not be looked for: the install state records no region"
    parent = f"projects/{target.project}/locations/{target.region}"
    try:
        deps.gcp.get(
            f"https://run.googleapis.com/v2/{parent}/jobs/{DOCTOR_JOB}",
            permission="run.jobs.get",
            resource=f"//run.googleapis.com/{parent}/jobs/{DOCTOR_JOB}",
        )
    except NotFound:
        return (
            "the hourly doctor is off: enable_scheduled_doctor is false in this deployment's "
            "Terraform, so Admin › Setup › Health shows no hourly findings"
        )
    except Exception as exc:  # noqa: BLE001
        return f"the hourly doctor's job could not be read ({type(exc).__name__}: {exc})"
    return f"the hourly doctor is on: the {DOCTOR_JOB} job posts to Admin › Setup › Health every hour"


def render(findings: Sequence[Finding], skipped: int, say: Callable[[str], None] = print) -> None:
    width = max((len(f.id) for f in findings), default=0)
    for f in findings:
        say(f"  {f.status:<7}  {f.id:<{width}}  {f.detail}")
        if f.status not in (MET, SKIPPED):
            say(f"           {f.fix}")
    met = sum(1 for f in findings if f.status == MET)
    unmet = sum(1 for f in findings if f.status == UNMET)
    unknown = sum(1 for f in findings if f.status == UNKNOWN)
    skipped_here = sum(1 for f in findings if f.status == SKIPPED)
    say(f"{met} met, {unmet} unmet, {unknown} unknown, {skipped_here} skipped of {len(findings)} probed entries; "
        f"{skipped} entries carry no probe and were verified by the install itself")


# -- the real transports (standard library only) -----------------------------------


METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"


def on_google_runtime(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("K_SERVICE") or env.get("CLOUD_RUN_JOB"))


def _metadata(path: str) -> str:
    request = urllib.request.Request(f"{METADATA}/{path}", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 - the fixed metadata URL
        return answer.read().decode()


def access_token() -> str:
    """An OAuth access token: the metadata server's on Cloud Run, else the
    caller's application-default credentials (imported only then)."""
    if on_google_runtime():
        return str(json.loads(_metadata("token"))["access_token"])
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    return str(credentials.token)


def identity_token(audience: str) -> str:
    """A Google ID token for `audience`, from the metadata server (the job)."""
    return _metadata(f"identity?audience={urllib.parse.quote(audience, safe='')}&format=full")


def principal() -> str:
    """The identity the probes run as, for the Policy Troubleshooter."""
    try:
        if on_google_runtime():
            return "serviceAccount:" + _metadata("email").strip()
        import google.auth

        credentials, _ = google.auth.default()
        email = getattr(credentials, "service_account_email", None) or getattr(credentials, "account", "")
        if email and "@" in str(email):
            kind = "serviceAccount" if str(email).endswith(".gserviceaccount.com") else "user"
            return f"{kind}:{email}"
    except Exception:  # noqa: BLE001 - no principal: a 403 is then left unexplained
        return ""
    return ""


def _call(method: str, url: str, token: str, body: Mapping[str, Any] | None,
          context: ssl.SSLContext | None = None) -> tuple[int, Mapping[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as answer:  # noqa: S310
            raw = answer.read()
            status = answer.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or "")[:300]
    return str(payload.get("message") or "")[:300]


@dataclass
class RestGcp:
    """Google's REST APIs with one access token (never printed or stored)."""

    token: Callable[[], str] = access_token
    _cached: str = field(default="", repr=False)

    def _bearer(self) -> str:
        if not self._cached:
            self._cached = self.token()
        return self._cached

    def _do(self, method: str, url: str, body: Mapping[str, Any] | None, permission: str,
            resource: str) -> Mapping[str, Any]:
        status, payload = _call(method, url, self._bearer(), body)
        if status == 404:
            raise NotFound(f"{url.split('?')[0]} is absent")
        if status == 403:
            raise Denied(permission, resource, _message(payload))
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status} from {url.split('?')[0]}: {_message(payload)}")
        return payload

    def get(self, url: str, *, permission: str, resource: str) -> Mapping[str, Any]:
        return self._do("GET", url, None, permission, resource)

    def post(self, url: str, body: Mapping[str, Any], *, permission: str,
             resource: str) -> Mapping[str, Any]:
        return self._do("POST", url, body, permission, resource)


@dataclass
class RestKube:
    """The cluster's API server, at its private endpoint, verified against the
    cluster's own CA; the same Google access token authenticates (GKE)."""

    endpoint: str
    ca_pem: str
    token: Callable[[], str]

    def get(self, path: str) -> Mapping[str, Any]:
        context = ssl.create_default_context(cadata=self.ca_pem)
        status, payload = _call("GET", f"https://{self.endpoint}{path}", self.token(), None, context)
        if status == 404:
            raise NotFound(path)
        if status == 403:
            raise PermissionError(f"the cluster refused GET {path}: {_message(payload)}")
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status} from the cluster for {path}")
        return payload


def kube_for(token: Callable[[], str]) -> Callable[[Mapping[str, Any]], Kube]:
    def build(cluster: Mapping[str, Any]) -> Kube:
        private = ((cluster.get("privateClusterConfig") or {}).get("privateEndpoint")) or ""
        endpoint = private if on_google_runtime() and private else str(cluster.get("endpoint") or "")
        ca = base64.b64decode(str((cluster.get("masterAuth") or {}).get("clusterCaCertificate") or "")).decode()
        if not endpoint or not ca:
            raise LookupError(f"cluster {cluster.get('name')} has no endpoint or CA")
        return RestKube(endpoint=endpoint, ca_pem=ca, token=token)

    return build
