"""The hourly doctor (`python -m swarm_install.scheduled_doctor`, the
`swarm-doctor` job's command; lane DAYONE-DOCTOR-1, part of #2688; D27).

No credentials and no emulator: Google's APIs and swarm-api are fakes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swarm_install import doctor
from swarm_install import manifest as m
from swarm_install import scheduled_doctor as sd
from swarm_install.exitcodes import ExitCode

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "proj-hourly"
ENV = {
    "PROJECT_ID": PROJECT,
    "REGION": "us-central1",
    "ENVIRONMENT": "prod",
    "API_URL": "https://swarm-api-abc-uc.a.run.app",
    "API_AUDIENCE": "https://swarm-api-abc-uc.a.run.app",
}


class FakeGcp:
    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def get(self, url, *, permission, resource):
        self.calls.append(("GET", url))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                return answer
        raise doctor.NotFound(url)

    def post(self, url, body, *, permission, resource):
        self.calls.append(("POST", url))
        raise doctor.NotFound(url)


def _manifest() -> m.Manifest:
    entries = [
        m.Entry(id="api.run", kind="gcp-api", scope="core", stage=1, provider="engine",
                probe=m.Probe(type="service_enabled", name="run.googleapis.com"), step="foundations.apis"),
        m.Entry(id="secret.x", kind="secret", scope="core", stage=3, provider="engine",
                probe=m.Probe(type="secret_exists", secret_id="swarm-x"), step="infra.pass-1"),
        m.Entry(id="workspace-group.admins", kind="workspace-group", scope="core", stage=8,
                provider="human-in-console", probe=m.Probe(type="groups_probe_via_api"),
                step="identity.admin-group"),
    ]
    return m.Manifest(entries=entries, ignored={}, env={}, sources={})


def _api(sent):
    def send(method, url, bearer, body):
        sent.append((method, url, bearer, body))
        if url.endswith("/groups-probe"):
            return 200, {"groups": [{"group": "swarm-admins@example.test", "state": "met"}], "met": True}
        return 200, {"run_at": body["run_at"], "checks": len(body["checks"]), "findings": len(body["findings"])}

    return sd.SwarmApi(base_url=ENV["API_URL"], audience=ENV["API_AUDIENCE"],
                       id_token=lambda audience: "id-token-for-" + audience, send=send)


def test_findings_posted_to_api_only():
    sent: list = []
    gcp = FakeGcp({
        "https://storage.googleapis.com/storage/v1/b/swarm-tfstate-proj-hourly/":
            {"outputs": {"desktop_client_id": "c"}},
        "https://serviceusage.googleapis.com/": {"state": "DISABLED"},
    })
    at = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    code = sd.main(ENV, gcp=gcp, api=_api(sent), kube=None, manifest=_manifest(), principal="", now=lambda: at)

    assert code == ExitCode.OK
    # Every call to swarm-api is one of its two doctor routes, on swarm-api's own URL.
    assert [(meth, url) for meth, url, _, _ in sent] == [
        ("POST", ENV["API_URL"] + sd.GROUPS_PROBE_ROUTE),
        ("PUT", ENV["API_URL"] + sd.HEALTH_ROUTE),
    ]
    assert all(bearer == "id-token-for-" + ENV["API_AUDIENCE"] for _, _, bearer, _ in sent)
    # Google's APIs are only read.
    assert {meth for meth, _ in gcp.calls} == {"GET"}
    body = sent[-1][3]
    assert body["run_at"] == at.isoformat()
    assert {c["id"]: c["status"] for c in body["checks"]} == {
        "api.run": "unmet", "secret.x": "unmet", "workspace-group.admins": "met"}
    assert [f["id"] for f in body["findings"]] == ["api.run", "secret.x"]
    assert body["findings"][0]["repair"] == "swarm install --repair api.run"


def test_a_route_other_than_the_two_is_refused():
    api = _api([])
    with pytest.raises(ValueError):
        api._call("POST", "/v1/tasks", {})


def test_post_failure_exits_1(capsys):
    def refused(method, url, bearer, body):
        return 403, {}

    api = sd.SwarmApi(base_url=ENV["API_URL"], audience=ENV["API_AUDIENCE"],
                      id_token=lambda audience: "t", send=refused)
    code = sd.main(ENV, gcp=FakeGcp({}), api=api, kube=None, manifest=_manifest(), principal="")
    assert code == ExitCode.FAIL
    assert "could not be posted" in capsys.readouterr().err


def test_missing_environment_is_a_usage_error(capsys):
    code = sd.main({k: v for k, v in ENV.items() if k != "API_URL"}, gcp=FakeGcp({}), manifest=_manifest())
    assert code == ExitCode.USAGE
    assert "API_URL" in capsys.readouterr().err


def test_job_command_is_this_module():
    # terraform/infra/doctor.tf runs `python -m swarm_install.scheduled_doctor`
    # in the swarm-verify image: the image carries the package and a `python`.
    doctor_tf = (ROOT / "terraform/infra/doctor.tf").read_text()
    assert 'args = ["python -m swarm_install.scheduled_doctor"]' in doctor_tf
    dockerfile = (ROOT / "images/swarm-verify/Dockerfile").read_text()
    assert re.search(r"^COPY apps/swarm-install/swarm_install/ ", dockerfile, re.M)
    assert re.search(r"^COPY install/ ", dockerfile, re.M)
    assert "/usr/bin/python" in dockerfile and "PYTHONPATH" in dockerfile
    # The image is built from the context cloudbuild.yaml assembles, so the
    # package and the manifest are copied into it too.
    context = (ROOT / "images/swarm-verify/cloudbuild.yaml").read_text()
    assert "cp -R apps/swarm-install/swarm_install /workspace/verify-ctx/apps/swarm-install/swarm_install" in context
    assert "cp -R install /workspace/verify-ctx/install" in context


def test_scheduled_doctor_stays_off_until_a_later_lane_turns_it_on():
    # Brief item 5: this lane does not edit tfvars.py; `swarm doctor` says the
    # hourly doctor is off while the swarm-doctor job does not exist.
    target = doctor.Target(project=PROJECT, environment="prod", region="us-central1")
    line = doctor.hourly_doctor(target, doctor.Deps(gcp=FakeGcp({})))
    assert "off" in line and "enable_scheduled_doctor" in line
    on = FakeGcp({f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/us-central1/jobs/swarm-doctor": {}})
    assert "is on" in doctor.hourly_doctor(target, doctor.Deps(gcp=on))
