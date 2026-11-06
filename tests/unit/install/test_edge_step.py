"""Stage 2's human pages (DAYONE-OPEN-PAGES): the DNS record opens its exact
page (when no Cloud DNS zone holds the hostname) and the engine keeps polling
where it waits. The brand and the Desktop client open nothing and ask
nothing: the console's Admin › Setup step Sign-in client links their pages
and takes the client (issue #2964).

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


def test_brand_opens_no_page_and_asks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #2964: Branding, Audience (Internal) and Create client are linked
    from the console's Admin › Setup step Sign-in client, so the brand, a
    wait after the hand-off, opens nothing and asks nothing; its plan still
    names Branding, then Audience with Internal."""
    from .test_sign_in_client_wait import NeverAsk

    log = _pages(monkeypatch, engine_module, edge)
    rig = _rig(tmp_path)
    step = edge.BrandStep(lambda: FakeGcp(), NeverAsk(), lambda: FakeEdge())
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert log == []
    actions = " ".join(step.plan(ctx_for()).actions)
    assert actions.index(BRANDING) < actions.index(AUDIENCE) and "(Internal)" in actions


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


def test_desktop_client_opens_no_page_and_never_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #2964: the recorded client's secret, missing from this machine's
    credential store, is read from Secret Manager; no page and no prompt."""
    from .test_sign_in_client_wait import ConsoleGcp, NeverAsk

    log = _pages(monkeypatch, engine_module, edge)
    value = _value()
    gcp = ConsoleGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [value]
    rig = _rig(tmp_path)
    rig.store.update(lambda s: s.outputs.update({"desktop_client_id": CLIENT_ID}))
    contexts = FakeContexts()
    step = edge.DesktopClientStep(lambda: gcp, NeverAsk(), contexts)

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log == [] and contexts.stored == {HOST: value}
    text = value.decode()
    assert text not in json.dumps(rig.events.record) and text not in json.dumps(rig.store.load().outputs)


# -- DAYONE-EDGE-2 (part of #2688): audit findings N17, N18 and N19 -----------


def test_stage_2_holds_neither_the_brand_nor_the_client() -> None:
    """N18 is moot (issue #2964): the console takes the secret in the dialog's
    own moment, so stage 2 has no step that waits on it before edge.dns."""
    ids = [step.id for step in edge.build_steps(lambda: FakeGcp(), Answers(), lambda: FakeEdge(),
                                                 FakeContexts())]
    assert "edge.brand" not in ids and "edge.desktop-client" not in ids
    assert ids.index("edge.client-secret-container") < ids.index("edge.dns")


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


def test_brand_support_email_is_the_persons_own_address_not_a_group() -> None:
    """Audit 5b M7: the install's groups exist only from stage 8
    (`identity.groups`), so "a group you own" would send the person to Google
    Admin to make one. The Branding page's dropdown offers the person's own
    address, which works: the plan and docs/install.md say that."""
    step = edge.BrandStep(lambda: FakeGcp(), Answers(), lambda: FakeEdge())
    branding_action = [a for a in step.plan(ctx_for()).actions if BRANDING in a]
    docs = (Path(__file__).resolve().parents[3] / "docs" / "install.md").read_text()
    brand_doc = docs[docs.index("auth/branding?project="):docs.index("auth/audience?project=")]
    for text in (*branding_action, brand_doc):
        flat = " ".join(text.split())
        assert "your own address" in flat, flat
        assert "a group you own" not in flat, flat
    assert branding_action
