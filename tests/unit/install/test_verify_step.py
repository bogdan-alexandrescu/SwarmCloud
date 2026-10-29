"""Stage 14, verify (DAYONE-ENGINE-4; design §4, D3, D26; lessons 18, 28,
30, 38, 43; manifest `other.verify-row`, `other.release-kubernetes-bundle`).

On fakes, with no credentials: every Scheduler target is called as its job
calls it and must answer 2xx; what a service serves is read from its
revision's digest, never a label; the reconciler may list Jobs per tenant
namespace; the console's verify row is written with its countdown; four runs
each reach a pull request, read from what the real `swarm` CLI prints, the
plugin's signed in with the Desktop client; lesson 43 is reported as not
checkable rather than read from a field nothing records; and the cluster's
objects render from the release bundle.
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


class FakeRuns:
    def __init__(self) -> None:
        self.launched: list[tuple[str, str, str, str]] = []
        self.states: dict[str, verify.RunStatus] = {}

    def launch(self, ctx: Any, surface: str, shape: str, repository: str, label: str) -> str:
        self.launched.append((surface, shape, repository, label))
        run_id = f"t-{surface}-{shape}"
        self.states[run_id] = verify.RunStatus("running")
        return run_id

    def status(self, ctx: Any, shape: str, run_id: str) -> verify.RunStatus:
        assert run_id.endswith(shape)
        return self.states[run_id]

    def finish(self, run_id: str, *, pr: str | None = None) -> None:
        url = pr if pr is not None else f"https://github.com/acme/demo/pull/{len(run_id)}"
        self.states[run_id] = verify.RunStatus("succeeded", url)


def test_four_end_to_end_runs_each_to_a_pr() -> None:
    """D3: a single task and a workflow from the plugin and from the console,
    each to a pull request; the step waits until all four have one."""
    runs = FakeRuns()
    ask = Answers(verify_end_to_end=["acme/demo"])
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), ask, runs=runs)
    ctx = _ctx()
    assert step.check(ctx).status == UNMET  # nothing started yet
    step.apply(ctx, step.plan(ctx))
    assert [(s, k) for s, k, _, _ in runs.launched] == [("plugin", "single"), ("plugin", "workflow"),
                                                        ("console", "single"), ("console", "workflow")]
    assert {repo for _, _, repo, _ in runs.launched} == {"acme/demo"}
    assert ctx.outputs["verify_repository"] == "acme/demo" and len(ctx.outputs["verify_runs"]) == 4

    step.apply(ctx, step.plan(ctx))  # a rerun launches nothing twice
    assert len(runs.launched) == 4

    for run_id in ("t-plugin-single", "t-plugin-workflow", "t-console-single"):
        runs.finish(run_id)
    observed = step.check(ctx)
    assert observed.status == WAITING and "3 of 4" in observed.detail

    runs.finish("t-console-workflow", pr="")
    observed = step.check(ctx)
    assert observed.status == UNMET and "console-workflow" in observed.detail and "without a pull request" in observed.detail

    runs.finish("t-console-workflow")
    observed = step.check(ctx)
    assert observed.status == MET
    assert set(observed.outputs["verify_pull_requests"]) == {"plugin-single", "plugin-workflow", "console-single",
                                                             "console-workflow"}
    assert "other.verify-row" in step.satisfies


def test_runs_use_installation_tokens() -> None:
    """Lesson 43: a run must clone and push with an App installation token,
    never a PAT. Nothing swarm-api serves records which credential a run used
    (no task, attempt or result names it), so the step never reads an invented
    field, and never claims the check: every pass says the credential is
    unrecorded, in its detail and in its outputs (question in the PR)."""
    runs = FakeRuns()
    step = verify.EndToEndStep(lambda: VerifyFakeGcp(), Answers(), runs=runs)
    ctx = _ctx(verify_runs={f"{s}-{k}": f"t-{s}-{k}" for s, k in verify.RUNS})
    for surface, shape in verify.RUNS:
        runs.finish(f"t-{surface}-{shape}")
    observed = step.check(ctx)
    assert observed.status == MET
    assert "lesson 43" in observed.detail and "not checked" in observed.detail
    assert observed.outputs["verify_forge_credential"] == "unrecorded"
    assert not hasattr(verify, "APP_INSTALLATION")
    assert set(verify.RunStatus.__dataclass_fields__) == {"state", "pull_request"}


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

    def task(self, task_id: str) -> dict[str, Any]:
        return self.tasks[task_id]

    def dispatch(self, **kw: Any) -> dict[str, Any]:
        self.dispatched.append(kw)
        self.tasks["t-single"] = {"id": "t-single", "state": "QUEUED", "runner_profile": kw["runner_profile"]}
        return self.tasks["t-single"]

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
    ctx = _ctx()
    assert cli.launch(ctx, "plugin", "single", "acme/demo", "v-1") == "t-single"
    assert api.dispatched[0]["metadata"] == {"unit": "v-1"}
    assert cli.launch(ctx, "plugin", "workflow", "acme/demo", "v-2") == "wf_1"
    assert [st["step_id"] for st in api.posted[0]["steps"]] == ["write", "open"]

    # Still going: a queued task, and a workflow the server did not derive (exit 1, state null).
    assert cli.status(ctx, "single", "t-single") == verify.RunStatus("running")
    assert cli.status(ctx, "workflow", "wf_1") == verify.RunStatus("running")

    pr = "https://github.com/acme/demo/pull/7"
    api.tasks["t-single"].update(state="SUCCEEDED",
                                 result_summary={"git": {"pull_request": {"url": pr, "number": 7}}})
    assert cli.status(ctx, "single", "t-single") == verify.RunStatus("succeeded", pr)

    wf_pr = "https://github.com/acme/demo/pull/8"
    api.workflows["wf_1"]["workflow"].update(state="SUCCEEDED", state_source="derived")
    api.workflows["wf_1"]["tasks"] = [
        {"id": "t-write", "state": "SUCCEEDED", "result_summary": {"git": {}}},
        {"id": "t-open", "state": "SUCCEEDED",
         "result_summary": {"git": {"pull_request": {"url": wf_pr, "number": 8}}}},
    ]
    assert cli.status(ctx, "workflow", "wf_1") == verify.RunStatus("succeeded", wf_pr)

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

    ask = Answers(verify_end_to_end=["t-console-1"])
    cli = verify.SwarmCli(ask, run=run)
    ctx = _ctx()
    assert cli.launch(ctx, "plugin", "single", "acme/demo", "v-1") == "t-single"
    assert cli.launch(ctx, "plugin", "workflow", "acme/demo", "v-2") == "wf_1"
    assert cli.launch(ctx, "console", "single", "acme/demo", "v-3") == "t-console-1"
    assert len(calls) == 2  # the console's run is started by the person, in the console
    for argv, _, env in calls:
        assert argv[0] == "swarm"
        assert env is not None and env["HOME"] == "/home/person"
        assert not set(verify.NOT_FOR_PLUGIN_RUNS) & set(env)
        for word in ("gcloud", "print-identity-token", "print-access-token", "--token", "--id-token"):
            assert not any(word in a for a in argv)
    assert "SWARM_ID_TOKEN" in verify.NOT_FOR_PLUGIN_RUNS and "SWARM_IMPERSONATE_SA" in verify.NOT_FOR_PLUGIN_RUNS


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
    assert row["check"] == "scheduler-targets" and row["done"] == 1 and row["total"] == verify.TOTAL == 8
    assert row["started"] == started
    assert row["expected_end"] == started + verify.RUNS_WAIT.expected_s
    assert row["deadline"] == started + verify.RUNS_WAIT.bound_s
    assert row["result"] is None
    assert rig.store.load().outputs["verify_started"] == started

    countdown = rig.of_kind("wait_countdown", "verify.scheduler-targets")
    assert countdown and countdown[-1]["remaining_s"] == verify.RUNS_WAIT.bound_s
    assert countdown[-1]["done"] == 1 and countdown[-1]["deadline"] == row["deadline"]

    # The last run's pass marks the row passed, which unlocks "Send to your team".
    runs = FakeRuns()
    ctx = _ctx(verify_runs={f"{s}-{k}": f"t-{s}-{k}" for s, k in verify.RUNS}, verify_started=started)
    for surface, shape in verify.RUNS:
        runs.finish(f"t-{surface}-{shape}")
    verify.EndToEndStep(lambda: gcp, Answers(), runs=runs).check(ctx)
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
