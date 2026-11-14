"""`swarm doctor`'s platform tier (lane DAYONE-DOCTOR-1, part of #2688; design
§2.1-§2.2, plan §1.5).

No credentials and no emulator: Google's REST APIs, swarm-api and the cluster
are fakes that answer by URL.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from swarm_install import doctor
from swarm_install import manifest as m
from swarm_install.exitcodes import MET, UNKNOWN, UNMET, ExitCode
from swarm_install.state import InstallState
from swarm_install.verbs import doctor as doctor_verb

PROJECT = "example-project"
REGION = "us-central1"


class FakeGcp:
    """Answers by URL prefix: a mapping, or an exception to raise."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, str]] = []

    def _answer(self, method: str, url: str) -> Mapping[str, Any]:
        self.calls.append((method, url))
        for prefix in sorted(self.answers, key=len, reverse=True):
            if url.startswith(prefix):
                found = self.answers[prefix]
                if isinstance(found, Exception):
                    raise found
                return found
        raise doctor.NotFound(url)

    def get(self, url, *, permission, resource):
        return self._answer("GET", url)

    def post(self, url, body, *, permission, resource):
        return self._answer("POST", url)


class FakeApi:
    def __init__(self, groups):
        self.groups = groups
        self.calls = 0

    def groups_probe(self):
        self.calls += 1
        return self.groups


class FakeKube:
    """The cluster's API server, reached the way the real network allows: off
    Google's runtime the public endpoint answers only while the authorised
    networks are off or hold the installer's entry; after the closing infra
    pass a laptop's call times out, as `_call`'s does (audit 4c M2)."""

    def __init__(self, objects: Mapping[str, Any], cluster: Mapping[str, Any] | None = None,
                 *, on_google: bool = True) -> None:
        self.objects = objects
        self.cluster = cluster or {}
        self.on_google = on_google
        self.calls = 0

    def get(self, path):
        self.calls += 1
        config = self.cluster.get("masterAuthorizedNetworksConfig") or {}
        open_to_installer = not config.get("enabled") or any(
            b.get("displayName") == "swarm-install" for b in config.get("cidrBlocks") or ())
        if not self.on_google and not open_to_installer:
            raise urllib.error.URLError(TimeoutError("timed out"))
        if path not in self.objects:
            raise doctor.NotFound(path)
        return self.objects[path]


def _entry(entry_id: str, probe: dict, *, step: str | None = "infra.pass-1",
           provider: str = "engine") -> m.Entry:
    return m.Entry(id=entry_id, kind="other", scope="core", stage=5, provider=provider,
                   explain=f"{entry_id} matters", probe=m.Probe(**probe), step=step)


def _manifest(*entries: m.Entry) -> m.Manifest:
    return m.Manifest(entries=list(entries), ignored={}, env={}, sources={})


def _target(**outputs) -> doctor.Target:
    return doctor.Target(project=PROJECT, environment="prod", region=REGION, outputs=outputs,
                         principal="user:admin@example.test")


def _one(entry: m.Entry, gcp: FakeGcp, **kw) -> doctor.Finding:
    outputs = kw.pop("outputs", {})
    return doctor.check_entry(entry, _target(**outputs), doctor.Deps(gcp=gcp, **kw))


def test_doctor_imports_no_apply_path():
    # `doctor` never writes: neither it, nor the hourly job, nor the verb loads
    # the engine, a step, the Terraform runner or the repair.
    probe = (
        "import sys, swarm_install.doctor, swarm_install.scheduled_doctor, swarm_install.verbs.doctor\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[:2] in ("
        "['swarm_install','engine'], ['swarm_install','steps'], ['swarm_install','terraform'], "
        "['swarm_install','repair'], ['swarm_install','tfvars']))\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_unchecked_is_exit_3_never_0():
    assert doctor.exit_code([]) == ExitCode.TROUBLE
    entry = _entry("api.run", {"type": "service_enabled", "name": "run.googleapis.com"})
    broken = FakeGcp({"https://serviceusage.googleapis.com/": RuntimeError("HTTP 500")})
    finding = _one(entry, broken)
    assert finding.status == UNKNOWN
    assert doctor.exit_code([finding]) == ExitCode.TROUBLE

    strange = _entry("other.strange", {"type": "no_such_probe"})
    assert _one(strange, FakeGcp({})).status == UNKNOWN

    ok = FakeGcp({"https://serviceusage.googleapis.com/": {"state": "ENABLED"}})
    met = _one(entry, ok)
    assert met.status == MET
    assert doctor.exit_code([met]) == ExitCode.OK
    assert doctor.exit_code([met, finding]) == ExitCode.TROUBLE


def test_unmet_names_the_fix_and_the_repair():
    entry = _entry("api.run", {"type": "service_enabled", "name": "run.googleapis.com"},
                   step="foundations.apis")
    finding = _one(entry, FakeGcp({"https://serviceusage.googleapis.com/": {"state": "DISABLED"}}))
    assert finding.status == UNMET
    assert finding.repair == "swarm install --repair api.run"
    assert "foundations.apis" in finding.fix and "swarm install --repair api.run" in finding.fix
    assert doctor.exit_code([finding]) == ExitCode.TROUBLE


def test_secret_absent_is_unmet_present_is_met():
    entry = _entry("secret.x", {"type": "secret_exists", "secret_id": "swarm-x"})
    url = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/swarm-x"
    assert _one(entry, FakeGcp({})).status == UNMET
    assert _one(entry, FakeGcp({url: {"name": "x"}})).status == MET


@pytest.mark.parametrize(("access", "cause"), [("CAN_ACCESS", "propagating"),
                                               ("CANNOT_ACCESS", "missing_grant")])
def test_propagation_told_apart_from_missing_grant(access, cause):
    entry = _entry("api.run", {"type": "service_enabled", "name": "run.googleapis.com"})
    gcp = FakeGcp({
        "https://serviceusage.googleapis.com/": doctor.Denied(
            "serviceusage.services.get", "//serviceusage.googleapis.com/projects/p/services/run"),
        "https://policytroubleshooter.googleapis.com/v3/iam:troubleshoot": {"overallAccessState": access},
    })
    finding = _one(entry, gcp)
    assert finding.status == UNKNOWN  # neither is fine
    assert finding.cause == cause
    if cause == "propagating":
        assert "propagating" in finding.detail and "again" in finding.detail
    else:
        assert "missing grant" in finding.detail and "serviceusage.services.get" in finding.detail
    assert ("POST", "https://policytroubleshooter.googleapis.com/v3/iam:troubleshoot") in gcp.calls


def test_troubleshooter_is_asked_about_the_bare_email():
    asked = []

    class Recording(FakeGcp):
        def post(self, url, body, *, permission, resource):
            asked.append(body["accessTuple"]["principal"])
            return {"overallAccessState": "CAN_ACCESS"}

    assert doctor.troubleshoot(Recording({}), "user:admin@example.test", "p.get", "//r") == "granted"
    assert asked == ["admin@example.test"]
    assert doctor.troubleshoot(Recording({}), "", "p.get", "//r") == "unknown"


def _jobs(count: int, finished_ago: timedelta | None) -> dict:
    done = None if finished_ago is None else (datetime.now(UTC) - finished_ago).isoformat()
    job = {"name": "j", "latestCreatedExecution": {"completionTime": done} if done else {}}
    return {"jobs": [job] * count}


@pytest.mark.parametrize(("count", "ago", "status"), [
    (10, timedelta(minutes=5), MET),
    (850, timedelta(minutes=5), UNMET),           # past the headroom
    (600, timedelta(days=2), UNMET),              # nothing deletes finished Jobs
    (100, timedelta(days=2), MET),                # kept, but far from the limit
])
def test_job_count_and_ttl(count, ago, status):
    entry = _entry("quota.cloud-run-jobs", {"type": "cloud_run_job_count", "region": "$R", "checks_ttl": True})
    gcp = FakeGcp({f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs":
                   _jobs(count, ago)})
    finding = _one(entry, gcp)
    assert finding.status == status, finding.detail
    assert f"{count} of 1000" in finding.detail
    assert "finished more than 60 min ago" in finding.detail


def _cluster(name="sw-autopilot", *, enabled=True, blocks=(), label=True) -> dict:
    return {
        "name": name,
        "resourceLabels": {"managed-by": "swarm-terraform"} if label else {},
        "masterAuthorizedNetworksConfig": {"enabled": enabled, "cidrBlocks": list(blocks)},
        "endpoint": "203.0.113.9",
        "masterAuth": {"clusterCaCertificate": ""},
    }


def test_authorised_networks_reported():
    entry = _entry("cluster.autopilot", {"type": "gke_authorized_networks", "location": "$R",
                                         "absent_display_name": "swarm-install"})
    url = f"https://container.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/clusters"
    office = {"displayName": "office", "cidrBlock": "198.51.100.0/24"}
    other_team = _cluster("peer-cluster", enabled=False, label=False)

    met = _one(entry, FakeGcp({url: {"clusters": [_cluster(blocks=[office]), other_team]}}))
    assert met.status == MET
    assert "office 198.51.100.0/24" in met.detail
    assert "peer-cluster" not in met.detail  # another team's cluster is never read

    left = {"displayName": "swarm-install", "cidrBlock": "192.0.2.7/32"}
    stale = _one(entry, FakeGcp({url: {"clusters": [_cluster(blocks=[office, left])]}}))
    assert stale.status == UNMET and "192.0.2.7/32" in stale.detail

    open_ = _one(entry, FakeGcp({url: {"clusters": [_cluster(enabled=False)]}}))
    assert open_.status == UNMET and "open to every address" in open_.detail


def test_registered_groups_reprobed_through_api():
    admins = _entry("workspace-group.admins", {"type": "groups_probe_via_api"}, provider="human-in-console")
    tenants = _entry("workspace-group.tenants", {"type": "groups_probe_via_api"}, provider="human-in-console")
    api = FakeApi([{"group": "swarm-admins@example.test", "state": "met"},
                   {"group": "eng@example.test", "state": "unmet", "reason": "unreadable"}])
    gcp = FakeGcp({})
    findings = doctor.run(_manifest(admins, tenants), _target(), doctor.Deps(gcp=gcp, api=api))
    assert [f.status for f in findings] == [UNMET, UNMET]
    assert "eng@example.test (unreadable)" in findings[0].detail
    assert api.calls == 1  # one probe per run, through swarm-api
    assert gcp.calls == []  # the doctor holds no Groups role and asks no Google API
    assert "opens its page" in findings[0].fix

    good = FakeApi([{"group": "swarm-admins@example.test", "state": "met"}])
    assert _one(admins, FakeGcp({}), api=good).status == MET
    assert _one(admins, FakeGcp({}), api=FakeApi([])).status == UNMET


def _rbac(namespace: str, users: list[str], verbs: list[str]) -> dict:
    base = "/apis/rbac.authorization.k8s.io/v1"
    return {
        f"{base}/namespaces/{namespace}/rolebindings": {"items": [{
            "subjects": [{"kind": "User", "name": u} for u in users],
            "roleRef": {"kind": "Role", "name": "dispatcher"},
        }]},
        f"{base}/namespaces/{namespace}/roles/dispatcher": {"rules": [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": verbs}]},
    }


_RBAC_ENTRY = {"type": "kubernetes_can_i", "subject": "swarm-reconciler", "verb": "list",
               "resource": "jobs.batch", "namespace": "swarm-tenant-$T"}
_RECONCILER = f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com"
_INSTALLER_BLOCK = {"displayName": "swarm-install", "cidrBlock": "198.51.100.7/32"}


def _rbac_gcp(cluster: Mapping[str, Any]) -> FakeGcp:
    return FakeGcp({
        f"https://container.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/clusters":
            {"clusters": [cluster]},
        f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/": {"uniqueId": "1234567890"},
    })


def _rbac_objects(users=(_RECONCILER, "1234567890"), verbs=("get", "list")) -> dict:
    objects = {"/api/v1/namespaces": {"items": [{"metadata": {"name": "swarm-tenant-eng"}},
                                                {"metadata": {"name": "kube-system"}}]}}
    objects.update(_rbac("swarm-tenant-eng", list(users), list(verbs)))
    return objects


def _rbac_finding(cluster: Mapping[str, Any], *, on_google: bool, objects=None) -> tuple[doctor.Finding, list]:
    built: list[FakeKube] = []

    def kube(found):
        built.append(FakeKube(objects if objects is not None else _rbac_objects(), found, on_google=on_google))
        return built[-1]

    entry = _entry("rbac.dispatcher-role", _RBAC_ENTRY, step="handoff.console")
    entry = entry.model_copy(update={"expand": "per_tenant",
                                     "refs": m.Refs(kubernetes=["rbac/dispatcher-rbac.yaml"])})
    return _one(entry, _rbac_gcp(cluster), kube=kube, on_google=on_google), built


@pytest.mark.parametrize(("users", "verbs", "status"), [
    ([_RECONCILER, "1234567890"], ["get", "list"], MET),
    ([_RECONCILER], ["get", "list"], UNMET),  # no uniqueId binding
    ([_RECONCILER, "1234567890"], ["get"], UNMET),
])
def test_reconciler_can_list_jobs(users, verbs, status):
    # The hourly job: on Google's runtime, at the private endpoint, after the close.
    finding, _ = _rbac_finding(_cluster(), on_google=True, objects=_rbac_objects(users, verbs))
    assert finding.status == status, finding.detail


def test_a_laptop_after_the_close_skips_the_cluster_probe_and_stays_green():
    # Audit 4c M2: infra-close took the installer's address out of the
    # authorised networks; a laptop's call would time out (the fake refuses it
    # as the network does), so the probe is not tried and the doctor is green.
    closed = _cluster(blocks=[{"displayName": "office", "cidrBlock": "203.0.113.0/24"}])
    finding, built = _rbac_finding(closed, on_google=False)
    assert finding.status == doctor.SKIPPED, finding.detail
    assert "closed after install" in finding.detail and "hourly doctor" in finding.detail
    assert all(k.calls == 0 for k in built), "a closed control plane is never called from off Google"
    met = _one(_entry("api.run", {"type": "service_enabled", "name": "run.googleapis.com"}),
               FakeGcp({"https://serviceusage.googleapis.com/": {"state": "ENABLED"}}))
    assert doctor.exit_code([met, finding]) == ExitCode.OK
    assert doctor.exit_code([finding]) == ExitCode.TROUBLE, "all skipped is nothing checked, never 0"
    said: list[str] = []
    doctor.render([met, finding], 0, say=said.append)
    assert not any("swarm install --repair" in line for line in said)
    assert "1 met, 0 unmet, 0 unknown, 1 skipped of 2 probed entries" in said[-1]


def test_a_laptop_while_the_installer_is_admitted_still_probes_the_cluster():
    finding, built = _rbac_finding(_cluster(blocks=[_INSTALLER_BLOCK]), on_google=False)
    assert finding.status == MET, finding.detail
    assert built and built[0].calls > 0


def test_the_fake_cluster_refuses_a_laptop_after_the_close():
    # The fake refuses what the real network refuses, so a probe that called
    # the closed control plane from off Google could not pass by accident.
    kube = FakeKube(_rbac_objects(), _cluster(), on_google=False)
    with pytest.raises(urllib.error.URLError):
        kube.get("/api/v1/namespaces")


def test_a_per_tenant_entry_prints_the_team_repair_for_that_team():
    # Audit 4c M2: rbac.dispatcher-role's step is the console hand-off, which
    # only sees Admin › Setup finished again, so no `swarm install --repair`.
    # DAYONE-TEAM-REPAIR: the team whose namespace lacks the RBAC is named, with
    # the command that re-applies its tenant render.
    finding, _ = _rbac_finding(_cluster(), on_google=True, objects=_rbac_objects(users=[_RECONCILER]))
    assert finding.status == UNMET
    command = f"swarm team repair eng --project {PROJECT}"
    assert finding.repair == command
    assert finding.to_json()["repair"] == command
    assert "swarm install --repair" not in finding.fix
    assert command in finding.fix
    said: list[str] = []
    doctor.render([finding], 0, say=said.append)
    assert any(command in line for line in said)


def test_a_met_per_tenant_entry_names_no_team():
    finding, _ = _rbac_finding(_cluster(), on_google=True)
    assert finding.status == MET and finding.repair is None


def test_the_real_manifest_dispatcher_entry_names_the_team_repair():
    entry = m.load().by_id()["rbac.dispatcher-role"]
    assert entry.probe is not None and entry.probe.type == "kubernetes_can_i"
    assert doctor.team_repairable(entry)
    words = doctor.fix_words(entry)
    assert "swarm install --repair rbac.dispatcher-role" not in words
    assert "swarm team repair <team> --project <project>" in words
    assert "swarm team repair" in (entry.apply or "")


def test_a_verify_check_says_repair_only_rechecks_it():
    """Epic finding 66: stage 14's checks apply nothing, so the fix never
    promises that `--repair` makes them right; it names what does."""
    entries = [e for e in m.load().entries if str(e.step or "").startswith("verify.") and e.step != "verify.end-to-end"]
    assert {e.step for e in entries} >= {"verify.digests", "verify.scheduler-targets"}
    for entry in entries:
        words = doctor.fix_words(entry)
        assert "re-runs the check and changes nothing" in words, entry.id
        assert doctor.CHECK_ONLY[str(entry.step)] in words
    words = doctor.fix_words(m.load().by_id()["other.verify-digests"])
    assert "makes it: swarm install --repair other.verify-digests" not in words
    # A plain `swarm install` skips a met pass (its check compares input digests, never live
    # traffic, digests or targets), so the fix for drift is the repair of an entry an infra pass
    # owns through `step:`, which runs that root's latest applied pass again.
    by_id = m.load().by_id()
    for step in ("verify.digests", "verify.scheduler-targets", "verify.plugins"):
        words = doctor.CHECK_ONLY[step]
        assert "swarm install re-runs" not in words, step
        found = re.search(r"swarm install --repair (\S+) ", words)
        assert found, step
        owner = by_id[found.group(1)]
        assert str(owner.step or "").startswith("infra.pass-") and owner.expand != doctor.PLATFORM_MADE, step
        assert f"(manifest step: {owner.step})" in words, step
    # On the release it already runs, swarm upgrade does nothing: it is no fix for either check.
    for step in ("verify.digests", "verify.firestore-migrations"):
        assert "swarm upgrade --to <the running release>" not in doctor.CHECK_ONLY[step]
        assert "the release it already runs does nothing" in doctor.CHECK_ONLY[step]


def test_check_only_names_every_verify_step_that_applies_nothing():
    """`CHECK_ONLY` is exactly the verify steps that keep the base no-op apply."""
    from swarm_install.steps import verify

    no_apply = {cls.id for cls in vars(verify).values()
                if isinstance(cls, type) and issubclass(cls, verify._Base) and cls is not verify._Base
                and getattr(cls, "id", "").startswith("verify.") and cls.apply is verify._Base.apply}
    assert no_apply == set(doctor.CHECK_ONLY)


def test_installer_network_name_matches_the_tfvars_one():
    from swarm_install import tfvars

    assert doctor.INSTALLER_NETWORK == tfvars.CLUSTER_REACH_NAME


def test_desktop_client_in_programmatic_allowlist():
    entry = _entry("iap-setting.frontend-programmatic-clients",
                   {"type": "iap_programmatic_clients", "backends": "every_frontend_backend",
                    "client_output": "desktop_client_id"}, step="bootstrap.pass-2")
    client = "123-desktop.apps.googleusercontent.com"
    backends = {"items": [
        {"name": "swarm-ui-backend", "iap": {"enabled": True}},
        {"name": "swarm-api-backend", "iap": {"enabled": True}},
        {"name": "their-backend", "iap": {"enabled": True}},  # another team's: never read
        {"name": "swarm-discovery-backend", "iap": {"enabled": False}},
    ]}
    iap = "https://iap.googleapis.com/v1/projects/42/iap_web/compute/services/"

    def gcp(api_clients):
        return FakeGcp({
            f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/global/backendServices": backends,
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}": {"projectNumber": "42"},
            f"{iap}swarm-ui-backend": {"accessSettings": {"oauthSettings": {"programmaticClients": [client]}}},
            f"{iap}swarm-api-backend": {"accessSettings": {"oauthSettings": {"programmaticClients": api_clients}}},
        })

    met = _one(entry, gcp([client]), outputs={"desktop_client_id": client})
    assert met.status == MET
    missing = gcp([])
    unmet = _one(entry, missing, outputs={"desktop_client_id": client})
    assert unmet.status == UNMET and "swarm-api-backend" in unmet.detail
    assert unmet.repair == "swarm install --repair iap-setting.frontend-programmatic-clients"
    assert not any("their-backend" in url for _, url in missing.calls)
    # Epic finding 46: no step records `frontend_iap_backends`; a value there
    # never widens the read to another team's backend.
    widened = gcp([client])
    _one(entry, widened, outputs={"desktop_client_id": client, "frontend_iap_backends": ["their-backend"]})
    assert not any("their-backend" in url for _, url in widened.calls)
    unknown = _one(entry, gcp([client]))
    assert unknown.status == UNKNOWN  # no client recorded: never fine
    # Issue #2964: the console's Admin › Setup step Sign-in client saves the
    # client and the hand-off wait (stage 13) records it; stage 2 no longer does.
    assert "Sign-in client" in unknown.detail and "stage 2" not in unknown.detail


def test_every_manifest_probe_type_has_a_runner():
    manifest = m.load()
    types = {e.probe.type for e in doctor.probed(manifest)}
    assert types, "the manifest carries no probe"
    assert types <= set(doctor.PROBES), sorted(types - set(doctor.PROBES))


# -- the verb -------------------------------------------------------------------


def _state() -> InstallState:
    return InstallState(project=PROJECT, environment="prod", region=REGION,
                        outputs={"desktop_client_id": "c"})


@pytest.fixture
def verb(monkeypatch):
    entry = _entry("api.run", {"type": "service_enabled", "name": "run.googleapis.com"},
                   step="foundations.apis")
    seen: dict[str, Any] = {"gcp": FakeGcp({"https://serviceusage.googleapis.com/": {"state": "ENABLED"},
                                            **_plugin_answers(), **_bundle_answers()})}
    monkeypatch.setattr(doctor_verb, "client_tier", lambda: None)
    monkeypatch.setattr(doctor_verb, "read_state", lambda project: (_state(), "gs://b/o"))
    monkeypatch.setattr(doctor_verb, "load_manifest", lambda path: _manifest(entry))
    monkeypatch.setattr(doctor_verb, "principal", lambda: "user:admin@example.test")
    monkeypatch.setattr(doctor_verb, "make_deps", lambda state: doctor.Deps(gcp=seen["gcp"]))
    return seen


def test_swarm_doctor_prints_each_entry_and_says_the_hourly_doctor_is_off(verb, capsys):
    from swarm_install import cli

    code = cli.main(["doctor", "--project", PROJECT])
    out = capsys.readouterr().out
    assert code == ExitCode.OK, out
    assert "met" in out and "api.run" in out
    assert "the hourly doctor is off" in out and "enable_scheduled_doctor" in out


def test_swarm_doctor_takes_the_region_from_the_state_only(verb, monkeypatch, capsys):
    """Epic finding 46: nothing writes an output named `region` (stage 0 sets
    `state.region`), so the doctor never falls back to one."""
    from swarm_install import cli

    state = _state()
    state.region = None
    state.outputs["region"] = "elsewhere1"
    monkeypatch.setattr(doctor_verb, "read_state", lambda project: (state, "gs://b/o"))
    seen: list[str] = []
    real = doctor.run
    monkeypatch.setattr(doctor, "run", lambda manifest, target, deps: seen.append(target.region) or real(
        manifest, target, deps))
    cli.main(["doctor", "--project", PROJECT])
    capsys.readouterr()
    assert seen == [""]


def test_swarm_doctor_json_and_exit_3_on_unmet(verb, capsys):
    from swarm_install import cli

    verb["gcp"] = FakeGcp({"https://serviceusage.googleapis.com/": {"state": "DISABLED"}, **_plugin_answers(),
                           **_bundle_answers()})
    code = cli.main(["doctor", "--project", PROJECT, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == ExitCode.TROUBLE
    assert doc["exit_code"] == 3
    assert doc["checks"][0]["repair"] == "swarm install --repair api.run"


def test_swarm_doctor_without_install_state_is_not_fine(verb, monkeypatch, capsys):
    from swarm_install import cli

    monkeypatch.setattr(doctor_verb, "read_state", lambda project: (None, "no install state"))
    code = cli.main(["doctor", "--project", PROJECT])
    assert code == ExitCode.FAIL
    assert "platform tier did not run" in capsys.readouterr().err


# -- the managed plugins' infrastructure (managed-plugins.md §15, lane 3) ------------

PLUGINS_BUCKET = f"{PROJECT}-swarm-plugins"
STORAGE_B = "https://storage.googleapis.com/storage/v1/b"
INTAKE_JOB = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/swarm-plugin-intake"
PLATFORM_ARM = f'resource.name.startsWith("projects/_/buckets/{PLUGINS_BUCKET}/objects/platform/")'
TENANT_READER = f"serviceAccount:swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"


def _plugin_policy(*bindings: Mapping[str, Any]) -> dict[str, Any]:
    return {"version": 3, "bindings": list(bindings)}


def _tenant_read(expression: str = PLATFORM_ARM + f' || resource.name.startsWith("projects/_/buckets/'
                 f'{PLUGINS_BUCKET}/objects/tenants/eng/")', member: str = TENANT_READER,
                 role: str = "roles/storage.objectViewer") -> dict[str, Any]:
    return {"role": role, "members": [member], "condition": {"title": "t", "expression": expression}}


def _plugin_answers(policy: Mapping[str, Any] | None = None, *, bucket: bool = True,
                    job: bool = True) -> dict[str, Any]:
    answers: dict[str, Any] = {
        f"{STORAGE_B}/{PLUGINS_BUCKET}/iam": dict(policy if policy is not None else _plugin_policy(_tenant_read())),
    }
    if bucket:
        answers[f"{STORAGE_B}/{PLUGINS_BUCKET}"] = {"name": PLUGINS_BUCKET}
    if job:
        answers[INTAKE_JOB] = {"name": INTAKE_JOB.split("/v2/", 1)[1]}
    return answers


def test_plugins_met_when_bucket_job_and_a_tenant_read_of_platform_exist():
    finding = doctor_verb.plugins_finding(_target(), FakeGcp(_plugin_answers()))
    assert finding.id == doctor_verb.PLUGINS_CHECK
    assert finding.status == MET, finding.detail
    assert PLUGINS_BUCKET in finding.detail and "swarm-agent-worker-eng" in finding.detail


@pytest.mark.parametrize(("answers", "said"), [
    (_plugin_answers(bucket=False), f"gs://{PLUGINS_BUCKET} does not exist"),
    (_plugin_answers(job=False), "swarm-plugin-intake is not deployed"),
    # a tenant reads its own prefix only, not platform/
    (_plugin_answers(_plugin_policy(_tenant_read(
        f'resource.name.startsWith("projects/_/buckets/{PLUGINS_BUCKET}/objects/tenants/eng/")'))),
     "no tenant worker account may read platform/"),
    # platform/ of another bucket
    (_plugin_answers(_plugin_policy(_tenant_read(PLATFORM_ARM.replace(PLUGINS_BUCKET, "other-bucket")))),
     "no tenant worker account may read platform/"),
    # platform/ narrowed by a second clause is not a read of platform/
    (_plugin_answers(_plugin_policy(_tenant_read(PLATFORM_ARM + " && request.time < timestamp(\"2020-01-01T00:00:00Z\")"))),
     "no tenant worker account may read platform/"),
    # a platform account reads platform/, but no tenant does
    (_plugin_answers(_plugin_policy(_tenant_read(member=f"serviceAccount:swarm-api@{PROJECT}.iam.gserviceaccount.com"))),
     "no tenant worker account may read platform/"),
    # a role with no object read
    (_plugin_answers(_plugin_policy(_tenant_read(role="roles/storage.objectCreator"))),
     "no tenant worker account may read platform/"),
])
def test_plugins_unmet_names_what_is_missing(answers, said):
    finding = doctor_verb.plugins_finding(_target(), FakeGcp(answers))
    assert finding.status == UNMET
    assert said in finding.detail
    assert "swarm install" in finding.fix


def test_plugins_read_the_bucket_infra_names():
    """Epic finding 46: nothing writes `plugins_bucket`, so the doctor reads the
    bucket terraform/infra names, whatever an output of that name says."""
    answers = _plugin_answers(_plugin_policy(_tenant_read(PLATFORM_ARM)))
    finding = doctor_verb.plugins_finding(_target(plugins_bucket="acme-swarm-plugins"), FakeGcp(answers))
    assert finding.status == MET, finding.detail
    assert doctor_verb.plugins_bucket(PROJECT) == f"{PROJECT}-swarm-plugins"


def test_plugins_unknown_when_a_read_is_refused():
    answers = _plugin_answers()
    answers[INTAKE_JOB] = doctor.Denied("run.jobs.get", INTAKE_JOB)
    finding = doctor_verb.plugins_finding(_target(), FakeGcp(answers))
    assert finding.status == UNKNOWN
    assert "run.jobs.get" in finding.detail


def test_swarm_doctor_checks_the_plugins_infrastructure(verb, capsys):
    from swarm_install import cli

    code = cli.main(["doctor", "--project", PROJECT])
    out = capsys.readouterr().out
    assert code == ExitCode.OK, out
    assert doctor_verb.PLUGINS_CHECK in out

    verb["gcp"].answers.pop(INTAKE_JOB)
    code = cli.main(["doctor", "--project", PROJECT, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == ExitCode.TROUBLE
    found = [c for c in doc["checks"] if c["id"] == doctor_verb.PLUGINS_CHECK]
    assert found and found[0]["status"] == UNMET and "swarm-plugin-intake" in found[0]["detail"]


# -- the bundle build (managed-plugins.md §1.4.2 and §15.2) -------------------------

BUNDLER = f"swarm-plugin-bundler@{PROJECT}.iam.gserviceaccount.com"
BUNDLER_ACCOUNT_URL = f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{BUNDLER}"
REPOSITORIES_URL = f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/repositories"


def _push(role: str = "roles/artifactregistry.writer", member: str = f"serviceAccount:{BUNDLER}") -> dict[str, Any]:
    return {"role": role, "members": [member]}


def _bundle_answers(own: Mapping[str, Any] | None = None, *, account: bool = True, repository: bool = True,
                    **read_only: Mapping[str, Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    if account:
        answers[BUNDLER_ACCOUNT_URL] = {"email": BUNDLER}
    if repository:
        answers[f"{REPOSITORIES_URL}/swarm-plugin-bundles:getIamPolicy"] = dict(
            own if own is not None else {"bindings": [_push()]})
    for name in doctor_verb.BUNDLER_READ_ONLY_REPOSITORIES:
        reader = {"bindings": [_push("roles/artifactregistry.reader")]}
        answers[f"{REPOSITORIES_URL}/{name}:getIamPolicy"] = dict(read_only.get(name.replace("-", "_"), reader))
    return answers


def test_bundle_build_met_when_the_builder_pushes_to_its_repository_only():
    gcp = FakeGcp(_bundle_answers())
    finding = doctor_verb.bundles_finding(_target(builtins_managed="on"), gcp)
    assert finding.id == doctor_verb.BUNDLES_CHECK
    assert finding.status == MET, finding.detail
    assert BUNDLER in finding.detail and "swarm-plugin-bundles only" in finding.detail
    assert all(method == "GET" for method, _ in gcp.calls), "the check only reads"


def test_bundle_build_switch_off_reads_nothing_and_is_met():
    gcp = FakeGcp({})
    for outputs in ({"builtins_managed": "off"}, {}):
        finding = doctor_verb.bundles_finding(_target(**outputs), gcp)
        assert finding.status == MET and "builtins_managed is off" in finding.detail
    assert gcp.calls == []
    # The doctor names the key the infra pass records (it loads no tfvars).
    from swarm_install import tfvars

    assert doctor_verb.BUILTINS_MANAGED_KEY == tfvars.BUILTINS_MANAGED_KEY


@pytest.mark.parametrize(("answers", "said"), [
    (_bundle_answers(account=False), f"{BUNDLER} does not exist"),
    (_bundle_answers(repository=False), "swarm-plugin-bundles repository does not exist"),
    (_bundle_answers({"bindings": [_push("roles/artifactregistry.reader")]}), "may not push to swarm-plugin-bundles"),
    # another account's push is not the builder's
    (_bundle_answers({"bindings": [_push(member=f"serviceAccount:swarm-api@{PROJECT}.iam.gserviceaccount.com")]}),
     "may not push to swarm-plugin-bundles"),
    (_bundle_answers(swarm_images={"bindings": [_push()]}), "may also push to swarm-images"),
    (_bundle_answers(swarm_mirror_npm={"bindings": [_push("roles/artifactregistry.repoAdmin")]}),
     "may also push to swarm-mirror-npm"),
])
def test_bundle_build_unmet_names_what_is_wrong(answers, said):
    finding = doctor_verb.bundles_finding(_target(builtins_managed="on"), FakeGcp(answers))
    assert finding.status == UNMET
    assert said in finding.detail
    assert "swarm install" in finding.fix


def test_bundle_build_unknown_when_a_read_is_refused():
    answers = _bundle_answers()
    answers[BUNDLER_ACCOUNT_URL] = doctor.Denied("iam.serviceAccounts.get", BUNDLER)
    finding = doctor_verb.bundles_finding(_target(builtins_managed="on"), FakeGcp(answers))
    assert finding.status == UNKNOWN and "iam.serviceAccounts.get" in finding.detail


def test_swarm_doctor_checks_the_bundle_build_infra_applied(verb, monkeypatch, capsys):
    from swarm_install import cli

    applied = _state()
    applied.outputs["builtins_managed"] = "on"
    monkeypatch.setattr(doctor_verb, "read_state", lambda project: (applied, "gs://b/o"))
    code = cli.main(["doctor", "--project", PROJECT])
    out = capsys.readouterr().out
    assert code == ExitCode.OK, out
    assert doctor_verb.BUNDLES_CHECK in out

    verb["gcp"].answers.pop(BUNDLER_ACCOUNT_URL)
    code = cli.main(["doctor", "--project", PROJECT, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == ExitCode.TROUBLE
    found = [c for c in doc["checks"] if c["id"] == doctor_verb.BUNDLES_CHECK]
    assert found and found[0]["status"] == UNMET and BUNDLER in found[0]["detail"]

    # Nothing recorded: no infra pass planned plugin-bundles.tf, nothing to find.
    monkeypatch.setattr(doctor_verb, "read_state", lambda project: (_state(), "gs://b/o"))
    code = cli.main(["doctor", "--project", PROJECT, "--json"])
    doc = json.loads(capsys.readouterr().out)
    found = [c for c in doc["checks"] if c["id"] == doctor_verb.BUNDLES_CHECK]
    assert code == ExitCode.OK and found[0]["status"] == MET and "off" in found[0]["detail"]
