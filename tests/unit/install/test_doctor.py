"""`swarm doctor`'s platform tier (lane DAYONE-DOCTOR-1, part of #2688; design
§2.1-§2.2, plan §1.5).

No credentials and no emulator: Google's REST APIs, swarm-api and the cluster
are fakes that answer by URL.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from swarm_install import doctor
from swarm_install import manifest as m
from swarm_install.exitcodes import MET, UNKNOWN, UNMET, ExitCode
from swarm_install.state import InstallState
from swarm_install.verbs import doctor as doctor_verb

PROJECT = "proj-doctor"
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
    def __init__(self, objects: Mapping[str, Any]) -> None:
        self.objects = objects

    def get(self, path):
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
    other_team = _cluster("agents-staging", enabled=False, label=False)

    met = _one(entry, FakeGcp({url: {"clusters": [_cluster(blocks=[office]), other_team]}}))
    assert met.status == MET
    assert "office 198.51.100.0/24" in met.detail
    assert "agents-staging" not in met.detail  # another team's cluster is never read

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


@pytest.mark.parametrize(("users", "verbs", "status"), [
    (["swarm-reconciler@proj-doctor.iam.gserviceaccount.com", "1234567890"], ["get", "list"], MET),
    (["swarm-reconciler@proj-doctor.iam.gserviceaccount.com"], ["get", "list"], UNMET),  # no uniqueId binding
    (["swarm-reconciler@proj-doctor.iam.gserviceaccount.com", "1234567890"], ["get"], UNMET),
])
def test_reconciler_can_list_jobs(users, verbs, status):
    entry = _entry("rbac.dispatcher-role", {"type": "kubernetes_can_i", "subject": "swarm-reconciler",
                                            "verb": "list", "resource": "jobs.batch",
                                            "namespace": "swarm-tenant-$T"})
    gcp = FakeGcp({
        f"https://container.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/clusters":
            {"clusters": [_cluster()]},
        f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/": {"uniqueId": "1234567890"},
    })
    objects = {"/api/v1/namespaces": {"items": [{"metadata": {"name": "swarm-tenant-eng"}},
                                                {"metadata": {"name": "kube-system"}}]}}
    objects.update(_rbac("swarm-tenant-eng", users, verbs))
    finding = _one(entry, gcp, kube=lambda cluster: FakeKube(objects))
    assert finding.status == status, finding.detail


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
    assert _one(entry, gcp([client])).status == UNKNOWN  # no client recorded: never fine


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
    seen: dict[str, Any] = {"gcp": FakeGcp({"https://serviceusage.googleapis.com/": {"state": "ENABLED"}})}
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


def test_swarm_doctor_json_and_exit_3_on_unmet(verb, capsys):
    from swarm_install import cli

    verb["gcp"] = FakeGcp({"https://serviceusage.googleapis.com/": {"state": "DISABLED"}})
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
