"""Stage 2 checks only what a customer can read (DAYONE-EDGE-PROBES, part of
#2688; audit findings 7, 9 and 25).

* #7: the IAP brands API is shut down (2026-03-19). `edge.brand` never calls
  it; the brand is met once a Desktop client id is recorded, because the
  console refuses to create a client before Branding and Audience exist.
* #9: the IAP service agent lives in a Google-owned project. `edge.iap-agent`
  never GETs its account: `generateServiceIdentity` is the apply and the
  proof, and the email it answers is recorded.
* #25: when one of the project's public Cloud DNS zones covers the hostname,
  `edge.dns` creates the A record after one typed approval; otherwise it
  prints it and opens the page. It polls Google Public DNS, never the local
  resolver.

On fake Google clients with no credentials and no network.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_install import adopt, remains
from swarm_install import engine as engine_module
from swarm_install.exitcodes import ExitCode
from swarm_install.steps import edge
from swarm_install.steps.preflight import ApiError, Gcp

from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, Rig, ctx_for

HOST = "swarm.example.com"
IP = "203.0.113.7"
CLIENT_ID = "123456789012-abc123def456.apps.googleusercontent.com"
AGENT = f"service-{NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
ZONE = {"name": "example-com", "dnsName": "example.com.", "visibility": "public"}


class FakeEdge:
    """`edge.EdgeApi` on dictionaries: Cloud DNS zones and records, Google
    Public DNS answers and Service Usage's generateServiceIdentity. Every call
    is in `calls`; a write is also in `writes`."""

    def __init__(self, clock: Any = None) -> None:
        self.clock = clock
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.zones: list[dict[str, Any]] = []
        self.zones_error: ApiError | None = None
        self.records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.answers: dict[str, set[str] | None] = {}
        #: Seconds after a Cloud DNS change before Google Public DNS answers it.
        self.propagation_s = 60.0
        self._landed: dict[str, tuple[float, set[str]]] = {}
        self.identity: list[dict[str, Any]] = [
            {"name": "operations/finished.du1", "done": True,
             "response": {"@type": "type.googleapis.com/google.api.serviceusage.v1beta1.ServiceIdentity",
                          "email": AGENT, "uniqueId": "1"}}]

    def _now(self) -> float:
        return self.clock.now() if self.clock is not None else 0.0

    def managed_zones(self, project):
        self.calls.append(("managed_zones", (project,)))
        if self.zones_error is not None:
            raise self.zones_error
        return list(self.zones)

    def a_records(self, project, zone, name):
        self.calls.append(("a_records", (project, zone, name)))
        return list(self.records.get((zone, name), []))

    def change_records(self, project, zone, change):
        self.calls.append(("change_records", (project, zone)))
        self.writes.append(("change_records", (project, zone, change)))
        for added in change["additions"]:
            self.records[(zone, added["name"])] = [added]
            self._landed[added["name"].rstrip(".")] = (self._now() + self.propagation_s, set(added["rrdatas"]))
        return {"status": "pending"}

    def generate_service_identity(self, project, service):
        self.calls.append(("generate_service_identity", (project, service)))
        self.writes.append(("generate_service_identity", (project, service)))
        return self.identity.pop(0) if len(self.identity) > 1 else dict(self.identity[0])

    def operation(self, name):
        self.calls.append(("operation", (name,)))
        return self.identity.pop(0) if len(self.identity) > 1 else dict(self.identity[0])

    def resolve(self, hostname):
        self.calls.append(("resolve", (hostname,)))
        landed = self._landed.get(hostname)
        if landed is not None and self._now() >= landed[0]:
            return set(landed[1])
        found = self.answers.get(hostname, set())
        return None if found is None else set(found)

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]


def _rig(tmp_path: Path, **outputs: Any) -> Rig:
    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "hostname", HOST),
                                s.outputs.update({"project_number": NUMBER, "global_ip": IP, **outputs})))
    return rig


def _pages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    opened: list[tuple[str, str]] = []
    for module in (engine_module, edge):
        monkeypatch.setattr(module, "open_page", lambda url, *, why, **_: opened.append((url, why)))
    return opened


# -- #7: the OAuth brand is never probed ---------------------------------------------


def test_brand_never_calls_the_brands_api_and_is_met_by_the_desktop_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The brand is a wait met by the console's Sign-in client step (issue
    #2964): the console links Branding and Audience, so this terminal opens
    no page and asks nothing; the saved Desktop client is the brand's proof."""
    from .test_sign_in_client_wait import Console, NeverAsk

    opened = _pages(monkeypatch)
    gcp = FakeGcp()
    gcp.iap_brands = lambda number: pytest.fail("the IAP brands API is shut down; it is never called")
    rig = _rig(tmp_path)
    step = edge.BrandStep(lambda: gcp, NeverAsk(), lambda: FakeEdge())
    engine = rig.engine([step])
    engine.record = Console(sign_in_client={"client_id": CLIENT_ID, "at": "2026-11-01T10:00:00Z",
                                            "by": "root@example.com"})
    result = engine.run()
    assert result.exit_code == ExitCode.OK, result.message
    assert opened == [] and gcp.mutations == []
    plan = step.plan(SimpleNamespace(project=PROJECT))
    assert any("(Internal)" in a for a in plan.actions), "the plan says Audience is Internal"
    assert any(f"auth/branding?project={PROJECT}" in a for a in plan.actions)

    again = rig.engine([edge.BrandStep(lambda: gcp, NeverAsk(), lambda: FakeEdge())])
    again.record = Console()
    assert rig.store.load().outputs.get("desktop_client_id") is None
    rig.store.update(lambda s: s.outputs.update({"desktop_client_id": CLIENT_ID}))
    assert again.run().exit_code == ExitCode.OK and opened == [], "a recorded Desktop client is the brand's proof"


def test_brand_refuses_what_is_not_a_desktop_client_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record that is not a Desktop client id meets nothing: the wait goes on."""
    from .test_sign_in_client_wait import Console, NeverAsk

    _pages(monkeypatch)
    rig = _rig(tmp_path, desktop_client_id="not-a-client")
    step = edge.BrandStep(lambda: FakeGcp(), NeverAsk(), lambda: FakeEdge())
    engine = rig.engine([step])
    engine.record = Console(sign_in_client={"client_id": "not-a-client", "at": "x", "by": "y"})
    result = engine.run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert rig.of_kind("wait_countdown", "edge.brand")


def test_adopt_and_remains_never_name_the_brands_api() -> None:
    """adopt.py probes no brand and lists no OAuth client (both APIs are shut
    down); the Desktop client is adopted from the record."""
    assert not hasattr(adopt, "brand")
    assert not hasattr(adopt.InstallGcp, "oauth_clients")
    for module in (adopt, remains, edge):
        source = inspect.getsource(module)
        assert "iap_brands" not in source and "/brands" not in source, module.__name__
        assert "identityAwareProxyClients" not in source, module.__name__
    assert adopt.desktop_client({"desktop_client_id": CLIENT_ID}) == CLIENT_ID
    assert adopt.desktop_client({"desktop_client_id": "x"}) is None
    assert adopt.desktop_client(None) is None


# -- #9: generateServiceIdentity is the apply and the proof ---------------------------


def test_iap_agent_records_the_email_generate_service_identity_answers(tmp_path: Path) -> None:
    gcp = FakeGcp()
    gcp.service_account = lambda email: pytest.fail("the service agent's account is never read")
    rig = _rig(tmp_path)
    fake = FakeEdge()
    fake.identity = [{"name": "operations/x", "done": True,
                      "response": {"email": "service-777@gcp-sa-iap.iam.gserviceaccount.com"}}]
    step = edge.IapAgentStep(lambda: gcp, Answers(), lambda: fake)

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.writes == [("generate_service_identity", (PROJECT, "iap.googleapis.com"))]
    assert rig.store.load().outputs["iap_service_agent"] == "service-777@gcp-sa-iap.iam.gserviceaccount.com"

    again = rig.engine([edge.IapAgentStep(lambda: gcp, Answers(), lambda: fake)]).run()
    assert again.exit_code == ExitCode.OK
    assert len(fake.writes) == 1, "a recorded email is met: apply does not run again"


def test_iap_agent_polls_an_unfinished_operation(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    fake = FakeEdge()
    fake.identity = [{"name": "operations/pending", "done": False},
                     {"name": "operations/pending", "done": False},
                     {"name": "operations/pending", "done": True, "response": {"email": AGENT}}]
    result = rig.engine([edge.IapAgentStep(lambda: FakeGcp(), Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.called("operation") == [("operations/pending",), ("operations/pending",)]
    assert rig.store.load().outputs["iap_service_agent"] == AGENT


def test_iap_agent_with_no_email_in_the_answer_records_the_documented_address(tmp_path: Path) -> None:
    """Whichever way Google answers: a finished operation with no email
    records the agent's documented address, service-N@gcp-sa-iap."""
    rig = _rig(tmp_path)
    fake = FakeEdge()
    fake.identity = [{"name": "operations/x", "done": True}]
    result = rig.engine([edge.IapAgentStep(lambda: FakeGcp(), Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.store.load().outputs["iap_service_agent"] == AGENT


def test_iap_agent_failed_operation_fails_the_step(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    fake = FakeEdge()
    fake.identity = [{"name": "operations/x", "done": True, "error": {"code": 7, "message": "denied"}}]
    result = rig.engine([edge.IapAgentStep(lambda: FakeGcp(), Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.FAIL
    assert "denied" in result.message
    assert "iap_service_agent" not in rig.store.load().outputs


# -- #25: Cloud DNS when the zone is there, Google Public DNS always -----------------


def test_dns_in_a_cloud_dns_zone_is_created_after_a_typed_approval(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones = [{"name": "com", "dnsName": "com.", "visibility": "public"}, ZONE,
                  {"name": "inside", "dnsName": "swarm.example.com.", "visibility": "private"}]
    ask = Answers(edge_dns=[HOST])
    gcp = FakeGcp()
    gcp.resolve = lambda host: pytest.fail("the local resolver is never asked")

    result = rig.engine([edge.DnsStep(lambda: gcp, ask, lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert opened == [], "nothing to set by hand"
    assert len(ask.asked) == 1 and HOST in ask.asked[0][1] and IP in ask.asked[0][1]
    assert fake.writes == [("change_records", (PROJECT, "example-com", {
        "deletions": [],
        "additions": [{"name": f"{HOST}.", "type": "A", "ttl": 300, "rrdatas": [IP]}],
    }))], "the longest public zone covering the hostname holds the record"
    assert rig.of_kind("wait_countdown", "edge.dns"), "then the engine polls Google Public DNS"
    assert rig.store.load().outputs["dns_zone"] == "example-com"


def test_dns_replaces_a_wrong_record_in_the_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones = [ZONE]
    old = {"name": f"{HOST}.", "type": "A", "ttl": 60, "rrdatas": ["198.51.100.1"]}
    fake.records[("example-com", f"{HOST}.")] = [old]
    ask = Answers(edge_dns=[HOST])
    result = rig.engine([edge.DnsStep(lambda: FakeGcp(), ask, lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert "replace" in ask.asked[0][1] and "198.51.100.1" in ask.asked[0][1]
    change = fake.writes[0][1][2]
    assert change["deletions"] == [old]


def test_dns_zone_refused_approval_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones = [ZONE]
    result = rig.engine([edge.DnsStep(lambda: FakeGcp(), Answers(edge_dns=["yes"]), lambda: fake)]).run()
    assert result.exit_code == ExitCode.FAIL
    assert "not confirmed" in result.message
    assert fake.writes == []


def test_dns_zone_manual_answer_opens_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones = [ZONE]
    appears_at = rig.clock.now() + 70
    resolve = fake.resolve
    fake.resolve = lambda host: {IP} if rig.clock.now() >= appears_at else resolve(host)
    result = rig.engine([edge.DnsStep(lambda: FakeGcp(), Answers(edge_dns=[edge.MANUAL]), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.writes == []
    assert [url for url, _ in opened] == [f"https://toolbox.googleapps.com/apps/dig/#A/{HOST}"]


def test_dns_record_already_in_the_zone_is_not_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones = [ZONE]
    fake.records[("example-com", f"{HOST}.")] = [{"name": f"{HOST}.", "type": "A", "ttl": 300, "rrdatas": [IP]}]
    appears_at = rig.clock.now() + 40
    fake.resolve = lambda host: {IP} if rig.clock.now() >= appears_at else set()
    result = rig.engine([edge.DnsStep(lambda: FakeGcp(), Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.writes == [] and opened == []


@pytest.mark.parametrize("zones, error", [
    ([], None),
    ([{"name": "other", "dnsName": "other.org.", "visibility": "public"}], None),
    ([{"name": "inside", "dnsName": "example.com.", "visibility": "private"}], None),
    ([], ApiError(403, "PERMISSION_DENIED", "dns.managedZones.list")),
])
def test_dns_with_no_covering_zone_prints_the_record_and_opens_the_page(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zones: list[dict[str, Any]],
        error: ApiError | None) -> None:
    opened = _pages(monkeypatch)
    rig = _rig(tmp_path)
    fake = FakeEdge(rig.clock)
    fake.zones, fake.zones_error = zones, error
    appears_at = rig.clock.now() + 70
    fake.resolve = lambda host: {IP} if rig.clock.now() >= appears_at else set()
    result = rig.engine([edge.DnsStep(lambda: FakeGcp(), Answers(), lambda: fake)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert fake.writes == []
    assert len(opened) == 1 and f"{HOST}.  300  IN  A  {IP}" in opened[0][1]


def test_dns_resolver_that_cannot_be_asked_keeps_the_wait_going() -> None:
    """dns.google not answering is not a failure: the step waits and asks again."""
    ctx = ctx_for(outputs={"project_number": NUMBER, "global_ip": IP}, hostname=HOST)
    fake = FakeEdge(ctx.clock)
    fake.answers[HOST] = None
    step = edge.DnsStep(lambda: FakeGcp(), Answers(), lambda: fake)
    observed = step.check(ctx)
    assert observed.status == "waiting" and "did not answer" in observed.detail


# -- EdgeApi over a fake REST session ------------------------------------------------


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self._body = status, body
        self.content = b"x"
        self.reason = "fake"

    def json(self) -> Any:
        return self._body


class _Session:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers, self.requests = answers, []

    def request(self, method, url, json=None, params=None, timeout=None):
        self.requests.append((method, url, json, params))
        return _Response(200, self.answers.get(url, {}))


def test_edge_api_urls_and_the_public_resolver() -> None:
    zones_url = f"{edge.DNS_API}/projects/{PROJECT}/managedZones"
    session = _Session({zones_url: {"managedZones": [ZONE]}})
    asked: list[tuple[str, dict[str, str]]] = []

    def get(url, *, params, timeout):
        asked.append((url, params))
        return _Response(200, {"Status": 0, "Answer": [
            {"name": f"{HOST}.", "type": 5, "data": "lb.example.net."},
            {"name": "lb.example.net.", "type": 1, "data": IP}]})

    api = edge.EdgeApi(lambda: Gcp(session), http_get=get)
    assert api.managed_zones(PROJECT) == [ZONE]
    api.a_records(PROJECT, "example-com", f"{HOST}.")
    api.change_records(PROJECT, "example-com", {"additions": [], "deletions": []})
    assert [(m, u) for m, u, _, _ in session.requests] == [
        ("GET", zones_url),
        ("GET", f"{zones_url}/example-com/rrsets"),
        ("POST", f"{zones_url}/example-com/changes"),
    ]
    assert session.requests[1][3] == {"name": f"{HOST}.", "type": "A"}
    assert api.resolve(HOST) == {IP}, "only the A answers count, a CNAME is followed by the resolver"
    assert asked == [("https://dns.google/resolve", {"name": HOST, "type": "A"})]

    nx = edge.EdgeApi(lambda: Gcp(session), http_get=lambda url, **_: _Response(200, {"Status": 3}))
    assert nx.resolve(HOST) == set(), "NXDOMAIN: resolves to nothing"
    servfail = edge.EdgeApi(lambda: Gcp(session), http_get=lambda url, **_: _Response(200, {"Status": 2}))
    assert servfail.resolve(HOST) is None
    down = edge.EdgeApi(lambda: Gcp(session), http_get=lambda url, **_: (_ for _ in ()).throw(OSError("down")))
    assert down.resolve(HOST) is None


# -- what an uninstall leaves names the brand and the zone without probing ------------


def test_remains_names_the_brand_page_and_the_cloud_dns_zone() -> None:
    state = SimpleNamespace(project=PROJECT, region="europe-west1", environment="dev", hostname=HOST,
                            outputs={"global_ip": IP, "dns_zone": "example-com"})
    rows = {r.kind: r for r in remains.survivors(state)}
    assert rows["oauth-brand"].name == f"https://console.cloud.google.com/auth/branding?project={PROJECT}"
    assert "example-com" in rows["dns-record"].why and "example-com" in rows["dns-record"].step
    state.outputs.pop("dns_zone")
    rows = {r.kind: r for r in remains.survivors(state)}
    assert "registrar" in rows["dns-record"].step
