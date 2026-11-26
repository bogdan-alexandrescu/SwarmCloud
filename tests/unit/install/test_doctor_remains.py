"""`swarm doctor --remains`: what an uninstalled project still holds (issue 344, gap 2).

A read-only listing, by kind and name, of what the install's project holds,
from list calls only. It says `Nothing remains in <project>.` when every kind
was read and none holds anything; a kind it could not read is reported as not
read, never left out, and then it never says nothing remains. A name on the
install's never-touch list is never printed, only counted.

No credentials, no emulator: Google is a fake under `preflight.Gcp`'s own
paging, which records every call's method.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Any

import pytest

from swarm_install import inventory
from swarm_install.exitcodes import ExitCode
from swarm_install.state import InstallState
from swarm_install.steps.preflight import ApiError, Gcp
from swarm_install.verbs import doctor as verb

PROJECT = "p-dayone-left"
#: A name the never-touch list holds: another team's bucket (generic).
PEER = "peer-team-bucket"


class FakeGcp(Gcp):
    """Answers by the end of the URL; records (method, url) of every call."""

    def __init__(self, answers: dict[str, Any] | None = None, refuse: dict[str, ApiError] | None = None,
                 project: dict[str, Any] | None = None) -> None:
        super().__init__(session=object())
        self.answers = dict(answers or {})
        self.refuse = dict(refuse or {})
        self.calls: list[tuple[str, str]] = []
        self.found_project = project if project is not None else {
            "projectId": PROJECT, "name": "projects/123456789012", "state": "ACTIVE"}

    def _call(self, method: str, url: str, *, body: Any = None, params: Any = None, missing_ok: bool = False,
              quota_project: str | None = None) -> Any:
        self.calls.append((method, url))
        if url.endswith("/v3/projects:search"):
            return {"projects": [self.found_project]} if self.found_project else {}
        for suffix, error in self.refuse.items():
            if url.endswith(suffix):
                raise error
        for suffix, answer in self.answers.items():
            if url.endswith(suffix):
                return answer
        return {}


def denied() -> ApiError:
    return ApiError(403, "PERMISSION_DENIED", "the caller does not have permission")


def api_off() -> ApiError:
    return ApiError(403, "PERMISSION_DENIED", "Kubernetes Engine API has not been used in project 1 before or it "
                                              "is disabled")


def holding() -> FakeGcp:
    return FakeGcp({
        "/b": {"items": [{"name": "swarm-artifacts-p"}, {"name": PEER}]},
        f"/projects/{PROJECT}/secrets": {"secrets": [{"name": f"projects/{PROJECT}/secrets/swarm-tenant-eng-git"}]},
        f"/projects/{PROJECT}/serviceAccounts": {"accounts": [
            {"email": f"swarm-api@{PROJECT}.iam.gserviceaccount.com"},
            {"email": "service-1@gcp-sa-example.iam.gserviceaccount.com"}]},
        f"/projects/{PROJECT}/roles": {"roles": [{"name": f"projects/{PROJECT}/roles/swarmWorker"}]},
    })


def state(**extra: Any) -> InstallState:
    return InstallState(project=PROJECT, environment="prod", region="europe-west1", **extra)


def run_verb(monkeypatch: pytest.MonkeyPatch, gcp: FakeGcp, *, st: InstallState | None = None,
             json_out: bool = False) -> tuple[int, str]:
    found = st if st is not None else state()
    monkeypatch.setattr(verb, "read_state", lambda project: (found, "the local copy"))
    monkeypatch.setattr(verb, "remains_gcp", lambda: gcp)
    parser = argparse.ArgumentParser()
    verb.add_arguments(parser)
    args = parser.parse_args(["--project", PROJECT, "--remains", *(["--json"] if json_out else [])])
    out = io.StringIO()
    with redirect_stdout(out):
        code = verb.run(args)
    return int(code), out.getvalue()


def test_the_listing_shows_each_resource_by_kind_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = run_verb(monkeypatch, holding())
    assert code == ExitCode.OK
    lines = out.splitlines()
    for kind, name in [("bucket", "swarm-artifacts-p"), ("secret", "swarm-tenant-eng-git"),
                       ("service-account", f"swarm-api@{PROJECT}.iam.gserviceaccount.com"),
                       ("custom-role", "swarmWorker")]:
        assert any(kind in line and name in line for line in lines), (kind, name, out)
    # Google's own service agents live under other domains and are not the project's to list.
    assert "gcp-sa-example" not in out
    assert "Nothing remains" not in out


def test_an_empty_project_says_nothing_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = run_verb(monkeypatch, FakeGcp())
    assert code == ExitCode.OK
    assert f"Nothing remains in {PROJECT}." in out


def test_an_unreadable_kind_is_reported_as_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    gcp = FakeGcp(refuse={f"/projects/{PROJECT}/secrets": denied()})
    code, out = run_verb(monkeypatch, gcp)
    assert code == ExitCode.TROUBLE
    assert "Nothing remains" not in out
    assert any("secret" in line and "not read" in line for line in out.splitlines()), out


def test_an_api_that_is_off_is_an_empty_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    gcp = FakeGcp(refuse={"/locations/-/clusters": api_off()})
    code, out = run_verb(monkeypatch, gcp)
    assert code == ExitCode.OK
    assert f"Nothing remains in {PROJECT}." in out


@pytest.mark.parametrize("kind,suffix", [
    ("kms-key-ring", "/keyRings"), ("firestore-database", "/databases"), ("firestore-backup", "/backups"),
    ("secret", "/secrets"), ("pubsub-topic", "/topics"), ("pubsub-subscription", "/subscriptions"),
    ("registry", "/repositories"), ("dns-zone", "/managedZones"), ("logging-bucket", "/buckets"),
    ("log-metric", "/metrics"), ("alert-policy", "/alertPolicies"),
    ("notification-channel", "/notificationChannels"), ("cloud-run-service", "/services"),
    ("cloud-run-job", "/locations/europe-west1/jobs"), ("network", "/global/networks"),
])
def test_an_api_that_is_off_is_not_read_never_empty(monkeypatch: pytest.MonkeyPatch, kind: str, suffix: str) -> None:
    """Turning an API off deletes nothing: the kind is `not read`, exit 3."""
    disabled = ApiError(403, "SERVICE_DISABLED", "API has not been used in project 1 before or it is disabled")

    class Off(FakeGcp):
        def _call(self, method: str, url: str, **kw: Any) -> Any:
            if url.split("?")[0].endswith(suffix) and not url.endswith("/v3/projects:search"):
                raise disabled
            return super()._call(method, url, **kw)

    code, out = run_verb(monkeypatch, Off(), json_out=True)
    found = json.loads(out)
    assert code == ExitCode.TROUBLE, (kind, out)
    assert kind in [u["kind"] for u in found["unread"]], (kind, out)
    assert found["complete"] is False


def test_it_only_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    gcp = holding()
    run_verb(monkeypatch, gcp)
    assert gcp.calls, "the listing made no call at all"
    assert {method for method, _ in gcp.calls} == {"GET"}


def test_a_never_touch_name_is_counted_not_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    st = state(deny_list=[PEER])
    code, out = run_verb(monkeypatch, holding(), st=st)
    assert PEER not in out
    assert "never-touch" in out
    code, out = run_verb(monkeypatch, holding(), st=st, json_out=True)
    found = json.loads(out)
    assert PEER not in out
    assert found["never_touch_not_named"] == 1


def test_the_json_document(monkeypatch: pytest.MonkeyPatch) -> None:
    gcp = FakeGcp(refuse={f"/projects/{PROJECT}/secrets": denied()})
    code, out = run_verb(monkeypatch, gcp, json_out=True)
    found = json.loads(out)
    assert code == ExitCode.TROUBLE
    assert found["project"] == PROJECT and found["complete"] is False and found["nothing_remains"] is False
    assert [u["kind"] for u in found["unread"]] == ["secret"]


def test_a_project_pending_deletion_is_said_plainly() -> None:
    gcp = FakeGcp(project={"projectId": PROJECT, "name": "projects/1", "state": "DELETE_REQUESTED"})
    found = inventory.read(gcp, PROJECT, region="europe-west1", deny_list=[])
    text = "\n".join(inventory.render(found))
    assert "pending deletion" in text
    assert [c for c in gcp.calls if "projects:search" not in c[1]] == []


def test_a_project_nobody_can_see_is_not_called_empty() -> None:
    gcp = FakeGcp(project={})
    found = inventory.read(gcp, PROJECT, region="europe-west1", deny_list=[])
    text = "\n".join(inventory.render(found))
    assert "Nothing remains" not in text
    assert not found.complete
