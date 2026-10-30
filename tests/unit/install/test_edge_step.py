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
from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, Rig
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
