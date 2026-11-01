"""`sc account add --provider openai`: a ChatGPT plan into the pool (#2688).

The sign-in runs on the person's own machine: the broker starts it (and keeps
the PKCE verifier), the browser lands on `localhost:1455` where `sc` listens,
and `sc` sends the code it caught to the same exchange route a Claude add
uses. It never prints the code or the state, and never sees a token.
"""

from __future__ import annotations

import io
import secrets
import socket
import threading
import urllib.error
import urllib.request

import pytest

from swarm_mcp import render, sc
from swarm_mcp.client import SwarmError

TENANT = "acme"


class FakeApi:
    def __init__(self, *, provider_echo: bool = True):
        self.sent: list[tuple[str, str, object]] = []
        self.state = secrets.token_hex(16)
        self.provider_echo = provider_echo

    def request(self, method, path, payload=None, **kwargs):
        self.sent.append((method, path, payload))
        if path == "/v1/accounts/authorize":
            answer = {
                "authorize_url": f"https://auth.example.test/oauth/authorize?state={self.state}",
                "state": self.state,
                "expires_in_seconds": 900,
            }
            if self.provider_echo:
                answer.update(provider="openai", redirect_uri="http://localhost:1455/auth/callback")
            return answer
        if path == "/v1/accounts/exchange":
            return {
                "account": {"account_id": "acme:plan", "label": "plan", "state": "AVAILABLE",
                            "provider": "openai"},
                "registered": "registered acme:plan: organisation ChatGPT (Plus)",
                "note": "stored write-only; no route in this service returns key material",
            }
        raise SwarmError(f"{method} {path} -> 500", status=500)


def run(argv, api):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    code = args.func(api, args, out)
    return code, out.getvalue()


@pytest.fixture
def caught(monkeypatch):
    code = secrets.token_hex(12)
    seen: dict = {}
    monkeypatch.setattr(sc, "_open_browser", lambda url: True)

    def catch(redirect_uri, state, timeout):
        seen.update(redirect_uri=redirect_uri, state=state, timeout=timeout)
        return code

    monkeypatch.setattr(sc, "_catch_callback", catch)
    monkeypatch.setattr(sc, "_read_code", lambda prompt: pytest.fail("a plan add pasted nothing"))
    return code, seen


def test_the_add_names_openai_and_sends_the_caught_code_to_the_exchange(caught, capsys):
    code, seen = caught
    api = FakeApi()
    status, out = run(["account", "add", "--label", "plan", "--provider", "openai"], api)
    assert status == sc.EXIT_OK, out
    assert api.sent == [
        ("POST", "/v1/accounts/authorize",
         {"label": "plan", "lend_to": [], "provider": "openai"}),
        ("POST", "/v1/accounts/exchange", {"state": api.state, "code": code}),
    ]
    assert seen["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert seen["state"] == api.state
    shown = out + capsys.readouterr().err
    assert code not in shown
    assert api.state not in shown.replace(f"?state={api.state}", "?state=<url>")
    assert "acme:plan" in out and "ChatGPT (Plus)" in out


def test_a_claude_add_sends_exactly_what_it_always_sent(monkeypatch):
    monkeypatch.setattr(sc, "_open_browser", lambda url: True)
    monkeypatch.setattr(sc, "_read_code", lambda prompt: "pasted-code")
    api = FakeApi(provider_echo=False)
    run(["account", "add", "--label", "work"], api)
    assert api.sent[0] == ("POST", "/v1/accounts/authorize", {"label": "work", "lend_to": []})


def test_an_api_that_started_a_claude_sign_in_instead_is_refused_unopened(monkeypatch):
    opened: list = []
    monkeypatch.setattr(sc, "_open_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(sc, "_catch_callback", lambda *a: pytest.fail("waited on a Claude page"))
    api = FakeApi(provider_echo=False)
    with pytest.raises(SwarmError, match="did not start a ChatGPT sign-in"):
        run(["account", "add", "--label", "plan", "--provider", "openai"], api)
    assert opened == []
    assert [p for _, p, _ in api.sent] == ["/v1/accounts/authorize"]


def test_an_unknown_provider_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["account", "add", "--label", "x", "--provider", "gemini"])


def test_a_taken_port_falls_back_to_the_pasted_address_and_checks_its_state(monkeypatch):
    api = FakeApi()
    code = secrets.token_hex(12)
    monkeypatch.setattr(sc, "_open_browser", lambda url: True)
    monkeypatch.setattr(sc, "_catch_callback", lambda *a: None)
    monkeypatch.setattr(
        sc, "_read_code",
        lambda prompt: f"http://localhost:1455/auth/callback?code={code}&state={api.state}",
    )
    status, _ = run(["account", "add", "--label", "plan", "--provider", "openai"], api)
    assert status == sc.EXIT_OK
    assert api.sent[-1] == ("POST", "/v1/accounts/exchange", {"state": api.state, "code": code})

    other = FakeApi()
    monkeypatch.setattr(
        sc, "_read_code",
        lambda prompt: f"http://localhost:1455/auth/callback?code={code}&state=someone-else",
    )
    with pytest.raises(SwarmError, match="different sign-in"):
        run(["account", "add", "--label", "plan", "--provider", "openai"], other)
    assert [p for _, p, _ in other.sent] == ["/v1/accounts/authorize"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_listener_takes_only_the_callback_with_its_own_state():
    port = _free_port()
    state, code = secrets.token_hex(16), secrets.token_hex(12)
    result: dict = {}
    listener = threading.Thread(
        target=lambda: result.update(
            code=sc._catch_callback(f"http://localhost:{port}/auth/callback", state, 10)
        )
    )
    listener.start()
    base = f"http://127.0.0.1:{port}/auth/callback"
    statuses = []
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{base}?code={code}&state=wrong", timeout=2)
        except urllib.error.HTTPError as exc:
            statuses.append(exc.code)
            break
        except urllib.error.URLError:
            threading.Event().wait(0.05)
    assert statuses == [400]
    with urllib.request.urlopen(f"{base}?code={code}&state={state}", timeout=2) as answer:
        page = answer.read().decode()
    listener.join(5)
    assert result["code"] == code
    assert code not in page and state not in page


def test_sc_accounts_shows_the_provider_once_the_pool_holds_a_plan():
    from datetime import datetime, timezone

    style = render.Style(width=140, color=False, unicode=False)
    accounts = [
        {"account_id": "acme:work", "owner_tenant": TENANT, "label": "work", "state": "AVAILABLE",
         "provider": "anthropic"},
        {"account_id": "acme:plan", "owner_tenant": TENANT, "label": "plan", "state": "AVAILABLE",
         "provider": "openai"},
    ]
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    text = "\n".join(render.render_accounts(accounts, style, now))
    assert "PROVIDER" in text and "openai" in text and "anthropic" in text
    claude_only = "\n".join(render.render_accounts(accounts[:1], style, now))
    assert "PROVIDER" not in claude_only
