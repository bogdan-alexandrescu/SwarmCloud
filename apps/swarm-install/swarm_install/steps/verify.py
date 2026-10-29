"""Stage 14, verify: done is proven, never assumed (design §4, D3; plan §5.4,
§5.5 step 3; lessons 18, 28, 30, 38, 43).

| Step | Manifest entries | Check | Lesson |
|---|---|---|---|
| `verify.scheduler-targets` | `other.verify-scheduler-targets` | every enabled Scheduler job's target, called as the job calls it, answers 2xx | 38: Scheduler said success while its target answered 403 on every tick |
| `verify.digests` | `other.verify-digests` | every service serves 100 % on its latest ready revision, and that revision's **digest** is the release's | 28: labels lie; a pinned revision served 0 % |
| `verify.reconciler-jobs` | (lesson 30) | the reconciler may list Jobs in every tenant namespace, namespaced, by email and by uniqueId | 30: it listed Jobs at cluster scope and saw nothing |
| `verify.firestore-migrations` | `other.firestore-migrations` | the state and the install record are at this engine's schema | |
| `verify.end-to-end` | `other.verify-end-to-end`, `other.verify-row` | four runs, each to a pull request: a single task and a workflow from the plugin, and from the console | 18: the plugin signs in with the Desktop client; 43: every run's task cloned and pushed with an App installation token (below) |

**Lesson 43 is read from what each run recorded.** The worker writes the
KIND of GitHub credential an attempt cloned and pushed with into its
result summary (`forge_credential`: `kind`, and `source`, the secret's
NAME; never a value), and swarm-api serves that summary to anyone who may
read the task. The step reads it for every task a run is made of (a
workflow's every step) through `swarm result <task> --json`, and passes
only when each says `app_installation`; any other kind, or none recorded,
fails the step naming the run, the task, the kind and the secret.

**Only the platform's own** (`swarm-*`) Scheduler jobs and Cloud Run services
are verified: the project may hold others, and calling their targets would
trigger them.

**The console's verify row** (D26): every step writes the engine document's
`verify` field as it goes (`check`, `done`, `total`, `started`,
`expected_end`, `deadline`, `result`), so Admin › Setup draws "Verify, run by
swarm install" with a countdown, and "Send to your team" unlocks only when it
passes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..api_client import IamCredentials, IdTokenMinter
from ..exitcodes import MET, UNMET, WAITING
from ..record import RECORD_SCHEMA_VERSION, engine_document
from ..state import SCHEMA_VERSION
from ..waits import WaitSpec
from . import Observation, Plan
from .cluster import Kube, account_email, kube_for
from .preflight import ASK, Ask, Gcp, remember

SCHEDULER_API = "https://cloudscheduler.googleapis.com/v1"

#: The checks the verify row counts, in the order stage 14 runs them; the
#: end-to-end check is four runs.
QUICK_CHECKS = ("scheduler-targets", "digests", "reconciler-jobs", "firestore-migrations")
RUNS = (("plugin", "single"), ("plugin", "workflow"), ("console", "single"), ("console", "workflow"))
TOTAL = len(QUICK_CHECKS) + len(RUNS)

#: A tick's target may 403 for a while after its grant lands (lesson 19).
TARGETS_WAIT = WaitSpec(
    reason="every Scheduler target answering 2xx",
    expected="at once, or up to 10 min while a new grant propagates",
    expected_s=60,
    bound_s=10 * 60,
    look_at="the failing job's target service logs; swarm doctor --explain other.verify-scheduler-targets",
    interval_s=30,
)

#: Four runs, each to a pull request: an agent's run takes minutes.
RUNS_WAIT = WaitSpec(
    reason="four verification runs each opening a pull request",
    expected="15 to 25 min",
    expected_s=25 * 60,
    bound_s=60 * 60,
    look_at="swarm status <task id> for each run named above; the console's task pages",
    interval_s=30,
)

#: The tier-forcing variables the swarm CLI honours before the signed-in tier
#: (apps/swarm-mcp/swarm_mcp/auth.py `detect`), and gcloud's own token
#: overrides: none of them reaches a plugin run (lesson 18).
NOT_FOR_PLUGIN_RUNS = (
    "SWARM_ID_TOKEN",
    "SWARM_IMPERSONATE_SA",
    "SWARM_IAP_CLIENT_ID",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
)

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

#: Task states (swarm_common's vocabulary) as `RunStatus.state` names them;
#: anything else is still going.
TERMINAL_STATES = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELLED": "cancelled"}


class VerifyGcp(Gcp):
    """`preflight.Gcp` plus the reads stage 14 makes."""

    def scheduler_jobs(self, project: str, region: str) -> list[dict[str, Any]]:
        return self._pages(f"{SCHEDULER_API}/projects/{project}/locations/{region}/jobs", "jobs")

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


# -- calling a Scheduler target as Scheduler calls it ---------------------------

#: `call(method, url, headers, body) -> HTTP status`, unauthenticated but for
#: the bearer in `headers`.
TargetCall = Callable[[str, str, Mapping[str, str], bytes], int]


def requests_call(method: str, url: str, headers: Mapping[str, str], body: bytes) -> int:
    import requests

    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, headers=dict(headers), data=body or None, timeout=120).status_code


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
        "answered 403 on every tick for weeks (lesson 38). Each target is called as its job calls it, "
        "with an ID token for the job's own account and audience, and must answer 2xx."
    )

    def __init__(self, gcp: Callable[[], VerifyGcp], ask: Ask, *, minter: IdTokenMinter | None = None,
                 call: TargetCall = requests_call) -> None:
        super().__init__(gcp, ask)
        self._minter = minter if minter is not None else IamCredentials()
        self._call = call

    def jobs(self, ctx: Any) -> list[dict[str, Any]]:
        return self.gcp.scheduler_jobs(ctx.project, str(ctx.state.region))

    def _check(self, ctx: Any) -> Observation:
        failing: list[str] = []
        called: list[str] = []
        for job in self.jobs(ctx):
            name = str(job.get("name", "")).rsplit("/", 1)[-1]
            target = job.get("httpTarget")
            if not name.startswith(PLATFORM_PREFIX) or job.get("state") == "PAUSED" or not target:
                continue
            uri = str(target["uri"])
            headers = {k: v for k, v in (target.get("headers") or {}).items() if k.lower() != "authorization"}
            oidc = target.get("oidcToken")
            if oidc:
                token = self._minter.generate_id_token(str(oidc["serviceAccountEmail"]),
                                                       str(oidc.get("audience") or uri), include_email=True)
                headers["Authorization"] = f"Bearer {token}"
            body = base64.b64decode(target.get("body") or b"")
            status = self._call(str(target.get("httpMethod") or "POST"), uri, headers, body)
            called.append(name)
            if not 200 <= status < 300:
                failing.append(f"{name} -> HTTP {status}")
        if failing:
            return Observation(WAITING, "Scheduler targets that did not answer 2xx: " + "; ".join(failing))
        return Observation(MET, f"{len(called)} Scheduler targets answered 2xx", {"scheduler_targets": called})


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


# -- the four runs ----------------------------------------------------------------


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


class Runs(Protocol):
    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str: ...

    def status(self, ctx: Any, shape: str, run_id: str) -> RunStatus: ...


def plugin_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a plugin run gets: the person's own, less anything
    that would put the swarm CLI on a tier other than the Desktop-client
    sign-in (lesson 18: a gcloud token is refused at IAP)."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k not in NOT_FOR_PLUGIN_RUNS}


#: `run(argv, stdin, env) -> (exit code, stdout, stderr)`.
Run = Callable[[Sequence[str], str | None, dict[str, str] | None], tuple[int, str, str]]


def run_command(argv: Sequence[str], stdin: str | None = None,
                env: dict[str, str] | None = None) -> tuple[int, str, str]:
    done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=300,
                          check=False, env=env)
    return done.returncode, done.stdout or "", done.stderr or ""


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


@dataclass
class SwarmCli:
    """The four runs through the `swarm` CLI, as the person, signed in with the
    deployment's Desktop client. The console's two are started by the person
    in the console (the engine cannot press its buttons); the engine asks for
    the task id or the workflow id the console shows, then follows all four
    the same way.

    Read as the CLI prints them (apps/swarm-mcp/swarm_mcp/cli.py):
    `swarm dispatch --json` prints the task (`id`); `swarm workflow --json`
    prints the envelope (`workflow.workflow_id`); `swarm status <task>` prints
    `<id>  <STATE>  ...`; `swarm result <task> --json` prints only the task's
    `result_summary` (`git.pull_request.url`); and a workflow, which
    `/v1/tasks/<id>` does not serve, is followed by `swarm workflow-status
    <id> --result --json`: its derived `state` (null, exit 1, while the
    server did not derive) and each step's `produced.pull_request`.

    Once a run succeeded, each of its tasks' `swarm result <task> --json`
    gives the credential kind the worker recorded (`forge_credential`,
    lesson 43): the single task's own, and every workflow step's."""

    ask: Ask
    run: Run = run_command
    swarm: str = "swarm"

    def _call(self, argv: Sequence[str], stdin: str | None = None, *, ok: Sequence[int] = (0,)) -> str:
        code, out, err = self.run([self.swarm, *argv], stdin, plugin_env())
        if code not in ok:
            raise RuntimeError(f"swarm {argv[0]} failed: {err.strip()[:300]}")
        return out

    def _json(self, argv: Sequence[str], stdin: str | None = None, *, ok: Sequence[int] = (0,)) -> dict[str, Any]:
        found = json.loads(self._call(argv, stdin, ok=ok) or "{}")
        return found if isinstance(found, dict) else {}

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str:
        if surface == "console":
            what = "a single task (its task id)" if shape == "single" else "a workflow (its workflow id)"
            return self.ask(ctx, "verify.end-to-end",
                            f"In the console, start {what} on {repository} labelled {label}").strip()
        if shape == "single":
            task = self._json(["dispatch", "--json", f"--repo={repository}", f"--label={label}", "-"], SINGLE_PROMPT)
            run_id = str(task.get("id") or task.get("task_id") or "")
        else:
            envelope = self._json(["workflow", "-", "--json", f"--repo={repository}", f"--label={label}"],
                                  workflow_spec(repository, label))
            run_id = str((envelope.get("workflow") or {}).get("workflow_id") or "")
        if not run_id:
            raise RuntimeError(f"swarm {'dispatch' if shape == 'single' else 'workflow'} returned no id")
        return run_id

    def status(self, ctx: Any, shape: str, run_id: str) -> RunStatus:
        if shape == "workflow":
            # Exit 1 with `state: null` is "not derived on this read": still going.
            report = self._json(["workflow-status", run_id, "--result", "--json"], ok=(0, 1))
            rows = report.get("steps") or []
            prs = [str((row.get("produced") or {}).get("pull_request") or "") for row in rows]
            state = _state(report.get("state"))
            if state != "succeeded":
                return RunStatus(state)
            tasks = [str(row.get("task_id") or "") for row in rows]
            used = tuple(credential_use(task, self._json(["result", task, "--json"])) if task
                         else CredentialUse(str(row.get("step_id") or "a step"), UNRECORDED)
                         for task, row in zip(tasks, rows))
            return RunStatus(state, next((p for p in reversed(prs) if p), ""), used)
        line = next((ln for ln in self._call(["status", run_id]).splitlines() if ln.startswith(run_id)), "")
        words = line.split()
        state = _state(words[1] if len(words) > 1 else "")
        if state != "succeeded":
            return RunStatus(state)
        summary = self._json(["result", run_id, "--json"])
        pull = ((summary.get("git") or {}).get("pull_request") or {})
        return RunStatus(state, str(pull.get("url") or "") if isinstance(pull, dict) else "",
                         (credential_use(run_id, summary),))


class EndToEndStep(_Base):
    id = "verify.end-to-end"
    kind = "wait"
    wait = RUNS_WAIT
    check_name = "end-to-end"
    satisfies = ("other.verify-end-to-end", "other.verify-row")
    _explain = (
        "Done means a team can use it: a single task and a workflow from the plugin and from the console, "
        "each to a pull request (D3). The plugin's runs sign in with the deployment's Desktop client, as a "
        "teammate's will (lesson 18). Every run must clone and push with a GitHub App installation token, "
        "never a personal access token (lesson 43): the worker records each task's credential kind in its "
        "result summary (`forge_credential`), and the step fails naming any task whose kind is not "
        "app_installation."
    )

    def __init__(self, gcp: Callable[[], VerifyGcp], ask: Ask, *, runs: Runs | None = None) -> None:
        super().__init__(gcp, ask)
        self._runs = runs if runs is not None else SwarmCli(ask)

    def _check(self, ctx: Any) -> Observation:
        launched: dict[str, str] = dict(ctx.outputs.get("verify_runs") or {})
        if len(launched) < len(RUNS):
            return Observation(UNMET, "the four verification runs have not all been started")
        done, problems, prs = 0, [], {}
        for surface, shape in RUNS:
            key = f"{surface}-{shape}"
            found = self._runs.status(ctx, shape, launched[key])
            if found.state in ("failed", "cancelled"):
                problems.append(f"{key} ({launched[key]}) {found.state}")
            elif found.state == "succeeded" and not found.pull_request:
                problems.append(f"{key} ({launched[key]}) finished without a pull request")
            elif found.state == "succeeded":
                wrong = [use for use in found.credentials if use.kind != APP_INSTALLATION]
                if wrong or not found.credentials:
                    said = "; ".join(use.words() for use in wrong) or "no task's credential was read"
                    problems.append(f"{key} ({launched[key]}) did not clone and push with a GitHub App "
                                    f"installation token (lesson 43): {said}")
                    continue
                done += 1
                prs[key] = found.pull_request
        result = "failed" if problems else ("passed" if done == len(RUNS) else None)
        write_progress(ctx, self.check_name, len(QUICK_CHECKS) + done, result=result)
        if problems:
            # The engine's wait cannot end early on a check's word, so the
            # failure is reported on every poll and in the verify row
            # (`result: failed`) until the bound; nothing reruns by itself.
            return Observation(UNMET, "; ".join(problems))
        if done < len(RUNS):
            return Observation(WAITING, f"{done} of {len(RUNS)} runs have opened their pull request")
        return Observation(MET, "four runs, each to a pull request, each task cloning and pushing with a "
                                "GitHub App installation token (lesson 43)",
                           {"verify_pull_requests": prs,
                            "verify_forge_credential": {key: APP_INSTALLATION for key in prs}})

    def plan(self, ctx: Any) -> Plan:
        return Plan("run a single task and a workflow from the plugin, and from the console, each to a PR",
                    tuple(f"{surface}: {shape}" for surface, shape in RUNS))

    def apply(self, ctx: Any, plan: Plan) -> None:
        repository = str(ctx.outputs.get("verify_repository") or "")
        if not repository:
            repository = self._ask(ctx, self.id, "A repository the first team's GitHub App installation covers, "
                                                 "for the verification PRs (owner/name)").strip()
            if repository.count("/") != 1:
                raise ValueError(f"{repository!r} is not owner/name")
            remember(ctx, outputs={"verify_repository": repository})
        launched: dict[str, str] = dict(ctx.outputs.get("verify_runs") or {})
        for surface, shape in RUNS:
            key = f"{surface}-{shape}"
            if key not in launched:
                launched[key] = self._runs.launch(ctx, surface, shape, repository, f"swarm-install-verify-{key}")
                remember(ctx, outputs={"verify_runs": dict(launched)})
        write_progress(ctx, self.check_name, len(QUICK_CHECKS))


def build_steps(gcp: Callable[[], VerifyGcp] = VERIFY_GCP, ask: Ask = ASK) -> list[Any]:
    """Stage 14 in order: the cheap reads first, then the four runs."""
    return [
        SchedulerTargetsStep(gcp, ask),
        DigestsStep(gcp, ask),
        ReconcilerJobsStep(gcp, ask),
        FirestoreMigrationsStep(gcp, ask),
        EndToEndStep(gcp, ask),
    ]


STEPS = build_steps()
