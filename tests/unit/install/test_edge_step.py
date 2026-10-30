"""Stage 2's human pages (DAYONE-OPEN-PAGES): the brand, the DNS record and the
Desktop client each open their exact page (the DNS one when no Cloud DNS zone
holds the hostname) and the engine keeps polling where it waits; the
Desktop client's secret is typed at a hidden prompt, never echoed.

On a fake Google Cloud with no credentials; the browser is a fake `open_page`.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from swarm_install import engine as engine_module
from swarm_install.exitcodes import ExitCode
from swarm_install.steps import edge

from .test_edge_probes import FakeEdge
from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, Rig, ctx_for
from .test_secret_entry import FakeContexts

HOST = "swarm.example.com"
IP = "203.0.113.7"
CLIENT_ID = "123456789012-abc123def456.apps.googleusercontent.com"


def _rig(tmp_path: Path) -> Rig:
    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "hostname", HOST),
                                s.outputs.update({"project_number": NUMBER, "global_ip": IP})))
    return rig


def _pages(monkeypatch: pytest.MonkeyPatch, *modules) -> list[tuple[str, str]]:
    opened: list[tuple[str, str]] = []
    for module in modules:
        monkeypatch.setattr(module, "open_page", lambda url, *, why, **_: opened.append((url, why)))
    return opened


BRANDING = f"https://console.cloud.google.com/auth/branding?project={PROJECT}"
AUDIENCE = f"https://console.cloud.google.com/auth/audience?project={PROJECT}"


def test_brand_opens_branding_then_audience_then_asks_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DAYONE-EDGE-AUDIENCE: Branding first, then Audience with "choose
    Internal", each opened for the person, and only then the Desktop client's
    id, the brand's one proof (no API reads a brand)."""
    log: list[tuple[str, str, str]] = []
    monkeypatch.setattr(engine_module, "open_page", lambda url, *, why, **_: log.append(("open", url, why)))
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: log.append(("open", url, why)))
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    ask = Answers(log, edge_brand=[CLIENT_ID])

    result = rig.engine([edge.BrandStep(lambda: gcp, ask, lambda: FakeEdge())]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert [entry[:2] for entry in log] == [("open", BRANDING), ("open", AUDIENCE), ("ask", "edge.brand")]
    assert "choose Internal" in log[1][2], "the Audience page's instructions say to choose Internal"
    assert "Internal" not in log[0][2], "the Branding page says only what to set on Branding"
    assert rig.store.load().outputs["desktop_client_id"] == CLIENT_ID


def test_brand_pages_honour_swarm_no_browser(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The real `open_page`: with SWARM_NO_BROWSER=1 and a display, neither
    page reaches the browser, and both are still printed, Branding first."""
    from swarm_install import steps

    browsed: list[str] = []
    monkeypatch.setattr(steps, "_browser", browsed.append)
    monkeypatch.setattr(engine_module, "open_page", lambda url, *, why, **_: None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv(steps.NO_BROWSER, "1")
    rig = _rig(tmp_path)

    result = rig.engine([edge.BrandStep(lambda: FakeGcp(), Answers(edge_brand=[CLIENT_ID]), lambda: FakeEdge())]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert browsed == []
    out = capsys.readouterr().out
    assert BRANDING in out and AUDIENCE in out
    assert out.index(BRANDING) < out.index(AUDIENCE)

    monkeypatch.delenv(steps.NO_BROWSER)
    (tmp_path / "browser").mkdir()
    rig = _rig(tmp_path / "browser")
    result = rig.engine([edge.BrandStep(lambda: FakeGcp(), Answers(edge_brand=[CLIENT_ID]), lambda: FakeEdge())]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert browsed == [BRANDING, AUDIENCE]


def test_dns_prints_record_opens_resolution_page_and_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch, engine_module, edge)
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    dns = FakeEdge()
    appears_at = rig.clock.now() + 70
    resolve = dns.resolve
    dns.resolve = lambda host: {IP} if rig.clock.now() >= appears_at else resolve(host)

    result = rig.engine([edge.DnsStep(lambda: gcp, Answers(), lambda: dns)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(opened) == 1
    url, why = opened[0]
    assert url == f"https://toolbox.googleapps.com/apps/dig/#A/{HOST}"
    assert f"{HOST}.  300  IN  A  {IP}" in why, "the record to set is printed with the page"
    assert rig.of_kind("wait_countdown", "edge.dns"), "the engine polls the record"


def _value() -> bytes:
    return ("GOCSPX-" + secrets.token_hex(12)).encode()  # built at run time: no literal credential


class Terminal:
    def __init__(self, value: bytes, log: list[str]) -> None:
        self.value, self.log, self.written = value, log, []

    def write(self, text: str) -> None:
        self.written.append(text)

    def read_line(self) -> str:
        return CLIENT_ID

    def read_hidden(self) -> bytearray:
        self.log.append("hidden prompt")
        return bytearray(self.value)

    def close(self) -> None:
        pass


def test_desktop_client_opens_create_page_then_reads_secret_hidden(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    log: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: log.append(f"open {url}"))
    value = _value()
    terminal = Terminal(value, log)
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    contexts = FakeContexts()
    step = edge.DesktopClientStep(lambda: gcp, Answers(edge_desktop_client=[CLIENT_ID]), lambda: terminal, contexts)

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log == [f"open https://console.cloud.google.com/auth/clients/create?project={PROJECT}", "hidden prompt"]
    assert gcp.secrets[edge.DESKTOP_SECRET] == [value], "Secret Manager holds the secret"
    assert contexts.stored == {HOST: value}, "and the new deployment's credential store, the same typed value"
    text = value.decode()
    captured = capsys.readouterr()
    assert text not in captured.out + captured.err + "".join(terminal.written)
    assert text not in json.dumps(rig.events.record) and text not in json.dumps(rig.store.load().outputs)


def test_desktop_client_recorded_id_opens_its_own_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The id already recorded is not asked again; the page opened is that client's."""
    log: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: log.append(f"open {url}"))
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    rig.store.update(lambda s: s.outputs.update({"desktop_client_id": CLIENT_ID}))
    step = edge.DesktopClientStep(lambda: gcp, Answers(), lambda: Terminal(_value(), log), FakeContexts())

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log == [f"open https://console.cloud.google.com/auth/clients/{CLIENT_ID}?project={PROJECT}",
                   "hidden prompt"]

    log.clear()
    assert rig.engine([step]).run().exit_code == ExitCode.OK
    assert log == [], "a met step opens nothing"


# -- DAYONE-EDGE-2 (part of #2688): audit findings N17, N18 and N19 -----------


def test_brand_asks_the_secret_right_after_the_id_before_the_dns_wait(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N18: Google shows a Desktop client's secret only in the dialog that
    creates it, so the brand step asks it at the hidden prompt right after the
    id, before edge.dns can wait up to 60 min; the client step is then met
    with nothing asked."""
    log: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: log.append(f"open {url}"))
    value = _value()
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    contexts = FakeContexts()
    ask = Answers(log, edge_brand=[CLIENT_ID])
    steps = edge.build_steps(lambda: gcp, ask, lambda: Terminal(value, log), lambda: FakeEdge(), contexts)
    ids = [step.id for step in steps]
    assert ids.index("edge.brand") < ids.index("edge.dns")

    result = rig.engine([steps[ids.index("edge.brand")]]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert [entry if isinstance(entry, str) else entry[:2] for entry in log] == [
        f"open {BRANDING}", f"open {AUDIENCE}", ("ask", "edge.brand"), "hidden prompt"]
    assert gcp.secrets[edge.DESKTOP_SECRET] == [value] and contexts.stored == {HOST: value}

    log.clear()
    client = steps[ids.index("edge.desktop-client")]
    assert rig.engine([client]).run().exit_code == ExitCode.OK
    assert log == [], "the client step finds the secret already kept and asks nothing"


def test_desktop_client_re_asking_the_secret_says_add_secret_on_this_page(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N18: a recorded client whose secret was never kept (a run that stopped
    after the id) is sent to its own page with "Add secret on this page"; the
    old secret is not shown there any more."""
    whys: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: whys.append(why))
    rig = _rig(tmp_path)
    rig.store.update(lambda s: s.outputs.update({"desktop_client_id": CLIENT_ID}))
    gcp = FakeGcp()
    step = edge.DesktopClientStep(lambda: gcp, Answers(), lambda: Terminal(_value(), []), FakeContexts())

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(whys) == 1 and "Add secret on this page" in whys[0]


class _PendingAddressGcp(FakeGcp):
    """`addresses.insert` answers a RUNNING operation; the address exists only
    once the operation is DONE, as on Google."""

    def __init__(self, *, error: dict | None = None) -> None:
        super().__init__()
        self.error = error

    def create_global_address(self, project, body):
        self._call("create_global_address", project, body["name"])
        return {"name": "operation-1", "status": "RUNNING"}


class _OperationEdge(FakeEdge):
    def __init__(self, gcp: _PendingAddressGcp, done_after: int) -> None:
        super().__init__()
        self.gcp, self.done_after, self.polls = gcp, done_after, 0

    def global_operation(self, project, name):
        self.polls += 1
        if self.polls < self.done_after:
            return {"name": name, "status": "RUNNING"}
        if self.gcp.error is not None:
            return {"name": name, "status": "DONE", "error": {"errors": [self.gcp.error]}}
        self.gcp.addresses[edge.ADDRESS] = {"address": IP, "status": "RESERVED"}
        return {"name": name, "status": "DONE"}


def test_global_ip_waits_for_the_insert_operation_before_its_re_check(tmp_path: Path) -> None:
    """N19: the engine re-checks an engine step at once after apply; the
    address is reserved only when Google's operation is done, so apply polls
    it first and the first run passes."""
    gcp = _PendingAddressGcp()
    ops = _OperationEdge(gcp, done_after=3)
    rig = _rig(tmp_path)
    rig.store.update(lambda s: s.outputs.pop("global_ip"))

    result = rig.engine([edge.GlobalIpStep(lambda: gcp, Answers(), lambda: ops)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert ops.polls == 3
    assert rig.store.load().outputs["global_ip"] == IP


def test_global_ip_operation_error_is_named(tmp_path: Path) -> None:
    gcp = _PendingAddressGcp(error={"code": "QUOTA_EXCEEDED", "message": "Quota STATIC_ADDRESSES exceeded"})
    rig = _rig(tmp_path)
    rig.store.update(lambda s: s.outputs.pop("global_ip"))

    result = rig.engine([edge.GlobalIpStep(lambda: gcp, Answers(), lambda: _OperationEdge(gcp, 1))]).run()
    assert result.exit_code != ExitCode.OK
    assert "Quota STATIC_ADDRESSES exceeded" in result.message


class _ConsoleEdge(FakeEdge):
    """`https://<H>/` as the clock goes: no TLS, then the load balancer's 404,
    then IAP's sign-in redirect."""

    def __init__(self, clock, tls_at: float, iap_at: float) -> None:
        super().__init__(clock)
        self.tls_at, self.iap_at, self.asked = tls_at, iap_at, []

    def http_status(self, url):
        self.asked.append(url)
        now = self._now()
        return None if now < self.tls_at else 404 if now < self.iap_at else 302


def test_console_wait_polls_until_the_certificate_is_active_and_iap_answers(tmp_path: Path) -> None:
    """N17: the step waits while the certificate is PROVISIONING, while
    https://<H>/ fails TLS and while it answers 404, and is met only once
    the certificate is ACTIVE and IAP answers its sign-in."""
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    start = rig.clock.now()
    gcp.certs[edge.CERT] = {"managed": {"status": "PROVISIONING"}, "inUseBy": ["swarm-ui-https-proxy"]}
    active_at = start + 120
    certificate = gcp.ssl_certificate
    gcp.ssl_certificate = lambda project, name: (
        {"managed": {"status": "ACTIVE"}, "inUseBy": ["swarm-ui-https-proxy"]}
        if rig.clock.now() >= active_at else certificate(project, name))
    web = _ConsoleEdge(rig.clock, tls_at=start + 200, iap_at=start + 300)
    step = edge.ConsoleAnswersStep(lambda: gcp, Answers(), lambda: web)

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.clock.now() >= start + 300
    assert web.asked and set(web.asked) == {f"https://{HOST}/"}
    assert rig.of_kind("wait_countdown", "edge.console-answers"), "the engine polls the wait"


def test_console_wait_names_what_it_waits_for() -> None:
    """N17: each state before IAP answers is `waiting`, and says which."""
    gcp = FakeGcp()
    ctx = ctx_for(hostname=HOST)
    start = ctx.clock.now()
    web = _ConsoleEdge(ctx.clock, tls_at=start + 10, iap_at=start + 20)
    step = edge.ConsoleAnswersStep(lambda: gcp, Answers(), lambda: web)

    assert step.check(ctx).status == "unmet", "no certificate yet: infra pass 1 makes it"
    gcp.certs[edge.CERT] = {"managed": {"status": "PROVISIONING"}}
    observed = step.check(ctx)
    assert (observed.status, "PROVISIONING" in observed.detail) == ("waiting", True)
    gcp.certs[edge.CERT] = {"managed": {"status": "ACTIVE"}}
    observed = step.check(ctx)
    assert (observed.status, "TLS" in observed.detail) == ("waiting", True)
    ctx.clock.sleep(10)
    observed = step.check(ctx)
    assert (observed.status, "404" in observed.detail) == ("waiting", True)
    ctx.clock.sleep(10)
    assert step.check(ctx).status == "met"


def test_console_wait_runs_after_both_pass_2s_and_before_stage_8s_first_call(tmp_path: Path) -> None:
    """N17: the wait is the first step of stage 8, so it runs after infra
    pass 2 and bootstrap pass 2 (stage 7) and before identity.group-read,
    stage 8's first call through https://<H>."""
    from swarm_install import steps as steps_module
    from swarm_install.manifest import load as load_manifest

    found = steps_module.discover()
    ordered = [s.id for s in Rig(tmp_path).engine(found, manifest=load_manifest())._ordered()]
    wait = ordered.index("edge.console-answers")
    assert ordered.index("infra.pass-2") < wait and ordered.index("bootstrap.pass-2") < wait
    assert wait < ordered.index("identity.group-read")
    stage_8 = [s.id for s in found if s.stage == 8]
    assert ordered[wait:wait + len(stage_8)][0] == "edge.console-answers" and set(ordered[wait:wait + len(stage_8)]) == set(stage_8)


def test_edge_api_http_status_is_none_on_a_tls_error() -> None:
    """While the certificate is not served the GET raises (an SSLError): the
    wait polls again instead of failing the run."""
    import requests

    def tls_error(url, **_):
        raise requests.exceptions.SSLError("certificate verify failed")

    assert edge.EdgeApi(lambda: FakeGcp(), tls_error).http_status(f"https://{HOST}/") is None

    class Answer:
        status_code = 302

    seen: dict = {}
    assert edge.EdgeApi(lambda: FakeGcp(), lambda url, **kw: (seen.update(kw), Answer())[1]).http_status("x") == 302
    assert seen["allow_redirects"] is False
