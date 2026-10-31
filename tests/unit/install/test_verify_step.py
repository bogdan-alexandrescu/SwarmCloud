"""Stage 14, verify (DAYONE-ENGINE-4; design §4, D3, D26; lessons 18, 28,
30, 38, 43; manifest `other.verify-row`, `other.release-kubernetes-bundle`).

On fakes, with no credentials: every platform Scheduler job is judged from
the Scheduler API and its target's invoker binding, and no target is called; what a service serves is read from its
revision's digest, never a label; the reconciler may list Jobs per tenant
namespace; the console's verify row is written with its countdown; four runs
each reach a pull request, read from what the real `swarm` CLI prints, the
plugin's signed in with the Desktop client; lesson 43 is read from the
credential kind each run's tasks recorded, and fails on anything but an App
installation token; and the cluster's objects render from the release bundle.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import os
import secrets
import shlex
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_install.exitcodes import MET, UNMET, WAITING, ExitCode
from swarm_install import record
from swarm_install.record import InstallRecord, engine_document
from swarm_install import swarm_context
from swarm_install.steps import cluster, verify
from swarm_install.steps.preflight import RELEASE_HOME
from swarm_install.terraform import TerraformRefused

from .test_cluster_step import VERSION, ClusterFakeGcp, FakeShell, _kube, bundle_bytes, cosign  # noqa: F401
from .test_preflight import PROJECT, Answers, FakeGcp, Rig, ctx_for
from .test_record_writers import FakeDocuments

REGION = "us-central1"
RECONCILER = cluster.account_email(PROJECT, "swarm-reconciler")
RECONCILER_UID = "100000000000000000002"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


class VerifyFakeGcp(FakeGcp):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[dict[str, Any]] = []
        self.services: list[dict[str, Any]] = []
        self.revisions: dict[str, dict[str, Any]] = {}
        self.run_services_list: list[dict[str, Any]] = []
        self.policies: dict[str, dict[str, Any]] = {}

    def scheduler_jobs(self, project: str, region: str) -> list[dict[str, Any]]:
        self._call("scheduler_jobs", project, region)
        return list(self.jobs)

    def run_services(self, project: str, region: str) -> list[dict[str, Any]]:
        self._call("run_services", project, region)
        return list(self.run_services_list)

    def run_iam_policy(self, name: str) -> dict[str, Any]:
        self._call("run_iam_policy", name)
        return json.loads(json.dumps(self.policies.get(name) or {"etag": "e0"}))

    def knative_services(self, project: str, region: str) -> list[dict[str, Any]]:
        self._call("knative_services", project, region)
        return list(self.services)

    def knative_revision(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        self._call("knative_revision", project, region, name)
        return self.revisions.get(name)

    def service_account(self, email: str) -> dict[str, Any] | None:
        self._call("service_account", email)
        return {"email": email, "uniqueId": RECONCILER_UID}


SCHEDULER_URL = "https://swarm-scheduler-abc123-uc.a.run.app"
SCHEDULER_STABLE_URL = f"https://swarm-scheduler-123456789.{REGION}.run.app"
RECONCILER_URL = "https://swarm-reconciler-abc123-uc.a.run.app"
SCHEDULER_AUDIENCE = "https://swarm-scheduler.dev.swarm.internal"
DOCTOR = cluster.account_email(PROJECT, "swarm-doctor")
DOCTOR_JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-doctor"


def _run_service(name: str, url: str, *more: str) -> dict[str, Any]:
    """A Cloud Run v2 service as `services.list` answers it."""
    return {"name": f"projects/{PROJECT}/locations/{REGION}/services/{name}", "uri": url, "urls": [url, *more]}


def _invokers(*members: str, role: str = "roles/run.invoker") -> dict[str, Any]:
    return {"etag": "e1", "bindings": [{"role": role, "members": list(members)}]}


def _job(name: str, uri: str, *, account: str, audience: str | None = None, state: str = "ENABLED",
         headers: dict[str, str] | None = None, oauth: bool = False,
         last: dict[str, Any] | None = None) -> dict[str, Any]:
    token = ({"oauthToken": {"serviceAccountEmail": account,
                             "scope": "https://www.googleapis.com/auth/cloud-platform"}} if oauth else
             {"oidcToken": {"serviceAccountEmail": account, **({"audience": audience} if audience else {})}})
    job = {"name": f"projects/{PROJECT}/locations/{REGION}/jobs/{name}", "state": state,
           "httpTarget": {"uri": uri, "httpMethod": "POST", "headers": headers or {}, **token,
                          "body": base64.b64encode(b"{}").decode()}}
    if last is not None:
        job["lastAttemptTime"] = "2026-10-30T09:00:00Z"
        job["status"] = last
    return job


def _ctx(**outputs: Any) -> Any:
    ctx = ctx_for(outputs=outputs)
    ctx.state.release = VERSION
    return ctx


def _scheduler_rig() -> VerifyFakeGcp:
    """Three platform jobs as terraform/infra makes them: two OIDC ticks to
    Cloud Run services and the hourly doctor, an OAuth call to `jobs:run`."""
    gcp = VerifyFakeGcp()
    sweep = cluster.account_email(PROJECT, "swarm-scheduler")
    gcp.run_services_list = [{**_run_service("swarm-scheduler", SCHEDULER_URL, SCHEDULER_STABLE_URL),
                              "customAudiences": [SCHEDULER_AUDIENCE]},
                             _run_service("swarm-reconciler", RECONCILER_URL)]
    gcp.jobs = [
        # terraform/infra's push audience: a custom audience the service names.
        _job("swarm-quota-sweep", f"{SCHEDULER_STABLE_URL}/sweep", account=sweep, audience=SCHEDULER_AUDIENCE,
             headers={"Content-Type": "application/json"}),
        _job("swarm-reconcile", f"{RECONCILER_URL}/tick", account=RECONCILER, last={}),
        _job("swarm-doctor-hourly", f"https://run.googleapis.com/v2/{DOCTOR_JOB}:run", account=DOCTOR, oauth=True,
             last={"code": 0}),
        _job("swarm-paused", f"{SCHEDULER_URL}/x", account=sweep, state="PAUSED"),
        # Not the platform's: never judged.
        _job("nightly-export", "https://export.example/run", account=sweep),
    ]
    gcp.policies = {
        f"projects/{PROJECT}/locations/{REGION}/services/swarm-scheduler": _invokers(f"serviceAccount:{sweep}"),
        f"projects/{PROJECT}/locations/{REGION}/services/swarm-reconciler": {"etag": "e1"},
        DOCTOR_JOB: _invokers(f"serviceAccount:{DOCTOR}"),
    }
    # The reconciler's tick account holds run.invoker on the project: inherited, still a grant.
    gcp.iam = _invokers(f"serviceAccount:{RECONCILER}")
    return gcp


def test_scheduler_targets_judged_from_scheduler_and_invoker_bindings() -> None:
    """Lesson 38, without calling a target (#11): calling the doctor's
    `jobs:run` starts a doctor run, and minting an ID token as each job's own
    account needs grants the installer does not hold. Each enabled platform
    job is judged from the Cloud Scheduler API (state ENABLED; its last
    attempt's status once it has run) and from its target's invoker binding
    for the job's own account: an OAuth job to `jobs:run` on the Cloud Run
    job, an OIDC job on the service whose URL it calls, with an audience that
    service accepts."""
    gcp = _scheduler_rig()
    step = verify.SchedulerTargetsStep(lambda: gcp, Answers())
    observed = step.check(_ctx())
    assert observed.status == MET, observed.detail
    assert observed.outputs["scheduler_targets"] == ["swarm-quota-sweep", "swarm-reconcile", "swarm-doctor-hourly"]
    assert "swarm-paused" in observed.detail
    assert step.kind == "wait" and step.wait.bound_s >= 60
    # Read only: the Scheduler list, the services, the policies; nothing else.
    assert {name for name, _ in gcp.calls} <= {"scheduler_jobs", "run_services", "run_iam_policy", "iam_policy"}
    assert (DOCTOR_JOB,) in gcp.called("run_iam_policy")

    # The doctor's own account lost run.invoker on its job: named, never called.
    gcp.policies[DOCTOR_JOB] = _invokers(f"serviceAccount:{RECONCILER}")
    observed = step.check(_ctx())
    assert observed.status == WAITING
    assert f"swarm-doctor-hourly: {DOCTOR} may not invoke {DOCTOR_JOB}" in observed.detail
    gcp.policies[DOCTOR_JOB] = _invokers(f"serviceAccount:{DOCTOR}", role="roles/run.jobsExecutor")
    assert step.check(_ctx()).status == MET

    # The hourly doctor's tick refused before its grant propagated (lesson
    # 19): its binding is in place and its next tick is past this wait, so it
    # is named, not held against the step; a tick this wait will see is waited for.
    ctx = _ctx()
    refused = {"code": 7, "message": "PERMISSION_DENIED"}
    later = datetime.fromtimestamp(ctx.clock.now() + 3600, timezone.utc).isoformat()
    gcp.jobs[2] = {**_job("swarm-doctor-hourly", f"https://run.googleapis.com/v2/{DOCTOR_JOB}:run", account=DOCTOR,
                          oauth=True, last=refused), "scheduleTime": later}
    observed = step.check(ctx)
    assert observed.status == MET and "swarm-doctor-hourly: its last attempt" in observed.detail
    gcp.jobs[2]["scheduleTime"] = datetime.fromtimestamp(ctx.clock.now() + 60, timezone.utc).isoformat()
    assert step.check(ctx).status == WAITING
    gcp.jobs[2] = _job("swarm-doctor-hourly", f"https://run.googleapis.com/v2/{DOCTOR_JOB}:run", account=DOCTOR,
                       oauth=True)

    # A last attempt the target refused, a job Scheduler disabled, an audience
    # the service would refuse and a target that is no Cloud Run service here.
    gcp.jobs[1] = _job("swarm-reconcile", f"{RECONCILER_URL}/tick", account=RECONCILER,
                       last={"code": 7, "message": "PERMISSION_DENIED"})
    gcp.jobs[0]["state"] = "UPDATE_FAILED"
    gcp.jobs.append(_job("swarm-other", f"{SCHEDULER_URL}/y", account=RECONCILER,
                         audience="https://elsewhere.example"))
    gcp.jobs.append(_job("swarm-lost", "https://gone.example/tick", account=RECONCILER))
    observed = step.check(_ctx())
    assert observed.status == WAITING
    assert "swarm-quota-sweep: state UPDATE_FAILED, not ENABLED" in observed.detail
    assert "swarm-reconcile: its last attempt at 2026-10-30T09:00:00Z failed (code 7: PERMISSION_DENIED)" \
        in observed.detail
    assert "swarm-other: audience https://elsewhere.example is not swarm-scheduler's" in observed.detail
    assert "swarm-lost: https://gone.example/tick is not a Cloud Run service or job" in observed.detail
    assert "swarm-doctor-hourly" not in observed.detail

    # A service whose binding does not name the job's account.
    gcp.jobs = gcp.jobs[1:3]
    gcp.jobs[0] = _job("swarm-reconcile", f"{RECONCILER_URL}/tick", account=RECONCILER)
    gcp.iam = {"etag": "e1", "bindings": []}
    observed = step.check(_ctx())
    assert observed.status == WAITING
    assert (f"swarm-reconcile: {RECONCILER} may not invoke "
            f"projects/{PROJECT}/locations/{REGION}/services/swarm-reconciler") in observed.detail


def _service(name: str, latest: str, traffic: list[dict[str, Any]], label: str = DIGEST) -> dict[str, Any]:
    return {"metadata": {"name": name, "labels": {"swarm-image-tag": label}},
            "status": {"latestReadyRevisionName": latest, "traffic": traffic}}


def test_digests_read_never_labels() -> None:
    """Lesson 28: a label that names the release's digest proves nothing; the
    revision taking 100 % of traffic must have resolved that digest."""
    gcp = VerifyFakeGcp()
    gcp.services = [_service("swarm-api", "swarm-api-00002", [{"latestRevision": True, "percent": 100}])]
    gcp.revisions = {"swarm-api-00002": {"status": {"imageDigest": f"us-docker.pkg.dev/x/swarm-api@{OTHER_DIGEST}"}}}
    ctx = _ctx(image_digests={"swarm-api": f"us-docker.pkg.dev/x/swarm-api@{DIGEST}"})
    step = verify.DigestsStep(lambda: gcp, Answers())

    observed = step.check(ctx)
    assert observed.status == UNMET and OTHER_DIGEST in observed.detail
    assert gcp.called("knative_revision") == [(PROJECT, REGION, "swarm-api-00002")]

    gcp.services = [_service("swarm-api", "swarm-api-00003",
                             [{"revisionName": "swarm-api-00001", "percent": 100},
                              {"revisionName": "swarm-api-00003", "percent": 0}])]
    observed = step.check(ctx)
    assert observed.status == UNMET and "serves 0 %" in observed.detail

    gcp.services = [_service("swarm-api", "swarm-api-00002", [{"latestRevision": True, "percent": 100}],
                             label="anything"),
                    # Not the platform's: not held to the release's digests.
                    _service("someone-elses", "someone-elses-00001", [{"latestRevision": True, "percent": 100}])]
    gcp.revisions["swarm-api-00002"] = {"status": {"imageDigest": f"us-docker.pkg.dev/x/swarm-api@{DIGEST}"}}
    observed = step.check(ctx)
    assert observed.status == MET
    assert observed.outputs["serving_digests"] == {"swarm-api": DIGEST}

    with pytest.raises(LookupError, match="image digests"):
        step.check(_ctx())


def test_reconciler_can_list_jobs() -> None:
    """Lesson 30: the reconciler may list Jobs in every tenant namespace, as
    its email and as its uniqueId, asked namespace by namespace, never at
    cluster scope."""
    asked: list[tuple[str, ...]] = []
    allowed = {RECONCILER}

    def kube(*args: str, stdin: str | None = None) -> tuple[int, str, str]:
        asked.append(args)
        if args[:2] == ("get", "namespaces"):
            return 0, "namespace/kube-system\nnamespace/swarm-tenant-a\nnamespace/swarm-tenant-b\n", ""
        who = next(a for a in args if a.startswith("--as=")).removeprefix("--as=")
        return (0, "yes\n", "") if who in allowed else (1, "no\n", "")

    step = verify.ReconcilerJobsStep(lambda: VerifyFakeGcp(), Answers(), kube=lambda ctx: kube)
    observed = step.check(_ctx())
    assert observed.status == UNMET
    assert f"swarm-tenant-a as {RECONCILER_UID}" in observed.detail and RECONCILER not in observed.detail
    can_i = [a for a in asked if a[:2] == ("auth", "can-i")]
    assert len(can_i) == 4 and not any("--all-namespaces" in a or "-A" in a for a in can_i)
    assert {next(x for x in a if x.startswith("--namespace=")) for a in can_i} == {
        "--namespace=swarm-tenant-a", "--namespace=swarm-tenant-b"}
    assert all("jobs.batch" in a and "list" in a for a in can_i)

    allowed.add(RECONCILER_UID)
    assert step.check(_ctx()).status == MET


REPO = "acme/demo"
HOSTNAME = "swarm.acme.example"
TEAM = "eng"
#: The installer's own personal workspace, as `tenant_id_for_user` names it.
PERSONAL_TENANT = "u-ada-acme-example"
#: The code graph's indexed commit, made at runtime.
INDEX_SHA = secrets.token_hex(20)
REPO_ID = "repo_" + secrets.token_hex(8)
CONSOLE = {"console_task": "t-console-single", "console_workflow": "t-console-workflow"}
PLUGIN_KEYS = [f"plugin-{k}" for s, k in verify.RUNS if s == "plugin"]
CHECK_KEYS = [f"plugin-{k}" for k in verify.CHECK_SHAPES]


def check_artifact(shape: str) -> str:
    """What each check run's agent leaves, as the real tools answer."""
    if shape == "browser":
        return f"GitHub - {REPO}: the demo repository\n"
    if shape == "code-intelligence":
        return json.dumps({"freshness": {"index_sha": INDEX_SHA, "behind_by": 0},
                           "rows": [{"symbol": "src/main.py#main", "kind": "function"}]})
    return "README.md, src, tests\n"


class FakeRuns:
    """As on a new deployment: the code graph records no index of the
    repository until verify asks for one (`index`) and swarm-graph promotes
    it (`promote`), so a code-intelligence run started before reads none."""

    def __init__(self) -> None:
        self.launched: list[tuple[str, str, str, str]] = []
        self.states: dict[str, verify.RunStatus] = {}
        self.targeted = 0
        self.index_requests: list[str] = []
        self.indexed = ""

    def index(self, ctx: Any, repository: str) -> None:
        self.index_requests.append(repository)

    def promote(self) -> None:
        assert self.index_requests, "swarm-graph indexes only a repository someone asked to index"
        self.indexed = INDEX_SHA

    def index_commit(self, ctx: Any) -> str:
        return self.indexed

    def target(self, ctx: Any) -> None:
        self.targeted += 1
        ctx.outputs.update(verify_context=HOSTNAME, verify_tenant=TEAM)

    def repository(self, ctx: Any) -> str:
        return REPO

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str:
        self.launched.append((surface, shape, repository, label))
        run_id, again = f"t-{surface}-{shape}", 1
        while run_id in self.states:  # a relaunch is a new run with a new id
            again += 1
            run_id = f"t{again}-{surface}-{shape}"
        self.states[run_id] = verify.RunStatus("running")
        return run_id

    def status(self, ctx: Any, shape: str, run_id: str) -> verify.RunStatus:
        assert run_id.endswith(shape)
        return self.states.get(run_id, verify.RunStatus("running"))

    def finish(self, run_id: str, *, pr: str | None = None, kind: str = "app_installation",
               artifact: str | None = None, profile: str | None = None, index_commit: str | None = None) -> None:
        used = verify.credential_use(run_id, {"forge_credential": {"kind": kind, "source": TENANT_GIT}})
        shape = run_id.split("-", 2)[2]
        check = verify.CHECK_RUNS.get(shape)
        if check is not None:
            self.states[run_id] = verify.RunStatus(
                "succeeded", "", (used,), profile or check.profile,
                artifact if artifact is not None else check_artifact(shape),
                (self.indexed if index_commit is None else index_commit) if check.graph_record else "")
            return
        url = pr if pr is not None else f"https://github.com/{REPO}/pull/{len(run_id)}"
        self.states[run_id] = verify.RunStatus("succeeded", url, (used,))


def _worker_job(task_id: str, *, live: bool = False) -> dict[str, Any]:
    """A worker Job as scheduler dispatch makes it: `swarm-<id>-<generation>`;
    a live-browser one with its pod label and display sidecar."""
    stem = task_id.removeprefix("task_").lower().replace("_", "-")
    template: dict[str, Any] = {"metadata": {"labels": {"swarm-tenant": TEAM}},
                                "spec": {"containers": [{"name": "worker"}]}}
    if live:
        template["metadata"]["labels"]["swarm-live"] = "enabled"
        template["spec"]["initContainers"] = [{"name": "display", "restartPolicy": "Always"}]
    return {"metadata": {"name": f"swarm-{stem}-1", "namespace": f"swarm-tenant-{TEAM}"},
            "spec": {"template": template}}


class FakeKube:
    """`kubectl` on the install cluster: the tenant namespace's Jobs, as JSON."""

    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs = list(jobs if jobs is not None else
                         [_worker_job("t-plugin-gke"), _worker_job("t-plugin-browser", live=True)])
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, ctx: Any) -> Any:
        def kube(*args: str) -> tuple[int, str, str]:
            self.calls.append(args)
            assert args[:2] == ("get", "jobs") and f"--namespace=swarm-tenant-{TEAM}" in args
            return 0, json.dumps({"items": self.jobs}), ""
        return kube


def test_four_end_to_end_runs_each_to_a_pr() -> None:
    """D3: a single task and a workflow from the plugin and from the console,
    each to a pull request, and the three check runs; the step waits until all
    seven pass. The engine launches the plugin's five and never asks for
    anything: not the repository, not a console run's id."""
    runs = FakeRuns()
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs, kube=FakeKube())
    ctx = _ctx()
    ctx.dry_run = False
    ctx.engine = _Saves(ctx)
    assert step.check(ctx).status == UNMET  # nothing started yet
    step.apply(ctx, step.plan(ctx))
    # The code-intelligence run waits for the index verify asked for (audit 4c, B2).
    assert runs.index_requests == [REPO]
    assert [(s, k) for s, k, _, _ in runs.launched] == [("plugin", "single"), ("plugin", "workflow"),
                                                        ("plugin", "gke"), ("plugin", "browser")]
    observed = step.check(ctx)
    assert observed.status == WAITING and "code graph has indexed" in observed.detail
    runs.promote()
    assert step.check(ctx).status == WAITING
    assert runs.launched[-1][:2] == ("plugin", "code-intelligence")
    assert {repo for _, _, repo, _ in runs.launched} == {REPO}
    assert ctx.outputs["verify_repository"] == REPO and set(ctx.outputs["verify_runs"]) == set(PLUGIN_KEYS)

    step.apply(ctx, step.plan(ctx))  # a rerun launches nothing twice, and asks for no second index
    assert len(runs.launched) == 5 and runs.index_requests == [REPO]

    for key in PLUGIN_KEYS:
        runs.finish(f"t-{key}")
    observed = step.check(ctx)
    assert observed.status == WAITING and "5 of 7" in observed.detail
    assert "console-single" in observed.detail and "Admin › Setup" in observed.detail

    # Admin › Setup started the console's two; swarm-api recorded their ids.
    ctx.outputs["verify_runs"].update(CONSOLE)
    runs.finish("t-console-single")
    observed = step.check(ctx)
    assert observed.status == WAITING and "6 of 7" in observed.detail

    runs.finish("t-console-workflow", pr="")
    observed = step.check(ctx)
    assert observed.status == UNMET and "console-workflow" in observed.detail and "without a pull request" in observed.detail

    runs.finish("t-console-workflow")
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    assert set(observed.outputs["verify_pull_requests"]) == {"plugin-single", "plugin-workflow", "console-single",
                                                             "console-workflow"}
    assert set(observed.outputs["verify_checks"]) == set(CHECK_KEYS)
    assert "other.verify-row" in step.satisfies


TENANT_GIT = "swarm-tenant-acme-git"


def _all_finished(kinds: dict[str, str] | None = None, *, kube: FakeKube | None = None) -> tuple[Any, Any]:
    runs = FakeRuns()
    runs.index_requests, runs.indexed = [REPO], INDEX_SHA
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs, kube=kube or FakeKube())
    plugin = {key: f"t-{key}" for key in PLUGIN_KEYS}
    ctx = _ctx(verify_runs={**plugin, **CONSOLE}, verify_repository=REPO, verify_context=HOSTNAME,
               verify_tenant=TEAM)
    for surface, shape in verify.RUNS:
        key = f"{surface}-{shape}"
        runs.finish(f"t-{key}", kind=(kinds or {}).get(key, "app_installation"))
    step.runs = runs
    return step, ctx


def test_runs_use_installation_tokens() -> None:
    """Lesson 43: a run must clone and push with an App installation token,
    never a PAT. The worker records each task's credential kind in its result
    summary, and the step passes only when every run's says
    `app_installation`; nothing says the check was not made."""
    step, ctx = _all_finished()
    observed = step.check(ctx)
    assert observed.status == MET
    assert "lesson 43" in observed.detail and "not checked" not in observed.detail
    assert observed.outputs["verify_forge_credential"] == {
        f"{s}-{k}": "app_installation" for s, k in verify.RUNS}
    assert not hasattr(verify, "CREDENTIAL_UNRECORDED")


#: BGD, 2026-10-30 ("Person's token for plugin runs"): the plugin's runs are
#: the person's own submissions, so their own GitHub App user token
#: (`app_user`) is accepted there; the console's runs are service
#: submissions, minted an installation token, and must show one.
PLUGIN_RUN_KEYS = tuple(f"{s}-{k}" for s, k in verify.RUNS if s == "plugin")
CONSOLE_RUN_KEYS = tuple(f"{s}-{k}" for s, k in verify.RUNS if s == "console")


@pytest.mark.parametrize("key", PLUGIN_RUN_KEYS)
def test_a_plugin_run_on_the_persons_own_app_user_token_passes(key: str) -> None:
    step, ctx = _all_finished({key: "app_user"})
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    assert observed.outputs["verify_forge_credential"][key] == "app_user"


def test_every_plugin_run_on_the_persons_token_passes() -> None:
    step, ctx = _all_finished(dict.fromkeys(PLUGIN_RUN_KEYS, "app_user"))
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    assert observed.outputs["verify_forge_credential"] == {
        **dict.fromkeys(PLUGIN_RUN_KEYS, "app_user"),
        **dict.fromkeys(CONSOLE_RUN_KEYS, "app_installation")}


@pytest.mark.parametrize("key", CONSOLE_RUN_KEYS)
@pytest.mark.parametrize("kind", ["pat", "app_user", "none", "mixed", "unknown"])
def test_a_console_run_on_anything_but_an_installation_token_fails_naming_it(key: str, kind: str) -> None:
    step, ctx = _all_finished({key: kind})
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert f"{key} (t-{key})" in observed.detail
    assert f"used a {kind} credential from {TENANT_GIT}" in observed.detail
    assert "lesson 43" in observed.detail
    for other in (*PLUGIN_RUN_KEYS, *CONSOLE_RUN_KEYS):
        if other != key:
            assert other not in observed.detail


@pytest.mark.parametrize("key", PLUGIN_RUN_KEYS)
@pytest.mark.parametrize("kind", ["pat", "none", "mixed", "unknown"])
def test_a_plugin_run_on_any_other_credential_fails_naming_it(key: str, kind: str) -> None:
    step, ctx = _all_finished({key: kind})
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert f"{key} (t-{key})" in observed.detail
    assert f"used a {kind} credential from {TENANT_GIT}" in observed.detail


def test_a_check_run_that_used_a_personal_token_fails_too() -> None:
    step, ctx = _all_finished({"plugin-browser": "pat"})
    observed = step.check(ctx)
    assert observed.status == UNMET and "plugin-browser (t-plugin-browser)" in observed.detail
    assert "lesson 43" in observed.detail


def test_a_run_whose_task_recorded_no_credential_fails() -> None:
    step, ctx = _all_finished()
    step.runs.states["t-plugin-single"] = verify.RunStatus(
        "succeeded", f"https://github.com/{REPO}/pull/1",
        (verify.credential_use("t-plugin-single", {"git": {}}),))
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert "plugin-single" in observed.detail and "recorded no GitHub credential kind" in observed.detail


# -- the console's runs come from the record ------------------------------------


def _console_rig(tenant: str, docs: dict[str, dict[str, Any]], **outputs: Any) -> tuple[Any, Any, list[str]]:
    runs = FakeRuns()
    runs.index_requests, runs.indexed = [REPO], INDEX_SHA
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs, kube=FakeKube())
    ctx = _ctx(verify_runs={key: f"t-{key}" for key in PLUGIN_KEYS}, verify_repository=REPO,
               verify_context=HOSTNAME, verify_tenant=tenant, **outputs)
    ctx.state.console = {}
    read: list[str] = []

    def read_console(name: str) -> dict[str, Any] | None:
        read.append(name)
        found = docs.get(name)
        if found is not None:
            ctx.state.console[name] = found
        return found

    ctx.read_console = read_console
    for key in PLUGIN_KEYS:
        runs.finish(f"t-{key}")
    return (step, runs), ctx, read


def test_console_runs_read_from_the_record_never_asked() -> None:
    """INTERFACE SHEET: Admin › Setup starts the console's two runs and
    swarm-api writes `verify_runs: {console_task, console_workflow}` into the
    console document `console/verify_runs` (record.VERIFY_RUNS, #10); the
    engine copies it into state.outputs and follows those ids. Nothing is
    ever asked (the Answers here refuse any question). No step reads these
    documents before this one, so the step reads `verify_runs`, the first
    team's document and `finished` itself on every poll. A team's document
    is not under `console/` (audit 3, A2) and never held the ids, so it is
    not read."""
    docs: dict[str, dict[str, Any]] = {}
    (step, runs), ctx, read = _console_rig(TEAM, docs)
    observed = step.check(ctx)
    assert observed.status == WAITING and "console-single" in observed.detail
    assert read == [record.VERIFY_RUNS, "finished"]

    # swarm-api writes the ids into console/verify_runs, never the team's document.
    docs[record.VERIFY_RUNS] = {"verify_runs": dict(CONSOLE), "repository": REPO}
    runs.finish("t-console-single")
    runs.finish("t-console-workflow")
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    assert ctx.outputs["verify_runs"]["console_task"] == "t-console-single"
    assert ctx.outputs["verify_runs"]["console_workflow"] == "t-console-workflow"
    assert set(observed.outputs["verify_pull_requests"]) >= {"console-single", "console-workflow"}

    # The CLI never asks for a console run's id: it is not its to start.
    with pytest.raises(ValueError, match="Admin › Setup"):
        verify.SwarmCli(Answers()).launch(ctx, "console", "single", REPO, "v-3")


def test_console_runs_found_for_one_person() -> None:
    """Decision 7: a one-person install's first workspace is the personal
    one, `console/personal`, never `teams/<tenant>`; the console's ids still
    come from `console/verify_runs`."""
    personal = TEAM  # the personal workspace's tenant; FakeKube serves its namespace
    docs: dict[str, dict[str, Any]] = {record.PERSONAL: {"workspace": personal},
                                       record.VERIFY_RUNS: {"verify_runs": dict(CONSOLE)}}
    (step, runs), ctx, read = _console_rig(personal, docs, sizing={"mode": "individual", "members": 1})
    runs.finish("t-console-single")
    runs.finish("t-console-workflow")
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    assert read[:1] == [record.VERIFY_RUNS] and not any(name.startswith("teams/") for name in read)
    assert ctx.outputs["verify_runs"]["console_workflow"] == "t-console-workflow"

    # Ids only on the personal document (an older swarm-api) are still found.
    docs = {record.PERSONAL: {"workspace": personal, "verify_runs": dict(CONSOLE)}}
    (step, runs), ctx, read = _console_rig(personal, docs, sizing={"mode": "individual", "members": 1})
    observed = step.check(ctx)
    assert record.PERSONAL in read and ctx.outputs["verify_runs"]["console_task"] == "t-console-single"


# -- a failed run is relaunched, a bounded number of times (#19) -------------------


class _Saves:
    """`ctx.engine` for a check that is not a dry run: `save` changes the state in place."""

    record = None

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def save(self, change: Any) -> None:
        change(self.ctx.state)


def test_failed_run_is_relaunched_at_most_twice() -> None:
    """A failed or cancelled run is dropped from `verify_runs` and relaunched,
    at most twice; then the step says why it stopped. A failed console run's
    id is never copied back from the console document: Admin › Setup starts
    a new one."""
    (step, runs), ctx, _ = _console_rig(TEAM, {record.VERIFY_RUNS: {"verify_runs": dict(CONSOLE)}})
    ctx.dry_run = False
    ctx.engine = _Saves(ctx)
    runs.finish("t-console-single")
    runs.finish("t-console-workflow")
    runs.states["t-plugin-single"] = verify.RunStatus("failed")

    observed = step.check(ctx)
    assert observed.status == WAITING, observed.detail
    assert "plugin-single (t-plugin-single) failed; relaunched (1 of 2)" in observed.detail
    assert [(s, k) for s, k, _, _ in runs.launched] == [("plugin", "single")]
    relaunched = ctx.outputs["verify_runs"]["plugin-single"]
    assert ctx.outputs["verify_failed"]["plugin-single"] == ["t-plugin-single"]

    # A dry run looks and relaunches nothing.
    runs.states[relaunched] = verify.RunStatus("cancelled")
    ctx.dry_run = True
    step.check(ctx)
    assert len(runs.launched) == 1
    ctx.dry_run = False

    observed = step.check(ctx)
    assert "relaunched (2 of 2)" in observed.detail and len(runs.launched) == 2
    runs.states[ctx.outputs["verify_runs"]["plugin-single"]] = verify.RunStatus("failed")
    observed = step.check(ctx)
    assert observed.status == UNMET and len(runs.launched) == 2
    assert "plugin-single failed 3 times" in observed.detail and "not relaunched again" in observed.detail

    # A console run: dropped, and its failed id never copied back.
    runs.finish(ctx.outputs["verify_runs"]["plugin-single"])
    runs.states["t-console-workflow"] = verify.RunStatus("failed")
    observed = step.check(ctx)
    assert "console-workflow (t-console-workflow) failed; Admin › Setup starts it again (1 of 2)" \
        in observed.detail
    assert "console_workflow" not in ctx.outputs["verify_runs"]
    observed = step.check(ctx)
    assert "console_workflow" not in ctx.outputs["verify_runs"]
    assert "console-workflow (started in the console's Admin › Setup" in observed.detail


# -- the GKE, live-browser and code-intelligence checks ---------------------------


def test_check_runs_pick_their_profiles_from_the_frozen_catalogue() -> None:
    from swarm_common.profiles import RUNNER_PROFILES, Backend

    gke, browser, graph = (verify.CHECK_RUNS[s] for s in ("gke", "browser", "code-intelligence"))
    assert RUNNER_PROFILES[gke.profile].backend == Backend.GKE_AUTOPILOT and gke.on_cluster
    assert browser.profile == "claude-code-browser" and RUNNER_PROFILES[browser.profile].live_browser
    assert browser.live and browser.on_cluster
    assert graph.artifact == "code-intelligence.json" and "swarm-graph" in graph.prompt and graph.graph_record
    assert verify.TOTAL == len(verify.QUICK_CHECKS) + 7


def test_check_runs_pass_on_their_own_proof() -> None:
    step, ctx = _all_finished()
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    checks = observed.outputs["verify_checks"]
    assert "swarm-t-plugin-gke-1" in checks["plugin-gke"] and f"swarm-tenant-{TEAM}" in checks["plugin-gke"]
    assert "swarm-t-plugin-browser-1" in checks["plugin-browser"] and REPO in checks["plugin-browser"]
    assert INDEX_SHA[:12] in checks["plugin-code-intelligence"]


def test_the_namespace_is_the_schedulers_sanitised_one() -> None:
    """scheduler/dispatch.py: sanitize_name(namespace_template.format(...))."""
    assert verify.tenant_namespace(TEAM) == f"swarm-tenant-{TEAM}"
    assert verify.tenant_namespace("Eng_Team") == "swarm-tenant-eng-team"
    long = verify.tenant_namespace("x" * 80)
    assert len(long) == 63 and long.startswith("swarm-tenant-xxx")


def test_a_finished_jobs_sighting_is_kept_past_its_ttl() -> None:
    """A finished Job is deleted 600 s later: what a poll saw while it ran is
    kept, so a later poll that finds it gone still passes."""
    kube = FakeKube()
    step, ctx = _all_finished(kube=kube)
    for key in ("t-plugin-gke", "t-plugin-browser"):
        step.runs.states[key] = verify.RunStatus("running")
    assert step.check(ctx).status == WAITING
    assert set(ctx.outputs["verify_cluster_jobs"]) == {"plugin-gke", "plugin-browser"}
    kube.jobs = []
    step.runs.finish("t-plugin-gke")
    step.runs.finish("t-plugin-browser")
    calls = len(kube.calls)
    assert step.check(ctx).status == MET
    assert len(kube.calls) == calls  # nothing left to look for


@pytest.mark.parametrize(("key", "breaks", "said"), [
    ("plugin-gke", "no job", "never seen in the team's namespace"),
    ("plugin-browser", "not live", "did not run the live browser"),
    ("plugin-browser", "other page", f"does not name {REPO}"),
    ("plugin-code-intelligence", "not indexed", "not indexed"),
    ("plugin-code-intelligence", "made-up commit", "no 40-hex commit"),
    ("plugin-code-intelligence", "no symbol", "answered no symbol"),
    ("plugin-code-intelligence", "no artifact", "left no code-intelligence.json"),
    ("plugin-code-intelligence", "graph disagrees", "is not the commit the code graph records"),
    ("plugin-code-intelligence", "graph records none", "records no index"),
    ("plugin-gke", "other profile", "not claude-code"),
])
def test_a_check_run_without_its_proof_fails_naming_it(key: str, breaks: str, said: str) -> None:
    kube = FakeKube()
    step, ctx = _all_finished(kube=kube)
    run_id = f"t-{key}"
    if breaks == "no job":
        kube.jobs = [j for j in kube.jobs if "gke" not in j["metadata"]["name"]]
    elif breaks == "not live":
        kube.jobs = [_worker_job("t-plugin-gke"), _worker_job("t-plugin-browser")]
    elif breaks == "other page":
        step.runs.finish(run_id, artifact="Example Domain\n")
    elif breaks == "not indexed":
        step.runs.finish(run_id, artifact=json.dumps({"freshness": {}, "rows": [{"symbol": "x"}]}))
    elif breaks == "made-up commit":
        step.runs.finish(run_id, artifact=json.dumps({"freshness": {"index_sha": "main"},
                                                      "rows": [{"symbol": "x"}]}))
    elif breaks == "no symbol":
        step.runs.finish(run_id, artifact=json.dumps({"freshness": {"index_sha": INDEX_SHA}, "rows": []}))
    elif breaks == "no artifact":
        step.runs.states[run_id] = verify.RunStatus(
            "succeeded", "", step.runs.states[run_id].credentials, "claude-code", None)
    elif breaks == "graph disagrees":
        step.runs.finish(run_id, index_commit=secrets.token_hex(20))
    elif breaks == "graph records none":
        step.runs.finish(run_id, index_commit="")
    elif breaks == "other profile":
        step.runs.finish(run_id, profile="claude-code-large")
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert f"{key} ({run_id})" in observed.detail and said in observed.detail
    assert "verify_checks" not in observed.outputs


def test_only_a_known_kind_and_a_slot_name_are_repeated_from_a_summary() -> None:
    odd = secrets.token_hex(12)
    used = verify.credential_use("t-1", {"forge_credential": {"kind": odd, "source": odd}})
    assert used == verify.CredentialUse("t-1", verify.UNRECOGNISED, "")
    assert odd not in used.words()


# -- the swarm CLI as it really prints ------------------------------------------


@pytest.fixture(autouse=True)
def swarm_off_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Audit 4b, B4: install.sh put swarm in ~/.local/bin, which is not on the
    PATH of the terminal it ran in, so the person typed `~/.local/bin/swarm
    install`. Every fake below spawns as a subprocess would (`spawn`). The
    tool's interpreter is a real one, as in uv's tool bin: stage 10 renders
    the release bundle with `sys.executable`."""
    path = tmp_path / "home" / ".local" / "bin" / "swarm"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    interpreter = tmp_path / "tools" / "swarmcloud" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    interpreter.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(path), "install"])
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    return str(path)


def spawn(argv0: str) -> None:
    """What `subprocess` does with argv[0]: a bare name is looked up on PATH
    and a path must be an executable file, else FileNotFoundError. A fake that
    took a bare "swarm" hid B4."""
    found = argv0 if os.sep in argv0 else shutil.which(argv0)
    if not found or not (os.path.isfile(found) and os.access(found, os.X_OK)):
        raise FileNotFoundError(2, "No such file or directory", argv0)


class FakeSwarmApi:
    """What the real `swarm` CLI's commands read through their client: tasks by
    id, the workflow route and the dispatch call; nothing else is answered."""

    tenant = None

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.workflows: dict[str, dict[str, Any]] = {}
        self.posted: list[dict[str, Any]] = []
        self.dispatched: list[dict[str, Any]] = []
        self.artifacts: dict[tuple[str, str], str] = {}
        #: As on a new deployment (audit 4c, B2): nothing is registered and the
        #: code graph records no index until `swarm repo-index` registers the
        #: repository, asks for its index run, and swarm-graph promotes it.
        self.registered: dict[str, str] = {}
        self.index_runs: list[str] = []
        #: The graph-cache route's `index`, by repo_id; absent is "no index".
        self.indexes: dict[str, dict[str, Any] | None] = {}

    def promote(self) -> None:
        """swarm-graph's promotion of the index run that was asked for."""
        assert self.index_runs, "an index is promoted only after an index run was asked for"
        self.indexes[self.index_runs[-1]] = {"commit_sha": INDEX_SHA, "manifest_digest": "sha256:" + "0" * 64}

    def registration(self, owner: str, repo: str) -> dict[str, Any] | None:
        repo_id = self.registered.get(f"{owner}/{repo}")
        return {"repo_id": repo_id, "owner": owner, "repo": repo} if repo_id else None

    def task(self, task_id: str) -> dict[str, Any]:
        return self.tasks[task_id]

    def dispatch(self, **kw: Any) -> dict[str, Any]:
        self.dispatched.append(kw)
        task_id = "t-single" if len(self.dispatched) == 1 else f"t-{len(self.dispatched)}"
        self.tasks[task_id] = {"id": task_id, "state": "QUEUED", "runner_profile": kw["runner_profile"]}
        return self.tasks[task_id]

    def artifact_content(self, task_id: str, name: str, **kw: Any) -> dict[str, Any]:
        content = self.artifacts.get((task_id, name))
        if content is None:
            return {"status": "missing", "detail": f"{name} is not in {task_id}'s manifest"}
        return {"status": "ok", "content": content, "truncated": False, "next_offset": None}

    def request(self, method: str, path: str, payload: Any = None, **kw: Any) -> Any:
        if method == "POST" and path == "/v1/workflows":
            self.posted.append(payload)
            steps = [{"step_id": st["step_id"], "task_id": f"t-{st['step_id']}",
                      "depends_on": st.get("depends_on") or []} for st in payload["steps"]]
            self.workflows["wf_1"] = {"workflow": {"workflow_id": "wf_1", "steps": steps,
                                                   "state": "QUEUED", "state_source": "stored"}, "tasks": []}
            return self.workflows["wf_1"]
        if method == "GET" and path.startswith("/v1/workflows/"):
            return self.workflows[path.rsplit("/", 1)[-1]]
        if method == "POST" and path == "/v1/repositories":
            # The route registers what the caller's GitHub connection reaches.
            assert payload == {"repository": REPO}, payload
            created = REPO not in self.registered
            self.registered.setdefault(REPO, REPO_ID)
            return {"repository": {"repo_id": REPO_ID, "repository": REPO}, "created": created}
        repo_id = path.split("/")[3] if path.startswith("/v1/repositories/") else ""
        if repo_id and repo_id not in self.registered.values():
            raise AssertionError(f"{method} {path}: the API answers 404 for a repository not registered")
        if method == "POST" and path == f"/v1/repositories/{repo_id}/index":
            coalesced = bool(self.index_runs) and repo_id not in self.indexes
            self.index_runs.append(repo_id)
            return {"run": None if coalesced else {"task_id": "task_index", "state": "QUEUED"},
                    "coalesced": coalesced, "skipped": None, "repo_id": repo_id}
        if method == "GET" and path.endswith("/graph-cache"):
            return {"repo_id": repo_id, "index": self.indexes.get(repo_id), "facts": None, "replicas": []}
        raise AssertionError(f"the CLI made a call this test does not expect: {method} {path}")


def real_swarm(api: FakeSwarmApi, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A `run` that executes the real `swarm` command (swarm_mcp.cli's parser
    and handlers) against `api`, and answers its exit code and its stdout."""
    from swarm_mcp import cli as swarm_cli
    from swarm_mcp.client import SwarmError

    def run(argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        spawn(argv[0])
        assert os.path.basename(argv[0]) == "swarm"
        args = swarm_cli.build_parser().parse_args(list(argv[1:]))
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin or ""))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = args.func(api, args)
            except SwarmError as exc:
                return 1, out.getvalue(), str(exc)
        return code, out.getvalue(), err.getvalue()

    return run


def test_plugin_runs_launch_and_finish_as_the_real_cli_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review findings 1-3: the workflow spec passes the CLI's own
    `read_spec`; the workflow id is read from the envelope `swarm workflow
    --json` prints; a task's state is read from `swarm status` and its pull
    request from `swarm result --json` (only `result_summary`); a workflow is
    followed through `swarm workflow-status`, never `/v1/tasks/<wf id>`."""
    from swarm_mcp import workflows as swarm_workflows

    spec = json.loads(verify.workflow_spec("acme/demo", "v-1"))
    assert [st["step_id"] for st in swarm_workflows.read_spec(spec, where="verify")["steps"]] == ["write", "open"]

    api = FakeSwarmApi()
    cli = verify.SwarmCli(Answers(), run=real_swarm(api, monkeypatch))
    ctx = _ctx(verify_context=HOSTNAME, verify_tenant=TEAM)
    assert cli.launch(ctx, "plugin", "single", "acme/demo", "v-1") == "t-single"
    assert api.dispatched[0]["metadata"] == {"unit": "v-1"}
    assert cli.launch(ctx, "plugin", "workflow", "acme/demo", "v-2") == "wf_1"
    assert [st["step_id"] for st in api.posted[0]["steps"]] == ["write", "open"]

    # Still going: a queued task, and a workflow the server did not derive (exit 1, state null).
    assert cli.status(ctx, "single", "t-single") == verify.RunStatus("running")
    assert cli.status(ctx, "workflow", "wf_1") == verify.RunStatus("running")

    pr = "https://github.com/acme/demo/pull/7"
    installation = {"kind": "app_installation", "source": TENANT_GIT}
    api.tasks["t-single"].update(state="SUCCEEDED",
                                 result_summary={"git": {"pull_request": {"url": pr, "number": 7}},
                                                 "forge_credential": installation})
    assert cli.status(ctx, "single", "t-single") == verify.RunStatus(
        "succeeded", pr, (verify.CredentialUse("t-single", "app_installation", TENANT_GIT),))

    wf_pr = "https://github.com/acme/demo/pull/8"
    api.workflows["wf_1"]["workflow"].update(state="SUCCEEDED", state_source="derived")
    pat = {"kind": "pat", "source": TENANT_GIT}
    api.workflows["wf_1"]["tasks"] = [
        {"id": "t-write", "state": "SUCCEEDED", "result_summary": {"git": {}, "forge_credential": pat}},
        {"id": "t-open", "state": "SUCCEEDED",
         "result_summary": {"git": {"pull_request": {"url": wf_pr, "number": 8}},
                            "forge_credential": installation}},
    ]
    # Each step's own task, read through `swarm result <task> --json`.
    for row in api.workflows["wf_1"]["tasks"]:
        api.tasks[row["id"]] = row
    assert cli.status(ctx, "workflow", "wf_1") == verify.RunStatus("succeeded", wf_pr, (
        verify.CredentialUse("t-write", "pat", TENANT_GIT),
        verify.CredentialUse("t-open", "app_installation", TENANT_GIT),
    ))

    api.tasks["t-failed"] = {"id": "t-failed", "state": "FAILED"}
    assert cli.status(ctx, "single", "t-failed") == verify.RunStatus("failed")


def test_plugin_run_signs_in_with_desktop_client_not_gcloud_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lesson 18: IAP refuses a gcloud user token. The plugin's runs go through
    the swarm CLI with every tier-forcing variable removed, so it signs in with
    the deployment's Desktop client; the engine never mints or passes a token."""
    for name in swarm_context.NOT_FOR_PLUGIN_RUNS:
        monkeypatch.setenv(name, secrets.token_hex(8))
    monkeypatch.setenv("HOME", "/home/person")
    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
    swarm = real_swarm(FakeSwarmApi(), monkeypatch)

    def run(argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        calls.append((list(argv), stdin, env))
        return swarm(argv, stdin, env)

    cli = verify.SwarmCli(Answers(), run=run)
    ctx = _ctx(verify_context=HOSTNAME, verify_tenant=TEAM)
    assert cli.launch(ctx, "plugin", "single", REPO, "v-1") == "t-single"
    assert cli.launch(ctx, "plugin", "workflow", REPO, "v-2") == "wf_1"
    assert len(calls) == 2
    for argv, _, env in calls:
        assert argv[0] == sys.argv[0]
        assert env is not None and env["HOME"] == "/home/person"
        assert not set(swarm_context.NOT_FOR_PLUGIN_RUNS) & set(env)
        for word in ("gcloud", "print-identity-token", "print-access-token", "--token", "--id-token"):
            assert not any(word in a for a in argv)
    assert "SWARM_ID_TOKEN" in swarm_context.NOT_FOR_PLUGIN_RUNS and "SWARM_IMPERSONATE_SA" in swarm_context.NOT_FOR_PLUGIN_RUNS


# -- the new deployment, never the person's current context -------------------------


CLIENT_ID = "123456789012-verifyengine.apps.googleusercontent.com"
LIVE = {"url": "https://swarm.live.example", "oauth_client_id": "999-live.apps.googleusercontent.com",
        "signed_in": True}


class FakeSwarm:
    """The `swarm` CLI's context file and credential store, as `swarm
    context`, `swarm login`, `swarm tenants` and `swarm sc access repos`
    change and print them; every call is kept."""

    def __init__(self, contexts: dict[str, dict[str, Any]], current: str | None) -> None:
        self.contexts = {k: dict(v) for k, v in contexts.items()}
        self.current = current
        self.calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
        self.secrets: dict[str, str] = {}
        self.repos: list[dict[str, Any]] | None = [{"repository": "acme/old", "archived": True},
                                                   {"repository": REPO, "archived": False}]
        self.connect_on_setup: list[dict[str, Any]] | None = None
        self.repos_error = ""
        #: What `GET /v1/tenants/mine` lists: the installer's own READY
        #: personal workspace first and current (owner decision 2026-10-12,
        #: "personal by default"), then the team groups.
        self.tenants = [PERSONAL_TENANT, TEAM]

    def __call__(self, argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        spawn(argv[0])
        assert argv[0] == sys.argv[0], "the swarm CLI running the engine, by its path"
        self.calls.append((["swarm", *argv[1:]], stdin, env))
        args = list(argv[1:])
        if args[:2] == ["context", "list"]:
            rows = [{"name": n, **c, "current": n == self.current} for n, c in sorted(self.contexts.items())]
            return 0, json.dumps({"path": "/home/person/.config/swarm/config.json", "contexts": rows}), ""
        if args[:2] == ["context", "add"]:
            assert "--use" not in args
            name = args[2]
            url = next(a.split("=", 1)[1] for a in args if a.startswith("--url="))
            client = next(a.split("=", 1)[1] for a in args if a.startswith("--client-id="))
            assert "--client-secret-stdin" in args and stdin
            self.secrets[name] = stdin.strip()
            self.contexts[name] = {"url": url, "oauth_client_id": client, "signed_in": False}
            if self.current is None:
                self.current = name
            return 0, f"added context {name} -> {url}\n", ""
        if args[0] == "sc":
            where = args[args.index("--context") + 1]
            assert where in self.contexts
            if args[1:3] == ["access", "repos"]:
                if self.repos_error:
                    return 1, "", self.repos_error
                if self.repos is None:
                    return 1, "", "sc: GitHub: not connected as you"
                return 0, json.dumps({"owner": args[3], "page": 1, "repositories": self.repos}), ""
            if args[1] == "setup":
                self.repos = self.connect_on_setup
                return 0, "", ""
        assert args[0] == "--context", args
        name, rest = args[1], args[2:]
        if rest == ["login"]:
            self.contexts[name]["signed_in"] = True
            return 0, "", ""
        if rest == ["tenants", "--json"]:
            current = self.tenants[0] if self.tenants else None
            rows = [{"tenant_id": t, "display_name": t, "current": t == current, "is_default": t == current,
                     "via": "personal" if t.startswith("u-") else "member"} for t in self.tenants]
            return 0, json.dumps({"tenants": rows, "current": current, "chosen": None,
                                  "default": current, "default_source": "personal"}), ""
        if rest[0] == "dispatch":
            return 0, json.dumps({"id": "task_0123456789abcdef0123"}), ""
        raise AssertionError(f"an unexpected swarm call: {argv}")


def _with_record(ctx: Any, *, teams: dict[str, dict[str, Any]] | None = None,
                 personal: dict[str, Any] | None = None) -> FakeDocuments:
    """The install record Admin › Setup left: `teams/<id>` per team (swarm-api
    writes `workspace` as the id, the team workspace's tenant) and, for one
    person, `console/personal`."""
    docs = FakeDocuments()
    for team_id, doc in (teams or {}).items():
        docs.create(f"{engine_document(ctx.environment)}/teams/{team_id}", doc)
    if personal is not None:
        docs.create(record.console_document(ctx.environment, record.PERSONAL), personal)
    ctx.engine = SimpleNamespace(record=InstallRecord(ctx.environment, docs))
    return docs


def _team_doc(tenant: str, created_at: str = "2026-10-30T20:00:00+00:00") -> dict[str, Any]:
    return {"group": f"{tenant}@acme.example", "workspace": tenant, "first_admin": None, "invites": [],
            "progress": {"created_at": created_at}}


def _target_ctx(**outputs: Any) -> Any:
    ctx = _ctx(desktop_client_id=CLIENT_ID, github_org="acme", **outputs)
    ctx.state.hostname = HOSTNAME
    _with_record(ctx, teams={TEAM: _team_doc(TEAM)})
    return ctx


def _signed_in(swarm: "FakeSwarm") -> None:
    swarm.contexts[HOSTNAME] = {"url": f"https://{HOSTNAME}", "oauth_client_id": CLIENT_ID, "signed_in": True}


def test_a_team_runs_in_the_first_team_never_the_personal_default() -> None:
    """Audit 5b, M3: `swarm tenants` lists the installer's own READY personal
    workspace first, and it is the current one (or a stored profile default
    is). The plugin's runs must land in the first team's workspace, where
    swarm-api sends the console's runs, the pool account and the repository
    grant (`first_workspace`), so the tenant is the install record's first
    team, never `current`."""
    swarm = FakeSwarm({}, None)
    _signed_in(swarm)
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm)
    ctx = _target_ctx(verify_context=HOSTNAME)
    cli.target(ctx)
    assert ctx.outputs["verify_tenant"] == TEAM

    # A profile default naming another listed team: still the first team.
    swarm.tenants = ["design", PERSONAL_TENANT, TEAM]
    cli.target(ctx)
    assert ctx.outputs["verify_tenant"] == TEAM

    # Two teams: the oldest, as swarm-api's InstallRecord.teams orders them,
    # even when the group stage 8 recorded is the later one.
    ctx = _target_ctx(verify_context=HOSTNAME, workspace_groups={"team": "late@acme.example"})
    _with_record(ctx, teams={"late": _team_doc("late", "2026-10-30T22:00:00+00:00"),
                             TEAM: _team_doc(TEAM, "2026-10-30T21:00:00+00:00")})
    swarm.tenants = [PERSONAL_TENANT, "late", TEAM]
    cli.target(ctx)
    assert ctx.outputs["verify_tenant"] == TEAM

    # The team stage 8 recorded, found by its group even before it is listed:
    # verify waits for it rather than running anywhere else.
    ctx = _target_ctx(verify_context=HOSTNAME, workspace_groups={"team": f"{TEAM}@acme.example"})
    swarm.tenants = [PERSONAL_TENANT]
    with pytest.raises(LookupError, match=f"does not list the first workspace, {TEAM}"):
        cli.target(ctx)
    assert "verify_tenant" not in ctx.outputs

    # No team filed yet: verify says so and never falls back to the personal one.
    ctx = _target_ctx(verify_context=HOSTNAME)
    _with_record(ctx)
    swarm.tenants = [PERSONAL_TENANT, TEAM]
    with pytest.raises(LookupError, match="has not filed the first team"):
        cli.target(ctx)
    assert "verify_tenant" not in ctx.outputs


def test_one_person_runs_in_the_personal_workspace_admin_setup_filed() -> None:
    """Decision 7: for one person the first workspace is the installer's
    personal one, read from `console/personal`, never guessed."""
    swarm = FakeSwarm({}, None)
    _signed_in(swarm)
    swarm.tenants = [PERSONAL_TENANT]
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm)
    ctx = _target_ctx(verify_context=HOSTNAME, sizing={"mode": "individual"})
    _with_record(ctx)
    with pytest.raises(LookupError, match="personal workspace"):
        cli.target(ctx)
    _with_record(ctx, personal={"owner": "ada@acme.example", "workspace": PERSONAL_TENANT})
    cli.target(ctx)
    assert ctx.outputs["verify_tenant"] == PERSONAL_TENANT


def _stored(swarm: "FakeSwarm", ctx: Any) -> tuple[str, str]:
    """What edge.desktop-client (stage 2) leaves: the context for the new
    deployment, the typed secret in its credential store, `verify_context`."""
    secret = "GOCSPX-" + secrets.token_hex(12)
    name = swarm_context.store_desktop_secret(ctx, secret.encode(), run=swarm)
    ctx.outputs["verify_context"] = name
    return name, secret


def test_verify_targets_the_new_deployment_not_the_current_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the owner's laptop the current swarm context is the live deployment.
    Verify signs the person in to the engine's own context, named after the
    new hostname and written at stage 2 with the Desktop secret, and sends
    every run there as the first team; the current context and the context it
    found are left untouched, and no address or tenant from the environment
    reaches a run."""
    for name in ("SWARM_URL", "SWARM_CONTEXT", "SWARM_API_URL", "SWARM_TENANT"):
        monkeypatch.setenv(name, "https://swarm.live.example" if "URL" in name else "live")
    swarm = FakeSwarm({"live": LIVE}, "live")
    ctx = _target_ctx()
    name, secret = _stored(swarm, ctx)
    assert name == HOSTNAME and swarm.secrets[HOSTNAME] == secret
    stored_at = len(swarm.calls)
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm)
    cli.target(ctx)

    assert ctx.outputs["verify_context"] == HOSTNAME and ctx.outputs["verify_tenant"] == TEAM
    assert swarm.current == "live" and swarm.contexts["live"] == LIVE
    assert swarm.contexts[HOSTNAME] == {"url": f"https://{HOSTNAME}", "oauth_client_id": CLIENT_ID,
                                        "signed_in": True}
    assert not any(secret in a for argv, _, _ in swarm.calls for a in argv)
    verify_calls = [(argv, stdin) for argv, stdin, _ in swarm.calls[stored_at:]]
    assert not [argv for argv, _ in verify_calls if argv[1:3] == ["context", "add"]], "verify writes no context"
    assert all(stdin is None for _, stdin in verify_calls), "verify never handles the secret"
    assert ["swarm", "--context", HOSTNAME, "login"] in [argv for argv, _ in verify_calls]
    assert not any("use" in argv[1:3] or "remove" in argv[1:3] for argv, _, _ in swarm.calls)

    assert cli.launch(ctx, "plugin", "single", REPO, "v-1") == "task_0123456789abcdef0123"
    argv, _, env = swarm.calls[-1]
    assert argv[:4] == ["swarm", "--context", HOSTNAME, "dispatch"]
    assert env is not None and env["SWARM_TENANT"] == TEAM
    assert not {"SWARM_URL", "SWARM_CONTEXT", "SWARM_API_URL"} & set(env)

    # Run again: signed in already, nothing written, nobody asked.
    before = len(swarm.calls)
    cli.target(ctx)
    assert not [argv for argv, _, _ in swarm.calls[before:] if argv[1:3] == ["context", "add"] or "login" in argv]


def test_a_context_with_the_name_and_another_address_is_never_rewritten() -> None:
    other = {"url": "https://elsewhere.example", "oauth_client_id": "", "signed_in": False}
    swarm = FakeSwarm({HOSTNAME: other}, HOSTNAME)
    ctx = _target_ctx()
    _stored(swarm, ctx)
    verify.SwarmCli(Answers(), run=swarm, interactive=swarm).target(ctx)
    assert swarm.contexts[HOSTNAME] == other and swarm.current == HOSTNAME
    assert ctx.outputs["verify_context"] == f"{HOSTNAME}-install"
    assert swarm.contexts[f"{HOSTNAME}-install"]["url"] == f"https://{HOSTNAME}"


def test_verify_never_needs_the_desktop_secret() -> None:
    """Owner pick store-at-stage-6: the secret is in the swarm credential
    store from edge.desktop-client, typed once at its hidden prompt. Verify
    reads it from nowhere (not Secret Manager, not the discovery document)
    and writes no context; without the stage-2 context it stops, naming it."""
    assert not hasattr(verify, "secret_from_discovery")
    assert "desktop_secret" not in {f.name for f in dataclasses.fields(verify.SwarmCli)}
    swarm = FakeSwarm({}, None)
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm)
    with pytest.raises(LookupError, match="edge.desktop-client"):
        cli.target(_target_ctx())
    with pytest.raises(LookupError, match="not on this machine"):
        cli.target(_target_ctx(verify_context=HOSTNAME))
    moved = {"url": "https://elsewhere.example", "oauth_client_id": CLIENT_ID, "signed_in": True}
    swarm.contexts[HOSTNAME] = moved
    with pytest.raises(LookupError, match="not on this machine"):
        cli.target(_target_ctx(verify_context=HOSTNAME))
    assert not [argv for argv, _, _ in swarm.calls if argv[1:3] == ["context", "add"] or "login" in argv]


def test_repository_from_the_app_installation_never_asked() -> None:
    """The verification repository is the App installation's first repository
    that is not archived, read on the new deployment; nobody types it. With
    no GitHub connection yet, the person signs in to GitHub once (`swarm sc
    setup`, a browser sign-in) and it is read again."""
    swarm = FakeSwarm({}, None)
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm)
    ctx = _target_ctx(verify_context=HOSTNAME, verify_tenant=TEAM)
    swarm.contexts[HOSTNAME] = {"url": f"https://{HOSTNAME}", "oauth_client_id": CLIENT_ID, "signed_in": True}
    assert cli.repository(ctx) == REPO
    argv, _, env = swarm.calls[-1]
    assert argv == ["swarm", "sc", "access", "repos", "acme", "--json", "--context", HOSTNAME]
    assert env is not None and env["SWARM_TENANT"] == TEAM

    swarm.repos, swarm.connect_on_setup = None, [{"repository": "acme/first", "archived": False}]
    assert cli.repository(ctx) == "acme/first"
    assert ["swarm", "sc", "setup", "--context", HOSTNAME] in [argv for argv, _, _ in swarm.calls]

    swarm.repos, swarm.connect_on_setup = None, None
    with pytest.raises(RuntimeError, match="could not be read"):
        cli.repository(ctx)

    # Any other failure (a network error) is reported, never answered with setup.
    setups = sum(argv[:2] == ["swarm", "sc"] and argv[2] == "setup" for argv, _, _ in swarm.calls)
    swarm.repos, swarm.repos_error = [{"repository": REPO, "archived": False}], "sc: connection reset by peer"
    with pytest.raises(RuntimeError, match="connection reset"):
        cli.repository(ctx)
    assert sum(argv[:2] == ["swarm", "sc"] and argv[2] == "setup" for argv, _, _ in swarm.calls) == setups

    runs = FakeRuns()
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs, kube=FakeKube())
    ctx = _ctx()
    step.apply(ctx, step.plan(ctx))
    assert runs.targeted == 1 and ctx.outputs["verify_repository"] == REPO


def test_check_runs_launch_and_finish_as_the_real_cli_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each check run is `swarm dispatch --profile <its profile>` with its
    prompt; `swarm status` names the profile it ran on, and its proof is read
    through `swarm artifact`."""
    api = FakeSwarmApi()
    cli = verify.SwarmCli(Answers(), run=real_swarm(api, monkeypatch))
    ctx = _ctx(verify_context=HOSTNAME, verify_tenant=TEAM, verify_repository=REPO)
    assert cli.launch(ctx, "plugin", "single", REPO, "v-1") == "t-single"
    browser = cli.launch(ctx, "plugin", "browser", REPO, "v-b")
    graph = cli.launch(ctx, "plugin", "code-intelligence", REPO, "v-c")
    assert [d["runner_profile"] for d in api.dispatched] == ["claude-code", "claude-code-browser", "claude-code"]
    assert f"https://github.com/{REPO}" in api.dispatched[1]["prompt"]
    assert "code-intelligence.json" in api.dispatched[2]["prompt"]

    assert cli.status(ctx, "browser", browser) == verify.RunStatus("running", profile="claude-code-browser")
    assert cli.index_commit(ctx) == ""  # not registered on a new deployment: no record
    cli.index(ctx, REPO)  # `swarm repo-index`: registered, its index run asked for
    assert api.registered == {REPO: REPO_ID} and api.index_runs == [REPO_ID]
    assert cli.index_commit(ctx) == ""  # asked for, not yet promoted
    api.promote()
    installation = {"kind": "app_installation", "source": TENANT_GIT}
    for task_id, shape in ((browser, "browser"), (graph, "code-intelligence")):
        api.tasks[task_id].update(state="SUCCEEDED", result_summary={"git": {}, "forge_credential": installation})
        api.artifacts[(task_id, verify.CHECK_RUNS[shape].artifact)] = check_artifact(shape)
    assert cli.status(ctx, "browser", browser) == verify.RunStatus(
        "succeeded", "", (verify.CredentialUse(browser, "app_installation", TENANT_GIT),), "claude-code-browser",
        check_artifact("browser"))
    found = cli.status(ctx, "code-intelligence", graph)
    assert found.artifact == check_artifact("code-intelligence") and found.profile == "claude-code"
    # The graph's own record, through `swarm graph-index` (the graph-cache route), not the agent's answer.
    assert found.index_commit == INDEX_SHA
    api.indexes[REPO_ID] = None
    assert cli.status(ctx, "code-intelligence", graph).index_commit == ""
    assert cli.status(ctx, "browser", browser).index_commit == ""
    del api.artifacts[(graph, "code-intelligence.json")]
    assert cli.status(ctx, "code-intelligence", graph).artifact is None


def test_verify_progress_written_with_countdown(tmp_path: Path) -> None:
    """D26: the engine document's `verify` field carries the check, how many
    of the eight are done and the countdown (started, expected end, deadline),
    so Admin › Setup draws the verify row; the terminal sees the same."""
    rig = Rig(tmp_path)
    rig.store.update(lambda s: setattr(s, "region", REGION))
    documents = FakeDocuments()
    gcp = VerifyFakeGcp()
    gcp.jobs = [_job("swarm-reconcile", f"{RECONCILER_URL}/tick", account=RECONCILER)]
    gcp.run_services_list = [_run_service("swarm-reconciler", RECONCILER_URL)]
    gcp.iam = _invokers(f"serviceAccount:{RECONCILER}")
    targets = verify.SchedulerTargetsStep(lambda: gcp, Answers())
    engine = rig.engine([targets])
    engine.record = InstallRecord(env="dev", documents=documents)
    started = rig.clock.now()
    result = engine.run()
    assert result.exit_code == ExitCode.OK, result.message

    row = documents.docs[engine_document("dev")][0]["verify"]
    assert row["check"] == "scheduler-targets" and row["done"] == 1 and row["total"] == verify.TOTAL == 11
    assert row["started"] == started
    assert row["expected_end"] == started + verify.RUNS_WAIT.expected_s
    assert row["deadline"] == started + verify.RUNS_WAIT.bound_s
    assert row["result"] is None
    assert rig.store.load().outputs["verify_started"] == started

    countdown = rig.of_kind("wait_countdown", "verify.scheduler-targets")
    assert countdown and countdown[-1]["remaining_s"] == verify.RUNS_WAIT.bound_s
    assert countdown[-1]["done"] == 1 and countdown[-1]["deadline"] == row["deadline"]

    # The last run's pass marks the row passed, which unlocks "Send to your team".
    step, ctx = _all_finished()
    ctx.outputs["verify_started"] = started
    step.check(ctx)
    last = ctx.events.of_kind("wait_countdown")[-1]
    assert last["done"] == verify.TOTAL and last["result"] == "passed" and last["started"] == started


@pytest.mark.usefixtures("cosign")
def test_cluster_objects_from_release_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`other.release-kubernetes-bundle`: what stage 10 applies and stage 14
    then verifies renders from the release's `kubernetes.tgz`, checked against
    SHA256SUMS and unpacked; never a checkout. A bundle that does not match its
    sum, or escapes its root, is refused."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    gcp, shell = ClusterFakeGcp(), FakeShell()
    bundle, policies, *_ = cluster.build_steps(lambda: gcp, Answers(), kube=_kube(shell), run=shell)
    ctx = _ctx()
    # A dry run checks the digest and writes nothing (review minor).
    assert bundle.check(ctx).status == MET and not cluster.bundle_dir(PROJECT, VERSION).exists()
    ctx.dry_run = False
    observed = bundle.check(ctx)
    assert observed.status == MET
    root = Path(observed.outputs["kubernetes_bundle"])
    assert root == cluster.bundle_dir(PROJECT, VERSION) and (root / "render.py").is_file()
    policies.rendered(ctx)
    render = next(argv for argv, _, _ in shell.calls if argv[1].endswith("render.py"))
    assert render[1] == str(root / "render.py")
    repo = Path(__file__).resolve().parents[3]
    assert not render[1].startswith(str(repo))
    assert f"{RELEASE_HOME}/releases/{VERSION}/{cluster.BUNDLE}" in {a[0] for a in gcp.called("read_public")}

    tampered = ClusterFakeGcp(summed=b"another file")
    with pytest.raises(TerraformRefused, match="SHA256SUMS"):
        cluster.build_steps(lambda: tampered, Answers(), kube=_kube(FakeShell()))[0].check(_ctx())

    escaping = ClusterFakeGcp(bundle=bundle_bytes({"../outside.py": b"x"}))
    with pytest.raises(ValueError, match="escapes"):
        cluster.build_steps(lambda: escaping, Answers(), kube=_kube(FakeShell()))[0].check(ctx)


def test_steps_satisfy_every_stage_14_entry() -> None:
    from swarm_install.manifest import load

    ids = {e.id for e in load().entries}
    covered = {entry for step in verify.STEPS for entry in step.satisfies}
    assert covered <= ids
    assert {"other.verify-row", "other.verify-end-to-end", "other.verify-digests",
            "other.verify-scheduler-targets"} <= covered
    assert [s.stage for s in verify.STEPS] == [14] * len(verify.STEPS)


# -- audit 4: the swarm CLI by its own path; the verify repository indexed ----------


def _executable(path: Path, body: str = "") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_the_swarm_cli_is_found_by_its_own_path(tmp_path: Path, swarm_off_path: str) -> None:
    """B4: `~/.local/bin/swarm install` with that directory off PATH. The
    engine spawns the CLI that is running it, by its path; then the one beside
    its interpreter (uv's tool bin); then PATH; a bare name only when none is."""
    assert swarm_context.swarm_cli() == swarm_off_path
    tool_python = tmp_path / "tool" / "bin" / "python3"
    beside = _executable(tool_python.parent / "swarm")
    assert swarm_context.swarm_cli(argv0="/usr/lib/python3/pytest", executable=str(tool_python)) == beside
    on_path = str(tmp_path / "elsewhere" / "swarm")
    assert swarm_context.swarm_cli(argv0="", executable=str(tmp_path / "x" / "python3"),
                                   which=lambda name: on_path) == on_path
    assert swarm_context.swarm_cli(argv0="", executable=str(tmp_path / "x" / "python3"),
                                   which=lambda name: None) == "swarm"
    # A running `swarm` that is not an executable file is not spawned.
    assert swarm_context.swarm_cli(argv0=str(tmp_path / "gone" / "swarm"), executable=str(tool_python)) == beside


def test_the_desktop_secret_is_stored_through_the_running_cli_off_path(tmp_path: Path, swarm_off_path: str) -> None:
    """B4, through a real subprocess: the default `run` and the default CLI
    store the secret although PATH holds no swarm; on main the bare "swarm"
    raised "swarm is not installed"."""
    store = tmp_path / "stored"
    _executable(Path(swarm_off_path), f"""if [ "$2" = list ]; then printf '{{"contexts": []}}\\n'; exit 0; fi
read -r value
printf '%s %s\\n' "$3" "$value" > '{store}'
""")
    ctx = _target_ctx()
    secret = "GOCSPX-" + secrets.token_hex(12)
    assert swarm_context.store_desktop_secret(ctx, secret.encode()) == HOSTNAME
    assert store.read_text() == f"{HOSTNAME} {secret}\n"


def test_a_cli_found_nowhere_is_named_not_called_uninstalled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(swarm_context.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not found at swarm, beside this engine or on PATH"):
        swarm_context.list_contexts(FakeSwarm({}, None))


def test_verify_registers_and_indexes_its_repository_before_the_graph_check(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """B2: nothing on a new deployment registers or indexes the repository
    verify picked, so the code graph never records the commit the check
    compares with. Verify registers it and asks for its index run through the
    real `swarm repo-index`, and starts the code-intelligence run only once
    `swarm graph-index` records a commit."""
    api = FakeSwarmApi()
    cli = verify.SwarmCli(Answers(), run=real_swarm(api, monkeypatch))
    cli.target = lambda ctx: None  # type: ignore[method-assign]
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=cli, kube=FakeKube())
    ctx = _ctx(verify_context=HOSTNAME, verify_tenant=TEAM, verify_repository=REPO)
    ctx.dry_run = False
    ctx.engine = _Saves(ctx)
    step.apply(ctx, step.plan(ctx))
    assert api.registered == {REPO: REPO_ID} and api.index_runs == [REPO_ID]
    assert ctx.outputs["verify_index_requested"] is True
    assert [d["runner_profile"] for d in api.dispatched] == ["claude-code", "claude-code", "claude-code-browser"]
    assert "plugin-code-intelligence" not in ctx.outputs["verify_runs"]

    observed = step.check(ctx)
    assert observed.status == WAITING and f"the code graph has indexed {REPO}" in observed.detail
    assert len(api.dispatched) == 3
    ctx.dry_run = True
    api.promote()
    step.check(ctx)
    assert len(api.dispatched) == 3  # a dry run starts nothing
    ctx.dry_run = False
    step.check(ctx)
    assert len(api.dispatched) == 4 and "code-intelligence.json" in api.dispatched[3]["prompt"]
    assert ctx.outputs["verify_runs"]["plugin-code-intelligence"]

    # A rerun of apply (swarm install again) asks for no second index.
    step.apply(ctx, step.plan(ctx))
    assert api.index_runs == [REPO_ID] and len(api.dispatched) == 4


def test_repo_index_registers_once_and_refuses_a_non_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeSwarmApi()
    swarm = real_swarm(api, monkeypatch)
    code, out, _ = swarm([sys.argv[0], "repo-index", REPO, "--json"], None, None)
    assert code == 0 and json.loads(out)["registered"] is True and json.loads(out)["task_id"] == "task_index"
    code, out, _ = swarm([sys.argv[0], "repo-index", REPO, "--json"], None, None)
    assert code == 0 and json.loads(out)["registered"] is False and json.loads(out)["coalesced"] is True
    assert api.index_runs == [REPO_ID, REPO_ID]
    code, _, err = swarm([sys.argv[0], "repo-index", "not a repository"], None, None)
    assert code == 1 and "owner/repo" in err


def test_verify_waits_point_only_at_what_exists() -> None:
    """M3: a wait that gives up named `swarm doctor --explain`, which no
    parser defines. What verify's waits point at must exist: no `--explain`,
    no raw gcloud, and a `--repair` names an entry a verify step satisfies."""
    import re

    covered = {entry for step in verify.STEPS for entry in step.satisfies}
    for spec in (verify.TARGETS_WAIT, verify.RUNS_WAIT):
        said = spec.look_at_for(_ctx())
        assert "--explain" not in said and "gcloud " not in said
        for entry in re.findall(r"--repair (\S+)", said):
            assert entry in covered
    # Audit 5b M2: the give-up already says "run swarm install again", which
    # re-checks it; the page is the project's own, not `<project>`.
    assert f"cloudscheduler?project={PROJECT}" in verify.TARGETS_WAIT.look_at_for(_ctx())
