"""Every request that carries the tenant's forge token refuses a redirect (#645, part of #307).

urllib's default opener (`urlopen`) follows 301/302/303/307/308 and builds the
follow-up request with the original's headers, so a forge answer of `302
Location: https://elsewhere/` re-sends the request -- `Authorization: Bearer
<token>` included -- to a host the token must never reach. A same-host
redirect is refused too: following one silently is the same machinery, and
GitHub's API has no call here it needs to be followed for.

What is held, against real local HTTP servers standing in for the forge and
for wherever a `Location` header could point:

  * `forge._request` (probe, pull-request open/update) and `issue._open` (the
    issue fetch) refuse a 302 to another host, and that host receives nothing;
  * both refuse a 302 to the same host, which is asked exactly once;
  * a 200 still answers, with the token in the Authorization header only;
  * one opener, `forge._NO_REDIRECT_OPENER`, sends every one of these calls,
    and no module that handles the forge token builds an opener or calls
    `urlopen` of its own.

MUTATIONS: put `urllib.request.urlopen` back in `_request` -- the other-host
test sees the second server hit and the identity test sees no call. Give
`issue.py` its own opener again -- the identity and source tests fail. Make
`_NoRedirect.redirect_request` call `super()` -- every refusal test follows.
"""

from __future__ import annotations

import ast
import http.server
import threading
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest

from agent_worker import forge
from agent_worker import issue as issue_mod

from fake_github import fresh_token

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Server:
    """One local HTTP server recording every path and Authorization it receives."""

    def __init__(self, answer) -> None:  # noqa: ANN001
        self.hits: list[str] = []
        self.auth: list[str | None] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self) -> None:
                outer.hits.append(self.path)
                outer.auth.append(self.headers.get("Authorization"))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                status, headers, body = answer(self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PATCH = _any  # noqa: N815

            def log_message(self, *args: Any) -> None:
                return

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def _ok(_path: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json"}, b'{"stolen": true}'


def _redirect_to(target: str):  # noqa: ANN202
    def answer(path: str) -> tuple[int, dict[str, str], bytes]:
        if path == "/start":
            return 302, {"Location": target}, b""
        return _ok(path)

    return answer


@pytest.fixture
def elsewhere() -> Iterator[_Server]:
    with _Server(_ok) as server:
        yield server


def _issue_request(url: str, token: str) -> urllib.request.Request:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    return req


# -- forge._request: the probe and the pull-request calls -----------------------


def test_request_refuses_a_redirect_to_another_host_and_that_host_receives_nothing(elsewhere):
    token = fresh_token()
    with _Server(_redirect_to(f"{elsewhere.base}/steal")) as forge_host:
        with pytest.raises(forge.ForgeRedirectRefused) as raised:
            forge._request(f"{forge_host.base}/start", token=token)
    assert raised.value.status == 302
    assert forge_host.hits == ["/start"]
    assert elsewhere.hits == [], f"the redirect was followed: {elsewhere.hits}"
    assert token not in str(raised.value)


def test_request_refuses_a_redirect_to_the_same_host_too():
    token = fresh_token()
    with _Server(_redirect_to("/moved")) as forge_host:
        with pytest.raises(forge.ForgeRedirectRefused):
            forge._request(f"{forge_host.base}/start", token=token)
    assert forge_host.hits == ["/start"], f"the redirect was followed: {forge_host.hits}"


def test_request_refuses_a_redirected_post():
    """A 307 keeps the method and body: the open-pull-request POST is refused too."""
    token = fresh_token()

    def answer(path: str) -> tuple[int, dict[str, str], bytes]:
        if path == "/start":
            return 307, {"Location": "/moved"}, b""
        return _ok(path)

    with _Server(answer) as forge_host:
        with pytest.raises(forge.ForgeRedirectRefused):
            forge._request(
                f"{forge_host.base}/start", token=token, method="POST", payload={"a": 1}
            )
    assert forge_host.hits == ["/start"]


def test_request_answers_a_200_with_the_token_in_the_authorization_header_only():
    """The control: the opener that refuses redirects still answers."""
    token = fresh_token()
    with _Server(_ok) as forge_host:
        status, data = forge._request(f"{forge_host.base}/repos/o/r", token=token)
    assert (status, data) == (200, {"stolen": True})
    assert forge_host.auth == [f"Bearer {token}"]
    assert all(token not in hit for hit in forge_host.hits)


# -- issue._open: the issue fetch ------------------------------------------------


def test_issue_fetch_refuses_a_redirect_to_another_host_and_that_host_receives_nothing(
    elsewhere,
):
    token = fresh_token()
    with _Server(_redirect_to(f"{elsewhere.base}/steal")) as forge_host:
        with pytest.raises(issue_mod.IssueUnavailable) as raised:
            issue_mod._open(_issue_request(f"{forge_host.base}/start", token))
    assert forge_host.hits == ["/start"]
    assert elsewhere.hits == [], f"the redirect was followed: {elsewhere.hits}"
    assert token not in str(raised.value)
    # The refusal names the status: the old same-host handler refused this
    # local http:// target only for its scheme, and said nothing of a 302.
    assert "302" in str(raised.value) and "redirect" in str(raised.value)


def test_issue_fetch_refuses_a_redirect_to_the_same_host_too():
    token = fresh_token()
    with _Server(_redirect_to("/repositories/42/issues/1")) as forge_host:
        with pytest.raises(issue_mod.IssueUnavailable) as raised:
            issue_mod._open(_issue_request(f"{forge_host.base}/start", token))
    assert forge_host.hits == ["/start"], f"the redirect was followed: {forge_host.hits}"
    assert "302" in str(raised.value)


def test_issue_fetch_answers_a_200():
    token = fresh_token()
    with _Server(_ok) as forge_host:
        status, data, _headers = issue_mod._open(
            _issue_request(f"{forge_host.base}/repos/o/r/issues/1", token)
        )
    assert (status, data) == (200, {"stolen": True})
    assert forge_host.auth == [f"Bearer {token}"]


# -- one opener -----------------------------------------------------------------


class _RecordingOpener:
    """Stands in for `_NO_REDIRECT_OPENER`; answers 404 and records each request."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, req, timeout=None):  # noqa: ANN001
        self.urls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)


def test_every_token_carrying_call_goes_through_the_one_no_redirect_opener(monkeypatch):
    """`_request`, the pinned client's transport and the issue fetch all send
    through `forge._NO_REDIRECT_OPENER`: replace it, and every one of them is
    seen by the replacement. A call site that builds its own opener or calls
    `urlopen` would reach the network instead, and not be recorded."""
    import urllib.error  # noqa: F401  (used by _RecordingOpener)

    recorder = _RecordingOpener()
    monkeypatch.setattr(forge, "_NO_REDIRECT_OPENER", recorder)
    # An address nothing listens on: a call that escaped the recorder fails to
    # connect rather than reaching anything.
    base = "http://127.0.0.1:9"
    token = fresh_token()

    status, _ = forge._request(f"{base}/a", token=token)
    assert status == 404
    status, _headers, _body = forge._open(_issue_request(f"{base}/b", token))
    assert status == 404
    status, _data, _headers = issue_mod._open(_issue_request(f"{base}/c", token))
    assert status == 404

    assert recorder.urls == [f"{base}/a", f"{base}/b", f"{base}/c"]


def test_the_opener_has_no_handler_that_follows_a_redirect():
    handlers = forge._NO_REDIRECT_OPENER.handlers
    redirecting = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirecting and all(isinstance(h, forge._NoRedirect) for h in redirecting)
    req = _issue_request("https://api.github.com/x", fresh_token())
    for code in (301, 302, 303, 307, 308):
        assert (
            redirecting[0].redirect_request(req, None, code, "Moved", {}, "https://api.github.com/y")
            is None
        )


def _forge_modules(package: Path, forge_import: str) -> list[Path]:
    """Every module of `package` that is, or imports, its forge module."""
    found = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "forge.py" and path.parent == package or forge_import in text:
            found.append(path)
    return found


def _opener_calls(path: Path) -> list[tuple[str, int]]:
    """Every call to `urlopen` or `build_opener` in `path`, by name and line."""
    calls = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("urlopen", "build_opener"):
                calls.append((name, node.lineno))
    return calls


@pytest.mark.parametrize(
    ("package", "forge_import"),
    [
        ("apps/agent-worker/agent_worker", "forge"),
        ("apps/swarm-api/swarm_api", "forge"),
    ],
)
def test_no_module_that_handles_the_forge_token_builds_its_own_opener(package, forge_import):
    """forge.py builds the one opener; nothing that imports it opens a URL another way."""
    root = REPO_ROOT / package
    modules = _forge_modules(root, forge_import)
    assert len(modules) >= 3, f"the scan found too little to mean anything: {modules}"
    offenders = []
    for path in modules:
        calls = _opener_calls(path)
        if path == root / "forge.py":
            # The one opener itself.
            assert [name for name, _ in calls] == ["build_opener"], (path, calls)
            continue
        offenders += [f"{path.relative_to(REPO_ROOT)}:{line} {name}" for name, line in calls]
    assert offenders == [], f"a token-carrying module opens URLs with its own opener: {offenders}"


def test_issue_reads_github_hosts_from_forge_rather_than_restating_it():
    assert issue_mod.GITHUB_HOSTS is forge.GITHUB_HOSTS
    tree = ast.parse((REPO_ROOT / "apps/agent-worker/agent_worker/issue.py").read_text())
    assigned = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(getattr(t, "id", None) == "GITHUB_HOSTS"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    ]
    assert assigned == []
