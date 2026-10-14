"""The TypeSafe key from the sc plugin and the `sc` CLI (lane KEYS-PLUGIN).

Owner request 2026-10-14: set the TypeSafe key safely from `/sc`, and the key
never passes through Claude -- never an MCP tool argument, a transcript, a
response or a log. The API is KEYS-API's (docs/provider-keys.md); these hold
the two halves that call it:

  * `swarm_keys` reads `GET /v1/provider-keys/typesafe` and answers status
    only, field by field, so even an API that (wrongly) sent a value back
    would not have it forwarded;
  * `swarm_key_set_link` mints a one-time console link
    (`POST .../links`) and answers ONLY its url and expiry. Neither tool has
    a schema field that could carry a key, and an argument that is not in
    the schema is refused with nothing sent;
  * `sc key set typesafe [--tenant] [--stdin]` reads the key with getpass at
    a terminal, or one line from stdin with --stdin, PUTs it once, and
    prints only "stored: version N". A key typed on the command line is
    refused before anything is sent, and the refusal does not repeat it.

Every fake key is built at runtime (CLAUDE.md: nothing added may look like a
credential). Offline: no credentials, no emulator.
"""

from __future__ import annotations

import getpass
import io
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import pytest

from swarm_mcp import config, sc, server
from swarm_mcp.client import SwarmError

STATUS_PATH = "/v1/provider-keys/typesafe"
LINKS_PATH = "/v1/provider-keys/typesafe/links"
USER_PATH = "/v1/provider-keys/typesafe/user"
TENANT_PATH = "/v1/provider-keys/typesafe/tenant"
KEY_TOOLS = ("swarm_keys", "swarm_key_set_link")
CONSOLE = "https://console.example.test"


def _key() -> str:
    """A fake TypeSafe key, unique per test so a leak is unambiguous.
    TypeSafe documents no prefix, so none is assumed."""
    return secrets.token_urlsafe(30)


def _pieces(value: str, width: int = 8) -> list[str]:
    return [value[i:i + width] for i in range(len(value) - width + 1)]


def _leaked(value: str, *texts: str) -> list[str]:
    joined = "\n".join(texts)
    return [piece for piece in _pieces(value) if piece in joined]


def _status(secret_id: str, *, is_set: bool, version: int | None = None) -> dict:
    return {"available": True, "set": is_set,
            "set_at": "2026-10-14T09:00:00+00:00" if is_set else None,
            "set_by": "alice@example.test" if is_set else None,
            "version": version, "secret_id": secret_id}


class FakeApi:
    """The KEYS-API routes, recording every request. `planted` is a value
    the fake (wrongly) puts in every answer, so a test can show it is not
    forwarded."""

    def __init__(self, *, planted: str | None = None, error: SwarmError | None = None,
                 console: str | None = CONSOLE, tenant: str | None = "eng"):
        self.sent: list[tuple[str, str, object]] = []
        self.planted = planted
        self.error = error
        self.console = console
        self.tenant = tenant
        self.link_token = secrets.token_urlsafe(32)

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if self.error is not None:
            raise self.error
        extra = {"api_key": self.planted} if self.planted else {}
        if (method, path) == ("GET", STATUS_PATH):
            return {
                "provider": "typesafe",
                "user": {**_status("swarm-tenant-u-alice-typesafe", is_set=True, version=2),
                         **extra},
                "tenant": {**_status("swarm-tenant-eng-typesafe", is_set=False),
                           "tenant_id": "eng"},
                "resolution": {"tenant_id": "eng", "uses": "user",
                               "secret_id": "swarm-tenant-u-alice-typesafe"},
                "consumed_by": [],
                **extra,
            }
        if (method, path) == ("POST", LINKS_PATH):
            scope = (payload or {}).get("scope")
            link_path = f"/settings/keys/typesafe?scope={scope}&t={self.link_token}"
            return {"url": f"{self.console}{link_path}" if self.console else None,
                    "path": link_path, "scope": scope, "provider": "typesafe",
                    "expires_at": "2026-10-14T09:10:00+00:00", **extra}
        if method == "PUT" and path in (USER_PATH, TENANT_PATH):
            scope = "user" if path == USER_PATH else "tenant"
            return {"status": _status(f"swarm-tenant-x-{scope}", is_set=True, version=3),
                    "scope": scope, "via_link": False,
                    "note": "stored write-only; no route in this API can return key material",
                    **extra}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


# -- the MCP tools ----------------------------------------------------------------


def _tool(name: str) -> dict:
    return next(t for t in server.TOOLS if t["name"] == name)


#: A property name that could be read as "put the key here".
_KEYISH = re.compile(r"key|secret|token|value|password|credential|material|payload|body",
                     re.IGNORECASE)


@pytest.mark.parametrize("name", KEY_TOOLS)
def test_a_key_tool_has_no_schema_field_that_could_carry_a_key(name):
    schema = _tool(name)["inputSchema"]
    properties = schema.get("properties") or {}
    assert schema.get("additionalProperties") is False, "an undeclared field must be refused"
    assert set(properties) <= {"provider", "scope"}, sorted(properties)
    for prop, spec in properties.items():
        assert not _KEYISH.search(prop), prop
        # A free string is a field a key fits in; an enum is not.
        assert spec.get("type") == "string" and spec.get("enum"), (prop, spec)
        assert all(len(choice) <= 16 for choice in spec["enum"]), spec["enum"]


def test_no_bridge_tool_anywhere_takes_an_api_key():
    offending = [(t["name"], p) for t in server.TOOLS
                 for p in (t["inputSchema"].get("properties") or {})
                 if p.lower() in {"api_key", "apikey", "key", "typesafe_key", "provider_key"}]
    assert offending == [], offending


def test_swarm_keys_reads_the_status_route_and_forwards_status_fields_only():
    planted = _key()
    api = FakeApi(planted=planted)
    reply = server._call(api, "swarm_keys", {})
    assert api.sent == [("GET", STATUS_PATH, None)]
    assert _leaked(planted, reply) == [], "a value the API sent back is not forwarded"
    body = json.loads(reply)
    assert body["user"]["set"] is True and body["user"]["version"] == 2
    assert body["tenant"]["set"] is False and body["tenant"]["tenant_id"] == "eng"
    assert body["resolution"]["uses"] == "user"
    assert "api_key" not in json.dumps(body)


def test_swarm_key_set_link_answers_only_the_url_and_its_expiry():
    planted = _key()
    api = FakeApi(planted=planted)
    reply = server._call(api, "swarm_key_set_link", {"provider": "typesafe", "scope": "user"})
    assert api.sent == [("POST", LINKS_PATH, {"scope": "user"})]
    body = json.loads(reply)
    assert set(body) == {"url", "expires_at"}, sorted(body)
    assert body["url"].startswith(CONSOLE + "/settings/keys/typesafe?")
    assert body["expires_at"] == "2026-10-14T09:10:00+00:00"
    assert _leaked(planted, reply) == []


def test_the_set_link_reply_holds_no_secret_shaped_string_but_the_links_own_token():
    api = FakeApi(planted=_key())
    reply = server._call(api, "swarm_key_set_link", {"provider": "typesafe", "scope": "tenant"})
    assert api.sent == [("POST", LINKS_PATH, {"scope": "tenant"})]
    body = json.loads(reply)
    token = parse_qs(urlparse(body["url"]).query)["t"][0]
    assert token == api.link_token
    rest = reply.replace(token, "")
    shaped = re.findall(r"[A-Za-z0-9_\-+/=]{24,}", rest)
    assert shaped == [], shaped


def test_with_no_console_address_the_link_tool_says_so_and_gives_no_path():
    api = FakeApi(console=None)
    body = json.loads(server._call(api, "swarm_key_set_link",
                                   {"provider": "typesafe", "scope": "user"}))
    assert body["url"] is None
    assert set(body) == {"url", "expires_at", "note"}
    assert api.link_token not in json.dumps(body), "the path's token is not handed out bare"
    assert "SWARM_CONSOLE_URL" in body["note"]


@pytest.mark.parametrize("field", ["api_key", "key", "value"])
def test_a_key_handed_to_the_link_tool_is_refused_and_nothing_is_sent(field):
    value = _key()
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_key_set_link",
                     {"provider": "typesafe", "scope": "user", field: value})
    assert api.sent == []
    assert _leaked(value, str(exc.value)) == []


@pytest.mark.parametrize("args", [
    {"provider": "<key>", "scope": "user"},
    {"provider": "typesafe", "scope": "<key>"},
])
def test_a_bad_provider_or_scope_is_refused_without_repeating_it(args):
    value = _key()
    args = {k: (value if v == "<key>" else v) for k, v in args.items()}
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_key_set_link", args)
    assert api.sent == []
    assert _leaked(value, str(exc.value)) == []


# -- the CLI ----------------------------------------------------------------------


class TtyInput(io.StringIO):
    """A terminal: `isatty()` is true, and a readline here would be the bug."""

    def isatty(self):
        return True


def _main(argv, api, stdin, monkeypatch, capsys):
    class Client:
        def __init__(self, context=None, tenant=None):
            pass

        def __enter__(self):
            return api

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sc, "SwarmClient", Client)
    monkeypatch.setattr(config, "session_target", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", stdin)
    out = io.StringIO()
    code = sc.main(argv, out=out)
    captured = capsys.readouterr()
    return code, out.getvalue(), captured.out + captured.err


@pytest.mark.parametrize("shape", [
    ["key", "set", "typesafe", "<key>"],
    ["key", "set", "typesafe", "--stdin", "<key>"],
    ["key", "set", "typesafe", "--api-key", "<key>"],
    ["key", "set", "typesafe", "--key=<key>"],
    ["key", "set", "<key>"],
    ["key", "set", "--tenant", "typesafe", "<key>"],
])
def test_a_key_on_the_command_line_is_refused_unsent_and_unrepeated(shape, monkeypatch, capsys):
    value = _key()
    argv = [w.replace("<key>", value) for w in shape]
    api = FakeApi()
    code, out, err = _main(argv, api, io.StringIO(value + "\n"), monkeypatch, capsys)
    assert code != sc.EXIT_OK
    assert api.sent == [], "nothing is sent when a key was typed on the command line"
    assert _leaked(value, out, err) == []
    assert "shell history" in (out + err)


def test_stdin_is_read_put_once_and_only_the_version_is_printed(monkeypatch, capsys):
    value = _key()
    api = FakeApi()
    stdin = io.StringIO(f"  {value}  \nsecond line\n")
    code, out, err = _main(["key", "set", "typesafe", "--stdin"], api, stdin, monkeypatch, capsys)
    assert code == sc.EXIT_OK
    assert api.sent == [("PUT", USER_PATH, {"api_key": value})]
    assert out == "stored: version 3\n"
    assert _leaked(value, out, err) == []
    assert stdin.read() == "second line\n", "one line, and the rest is not read"


def test_tenant_puts_the_tenant_key(monkeypatch, capsys):
    value = _key()
    api = FakeApi()
    code, out, _ = _main(["key", "set", "typesafe", "--tenant", "--stdin"], api,
                         io.StringIO(value + "\n"), monkeypatch, capsys)
    assert code == sc.EXIT_OK
    assert api.sent == [("PUT", TENANT_PATH, {"api_key": value})]
    assert out == "stored: version 3\n"


def test_at_a_terminal_the_key_is_read_through_getpass(monkeypatch, capsys):
    value = _key()
    prompts: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", stream=None: (
        prompts.append(prompt) or value))
    api = FakeApi()
    stdin = TtyInput("this line must not be read\n")
    code, out, err = _main(["key", "set", "typesafe"], api, stdin, monkeypatch, capsys)
    assert code == sc.EXIT_OK
    assert len(prompts) == 1 and "hidden" in prompts[0]
    assert stdin.read() == "this line must not be read\n"
    assert api.sent == [("PUT", USER_PATH, {"api_key": value})]
    assert _leaked(value, out, err, prompts[0]) == []


def test_without_a_terminal_and_without_stdin_flag_nothing_is_read_or_sent(monkeypatch, capsys):
    value = _key()
    api = FakeApi()
    stdin = io.StringIO(value + "\n")
    code, out, err = _main(["key", "set", "typesafe"], api, stdin, monkeypatch, capsys)
    assert code != sc.EXIT_OK
    assert api.sent == []
    assert stdin.read() == value + "\n"
    assert "--stdin" in (out + err)


def test_nothing_read_sends_nothing(monkeypatch, capsys):
    api = FakeApi()
    code, _, text = _main(["key", "set", "typesafe", "--stdin"], api, io.StringIO("\n"),
                          monkeypatch, capsys)
    assert code != sc.EXIT_OK
    assert api.sent == []
    assert "nothing was sent" in text


def test_a_503_says_plainly_that_keys_cannot_be_saved_yet(monkeypatch, capsys):
    value = _key()
    error = SwarmError(f"PUT {USER_PATH} -> 503: could not create the tenant secret",
                       status=503, code="unavailable")
    api = FakeApi(error=error)
    code, out, err = _main(["key", "set", "typesafe", "--stdin"], api,
                           io.StringIO(value + "\n"), monkeypatch, capsys)
    assert code == sc.EXIT_TROUBLE
    assert len(api.sent) == 1
    assert "keys can't be saved yet: could not create the tenant secret" in out
    assert "stored" not in out.replace("Nothing was stored", "")
    assert _leaked(value, out, err) == []


def test_an_api_that_echoes_the_key_in_its_refusal_is_redacted(monkeypatch, capsys):
    value = _key()
    error = SwarmError(f"PUT {USER_PATH} -> 422: bad key {value}", status=422,
                       code="validation_failed")
    api = FakeApi(error=error)
    code, out, err = _main(["key", "set", "typesafe", "--stdin"], api,
                           io.StringIO(value + "\n"), monkeypatch, capsys)
    assert code == sc.EXIT_TROUBLE
    assert _leaked(value, out, err) == []


def test_an_unreachable_api_does_not_print_the_key(monkeypatch, capsys):
    value = _key()
    api = FakeApi(error=SwarmError(f"could not reach the API with {value}"))
    code, out, err = _main(["key", "set", "typesafe", "--stdin"], api,
                           io.StringIO(value + "\n"), monkeypatch, capsys)
    assert code == sc.EXIT_FAIL
    assert _leaked(value, out, err) == []


def test_an_unknown_provider_is_refused_unsent(monkeypatch, capsys):
    api = FakeApi()
    code, out, err = _main(["key", "set", "openai", "--stdin"], api, io.StringIO("x" * 20 + "\n"),
                           monkeypatch, capsys)
    assert code != sc.EXIT_OK
    assert api.sent == []
    assert "typesafe" in (out + err)
