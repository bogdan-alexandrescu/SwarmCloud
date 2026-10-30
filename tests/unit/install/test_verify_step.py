"""Stage 14, verify (DAYONE-ENGINE-4; design §4, D3, D26; lessons 18, 28,
30, 38, 43; manifest `other.verify-row`, `other.release-kubernetes-bundle`).

On fakes, with no credentials: every Scheduler target is called as its job
calls it and must answer 2xx; what a service serves is read from its
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
import io
import json
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from swarm_install.exitcodes import MET, UNMET, WAITING, ExitCode
from swarm_install.record import InstallRecord, engine_document
from swarm_install.steps import cluster, verify
from swarm_install.steps.preflight import RELEASE_HOME

from .test_cluster_step import VERSION, ClusterFakeGcp, FakeShell, _kube, bundle_bytes
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

    def scheduler_jobs(self, project: str, region: str) -> list[dict[str, Any]]:
        self._call("scheduler_jobs", project, region)
        return list(self.jobs)

    def knative_services(self, project: str, region: str) -> list[dict[str, Any]]:
        self._call("knative_services", project, region)
        return list(self.services)

    def knative_revision(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        self._call("knative_revision", project, region, name)
        return self.revisions.get(name)

    def service_account(self, email: str) -> dict[str, Any] | None:
        self._call("service_account", email)
        return {"email": email, "uniqueId": RECONCILER_UID}


class FakeMinter:
    def __init__(self) -> None:
        self.minted: list[tuple[str, str, bool]] = []
        self.tokens: list[str] = []

    def generate_id_token(self, service_account: str, audience: str, *, include_email: bool) -> str:
        self.minted.append((service_account, audience, include_email))
        self.tokens.append(secrets.token_hex(16))
        return self.tokens[-1]


def _job(name: str, uri: str, *, account: str, audience: str | None = None, state: str = "ENABLED",
         headers: dict[str, str] | None = None) -> dict[str, Any]:
    oidc = {"serviceAccountEmail": account, **({"audience": audience} if audience else {})}
    return {"name": f"projects/{PROJECT}/locations/{REGION}/jobs/{name}", "state": state,
            "httpTarget": {"uri": uri, "httpMethod": "POST", "headers": headers or {}, "oidcToken": oidc,
                           "body": base64.b64encode(b"{}").decode()}}


def _ctx(**outputs: Any) -> Any:
    ctx = ctx_for(outputs=outputs)
    ctx.state.release = VERSION
    return ctx


def test_every_scheduler_target_must_answer_2xx() -> None:
    """Lesson 38: Scheduler says "attempted" while its target refuses. Each
    enabled job's target is called as the job calls it, with an ID token for
    the job's own account and audience, and a non-2xx keeps the step waiting."""
    gcp, minter = VerifyFakeGcp(), FakeMinter()
    sweep = cluster.account_email(PROJECT, "swarm-scheduler")
    stale_bearer = "Bearer " + secrets.token_hex(8)
    gcp.jobs = [
        _job("swarm-quota-sweep", "https://scheduler.run.app/sweep", account=sweep,
             audience="https://scheduler.run.app",
             headers={"Authorization": stale_bearer, "Content-Type": "application/json"}),
        _job("swarm-reconcile", "https://reconciler.run.app/tick", account=RECONCILER),
        _job("swarm-paused", "https://paused.run.app/x", account=sweep, state="PAUSED"),
        # Not the platform's: never triggered by the check.
        _job("nightly-export", "https://export.example/run", account=sweep),
    ]
    status = {"https://scheduler.run.app/sweep": 403, "https://reconciler.run.app/tick": 200}
    called: list[tuple[str, str, dict[str, str], bytes]] = []

    def call(method: str, url: str, headers: Any, body: bytes) -> int:
        called.append((method, url, dict(headers), body))
        return status[url]

    step = verify.SchedulerTargetsStep(lambda: gcp, Answers(), minter=minter, call=call)
    observed = step.check(_ctx())
    assert observed.status == WAITING
    assert "swarm-quota-sweep -> HTTP 403" in observed.detail and "swarm-reconcile" not in observed.detail
    assert [url for _, url, _, _ in called] == ["https://scheduler.run.app/sweep", "https://reconciler.run.app/tick"]
    assert minter.minted == [(sweep, "https://scheduler.run.app", True),
                             (RECONCILER, "https://reconciler.run.app/tick", True)]
    first = called[0][2]
    assert first["Authorization"] == f"Bearer {minter.tokens[0]}" and stale_bearer not in first.values()
    assert first["Content-Type"] == "application/json" and called[0][3] == b"{}"
    assert step.kind == "wait" and step.wait.bound_s >= 60

    status["https://scheduler.run.app/sweep"] = 204
    observed = step.check(_ctx())
    assert observed.status == MET
    assert observed.outputs["scheduler_targets"] == ["swarm-quota-sweep", "swarm-reconcile"]


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
#: The code graph's indexed commit, made at runtime.
INDEX_SHA = secrets.token_hex(20)
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
    def __init__(self) -> None:
        self.launched: list[tuple[str, str, str, str]] = []
        self.states: dict[str, verify.RunStatus] = {}
        self.targeted = 0

    def target(self, ctx: Any) -> None:
        self.targeted += 1
        ctx.outputs.update(verify_context=HOSTNAME, verify_tenant=TEAM)

    def repository(self, ctx: Any) -> str:
        return REPO

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str:
        self.launched.append((surface, shape, repository, label))
        run_id = f"t-{surface}-{shape}"
        self.states[run_id] = verify.RunStatus("running")
        return run_id

    def status(self, ctx: Any, shape: str, run_id: str) -> verify.RunStatus:
        assert run_id.endswith(shape)
        return self.states.get(run_id, verify.RunStatus("running"))

    def finish(self, run_id: str, *, pr: str | None = None, kind: str = "app_installation",
               artifact: str | None = None, profile: str | None = None) -> None:
        used = verify.credential_use(run_id, {"forge_credential": {"kind": kind, "source": TENANT_GIT}})
        shape = run_id.split("-", 2)[2]
        check = verify.CHECK_RUNS.get(shape)
        if check is not None:
            self.states[run_id] = verify.RunStatus(
                "succeeded", "", (used,), profile or check.profile,
                artifact if artifact is not None else check_artifact(shape))
            return
        url = pr if pr is not None else f"https://github.com/{REPO}/pull/{len(run_id)}"
        self.states[run_id] = verify.RunStatus("succeeded", url, (used,))


def _job(task_id: str, *, live: bool = False) -> dict[str, Any]:
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
                         [_job("t-plugin-gke"), _job("t-plugin-browser", live=True)])
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
    assert step.check(ctx).status == UNMET  # nothing started yet
    step.apply(ctx, step.plan(ctx))
    assert [(s, k) for s, k, _, _ in runs.launched] == [("plugin", "single"), ("plugin", "workflow"),
                                                        ("plugin", "gke"), ("plugin", "browser"),
                                                        ("plugin", "code-intelligence")]
    assert {repo for _, _, repo, _ in runs.launched} == {REPO}
    assert ctx.outputs["verify_repository"] == REPO and set(ctx.outputs["verify_runs"]) == set(PLUGIN_KEYS)

    step.apply(ctx, step.plan(ctx))  # a rerun launches nothing twice
    assert len(runs.launched) == 5

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


@pytest.mark.parametrize("kind", ["pat", "app_user", "none", "mixed", "unknown"])
def test_a_run_that_used_anything_but_an_installation_token_fails_naming_it(kind: str) -> None:
    step, ctx = _all_finished({"console-single": kind})
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert "console-single (t-console-single)" in observed.detail
    assert f"used a {kind} credential from {TENANT_GIT}" in observed.detail
    assert "lesson 43" in observed.detail
    for other in ("plugin-single", "plugin-workflow", "console-workflow", *CHECK_KEYS):
        assert other not in observed.detail


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


def test_console_runs_read_from_the_record_never_asked() -> None:
    """INTERFACE SHEET: Admin › Setup starts the console's two runs and
    swarm-api writes `verify_runs: {console_task, console_workflow}` into a
    console document; the engine copies it into state.outputs and follows
    those ids. Nothing is ever asked (the Answers here refuse any question)."""
    docs: dict[str, dict[str, Any]] = {"teams/eng": {"group": "eng@acme.example", "workspace": TEAM}}
    runs = FakeRuns()
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs, kube=FakeKube())
    ctx = _ctx(verify_runs={key: f"t-{key}" for key in PLUGIN_KEYS}, verify_repository=REPO,
               verify_context=HOSTNAME, verify_tenant=TEAM)
    ctx.state.console = dict(docs)
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
    observed = step.check(ctx)
    assert observed.status == WAITING and "console-single" in observed.detail
    assert read == ["teams/eng"]

    docs["teams/eng"] = {**docs["teams/eng"], "verify_runs": dict(CONSOLE)}
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


# -- the GKE, live-browser and code-intelligence checks ---------------------------


def test_check_runs_pick_their_profiles_from_the_frozen_catalogue() -> None:
    from swarm_common.profiles import RUNNER_PROFILES, Backend

    gke, browser, graph = (verify.CHECK_RUNS[s] for s in ("gke", "browser", "code-intelligence"))
    assert RUNNER_PROFILES[gke.profile].backend == Backend.GKE_AUTOPILOT and gke.on_cluster
    assert browser.profile == "claude-code-browser" and RUNNER_PROFILES[browser.profile].live_browser
    assert browser.live and browser.on_cluster
    assert graph.artifact == "code-intelligence.json" and "swarm-graph" in graph.prompt
    assert verify.TOTAL == len(verify.QUICK_CHECKS) + 7


def test_check_runs_pass_on_their_own_proof() -> None:
    step, ctx = _all_finished()
    observed = step.check(ctx)
    assert observed.status == MET, observed.detail
    checks = observed.outputs["verify_checks"]
    assert "swarm-t-plugin-gke-1" in checks["plugin-gke"] and f"swarm-tenant-{TEAM}" in checks["plugin-gke"]
    assert "swarm-t-plugin-browser-1" in checks["plugin-browser"] and REPO in checks["plugin-browser"]
    assert INDEX_SHA[:12] in checks["plugin-code-intelligence"]


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
    ("plugin-code-intelligence", "no symbol", "answered no symbol"),
    ("plugin-code-intelligence", "no artifact", "left no code-intelligence.json"),
    ("plugin-gke", "other profile", "not claude-code"),
])
def test_a_check_run_without_its_proof_fails_naming_it(key: str, breaks: str, said: str) -> None:
    kube = FakeKube()
    step, ctx = _all_finished(kube=kube)
    run_id = f"t-{key}"
    if breaks == "no job":
        kube.jobs = [j for j in kube.jobs if "gke" not in j["metadata"]["name"]]
    elif breaks == "not live":
        kube.jobs = [_job("t-plugin-gke"), _job("t-plugin-browser")]
    elif breaks == "other page":
        step.runs.finish(run_id, artifact="Example Domain\n")
    elif breaks == "not indexed":
        step.runs.finish(run_id, artifact=json.dumps({"freshness": {}, "rows": [{"symbol": "x"}]}))
    elif breaks == "no symbol":
        step.runs.finish(run_id, artifact=json.dumps({"freshness": {"index_sha": INDEX_SHA}, "rows": []}))
    elif breaks == "no artifact":
        step.runs.states[run_id] = verify.RunStatus(
            "succeeded", "", step.runs.states[run_id].credentials, "claude-code", None)
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
        raise AssertionError(f"the CLI made a call this test does not expect: {method} {path}")


def real_swarm(api: FakeSwarmApi, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A `run` that executes the real `swarm` command (swarm_mcp.cli's parser
    and handlers) against `api`, and answers its exit code and its stdout."""
    from swarm_mcp import cli as swarm_cli
    from swarm_mcp.client import SwarmError

    def run(argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        assert argv[0] == "swarm"
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
    for name in verify.NOT_FOR_PLUGIN_RUNS:
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
        assert argv[0] == "swarm"
        assert env is not None and env["HOME"] == "/home/person"
        assert not set(verify.NOT_FOR_PLUGIN_RUNS) & set(env)
        for word in ("gcloud", "print-identity-token", "print-access-token", "--token", "--id-token"):
            assert not any(word in a for a in argv)
    assert "SWARM_ID_TOKEN" in verify.NOT_FOR_PLUGIN_RUNS and "SWARM_IMPERSONATE_SA" in verify.NOT_FOR_PLUGIN_RUNS


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

    def __call__(self, argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        self.calls.append((list(argv), stdin, env))
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
            return 0, json.dumps({"tenants": [{"tenant_id": TEAM, "display_name": "Eng", "current": True}],
                                  "current": TEAM, "chosen": False}), ""
        if rest[0] == "dispatch":
            return 0, json.dumps({"id": "task_0123456789abcdef0123"}), ""
        raise AssertionError(f"an unexpected swarm call: {argv}")


def _target_ctx(**outputs: Any) -> Any:
    ctx = _ctx(desktop_client_id=CLIENT_ID, github_org="acme", **outputs)
    ctx.state.hostname = HOSTNAME
    return ctx


def test_verify_targets_the_new_deployment_not_the_current_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the owner's laptop the current swarm context is the live deployment.
    Verify writes its own context, named after the new hostname, signs the
    person in to it in the browser, and sends every run there as the first
    team; the current context and the context it found are left untouched,
    and no address or tenant from the environment reaches a run."""
    for name in ("SWARM_URL", "SWARM_CONTEXT", "SWARM_API_URL", "SWARM_TENANT"):
        monkeypatch.setenv(name, "https://swarm.live.example" if "URL" in name else "live")
    secret = secrets.token_hex(16)
    swarm = FakeSwarm({"live": LIVE}, "live")
    read: list[str] = []
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm,
                          desktop_secret=lambda ctx: read.append(ctx.project) or secret)
    ctx = _target_ctx()
    cli.target(ctx)

    assert ctx.outputs["verify_context"] == HOSTNAME and ctx.outputs["verify_tenant"] == TEAM
    assert swarm.current == "live" and swarm.contexts["live"] == LIVE
    assert swarm.contexts[HOSTNAME] == {"url": f"https://{HOSTNAME}", "oauth_client_id": CLIENT_ID,
                                        "signed_in": True}
    assert swarm.secrets[HOSTNAME] == secret and read == [PROJECT]
    assert not any(secret in a for argv, _, _ in swarm.calls for a in argv)
    assert ["swarm", "--context", HOSTNAME, "login"] in [argv for argv, _, _ in swarm.calls]
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
    cli = verify.SwarmCli(Answers(), run=swarm, interactive=swarm, desktop_secret=lambda ctx: secrets.token_hex(8))
    ctx = _target_ctx()
    cli.target(ctx)
    assert swarm.contexts[HOSTNAME] == other and swarm.current == HOSTNAME
    assert ctx.outputs["verify_context"] == f"{HOSTNAME}-install"
    assert swarm.contexts[f"{HOSTNAME}-install"]["url"] == f"https://{HOSTNAME}"


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
    ctx = _ctx(verify_context=HOSTNAME, verify_tenant=TEAM)
    assert cli.launch(ctx, "plugin", "single", REPO, "v-1") == "t-single"
    browser = cli.launch(ctx, "plugin", "browser", REPO, "v-b")
    graph = cli.launch(ctx, "plugin", "code-intelligence", REPO, "v-c")
    assert [d["runner_profile"] for d in api.dispatched] == ["claude-code", "claude-code-browser", "claude-code"]
    assert f"https://github.com/{REPO}" in api.dispatched[1]["prompt"]
    assert "code-intelligence.json" in api.dispatched[2]["prompt"]

    assert cli.status(ctx, "browser", browser) == verify.RunStatus("running", profile="claude-code-browser")
    installation = {"kind": "app_installation", "source": TENANT_GIT}
    for task_id, shape in ((browser, "browser"), (graph, "code-intelligence")):
        api.tasks[task_id].update(state="SUCCEEDED", result_summary={"git": {}, "forge_credential": installation})
        api.artifacts[(task_id, verify.CHECK_RUNS[shape].artifact)] = check_artifact(shape)
    assert cli.status(ctx, "browser", browser) == verify.RunStatus(
        "succeeded", "", (verify.CredentialUse(browser, "app_installation", TENANT_GIT),), "claude-code-browser",
        check_artifact("browser"))
    found = cli.status(ctx, "code-intelligence", graph)
    assert found.artifact == check_artifact("code-intelligence") and found.profile == "claude-code"
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
    gcp.jobs = [_job("swarm-reconcile", "https://reconciler.run.app/tick", account=RECONCILER)]
    targets = verify.SchedulerTargetsStep(lambda: gcp, Answers(), minter=FakeMinter(),
                                          call=lambda m, u, h, b: 200)
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
    with pytest.raises(ValueError, match="SHA256SUMS"):
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
