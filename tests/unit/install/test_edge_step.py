"""Stage 2's human pages (DAYONE-OPEN-PAGES): the brand, the DNS record and the
Desktop client each open their exact page and the engine keeps polling; the
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

from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, Rig

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


def test_brand_opens_branding_page_once_then_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch, engine_module, edge)
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    appears_at = rig.clock.now() + 95
    gcp.brands = lambda: [{"name": "brand", "orgInternalOnly": True}] if rig.clock.now() >= appears_at else []

    result = rig.engine([edge.BrandStep(lambda: gcp, Answers())]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert [url for url, _ in opened] == [f"https://console.cloud.google.com/auth/branding?project={PROJECT}"]
    assert len(rig.of_kind("wait_countdown", "edge.brand")) >= 9, "then it polls, without opening again"


def test_dns_prints_record_opens_resolution_page_and_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _pages(monkeypatch, engine_module, edge)
    gcp = FakeGcp()
    rig = _rig(tmp_path)
    appears_at = rig.clock.now() + 70
    resolve = gcp.resolve
    gcp.resolve = lambda host: {IP} if rig.clock.now() >= appears_at else resolve(host)

    result = rig.engine([edge.DnsStep(lambda: gcp, Answers())]).run()
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
    rig = Rig(tmp_path)
    step = edge.DesktopClientStep(lambda: gcp, Answers(edge_desktop_client=[CLIENT_ID]), lambda: terminal)

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log == [f"open https://console.cloud.google.com/auth/clients/create?project={PROJECT}", "hidden prompt"]
    assert gcp.secrets[edge.DESKTOP_SECRET] == [value], "Secret Manager is the one place the secret goes"
    text = value.decode()
    captured = capsys.readouterr()
    assert text not in captured.out + captured.err + "".join(terminal.written)
    assert text not in json.dumps(rig.events.record) and text not in json.dumps(rig.store.load().outputs)


def test_desktop_client_recorded_id_opens_its_own_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The id already recorded is not asked again; the page opened is that client's."""
    log: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: log.append(f"open {url}"))
    gcp = FakeGcp()
    rig = Rig(tmp_path)
    rig.store.update(lambda s: s.outputs.update({"desktop_client_id": CLIENT_ID}))
    step = edge.DesktopClientStep(lambda: gcp, Answers(), lambda: Terminal(_value(), log))

    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log == [f"open https://console.cloud.google.com/auth/clients/{CLIENT_ID}?project={PROJECT}",
                   "hidden prompt"]

    log.clear()
    assert rig.engine([step]).run().exit_code == ExitCode.OK
    assert log == [], "a met step opens nothing"
