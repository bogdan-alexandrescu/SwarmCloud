"""Stage 14, verify: done is proven, never assumed (design §4, D3; plan §5.4,
§5.5 step 3; lessons 18, 28, 30, 38, 43).

| Step | Manifest entries | Check | Lesson |
|---|---|---|---|
| `verify.scheduler-targets` | `other.verify-scheduler-targets` | every enabled platform Scheduler job is ENABLED, its last attempt (once it has run) answered, and its target's invoker binding names the job's own account with an audience the target accepts; no target is called | 38: Scheduler said success while its target answered 403 on every tick |
| `verify.digests` | `other.verify-digests` | every service serves 100 % on its latest ready revision, and that revision's **digest** is the release's | 28: labels lie; a pinned revision served 0 % |
| `verify.reconciler-jobs` | (lesson 30) | the reconciler may list Jobs in every tenant namespace, namespaced, by email and by uniqueId | 30: it listed Jobs at cluster scope and saw nothing |
| `verify.firestore-migrations` | `other.firestore-migrations` | the state and the install record are at this engine's schema | |
| `verify.end-to-end` | `other.verify-end-to-end`, `other.verify-row`, `other.verify-gke`, `other.verify-live-browser`, `other.verify-code-intelligence` | seven runs on the NEW deployment: a single task and a workflow from the plugin and from the console, each to a pull request, and three check runs (a task on GKE, a live-browser task, a code-intelligence read) | 18: the plugin signs in with the Desktop client; 43: every run's task cloned and pushed with an App installation token (below) |

**The new deployment, never the person's current context.** The step uses
the engine's own swarm context, which `edge.desktop-client` (stage 2) wrote
with the Desktop client's secret in the swarm credential store, the value
the person typed at its hidden prompt (`swarm_context`; named after the
hostname, never `--use`; a context of that name pointing elsewhere is left
alone and the engine's is `<hostname>-install`), so verify never needs the
secret. It signs the person in to that context once in the browser, and
picks the first team as `SWARM_TENANT`. Every run is `swarm
--context <it>`, with no `SWARM_URL`, `SWARM_CONTEXT` or `SWARM_TENANT` from
the person's shell. The repository is the first, not archived, the GitHub App
installation covers (`swarm sc access repos <github_org>`), recorded as
`verify_repository`. The console's two runs are started in Admin › Setup;
swarm-api writes their ids into a console document (`verify_runs`:
`console_task`, `console_workflow`) and the step copies them into
state.outputs: nobody is asked for an id.

**The check runs prove themselves.** The GKE task's worker Job is seen in the
team's namespace; the live-browser task (`claude-code-browser`) ran with the
`swarm-live=enabled` label and the `display` sidecar, and the page title it
left names the repository; the code-intelligence read left the swarm-graph
answer with at least one symbol and a `freshness.index_sha` that is the
commit the graph's own index record names for the repository (`swarm
graph-index`: swarm-api's graph-cache route, `index.commit_sha`, which only
swarm-graph's promotion writes), so the agent cannot make the proof up. A finished
Job is deleted after its TTL, so a poll's sighting is kept in state.outputs
(`verify_cluster_jobs`).

**Lesson 43 is read from what each run recorded.** The worker writes the
KIND of GitHub credential an attempt cloned and pushed with into its
result summary (`forge_credential`: `kind`, and `source`, the secret's
NAME; never a value), and swarm-api serves that summary to anyone who may
read the task. The step reads it for every task a run is made of (a
workflow's every step) through `swarm result <task> --json`, and passes
only when each says `app_installation`; any other kind, or none recorded,
fails the step naming the run, the task, the kind and the secret.

**Only the platform's own** (`swarm-*`) Scheduler jobs and Cloud Run services
are verified: the project may hold others. No target is called, the
platform's either: calling the doctor's `jobs:run` starts a doctor run, and an
ID token minted as each job's own account needs a Token Creator grant the
installer does not hold (#11). A job is judged from the Cloud Scheduler API
(its state, and its last attempt's status once it has run) and from the
target's IAM policy, the resource's own and the project's.

**The console's verify row** (D26): every step writes the engine document's
`verify` field as it goes (`check`, `done`, `total`, `started`,
`expected_end`, `deadline`, `result`), so Admin › Setup draws "Verify, run by
swarm install" with a countdown, and "Send to your team" unlocks only when it
passes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..exitcodes import MET, UNMET, WAITING
from ..record import PERSONAL, RECORD_SCHEMA_VERSION, VERIFY_RUNS, engine_document
from ..state import SCHEMA_VERSION
from ..swarm_context import Run, deployment_url, list_contexts, points_at, run_command, run_interactive, verify_env
from ..waits import WaitSpec
from . import Observation, Plan
from .cluster import Kube, account_email, kube_for
from .preflight import ASK, Ask, Gcp, remember

SCHEDULER_API = "https://cloudscheduler.googleapis.com/v1"
RUN_API = "https://run.googleapis.com/v2"

#: The checks the verify row counts, in the order stage 14 runs them; the
#: end-to-end check is seven runs: the four product runs, each to a pull
#: request, and the three check runs (`CHECK_RUNS`), all on the new deployment.
QUICK_CHECKS = ("scheduler-targets", "digests", "reconciler-jobs", "firestore-migrations")
CHECK_SHAPES = ("gke", "browser", "code-intelligence")
RUNS = (("plugin", "single"), ("plugin", "workflow"), *(("plugin", shape) for shape in CHECK_SHAPES),
        ("console", "single"), ("console", "workflow"))
TOTAL = len(QUICK_CHECKS) + len(RUNS)

#: Where each console run's id is in `verify_runs`: swarm-api writes these two
#: keys into the console document when Admin › Setup starts them (INTERFACE
#: SHEET), and the engine copies them into state.outputs.
CONSOLE_KEYS = {"single": "console_task", "workflow": "console_workflow"}

#: A tick's target may 403 for a while after its grant lands (lesson 19).
TARGETS_WAIT = WaitSpec(
    reason="every Scheduler job enabled, its last attempt answered and its target invocable by its account",
    expected="at once, or up to 10 min while a new grant propagates",
    expected_s=60,
    bound_s=10 * 60,
    look_at="the failing job's target service logs; swarm doctor --explain other.verify-scheduler-targets",
    interval_s=30,
)

#: Seven runs, four of them to a pull request: an agent's run takes minutes.
RUNS_WAIT = WaitSpec(
    reason="four verification runs each opening a pull request, and the GKE, live-browser and "
           "code-intelligence checks",
    expected="15 to 25 min",
    expected_s=25 * 60,
    bound_s=60 * 60,
    look_at="swarm status <task id> for each run named above; the console's task pages",
    interval_s=30,
)

#: How many times a failed or cancelled verification run is relaunched before
#: the step stops on it (#19): a flaky run gets two more chances; a third
#: failure is a fault, read in `swarm status`.
RELAUNCHES = 2

#: The platform's Scheduler jobs and Cloud Run services are named `swarm-*`.
PLATFORM_PREFIX = "swarm-"

#: Lesson 43: the only credential kind a verification run may clone and push
#: with, as the worker records it (`result_summary.forge_credential.kind`).
APP_INSTALLATION = "app_installation"

#: The kinds the worker writes (`agent_worker.secrets.FORGE_CREDENTIAL_KINDS`);
#: a run is named by one of these, by "unrecorded", or by "unrecognised" --
#: never by a string read from a summary that is not one of them.
CREDENTIAL_KINDS = frozenset({"app_installation", "app_user", "pat", "none", "unknown", "mixed"})
UNRECORDED = "unrecorded"
UNRECOGNISED = "unrecognised"

#: A forge slot's secret name (`Tenant.secret_name`), the only `source` named.
_SOURCE = re.compile(r"swarm-tenant-[a-z0-9][a-z0-9-]{0,200}")

#: `sc access repos` refusing because the person has not connected GitHub
#: (swarm-api's `github_not_connected`); any other failure is not retried.
_NOT_CONNECTED = re.compile(r"not[ _]connected", re.IGNORECASE)

#: The commit a code-intelligence answer's `freshness.index_sha` must be.
_COMMIT = re.compile(r"[0-9a-f]{40}")

#: Task states (swarm_common's vocabulary) as `RunStatus.state` names them;
#: anything else is still going.
TERMINAL_STATES = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELLED": "cancelled"}


class VerifyGcp(Gcp):
    """`preflight.Gcp` plus the reads stage 14 makes."""

    def scheduler_jobs(self, project: str, region: str) -> list[dict[str, Any]]:
        return self._pages(f"{SCHEDULER_API}/projects/{project}/locations/{region}/jobs", "jobs")

    def run_services(self, project: str, region: str) -> list[dict[str, Any]]:
        """Cloud Run's v2 view: `uri`, every one of `urls`, `customAudiences`, `invokerIamDisabled`."""
        return self._pages(f"{RUN_API}/projects/{project}/locations/{region}/services", "services")

    def run_iam_policy(self, name: str) -> dict[str, Any]:
        """A Cloud Run service's or job's own IAM policy (`projects/p/locations/r/services/s`
        or `.../jobs/j`)."""
        return self._call("GET", f"{RUN_API}/{name}:getIamPolicy") or {}

    def _knative(self, project: str, region: str) -> str:
        return f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project}"

    def knative_services(self, project: str, region: str) -> list[dict[str, Any]]:
        """Cloud Run's v1 view: `status.traffic` and the revision names."""
        return list((self._call("GET", f"{self._knative(project, region)}/services") or {}).get("items", []))

    def knative_revision(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        """A revision's v1 view: `status.imageDigest` is the digest it resolved."""
        return self._call("GET", f"{self._knative(project, region)}/revisions/{name}", missing_ok=True)


@dataclass
class _Lazy:
    """One `VerifyGcp` for every stage 14 step, built on first use (never at import)."""

    made: list[VerifyGcp] = field(default_factory=list)

    def __call__(self) -> VerifyGcp:
        if not self.made:
            self.made.append(VerifyGcp())
        return self.made[0]


VERIFY_GCP = _Lazy()


# -- the verify row ------------------------------------------------------------


def write_progress(ctx: Any, check: str, done: int, *, result: str | None = None) -> dict[str, Any]:
    """Write the engine document's `verify` field (the console's verify row).
    `started` is the stage's first write; the countdown runs to the runs'
    expected end and bound from there."""
    started = ctx.outputs.get("verify_started")
    if started is None:
        started = ctx.clock.now()
        remember(ctx, outputs={"verify_started": started})
    row = {
        "check": check,
        "done": done,
        "total": TOTAL,
        "started": started,
        "expected_end": started + RUNS_WAIT.expected_s,
        "deadline": started + RUNS_WAIT.bound_s,
        "result": result,
    }
    engine = getattr(ctx, "engine", None)
    record = getattr(engine, "record", None)
    if record is not None and not ctx.dry_run:
        record.write(ctx.state, getattr(engine, "_stage_of", {}), lease=engine.lease.as_record(), verify=row)
    # The terminal's countdown is the events' own `wait_countdown` kind,
    # carrying the row's fields, so a reader of the JSON events needs no new kind.
    ctx.events.emit("wait_countdown", step=f"verify.{check}", reason=RUNS_WAIT.reason,
                    state=result or "waiting", expected=RUNS_WAIT.expected,
                    remaining_s=max(0.0, row["deadline"] - ctx.clock.now()), **row)
    return row


# -- judging a Scheduler job without calling its target -------------------------

#: The roles that let a job's account call a Cloud Run service
#: (`run.routes.invoke`) or start a Cloud Run job (`run.jobs.run`).
SERVICE_INVOKER_ROLES = frozenset({"roles/run.invoker", "roles/run.servicesInvoker", "roles/run.developer",
                                   "roles/run.admin", "roles/editor", "roles/owner"})
JOB_RUNNER_ROLES = frozenset({"roles/run.invoker", "roles/run.jobsExecutor", "roles/run.jobsExecutorWithOverrides",
                              "roles/run.developer", "roles/run.admin", "roles/editor", "roles/owner"})

#: `jobs:run` on the Cloud Run Admin API, v2 or the regional v1 form.
_JOB_RUN = (
    re.compile(r"https://(?:[a-z0-9-]+-)?run\.googleapis\.com/v2/"
               r"(projects/[^/]+/locations/[^/]+/jobs/[^/:?]+):run"),
    re.compile(r"https://(?P<region>[a-z0-9-]+)-run\.googleapis\.com/apis/run\.googleapis\.com/v1/"
               r"namespaces/(?P<project>[^/]+)/jobs/(?P<job>[^/:?]+):run"),
)


def _origin(url: str) -> str:
    """`https://host` of a URL: what a Cloud Run service's URL is matched on."""
    found = re.match(r"(https?://[^/?#]+)", url.strip())
    return found.group(1).lower() if found else ""


def _origins(service: Mapping[str, Any]) -> set[str]:
    """Every `https://host` a Cloud Run service answers on."""
    return {_origin(str(u)) for u in [service.get("uri") or "", *(service.get("urls") or [])] if u}


def _epoch(stamp: str) -> float | None:
    """An RFC 3339 time as epoch seconds, or None."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else None
    except ValueError:
        return None


def _job_run_target(uri: str) -> str:
    """The Cloud Run job a `jobs:run` URI starts, as `projects/p/locations/r/jobs/j`, or ""."""
    v2 = _JOB_RUN[0].match(uri)
    if v2:
        return v2.group(1)
    v1 = _JOB_RUN[1].match(uri)
    if v1:
        return f"projects/{v1['project']}/locations/{v1['region']}/jobs/{v1['job']}"
    return ""


def _granted(policies: Sequence[Mapping[str, Any]], roles: frozenset[str], account: str) -> bool:
    """Whether an unconditional binding in any of `policies` gives `account`
    (or everyone) one of `roles`."""
    members = {"allUsers", "allAuthenticatedUsers"} | ({f"serviceAccount:{account}"} if account else set())
    return any(binding.get("role") in roles and not binding.get("condition")
               and members & set(binding.get("members") or ())
               for policy in policies for binding in (policy.get("bindings") or ()))


class _Base:
    stage = 14
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    check_name = ""
    _explain = ""

    def __init__(self, gcp: Callable[[], VerifyGcp], ask: Ask) -> None:
        self._gcp = gcp
        self._ask = ask

    @property
    def gcp(self) -> VerifyGcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"verify: {self.check_name}")

    def apply(self, ctx: Any, plan: Plan) -> None:
        """A verify check repairs nothing: its detail names what failed and where."""

    def _done(self, ctx: Any, observed: Observation) -> Observation:
        if observed.status == MET and self.check_name in QUICK_CHECKS:
            write_progress(ctx, self.check_name, QUICK_CHECKS.index(self.check_name) + 1)
        return observed

    def check(self, ctx: Any) -> Observation:
        return self._done(ctx, self._check(ctx))

    def _check(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")


class SchedulerTargetsStep(_Base):
    id = "verify.scheduler-targets"
    kind = "wait"
    wait = TARGETS_WAIT
    check_name = "scheduler-targets"
    satisfies = ("other.verify-scheduler-targets",)
    _explain = (
        "Cloud Scheduler reports a tick as attempted even when its target refuses it: the quota sweep "
        "answered 403 on every tick for weeks (lesson 38). Each enabled platform job is judged from the "
        "Cloud Scheduler API (state ENABLED, and its last attempt's status once it has run) and from its "
        "target's invoker binding for the job's own account, with an audience the service accepts. No "
        "target is called: calling the doctor's job starts a doctor run, and an ID token as a job's "
        "account needs a grant the installer does not hold."
    )

    def jobs(self, ctx: Any) -> list[dict[str, Any]]:
        return self.gcp.scheduler_jobs(ctx.project, str(ctx.state.region))

    def _check(self, ctx: Any) -> Observation:
        services: list[list[dict[str, Any]]] = []  # read once, on the first service target
        policies: dict[str, dict[str, Any]] = {}
        project_policy: list[dict[str, Any]] = []

        def policy(name: str) -> dict[str, Any]:
            if name not in policies:
                policies[name] = self.gcp.run_iam_policy(name) or {}
            return policies[name]

        def inherited() -> dict[str, Any]:
            if not project_policy:
                project_policy.append(self.gcp.iam_policy(ctx.project) or {})
            return project_policy[0]

        failing: list[str] = []
        judged: list[str] = []
        paused: list[str] = []
        noted: list[str] = []
        for job in self.jobs(ctx):
            name = str(job.get("name", "")).rsplit("/", 1)[-1]
            if not name.startswith(PLATFORM_PREFIX):
                continue
            state = str(job.get("state") or "STATE_UNSPECIFIED")
            if state == "PAUSED":
                paused.append(name)
                continue
            judged.append(name)
            before = len(failing)
            if state != "ENABLED":
                failing.append(f"{name}: state {state}, not ENABLED")
            last = job.get("status") or {}
            refused = ""
            if job.get("lastAttemptTime") and int(last.get("code") or 0) != 0:
                refused = (f"{name}: its last attempt at {job['lastAttemptTime']} failed "
                           f"(code {last.get('code')}: {str(last.get('message') or '')[:200]})")
            target = job.get("httpTarget") or {}
            failing.extend(self._target(ctx, name, target, policy, inherited, services) if target else ())
            if refused:
                # A tick refused before its grant propagated (lesson 19) is
                # only seen again at the next tick: one this wait will see is
                # waited for; an hourly or daily job's, past the bound, with
                # its target's binding now in place, is named and not held
                # against the step.
                next_tick = _epoch(str(job.get("scheduleTime") or ""))
                if (len(failing) == before and next_tick is not None
                        and next_tick > ctx.clock.now() + TARGETS_WAIT.bound_s):
                    noted.append(f"{refused}; its next tick is at {job['scheduleTime']}")
                else:
                    failing.append(refused)
        if failing:
            return Observation(WAITING, "Scheduler jobs whose targets would not take their ticks: "
                                        + "; ".join(failing))
        detail = f"{len(judged)} Scheduler jobs enabled, their last attempts answered and their targets invocable"
        if paused:
            detail += f" ({len(paused)} paused, not judged: {', '.join(paused)})"
        if noted:
            detail += "; binding in place, next tick past this wait: " + "; ".join(noted)
        return Observation(MET, detail, {"scheduler_targets": judged})

    def _target(self, ctx: Any, name: str, target: Mapping[str, Any], policy: Callable[[str], dict[str, Any]],
                inherited: Callable[[], dict[str, Any]], services: list[list[dict[str, Any]]]) -> list[str]:
        """What would stop `target` taking the job's tick: its invoker binding
        for the job's own account, and (OIDC) an audience it accepts."""
        uri = str(target.get("uri") or "")
        token = target.get("oidcToken") or target.get("oauthToken") or {}
        account = str(token.get("serviceAccountEmail") or "")
        caller = account or "an unauthenticated caller"
        run_job = _job_run_target(uri)
        if run_job:
            if _granted([policy(run_job), inherited()], JOB_RUNNER_ROLES, account):
                return []
            return [f"{name}: {caller} may not invoke {run_job}"]
        if not services:
            services.append(self.gcp.run_services(ctx.project, str(ctx.state.region)))
        origin = _origin(uri)
        service = next((s for s in services[0] if origin and origin in _origins(s)), None)
        if service is None:
            return [f"{name}: {uri} is not a Cloud Run service or job in {ctx.project}/{ctx.state.region}, "
                    "so its invoker cannot be read"]
        resource = str(service.get("name") or "")
        problems: list[str] = []
        oidc = target.get("oidcToken")
        if oidc:
            audience = str(oidc.get("audience") or uri)
            if _origin(audience) not in _origins(service) and audience not in (service.get("customAudiences") or []):
                problems.append(f"{name}: audience {audience} is not {resource.rsplit('/', 1)[-1]}'s "
                                "(its URLs or custom audiences)")
        if service.get("invokerIamDisabled") is not True and not _granted(
                [policy(resource), inherited()], SERVICE_INVOKER_ROLES, account):
            problems.append(f"{name}: {caller} may not invoke {resource}")
        return problems


class DigestsStep(_Base):
    id = "verify.digests"
    check_name = "digests"
    satisfies = ("other.verify-digests",)
    _explain = (
        "What a service serves is the digest of the revision that takes its traffic, never a label: the "
        "`swarm-image-tag` label was wrong on four services, and traffic pinned to a revision made a later "
        "deploy serve 0 % (lesson 28)."
    )

    def _check(self, ctx: Any) -> Observation:
        release = {str(d).rsplit("@", 1)[-1] for d in (ctx.outputs.get("image_digests") or {}).values()}
        if not release:
            raise LookupError("the release's image digests are not recorded (stage 4 copies by digest and records them)")
        region = str(ctx.state.region)
        services = self.gcp.knative_services(ctx.project, region)
        problems: list[str] = []
        serving: dict[str, str] = {}
        for service in services:
            name = str(service.get("metadata", {}).get("name", ""))
            if not name.startswith(PLATFORM_PREFIX):
                continue
            status = service.get("status") or {}
            latest = status.get("latestReadyRevisionName")
            traffic = status.get("traffic") or []
            on_latest = sum(int(t.get("percent") or 0) for t in traffic
                            if t.get("latestRevision") or t.get("revisionName") == latest)
            if not latest or on_latest != 100:
                problems.append(f"{name} serves {on_latest} % on its latest ready revision {latest}")
                continue
            revision = self.gcp.knative_revision(ctx.project, region, str(latest)) or {}
            digest = str((revision.get("status") or {}).get("imageDigest") or "").rsplit("@", 1)[-1]
            serving[name] = digest
            if digest not in release:
                problems.append(f"{name} serves {digest or 'no digest'}, not one of release {ctx.state.release}'s")
        if problems:
            return Observation(UNMET, "; ".join(problems))
        return Observation(MET, f"{len(serving)} services serve the release's digests", {"serving_digests": serving})


class ReconcilerJobsStep(_Base):
    id = "verify.reconciler-jobs"
    check_name = "reconciler-jobs"
    _explain = (
        "The reconciler releases a dead GKE lease only if it can see the Job; it once listed Jobs at "
        "cluster scope, which it was never granted, and saw nothing (lesson 30). It may list them in every "
        "tenant namespace, as its email and as its uniqueId (lesson 29)."
    )

    def __init__(self, gcp: Callable[[], VerifyGcp], ask: Ask, *, kube: Callable[[Any], Kube] = kube_for) -> None:
        super().__init__(gcp, ask)
        self._kube = kube

    def _check(self, ctx: Any) -> Observation:
        email = account_email(ctx.project, "swarm-reconciler")
        uid = str((self.gcp.service_account(email) or {}).get("uniqueId") or "")
        if not uid.isdigit():
            raise LookupError(f"{email} has no uniqueId in IAM")
        kube = self._kube(ctx)
        code, out, err = kube("get", "namespaces", "-o", "name")
        if code != 0:
            raise RuntimeError(f"kubectl get namespaces failed: {err.strip()[:300]}")
        namespaces = [n.split("/", 1)[-1] for n in out.split() if n.split("/", 1)[-1].startswith("swarm-tenant-")]
        if not namespaces:
            return Observation(UNMET, "no tenant namespace exists yet (the first team's, stage 11)")
        refused = []
        for namespace in namespaces:
            for who in (email, uid):
                code, answer, _ = kube("auth", "can-i", "list", "jobs.batch", f"--namespace={namespace}",
                                       f"--as={who}")
                if answer.strip() != "yes":
                    refused.append(f"{namespace} as {who}")
        if refused:
            return Observation(UNMET, "the reconciler may not list Jobs in " + "; ".join(refused))
        return Observation(MET, f"the reconciler lists Jobs in {len(namespaces)} tenant namespaces")


class FirestoreMigrationsStep(_Base):
    id = "verify.firestore-migrations"
    check_name = "firestore-migrations"
    satisfies = ("other.firestore-migrations",)
    _explain = (
        "The install state and the install record carry a schema version; an install is at this engine's, "
        "and swarm upgrade runs the release's numbered migrations before anything reads a newer shape."
    )

    def _check(self, ctx: Any) -> Observation:
        if ctx.state.schema_version != SCHEMA_VERSION:
            return Observation(UNMET, f"the install state is schema {ctx.state.schema_version}, not {SCHEMA_VERSION}")
        record = getattr(getattr(ctx, "engine", None), "record", None)
        if record is None:
            return Observation(UNMET, "the install record is not attached (Firestore, stage 5)")
        found = record.documents.get(engine_document(record.env))
        version = (found[0] if found else {}).get("schema_version")
        if version != RECORD_SCHEMA_VERSION:
            return Observation(UNMET, f"the install record is schema {version}, not {RECORD_SCHEMA_VERSION}")
        return Observation(MET, f"state schema {SCHEMA_VERSION}, record schema {RECORD_SCHEMA_VERSION}")


# -- the seven runs ---------------------------------------------------------------


@dataclass(frozen=True)
class CredentialUse:
    """Which kind of GitHub credential one task of a run used (lesson 43)."""

    task_id: str
    kind: str  # one of CREDENTIAL_KINDS, UNRECORDED or UNRECOGNISED
    source: str = ""  # the secret's NAME, or ""

    def words(self) -> str:
        source = f" from {self.source}" if self.source else ""
        if self.kind == UNRECORDED:
            return f"task {self.task_id} recorded no GitHub credential kind"
        return f"task {self.task_id} used a {self.kind} credential{source}"


def credential_use(task_id: str, summary: Any) -> CredentialUse:
    """`result_summary.forge_credential` as a `CredentialUse`: only a kind the
    worker writes and a slot-shaped secret name are carried; nothing else in
    the summary is repeated."""
    found = summary.get("forge_credential") if isinstance(summary, dict) else None
    if not isinstance(found, dict) or found.get("kind") is None:
        return CredentialUse(task_id, UNRECORDED)
    kind = found.get("kind")
    kind = kind if isinstance(kind, str) and kind in CREDENTIAL_KINDS else UNRECOGNISED
    source = found.get("source")
    source = source if isinstance(source, str) and _SOURCE.fullmatch(source) else ""
    return CredentialUse(task_id, kind, source)


@dataclass(frozen=True)
class RunStatus:
    state: str  # running | succeeded | failed | cancelled
    pull_request: str = ""
    #: Every task of a succeeded run, with the credential kind it recorded.
    credentials: tuple[CredentialUse, ...] = ()
    #: A check run's runner profile, as `swarm status` names it.
    profile: str = ""
    #: A succeeded check run's artifact (`CheckRun.artifact`), or None when it left none.
    artifact: str | None = None
    #: The commit the code graph's own index record names for the repository
    #: (`swarm graph-index`), for a `CheckRun.graph_record` run; "" when none.
    index_commit: str = ""


@dataclass(frozen=True)
class CheckRun:
    """One check run: a task on the new deployment whose proof is its own.

    `on_cluster`: its worker Job must be seen in the team's namespace (a GKE
    backend, not a fallback); `live`: that Job carries the live-browser pod
    label and its display sidecar; `artifact`: the file the agent leaves,
    read back through `swarm artifact` (the API redacts it at read time);
    `graph_record`: the answer's `freshness.index_sha` must be the commit the
    code graph's own index record names (`swarm graph-index`), which only
    swarm-graph's promotion writes, so an agent cannot make the proof up."""

    profile: str
    prompt: str
    artifact: str = ""
    on_cluster: bool = False
    live: bool = False
    graph_record: bool = False

    def prompt_for(self, repository: str) -> str:
        return self.prompt.format(repository=repository, artifact=self.artifact)


#: The three checks the console claims (smoke on GKE, the live browser, code
#: intelligence), each a run. The profiles are the frozen catalogue's
#: (swarm_common.profiles): `claude-code` runs on GKE Autopilot, and
#: `claude-code-browser` is the live-browser one.
CHECK_RUNS: dict[str, CheckRun] = {
    "gke": CheckRun(
        "claude-code",
        "List the top-level entries of {repository} and write them, comma-separated, to the artifact "
        "gke.txt. Change nothing in the repository and open no pull request.",
        "gke.txt", on_cluster=True),
    "browser": CheckRun(
        "claude-code-browser",
        "Open https://github.com/{repository} in the live browser and write the page's title, exactly as the "
        "browser shows it, as the only line of the artifact {artifact}. Change nothing in the repository and "
        "open no pull request.",
        "live-browser.txt", on_cluster=True, live=True),
    "code-intelligence": CheckRun(
        "claude-code",
        "Call the swarm-graph `search` tool once on {repository} for its entry point, and write its answer, "
        "unchanged, as JSON to the artifact {artifact}: the `freshness` object (with `index_sha`) and the "
        "`rows` it returned. Change nothing in the repository and open no pull request.",
        "code-intelligence.json", graph_record=True),
}


class Runs(Protocol):
    def target(self, ctx: Any) -> None: ...

    def repository(self, ctx: Any) -> str: ...

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str: ...

    def status(self, ctx: Any, shape: str, run_id: str) -> RunStatus: ...


def workflow_spec(repository: str, label: str) -> str:
    """A two-step workflow: one agent writes, a second builds on it and opens
    the PR. Every key is one `swarm_mcp.workflows.read_spec` accepts (`step_id`,
    `runner_profile`, `builds_on`); `swarm workflow` refuses any other. The
    repository and label travel as `--repo` and `--label`."""
    return json.dumps({
        "steps": [
            {"step_id": "write", "runner_profile": "claude-code",
             "prompt": "Add a file VERIFY.md saying this deployment ran a workflow; commit it."},
            {"step_id": "open", "runner_profile": "claude-code", "depends_on": ["write"], "builds_on": "write",
             "prompt": "Open a pull request with the previous step's commit, titled 'swarm install verify'."},
        ],
    })


SINGLE_PROMPT = ("Add a file VERIFY.md saying this deployment ran a single task, and open a pull request "
                 "titled 'swarm install verify'.")


def _state(raw: Any) -> str:
    return TERMINAL_STATES.get(str(raw or "").upper(), "running")


def _rows(found: Any, key: str) -> list[dict[str, Any]]:
    rows = found.get(key) if isinstance(found, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


@dataclass
class SwarmCli:
    """The runs through the `swarm` CLI, as the person, signed in with the
    deployment's Desktop client, on the engine's OWN context for the new
    deployment (`target`). The console's two are started by Admin › Setup
    (the engine cannot press its buttons) and their ids are read from the
    install record, never asked; then all seven are followed the same way.

    Read as the CLI prints them (apps/swarm-mcp/swarm_mcp/cli.py):
    `swarm dispatch --json` prints the task (`id`); `swarm workflow --json`
    prints the envelope (`workflow.workflow_id`); `swarm status <task>` prints
    `<id>  <STATE>  <profile>  ...`; `swarm result <task> --json` prints only
    the task's `result_summary` (`git.pull_request.url`); `swarm artifact
    <task> <name>` prints an artifact (exit 1 when there is none); and a
    workflow, which `/v1/tasks/<id>` does not serve, is followed by `swarm
    workflow-status <id> --result --json`: its derived `state` (null, exit 1,
    while the server did not derive) and each step's `produced.pull_request`.

    Once a run succeeded, each of its tasks' `swarm result <task> --json`
    gives the credential kind the worker recorded (`forge_credential`,
    lesson 43): the single task's own, and every workflow step's."""

    ask: Ask
    run: Run = run_command
    swarm: str = "swarm"
    #: The person's own steps: `swarm login` and `swarm sc setup` sign in in the browser.
    interactive: Run = run_interactive

    # -- the new deployment's own context ------------------------------------------

    def _env(self, ctx: Any) -> dict[str, str]:
        return verify_env(ctx.outputs.get("verify_tenant"))

    def _bare(self, ctx: Any, argv: Sequence[str], stdin: str | None = None) -> str:
        code, out, err = self.run([self.swarm, *argv], stdin, self._env(ctx))
        if code != 0:
            raise RuntimeError(f"swarm {' '.join(argv[:2])} failed: {err.strip()[:300]}")
        return out

    def _context(self, ctx: Any) -> str:
        name = str(ctx.outputs.get("verify_context") or "")
        if not name:
            raise LookupError("verify has no swarm context for the new deployment yet (verify.end-to-end writes it)")
        return name

    def target(self, ctx: Any) -> None:
        """Sign in (once) to the swarm context `edge.desktop-client` wrote for
        the new deployment, with the Desktop client's secret already in the
        credential store (`swarm_context.store_desktop_secret`), and pick the
        first team as the tenant. Verify never needs the secret: it writes no
        context, and the person's current context is never switched."""
        url = deployment_url(ctx)
        name = str(ctx.outputs.get("verify_context") or "")
        if not url or not name:
            raise LookupError("edge.desktop-client (stage 2) writes the swarm context for the new deployment, "
                              "with the Desktop client's secret, and records it as verify_context")
        row = list_contexts(self.run, self.swarm, self._env(ctx)).get(name)
        if row is None or not points_at(row, url):
            raise LookupError(f"swarm context {name} for {url} is not on this machine; edge.desktop-client "
                              f"writes it again, its secret typed at a hidden prompt: swarm install")
        if row.get("signed_in") is not True:
            ctx.events.emit("step_apply", step="verify.end-to-end",
                            message=f"sign in to {url} in the browser that opens (swarm --context {name} login)")
            code, _, err = self.interactive([self.swarm, "--context", name, "login"], None, self._env(ctx))
            if code != 0:
                raise RuntimeError(f"swarm --context {name} login failed: {err.strip()[:300]}")
        tenants = json.loads(self._bare(ctx, ["--context", name, "tenants", "--json"]) or "{}")
        ids = [str(row.get("tenant_id") or "") for row in _rows(tenants, "tenants")]
        tenant = str(tenants.get("current") or "") if isinstance(tenants, dict) else ""
        tenant = tenant if tenant in ids else next((t for t in ids if t), "")
        if not tenant:
            raise LookupError(f"{url} lists no team for you yet (the first team, stage 11)")
        remember(ctx, outputs={"verify_tenant": tenant})

    def repository(self, ctx: Any) -> str:
        """The first repository, not archived, the GitHub App installation
        covers, read on the new deployment. With no GitHub connection yet the
        person connects once in the browser (`swarm sc setup`) and it is read
        again."""
        name = self._context(ctx)
        org = str(ctx.outputs.get("github_org") or "")
        if not org:
            raise LookupError("the GitHub organisation is recorded by preflight (github_org)")
        argv = [self.swarm, "sc", "access", "repos", org, "--json", "--context", name]
        code, out, err = self.run(argv, None, self._env(ctx))
        if code != 0 and _NOT_CONNECTED.search(err):
            self.interactive([self.swarm, "sc", "setup", "--context", name], None, self._env(ctx))
            code, out, err = self.run(argv, None, self._env(ctx))
        found = next((str(row.get("repository") or "") for row in _rows(json.loads(out or "{}") if code == 0 else {},
                                                                          "repositories")
                      if not row.get("archived") and str(row.get("repository") or "").count("/") == 1), "")
        if not found:
            raise RuntimeError(f"the repositories the GitHub App installation covers in {org} could not be read "
                               f"on {name}: {err.strip()[:300] or 'none that is not archived'}")
        return found

    # -- the runs ---------------------------------------------------------------------

    def _call(self, ctx: Any, argv: Sequence[str], stdin: str | None = None, *,
              ok: Sequence[int] = (0,)) -> str:
        code, out, err = self.run([self.swarm, "--context", self._context(ctx), *argv], stdin, self._env(ctx))
        if code not in ok:
            raise RuntimeError(f"swarm {argv[0]} failed: {err.strip()[:300]}")
        return out

    def _json(self, ctx: Any, argv: Sequence[str], stdin: str | None = None, *,
              ok: Sequence[int] = (0,)) -> dict[str, Any]:
        found = json.loads(self._call(ctx, argv, stdin, ok=ok) or "{}")
        return found if isinstance(found, dict) else {}

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str:
        if surface == "console":
            raise ValueError("the console's runs are started in Admin › Setup and their ids read from the "
                             "install record (verify_runs); the engine never starts or asks for them")
        check = CHECK_RUNS.get(shape)
        if check is not None:
            task = self._json(ctx, ["dispatch", "--json", f"--profile={check.profile}", f"--repo={repository}",
                                    f"--label={label}", "-"], check.prompt_for(repository))
            run_id = str(task.get("id") or task.get("task_id") or "")
        elif shape == "single":
            task = self._json(ctx, ["dispatch", "--json", f"--repo={repository}", f"--label={label}", "-"],
                              SINGLE_PROMPT)
            run_id = str(task.get("id") or task.get("task_id") or "")
        else:
            envelope = self._json(ctx, ["workflow", "-", "--json", f"--repo={repository}", f"--label={label}"],
                                  workflow_spec(repository, label))
            run_id = str((envelope.get("workflow") or {}).get("workflow_id") or "")
        if not run_id:
            raise RuntimeError(f"swarm {'workflow' if shape == 'workflow' else 'dispatch'} returned no id")
        return run_id

    def index_commit(self, ctx: Any) -> str:
        """The commit the new deployment's code graph records for
        `verify_repository` (`swarm graph-index --json`: the graph-cache
        route's `index.commit_sha`), or "" when it records no index."""
        repository = str(ctx.outputs.get("verify_repository") or "")
        if not repository:
            return ""
        found = self._json(ctx, ["graph-index", repository, "--json"], ok=(0, 1))
        index = found.get("index")
        return str(index.get("commit_sha") or "") if isinstance(index, dict) else ""

    def status(self, ctx: Any, shape: str, run_id: str) -> RunStatus:
        if shape == "workflow":
            # Exit 1 with `state: null` is "not derived on this read": still going.
            report = self._json(ctx, ["workflow-status", run_id, "--result", "--json"], ok=(0, 1))
            rows = report.get("steps") or []
            prs = [str((row.get("produced") or {}).get("pull_request") or "") for row in rows]
            state = _state(report.get("state"))
            if state != "succeeded":
                return RunStatus(state)
            tasks = [str(row.get("task_id") or "") for row in rows]
            used = tuple(credential_use(task, self._json(ctx, ["result", task, "--json"])) if task
                         else CredentialUse(str(row.get("step_id") or "a step"), UNRECORDED)
                         for task, row in zip(tasks, rows))
            return RunStatus(state, next((p for p in reversed(prs) if p), ""), used)
        line = next((ln for ln in self._call(ctx, ["status", run_id]).splitlines() if ln.startswith(run_id)), "")
        words = line.split()
        state = _state(words[1] if len(words) > 1 else "")
        check = CHECK_RUNS.get(shape)
        profile = words[2] if check is not None and len(words) > 2 and words[2] != "masked" else ""
        if state != "succeeded":
            return RunStatus(state, profile=profile)
        summary = self._json(ctx, ["result", run_id, "--json"])
        used = (credential_use(run_id, summary),)
        if check is not None:
            artifact = None
            if check.artifact:
                code, out, _ = self.run([self.swarm, "--context", self._context(ctx), "artifact", run_id,
                                         check.artifact], None, self._env(ctx))
                artifact = out if code == 0 else None
            index_commit = self.index_commit(ctx) if check.graph_record else ""
            return RunStatus(state, "", used, profile, artifact, index_commit)
        pull = ((summary.get("git") or {}).get("pull_request") or {})
        return RunStatus(state, str(pull.get("url") or "") if isinstance(pull, dict) else "", used)


# -- the check runs' own proof ----------------------------------------------------


def job_stem(task_id: str) -> str:
    """The worker Job's name stem for a task, as scheduler dispatch makes it:
    `swarm-<id>-<generation>`, the id without `task_`, lower case, `-` for `_`."""
    return "swarm-" + task_id.removeprefix("task_").lower().replace("_", "-") + "-"


def sighting(job: Mapping[str, Any]) -> dict[str, Any]:
    """What a poll saw of a worker Job: its name, namespace, and whether it ran
    the live browser (pod label `swarm-live=enabled` and the `display`
    sidecar)."""
    meta = job.get("metadata") or {}
    template = (job.get("spec") or {}).get("template") or {}
    labels = (template.get("metadata") or {}).get("labels") or {}
    pod = template.get("spec") or {}
    sidecars = [c for c in (pod.get("initContainers") or []) + (pod.get("containers") or [])
                if isinstance(c, dict) and c.get("name") == "display"]
    return {"job": str(meta.get("name") or ""), "namespace": str(meta.get("namespace") or ""),
            "live": labels.get("swarm-live") == "enabled" and bool(sidecars)}


def tenant_namespace(tenant: str) -> str:
    """The team's namespace as the scheduler derives it: its
    `namespace_template` through `sanitize_name` (scheduler/dispatch.py),
    restated because the scheduler is another image."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", f"swarm-tenant-{tenant}".lower()).strip("-"))
    if len(slug) > 63:
        slug = slug[:54].rstrip("-") + "-" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return slug


def check_proof(key: str, run_id: str, check: CheckRun, found: RunStatus, seen: Mapping[str, Any] | None,
                repository: str, tenant: str) -> tuple[str, str]:
    """`(proof, "")` when a succeeded check run proved itself, else `("", problem)`."""
    named = f"{key} ({run_id})"
    if found.profile != check.profile:
        return "", f"{named} ran on {found.profile or 'no profile'}, not {check.profile}"
    proof: list[str] = []
    if check.on_cluster:
        if not seen:
            return "", f"{named}: its worker Job was never seen in the team's namespace {tenant_namespace(tenant)}"
        if check.live and not seen.get("live"):
            return "", (f"{named}: Job {seen.get('job')} did not run the live browser (no swarm-live=enabled "
                        "label and display sidecar)")
        proof.append(f"Job {seen.get('job')} in {seen.get('namespace')}"
                     + (" with the live display" if check.live else ""))
    if check.live:
        title = (found.artifact or "").strip()
        if found.artifact is None:
            return "", f"{named} left no {check.artifact}"
        if repository not in title:
            return "", f"{named}: the page it opened does not name {repository} ({title[:120]!r})"
        proof.append(f"page {title[:120]!r}")
    if check.artifact.endswith(".json"):
        if found.artifact is None:
            return "", f"{named} left no {check.artifact}"
        try:
            answer = json.loads(found.artifact)
        except ValueError:
            return "", f"{named}: {check.artifact} is not JSON"
        answer = answer if isinstance(answer, dict) else {}
        sha = str((answer.get("freshness") or {}).get("index_sha") or "") if isinstance(
            answer.get("freshness"), dict) else ""
        if not _COMMIT.fullmatch(sha):
            return "", (f"{named}: {repository} is not indexed (no 40-hex commit in freshness.index_sha "
                        f"of {check.artifact})")
        if check.graph_record and not found.index_commit:
            return "", f"{named}: the code graph records no index for {repository} (swarm graph-index)"
        if check.graph_record and sha != found.index_commit:
            return "", (f"{named}: freshness.index_sha {sha[:12]} is not the commit the code graph records "
                        f"for {repository} ({found.index_commit[:12]})")
        rows = answer.get("rows")
        if not isinstance(rows, list) or not rows:
            return "", f"{named}: the code graph answered no symbol for {repository}"
        proof.append(f"index {sha[:12]}, the graph's own record, {len(rows)} symbols")
    return "; ".join(proof) or "succeeded", ""


class EndToEndStep(_Base):
    id = "verify.end-to-end"
    kind = "wait"
    wait = RUNS_WAIT
    check_name = "end-to-end"
    satisfies = ("other.verify-end-to-end", "other.verify-row", "other.verify-gke", "other.verify-live-browser",
                 "other.verify-code-intelligence")
    _explain = (
        "Done means a team can use it: a single task and a workflow from the plugin and from the console, "
        "each to a pull request (D3), and three check runs: a task on GKE (its Job seen in the team's "
        "namespace), a live-browser task (claude-code-browser: the Job ran the display sidecar and the page "
        "it read names the repository) and a code-intelligence read (the swarm-graph index answered with "
        "its commit and a symbol). Every run goes to the NEW deployment through the engine's own swarm "
        "context named after the hostname; the person's current context is never switched. The plugin's "
        "runs sign in with the deployment's Desktop client, as a teammate's will (lesson 18). The console's "
        "two are started in Admin › Setup and read from the install record. Every run must clone and push "
        "with a GitHub App installation token, never a personal access token (lesson 43): the worker "
        "records each task's credential kind in its result summary (`forge_credential`), and the step "
        "fails naming any task whose kind is not app_installation."
    )

    def __init__(self, gcp: Callable[[], VerifyGcp], ask: Ask, *, runs: Runs | None = None,
                 kube: Callable[[Any], Kube] = kube_for) -> None:
        super().__init__(gcp, ask)
        self.runs: Runs = runs if runs is not None else SwarmCli(ask)
        self._kube = kube

    def _console_runs(self, ctx: Any, launched: dict[str, str]) -> dict[str, str]:
        """Copy the console's two run ids from the console document swarm-api
        writes them into, `console/verify_runs` (record.VERIFY_RUNS; INTERFACE
        SHEET), as each appears. An id that already failed here is never
        copied back: Admin › Setup starts a new one (#19)."""
        missing = [key for key in CONSOLE_KEYS.values() if not launched.get(key)]
        if not missing:
            return launched
        read = getattr(ctx, "read_console", None)
        cached = getattr(ctx.state, "console", None) or {}
        failed = ctx.outputs.get("verify_failed") or {}
        # Re-read on every poll, since no earlier step reads them: the
        # console's verify_runs document, the first workspace's (the first
        # team's, or for one person the personal workspace's; an older
        # swarm-api wrote the ids there), `finished`, and every document
        # already cached.
        tenant = str(ctx.outputs.get("verify_tenant") or "")
        one_person = (ctx.outputs.get("sizing") or {}).get("mode") == "individual"
        first = [PERSONAL] if one_person else ([f"teams/{tenant}"] if tenant else [])
        names = dict.fromkeys([VERIFY_RUNS, *first, "finished", *sorted(cached)])
        found_ids: dict[str, str] = {}
        for name in names:
            found = read(name) if callable(read) else cached.get(name)
            runs = found.get("verify_runs") if isinstance(found, dict) else None
            if not isinstance(runs, dict):
                continue
            for key in missing:
                run_id = str(runs.get(key) or "")
                if key not in found_ids and run_id and run_id not in (failed.get(key) or ()):
                    found_ids[key] = run_id
            if len(found_ids) == len(missing):
                break
        if found_ids:
            launched = {**launched, **found_ids}
            remember(ctx, outputs={"verify_runs": dict(launched)})
        return launched

    def _relaunch(self, ctx: Any, surface: str, shape: str, slot: str, run_id: str, state: str) -> tuple[str, str]:
        """A failed or cancelled run (#19): dropped from `verify_runs`, its id
        kept in `verify_failed`, and relaunched, at most `RELAUNCHES` times.
        `("waiting", why)` while relaunched, `("problem", why)` once it stops."""
        key = f"{surface}-{shape}"
        failed: dict[str, list[str]] = {k: list(v) for k, v in (ctx.outputs.get("verify_failed") or {}).items()}
        ids = failed.get(slot, [])
        if run_id not in ids:
            ids = [*ids, run_id]
        if len(ids) > RELAUNCHES:
            return "problem", (f"{key} failed {len(ids)} times ({', '.join(ids)}; the last {state}); not relaunched "
                               f"again: a run is relaunched at most {RELAUNCHES} times, so this is a fault to read "
                               f"in swarm status {run_id}, not a flaky run")
        attempt = f"({len(ids)} of {RELAUNCHES})"
        if ctx.dry_run:
            return "waiting", f"{key} ({run_id}) {state}; a run of swarm install relaunches it {attempt}"
        launched: dict[str, str] = dict(ctx.outputs.get("verify_runs") or {})
        launched.pop(slot, None)
        failed[slot] = ids
        seen = {k: v for k, v in (ctx.outputs.get("verify_cluster_jobs") or {}).items() if k != key}
        remember(ctx, outputs={"verify_runs": launched, "verify_failed": failed, "verify_cluster_jobs": seen})
        if surface == "console":
            return "waiting", f"{key} ({run_id}) {state}; Admin › Setup starts it again {attempt}"
        repository = str(ctx.outputs.get("verify_repository") or "")
        launched[slot] = self.runs.launch(ctx, surface, shape, repository, f"swarm-install-verify-{key}")
        remember(ctx, outputs={"verify_runs": dict(launched)})
        return "waiting", f"{key} ({run_id}) {state}; relaunched {attempt} as {launched[slot]}"

    def _look_for_jobs(self, ctx: Any, ids: Mapping[str, str]) -> dict[str, Any]:
        """Sightings of the on-cluster check runs' worker Jobs, kept in
        state.outputs: a finished Job is deleted after its TTL, so what a poll
        saw while it ran is what a later poll judges."""
        seen: dict[str, Any] = dict(ctx.outputs.get("verify_cluster_jobs") or {})
        wanted = {}
        for shape, check in CHECK_RUNS.items():
            key = f"plugin-{shape}"
            if check.on_cluster and ids.get(key) and not (seen.get(key) and (seen[key].get("live")
                                                                              or not check.live)):
                wanted[key] = ids[key]
        if not wanted:
            return seen
        namespace = tenant_namespace(str(ctx.outputs.get("verify_tenant") or ""))
        code, out, err = self._kube(ctx)("get", "jobs", f"--namespace={namespace}", "-o", "json")
        if code != 0:
            raise RuntimeError(f"kubectl get jobs in {namespace} failed: {err.strip()[:300]}")
        jobs = _rows(json.loads(out or "{}"), "items")
        for key, run_id in wanted.items():
            job = next((j for j in jobs if str((j.get("metadata") or {}).get("name") or "").startswith(
                job_stem(run_id))), None)
            if job is not None:
                seen[key] = sighting(job)
        if seen != dict(ctx.outputs.get("verify_cluster_jobs") or {}):
            remember(ctx, outputs={"verify_cluster_jobs": seen})
        return seen

    def _check(self, ctx: Any) -> Observation:
        launched: dict[str, str] = dict(ctx.outputs.get("verify_runs") or {})
        plugin = [f"{surface}-{shape}" for surface, shape in RUNS if surface == "plugin"]
        if not all(launched.get(key) for key in plugin):
            return Observation(UNMET, "the plugin's verification runs have not all been started")
        launched = self._console_runs(ctx, launched)
        ids = {f"{surface}-{shape}": launched.get(CONSOLE_KEYS[shape] if surface == "console"
                                                  else f"{surface}-{shape}", "")
               for surface, shape in RUNS}
        seen = self._look_for_jobs(ctx, ids)
        repository = str(ctx.outputs.get("verify_repository") or "")
        tenant = str(ctx.outputs.get("verify_tenant") or "")
        done, problems, prs, checks, waiting = 0, [], {}, {}, []
        for surface, shape in RUNS:
            key = f"{surface}-{shape}"
            run_id = ids[key]
            if not run_id:
                waiting.append(f"{key} (started in the console's Admin › Setup; its id is not recorded yet)")
                continue
            found = self.runs.status(ctx, shape, run_id)
            check = CHECK_RUNS.get(shape)
            if found.state in ("failed", "cancelled"):
                slot = CONSOLE_KEYS[shape] if surface == "console" else key
                verdict, why = self._relaunch(ctx, surface, shape, slot, run_id, found.state)
                (problems if verdict == "problem" else waiting).append(why)
            elif found.state != "succeeded":
                waiting.append(f"{key} ({run_id})")
            elif check is None and not found.pull_request:
                problems.append(f"{key} ({run_id}) finished without a pull request")
            else:
                wrong = [use for use in found.credentials if use.kind != APP_INSTALLATION]
                if wrong or not found.credentials:
                    said = "; ".join(use.words() for use in wrong) or "no task's credential was read"
                    problems.append(f"{key} ({run_id}) did not clone and push with a GitHub App "
                                    f"installation token (lesson 43): {said}")
                    continue
                if check is not None:
                    proof, problem = check_proof(key, run_id, check, found, seen.get(key), repository, tenant)
                    if problem:
                        problems.append(problem)
                        continue
                    checks[key] = proof
                else:
                    prs[key] = found.pull_request
                done += 1
        result = "failed" if problems else ("passed" if done == len(RUNS) else None)
        write_progress(ctx, self.check_name, len(QUICK_CHECKS) + done, result=result)
        if problems:
            # The engine's wait cannot end early on a check's word, so the
            # failure is reported on every poll and in the verify row
            # (`result: failed`) until the bound. A failed or cancelled run
            # was relaunched first, at most RELAUNCHES times (`_relaunch`).
            return Observation(UNMET, "; ".join(problems))
        if done < len(RUNS):
            return Observation(WAITING, f"{done} of {len(RUNS)} runs have passed; waiting for "
                                        + ", ".join(waiting))
        return Observation(MET, "four runs, each to a pull request, and the GKE, live-browser and "
                                "code-intelligence checks, each task cloning and pushing with a GitHub App "
                                "installation token (lesson 43)",
                           {"verify_pull_requests": prs, "verify_checks": checks,
                            "verify_forge_credential": {key: APP_INSTALLATION for key in (*prs, *checks)}})

    def plan(self, ctx: Any) -> Plan:
        return Plan("on the new deployment's own swarm context: a single task and a workflow from the plugin, "
                    "each to a PR, the GKE, live-browser and code-intelligence checks, and the console's two "
                    "runs from Admin › Setup",
                    tuple(f"{surface}: {shape}" for surface, shape in RUNS))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.runs.target(ctx)
        repository = str(ctx.outputs.get("verify_repository") or "")
        if not repository:
            repository = self.runs.repository(ctx)
            remember(ctx, outputs={"verify_repository": repository})
        launched: dict[str, str] = dict(ctx.outputs.get("verify_runs") or {})
        for surface, shape in RUNS:
            key = f"{surface}-{shape}"
            if surface == "plugin" and key not in launched:
                launched[key] = self.runs.launch(ctx, surface, shape, repository, f"swarm-install-verify-{key}")
                remember(ctx, outputs={"verify_runs": dict(launched)})
        write_progress(ctx, self.check_name, len(QUICK_CHECKS))


def build_steps(gcp: Callable[[], VerifyGcp] = VERIFY_GCP, ask: Ask = ASK) -> list[Any]:
    """Stage 14 in order: the cheap reads first, then the seven runs."""
    return [
        SchedulerTargetsStep(gcp, ask),
        DigestsStep(gcp, ask),
        ReconcilerJobsStep(gcp, ask),
        FirestoreMigrationsStep(gcp, ask),
        EndToEndStep(gcp, ask),
    ]


STEPS = build_steps()
