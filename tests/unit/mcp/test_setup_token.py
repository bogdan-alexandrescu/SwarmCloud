"""`sc setup token --owner <org>`: D5's fallback token, from a terminal (#780).

docs/onboarding.md §4.1 and D5: a person whose org will not install the App,
or forbids classic tokens, stores a fine-grained personal access token for
that ONE owner. The value is read from stdin -- through getpass, with no echo,
at a terminal; one line when piped -- and posted once to
`POST /v1/onboarding/github/token`. These hold it to:

  * one POST, carrying the owner and the value, and nothing else sent;
  * NO PART OF THE VALUE PRINTED, on stdout, stderr or in a raised error --
    not even when the API (wrongly) echoes it back;
  * exit 0 with the org record, 3 with §2.3's code and copy on a refusal,
    1 (through `main`) when the API could not be read;
  * no option anywhere in `sc` that takes a token value: an argument lands
    in shell history and `ps`, which is why there is no `--token`.

The fake token is built at runtime, never written as one literal (CLAUDE.md:
nothing added may look like a credential). Offline: no credentials, no
emulator.
"""

from __future__ import annotations

import argparse
import getpass
import io
import re
import secrets
import sys

import pytest

from swarm_mcp import config, sc
from swarm_mcp.client import SwarmError

OWNER = "example-org"
TOKEN_PATH = "/v1/onboarding/github/token"
CLASSIC_COPY = (f"{OWNER} does not accept classic personal access tokens. Connect with the "
                "SwarmCloud GitHub App instead (recommended), or create a fine-grained token "
                f"whose resource owner is {OWNER} and store it with "
                f"`uv run sc setup token --owner {OWNER}`.")


def _token() -> str:
    """A classic-token-shaped value, unique per test so a leak is unambiguous."""
    return "ghp_" + secrets.token_hex(18)


def _pieces(value: str, width: int = 8) -> list[str]:
    """Every `width`-character slice of the value's random part: "no part of
    the token" is checked slice by slice, not only as the whole string."""
    body = value.split("_", 1)[1]
    return [body[i:i + width] for i in range(len(body) - width + 1)]


def _leaked(value: str, *texts: str) -> list[str]:
    joined = "\n".join(texts)
    return [piece for piece in _pieces(value) if piece in joined]


class FakeApi:
    """The one route, recording every request."""

    def __init__(self, *, error: SwarmError | None = None):
        self.sent: list[tuple[str, str, object]] = []
        self.error = error

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) != ("POST", TOKEN_PATH):
            raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)
        if self.error is not None:
            raise self.error
        return {
            "org": {"owner": OWNER, "method": "pat", "owner_type": "Organization",
                    "installation_id": None, "install_state": "not_installed",
                    "sso": "not_required", "enabled": True},
            "token": {"token_id": "tok-1", "secret_name": "swarm-tenant-eng-git-u-0123456789abcdef",
                      "kind": "fine_grained_pat", "forge_login": "example-user",
                      "state": "active", "expires_at": "2027-01-01T00:00:00+00:00"},
        }


class TtyInput(io.StringIO):
    """A terminal: `isatty()` is true, and a readline here would be the bug."""

    def isatty(self):
        return True


def _run(argv, api, stdin):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    old = sys.stdin
    sys.stdin = stdin
    try:
        code = args.func(api, args, out)
    finally:
        sys.stdin = old
    return code, out.getvalue()


def test_a_piped_token_is_read_from_stdin_posted_once_and_never_printed(capsys):
    value = _token()
    api = FakeApi()
    code, text = _run(["setup", "token", "--owner", OWNER], api, io.StringIO(value + "\n"))
    captured = capsys.readouterr()
    assert code == sc.EXIT_OK
    assert api.sent == [("POST", TOKEN_PATH, {"owner": OWNER, "token": value})]
    assert _leaked(value, text, captured.out, captured.err) == []
    assert OWNER in text and "example-user" in text, "the org record is what is printed"


def test_a_piped_token_is_one_line_and_the_rest_is_not_read(capsys):
    value = _token()
    api = FakeApi()
    stdin = io.StringIO(f"  {value}  \nsecond line\n")
    code, _ = _run(["setup", "token", "--owner", OWNER], api, stdin)
    assert code == sc.EXIT_OK
    assert api.sent[0][2]["token"] == value, "surrounding whitespace is not part of the value"
    assert stdin.read() == "second line\n"


def test_at_a_terminal_the_token_is_read_through_getpass_with_no_echo(monkeypatch, capsys):
    value = _token()
    prompts: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="", stream=None: (
        prompts.append(prompt) or value))
    api = FakeApi()
    stdin = TtyInput("this line must not be read\n")
    code, text = _run(["setup", "token", "--owner", OWNER], api, stdin)
    captured = capsys.readouterr()
    assert code == sc.EXIT_OK
    assert len(prompts) == 1 and OWNER in prompts[0]
    assert stdin.read() == "this line must not be read\n", "a terminal is never readline'd"
    assert api.sent == [("POST", TOKEN_PATH, {"owner": OWNER, "token": value})]
    assert _leaked(value, text, captured.out, captured.err, prompts[0]) == []


def test_nothing_read_sends_nothing():
    api = FakeApi()
    with pytest.raises(SwarmError, match="nothing was sent"):
        _run(["setup", "token", "--owner", OWNER], api, io.StringIO("\n"))
    assert api.sent == []


def test_a_refusal_exits_3_with_the_section_2_3_copy_and_no_part_of_the_token(capsys):
    value = _token()
    refusal = SwarmError(f"POST {TOKEN_PATH} -> 403: refused", status=403,
                         code="authorisation_refused",
                         detail={"failure_code": "CLASSIC_PAT_BLOCKED", "recovery": CLASSIC_COPY})
    api = FakeApi(error=refusal)
    code, text = _run(["setup", "token", "--owner", OWNER], api, io.StringIO(value + "\n"))
    captured = capsys.readouterr()
    assert code == sc.EXIT_TROUBLE
    assert len(api.sent) == 1, "a refusal is not retried"
    everything = text + captured.out + captured.err
    assert "CLASSIC_PAT_BLOCKED" in everything
    assert CLASSIC_COPY in everything
    assert _leaked(value, everything) == []


def test_a_refusal_that_echoes_the_value_is_still_printed_without_it(capsys):
    """The route never echoes the value. If it ever did, this side still
    does not print it: every line it writes has the value taken out."""
    value = _token()
    refusal = SwarmError(f"POST {TOKEN_PATH} -> 422: token {value} is not valid", status=422,
                         code="validation_failed", detail={"field": "token", "input": value})
    api = FakeApi(error=refusal)
    code, text = _run(["setup", "token", "--owner", OWNER], api, io.StringIO(value + "\n"))
    captured = capsys.readouterr()
    assert code == sc.EXIT_TROUBLE
    assert _leaked(value, text, captured.out, captured.err) == []


def test_an_api_that_could_not_be_read_exits_1_through_main_without_the_value(monkeypatch,
                                                                              capsys):
    value = _token()
    api = FakeApi(error=SwarmError(f"could not reach https://swarm.example.com ({value})"))

    class Client:
        def __init__(self, context=None):
            pass

        def __enter__(self):
            return api

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sc, "SwarmClient", Client)
    monkeypatch.setattr(config, "session_target", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(value + "\n"))
    out = io.StringIO()
    code = sc.main(["setup", "token", "--owner", OWNER], out=out)
    captured = capsys.readouterr()
    assert code == sc.EXIT_FAIL
    assert len(api.sent) == 1
    assert _leaked(value, out.getvalue(), captured.out, captured.err) == []


# -- no option takes a token ------------------------------------------------------


def _parsers(parser: argparse.ArgumentParser, path=("sc",)):
    yield path, parser
    for action in parser._actions:  # noqa: SLF001 - argparse has no public walk
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, child in action.choices.items():
                yield from _parsers(child, path + (name,))


def _setup_token_parser() -> argparse.ArgumentParser:
    for path, parser in _parsers(sc.build_parser()):
        if path == ("sc", "setup", "token"):
            return parser
    raise AssertionError("sc has no `setup token`")


def test_setup_token_takes_the_owner_and_nothing_that_carries_a_value():
    parser = _setup_token_parser()
    common = argparse.ArgumentParser(add_help=False)
    sc._common(common, root=False)  # noqa: SLF001
    allowed = {"help", "owner"} | {a.dest for a in common._actions}  # noqa: SLF001
    dests = {a.dest for a in parser._actions}  # noqa: SLF001
    assert dests <= allowed, sorted(dests - allowed)
    assert not [a for a in parser._actions if not a.option_strings], "no positional value"  # noqa: SLF001
    owner = next(a for a in parser._actions if a.dest == "owner")  # noqa: SLF001
    assert owner.required


@pytest.mark.parametrize("flag", ["--token", "--value", "--pat", "--secret"])
def test_setup_token_refuses_a_value_on_the_command_line(flag, capsys):
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["setup", "token", "--owner", OWNER, flag, "x"])
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["setup", "token", "--owner", OWNER, "x"])


#: A credential's name as a whole word of a flag: `--pat`, `--git-token`,
#: never `--path` or `--patch`.
_CREDENTIAL_WORD = re.compile(r"(^|-)(token|pat|password|secret)(-|$)")

#: Flags named `token` that are not credentials: a listing's page cursor.
_CURSORS = {"--page-token"}


def test_no_sc_option_anywhere_takes_a_token_value():
    """A flag that takes a value and is named for a token, a PAT or a
    password is how a credential reaches shell history. `--client-secret-stdin`
    is a switch (it takes no value), which is the shape such a flag must have."""
    offending, visited = [], 0
    for path, parser in _parsers(sc.build_parser()):
        visited += 1
        for action in parser._actions:  # noqa: SLF001
            takes_value = action.nargs != 0 and not isinstance(
                action, (argparse._StoreTrueAction, argparse._StoreFalseAction,  # noqa: SLF001
                         argparse._HelpAction, argparse._SubParsersAction))  # noqa: SLF001
            for flag in action.option_strings:
                if takes_value and flag not in _CURSORS and _CREDENTIAL_WORD.search(flag.lower().lstrip("-")):
                    offending.append(" ".join(path) + " " + flag)
    assert visited > 20, f"walked only {visited} parsers; the scan ran over nothing"
    assert offending == []
