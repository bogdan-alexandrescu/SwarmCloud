"""`sc setup`, `sc access` and their bridge tools (#780, lane OB9).

The wizard walks docs/onboarding.md §4.3 against a fake API: connect GitHub
in the browser and wait for the checklist to say so, enable an owner, grant a
repository, verify, read the checklist again. These hold it to:

  * the happy path, from nothing to ready, in that order;
  * resuming: already connected means no new authorisation and no browser;
  * the browser half timing out with a clear message and nothing else sent;
  * grant and revoke sending exactly one write each, for the right repo_id;
  * NO TOKEN, CODE OR STATE PRINTED: the state appears only inside the one
    authorize URL shown, and a field the route should never carry is dropped.

The fake records every request, so "nothing was sent" is a measurement of a
list, not an absence of evidence. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import io
import json
import secrets
import sys
from pathlib import Path

import pytest

from swarm_mcp import sc, server
from swarm_mcp.client import SwarmError

TENANT = "eng"
USER = "example-user@example.com"
LOGIN = "example-user"

#: GitHub's `state`, minted at runtime so nothing here looks like a credential
#: to a scan, and unique so a leak cannot be confused with other text.
STATE = "st" + secrets.token_hex(16)
AUTHORIZE_URL = f"https://github.com/login/oauth/authorize?client_id=Iv1.example&state={STATE}"
#: A value the authorize route must never return. If it ever does, it must
#: still not reach a terminal or a tool reply.
STRAY = "never" + secrets.token_hex(12)

REPOS = ["example-user/example-api", "example-user/example-site"]


def _step(name, state, **evidence):
    return {"step": name, "state": state, "code": None, "copy": None, "checked_at": None,
            "evidence": evidence, "issues": []}


class FakeApi:
    """swarm-api's onboarding and access routes, over in-memory state."""

    def __init__(self, *, connected=False, connect_after_reads=None, via="user"):
        self.sent: list[tuple[str, str, object]] = []
        self.connected = connected
        #: After the authorize call, how many checklist reads until GitHub
        #: says connected (the person approving in the browser). None: never.
        self.connect_after_reads = connect_after_reads
        self.via = via
        self.authorized = False
        self.reads_since_authorize = 0
        self.enabled: set[str] = set()
        self.grants: dict[str, dict] = {}
        self.verified: set[str] = set()
        self.grant_error: SwarmError | None = None

    # -- helpers --------------------------------------------------------------
    def writes(self):
        return [(m, p, b) for m, p, b in self.sent if m != "GET"]

    def _view(self):
        if self.authorized and self.connect_after_reads is not None:
            self.reads_since_authorize += 1
            if self.reads_since_authorize >= self.connect_after_reads:
                # The callback page's exchange: a connection of the person's own.
                self.connected, self.via = True, "user"
        steps = [_step("signed_in", "done", email=USER, tenant_id=TENANT)]
        if self.connected:
            steps.append(_step("github_connected", "done", via=self.via, forge_login=LOGIN))
        else:
            steps.append(_step("github_connected", "todo", via=None))
        steps.append(_step("orgs_enabled", "done" if self.enabled else "todo"))
        steps.append(_step("repos_chosen", "done" if self.grants else "todo"))
        all_verified = bool(self.grants) and set(self.grants) <= self.verified
        steps.append(_step("access_verified", "done" if all_verified else "todo"))
        before = next((s["step"] for s in steps if s["state"] != "done"), None)
        steps.append(_step("ready", "done" if before is None else "todo"))
        nxt = next((s["step"] for s in steps if s["state"] != "done"), None)
        return {"tenant_id": TENANT, "user": USER, "steps": steps, "next_step": nxt,
                "complete": nxt is None}

    def _owners(self):
        return {"owners": [
            {"owner": LOGIN, "owner_type": "User", "installation_id": 11,
             "install_state": "installed", "sso": "unknown", "enabled": LOGIN in self.enabled,
             "install_url": None},
            {"owner": "example-org", "owner_type": "Organization", "installation_id": None,
             "install_state": "not_installed", "sso": "unknown", "enabled": False,
             "install_url": "https://github.com/apps/swarmcloud/installations/new"},
        ], "orgs_listed": True, "install_url": "https://github.com/apps/swarmcloud/installations/new",
            "tenant_id": TENANT}

    def _repos(self, owner):
        rows = []
        for name in REPOS:
            repo_id = sc.repo_id_for(TENANT, name)
            grant = self.grants.get(repo_id)
            rows.append({"repository": name, "owner": owner, "repo": name.split("/")[1],
                         "repo_id": repo_id, "visibility": "private", "archived": False,
                         "default_branch": "main", "can_push": True, "registered": False,
                         "granted": grant is not None, "mode": grant and grant["mode"]})
        return {"owner": owner, "repositories": rows, "page": 1, "per_page": 100,
                "max_pages": 10, "next_page": None, "capped": False, "q": None,
                "total_count": len(rows), "tenant_id": TENANT}

    # -- the transport -----------------------------------------------------------
    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/onboarding"):
            return self._view()
        if (method, path) == ("POST", "/v1/onboarding/github/authorize"):
            self.authorized = True
            return {"authorize_url": AUTHORIZE_URL, "expires_in_seconds": 600,
                    "state": STRAY, "code": STRAY}
        if (method, path) == ("GET", "/v1/access"):
            conn = ({"state": "active", "forge_login": LOGIN, "method": "app_user"}
                    if self.connected else None)
            return {"tenant_id": TENANT, "connection": conn,
                    "orgs": [{"owner": o, "enabled": True} for o in sorted(self.enabled)],
                    "grants": list(self.grants.values())}
        if (method, path) == ("GET", "/v1/access/orgs"):
            return self._owners()
        if (method, path) == ("POST", "/v1/access/orgs"):
            self.enabled.add(payload["owner"])
            return {"org": {"owner": payload["owner"], "enabled": True}, "tenant_id": TENANT}
        if method == "GET" and path.startswith("/v1/access/orgs/") and "/repositories?" in path:
            return self._repos(path.split("/")[4])
        if method == "PUT" and path.startswith("/v1/access/grants/"):
            if self.grant_error is not None:
                raise self.grant_error
            repo_id = path.rsplit("/", 1)[1]
            assert repo_id == sc.repo_id_for(TENANT, payload["repository"])
            grant = {"repo_id": repo_id, "repository": payload["repository"],
                     "owner": payload["repository"].split("/")[0], "mode": payload["mode"],
                     "can_push": True, "checks": {}}
            self.grants[repo_id] = grant
            return {"grant": grant, "registered": True, "tenant_id": TENANT}
        if method == "DELETE" and path.startswith("/v1/access/grants/"):
            repo_id = path.rsplit("/", 1)[1]
            self.grants.pop(repo_id)
            return {"repo_id": repo_id, "revoked": True, "unregistered": True}
        if method == "POST" and path.endswith("/verify"):
            repo_id = path.split("/")[4]
            self.verified.add(repo_id)
            grant = self.grants[repo_id]
            checks = {"clone": {"state": "ok"},
                      "push": {"state": "ok" if grant["mode"] == "write" else "not_required"},
                      "pull_request": {"state": "ok" if grant["mode"] == "write"
                                       else "not_required"}}
            grant["checks"] = checks
            return {"grant": grant, "failures": [], "passed": True, "tenant_id": TENANT}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


def _answers(*lines):
    """`ask`, answering each prompt in turn, then blank -- as a closed stdin does."""
    queue = list(lines)
    asked: list[str] = []

    def ask(prompt):
        asked.append(prompt)
        return queue.pop(0) if queue else ""

    ask.asked = asked
    return ask


def _wizard(api, *answers, **kwargs):
    out, clock, opened = io.StringIO(), Clock(), []

    def open_browser(url):
        opened.append(url)
        return True

    ask = _answers(*answers)
    code = sc.run_setup(api, out, ask=ask, open_browser=open_browser, sleep=clock.sleep,
                        clock=clock, interval=3, **kwargs)
    return code, out.getvalue(), opened, ask, clock


def _cli(argv, api, *, stdin=""):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        code = args.func(api, args, out)
    finally:
        sys.stdin = old
    return code, out.getvalue()


# -- the wizard -------------------------------------------------------------------


def test_setup_walks_from_nothing_to_ready_in_order():
    api = FakeApi(connect_after_reads=2)
    # enable: blank (every installed owner); open the install page: no;
    # choose in example-user: yes; grant #1 write; then done.
    code, text, opened, ask, _ = _wizard(api, "", "n", "y", "1 write", "")

    assert code == sc.EXIT_OK, text
    assert opened == [AUTHORIZE_URL]
    assert ("POST", "/v1/onboarding/github/authorize", {"surface": "plugin"}) in api.sent
    assert ("POST", "/v1/access/orgs", {"owner": LOGIN}) in api.sent
    repo_id = sc.repo_id_for(TENANT, REPOS[0])
    assert ("PUT", f"/v1/access/grants/{repo_id}",
            {"repository": REPOS[0], "mode": "write"}) in api.sent
    assert ("POST", f"/v1/access/grants/{repo_id}/verify", {}) in api.sent
    # The order is the checklist's: connect, owners, grant, verify.
    writes = [p for m, p, _ in api.writes()]
    assert writes == ["/v1/onboarding/github/authorize", "/v1/access/orgs",
                      f"/v1/access/grants/{repo_id}", f"/v1/access/grants/{repo_id}/verify"]
    assert f"Connected as {LOGIN}." in text
    assert "Ready: every step is done." in text
    assert "not installed on: example-org" in text


def test_already_connected_starts_no_authorisation_and_opens_no_browser():
    api = FakeApi(connected=True)
    code, text, opened, _, _ = _wizard(api, "", "n", "y", "2 read", "")

    assert code == sc.EXIT_OK, text
    assert opened == []
    assert not any(p == "/v1/onboarding/github/authorize" for _, p, _ in api.sent)
    assert "[x] github_connected  connected as example-user" in text


def test_the_tenant_token_is_not_connecting_as_yourself():
    """`github_connected` is done through the tenant's token: that is not D1's
    'as the user', so the wizard still connects."""
    api = FakeApi(connected=True, via="tenant", connect_after_reads=1)
    assert sc.connected_as_you(api._view()) is None
    _, text, opened, _, _ = _wizard(api, "", "n", "n")
    assert opened == [AUTHORIZE_URL]
    assert "the tenant's token, not yours" in text


def test_the_browser_half_times_out_with_a_clear_message_and_nothing_else_sent():
    api = FakeApi(connect_after_reads=None)
    code, text, _, ask, clock = _wizard(api, timeout=30)

    assert code == sc.EXIT_TROUBLE
    assert "GitHub was not connected within 1 minute" in text
    assert "again for a fresh link" in text and "Nothing was stored" in text
    # Polled across the whole window, then stopped: no owner, grant or verify.
    assert clock.now >= 30 and 5 <= clock.sleeps <= 12
    assert [p for _, p, _ in api.writes()] == ["/v1/onboarding/github/authorize"]
    assert not any(p.startswith("/v1/access") for _, p, _ in api.sent)
    assert ask.asked == []


def test_nothing_installed_stops_short_and_says_where_to_install():
    api = FakeApi(connected=True)
    api._owners = lambda: {"owners": [{"owner": "example-org", "owner_type": "Organization",
                                       "install_state": "not_installed", "enabled": False}],
                           "install_url": "https://github.com/apps/swarmcloud/installations/new"}
    code, text, _, _, _ = _wizard(api)
    assert code == sc.EXIT_TROUBLE
    assert "https://github.com/apps/swarmcloud/installations/new" in text
    assert "No owner is enabled yet" in text
    assert api.writes() == []


def test_setup_status_exits_by_the_checklist():
    assert _cli(["setup", "status"], FakeApi())[0] == sc.EXIT_TROUBLE
    api = FakeApi(connected=True)
    api.enabled.add(LOGIN)
    repo_id = sc.repo_id_for(TENANT, REPOS[0])
    api.grants[repo_id] = {"repo_id": repo_id, "repository": REPOS[0], "mode": "read"}
    api.verified.add(repo_id)
    code, text = _cli(["setup", "status"], api)
    assert code == sc.EXIT_OK and "Ready: every step is done." in text
    assert api.writes() == []


# -- sc access --------------------------------------------------------------------


def test_access_grant_sends_one_put_for_the_tenants_repo_id():
    api = FakeApi(connected=True)
    code, text = _cli(["access", "grant", "Example-User/Example-API", "--write"], api)
    assert code == sc.EXIT_OK
    repo_id = sc.repo_id_for(TENANT, "example-user/example-api")
    assert api.writes() == [("PUT", f"/v1/access/grants/{repo_id}",
                             {"repository": "Example-User/Example-API", "mode": "write"})]
    assert "granted" in text and "write" in text


def test_access_grant_needs_exactly_one_mode_and_sends_nothing_without_it():
    for flags in ([], ["--read", "--write"]):
        api = FakeApi(connected=True)
        with pytest.raises(SwarmError, match="--read or --write"):
            _cli(["access", "grant", REPOS[0], *flags], api)
        assert api.sent == []


def test_access_grant_refused_reads_the_recovery_copy():
    api = FakeApi(connected=True)
    copy = f"{REPOS[0]} is archived on GitHub, so nothing can be pushed to it."
    api.grant_error = SwarmError("PUT -> 422: archived", status=422, code="access_refused",
                                 detail={"failure_code": "REPO_ARCHIVED", "recovery": copy})
    with pytest.raises(SwarmError) as exc:
        _cli(["access", "grant", REPOS[0], "--write"], api)
    assert str(exc.value) == f"REPO_ARCHIVED: {copy}"


def test_access_revoke_deletes_the_grant_it_finds_and_refuses_one_not_held():
    api = FakeApi(connected=True)
    _cli(["access", "grant", REPOS[1], "--read"], api)
    repo_id = sc.repo_id_for(TENANT, REPOS[1])

    code, text = _cli(["access", "revoke", REPOS[1]], api)
    assert code == sc.EXIT_OK and f"revoked {REPOS[1]}" in text
    assert api.writes()[-1] == ("DELETE", f"/v1/access/grants/{repo_id}", None)

    before = len(api.writes())
    with pytest.raises(SwarmError, match="you hold no grant"):
        _cli(["access", "revoke", REPOS[1]], api)
    assert len(api.writes()) == before


def test_access_verify_exits_by_whether_every_check_passed():
    api = FakeApi(connected=True)
    _cli(["access", "grant", REPOS[0], "--write"], api)
    code, text = _cli(["access", "verify"], api)
    assert code == sc.EXIT_OK
    assert "pull request   ok" in text
    assert _cli(["access", "verify"], FakeApi(connected=True))[0] == sc.EXIT_TROUBLE


def test_interactive_access_grants_and_revokes_until_a_blank_answer():
    api = FakeApi(connected=True)
    api.enabled.add(LOGIN)
    code, _ = _cli(["access"], api, stdin=f"g\n2 read\n\nr {REPOS[1]}\n\n")
    assert code == sc.EXIT_OK
    repo_id = sc.repo_id_for(TENANT, REPOS[1])
    assert [(m, p) for m, p, _ in api.writes()] == [
        ("PUT", f"/v1/access/grants/{repo_id}"), ("DELETE", f"/v1/access/grants/{repo_id}")]


@pytest.mark.parametrize("argv,typed", [
    (["access", "remove-org", LOGIN], "someone-else"),
    (["access", "disconnect"], "yes"),
])
def test_removing_an_owner_or_disconnecting_needs_it_typed(argv, typed, monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi(connected=True)
    with pytest.raises(SwarmError, match="nothing was"):
        _cli(argv, api, stdin=typed + "\n")
    assert api.writes() == []


def test_the_repo_id_recipe_is_swarm_apis():
    """The access routes take the id; this restates swarm-api's recipe. The
    API refuses a wrong one, but a drift is better caught here."""
    repositories = pytest.importorskip("swarm_api.repositories")
    for name in ("example-user/example-api", "Saga/Widgets"):
        owner, repo = name.split("/")
        assert sc.repo_id_for("eng", name) == repositories.repo_id_for("eng", owner, repo)


# -- no token, code or state -----------------------------------------------------


def _leaks(text: str) -> list[str]:
    """Where the state shows OUTSIDE the one authorize URL, and any stray value."""
    found = []
    if STATE in text.replace(AUTHORIZE_URL, ""):
        found.append("state outside the authorize URL")
    if STRAY in text:
        found.append("a value the route should never carry")
    return found


def test_no_state_code_or_stray_value_is_printed(capsys):
    api = FakeApi(connect_after_reads=2)
    _, text, _, ask, _ = _wizard(api, "", "y", "n", "y", "1 write", "")
    captured = capsys.readouterr()
    everything = text + captured.out + captured.err + "".join(ask.asked)
    assert _leaks(everything) == []
    assert text.count(AUTHORIZE_URL) == 1, "the URL is shown once, and only once"


def test_the_timeout_message_carries_no_state(capsys):
    api = FakeApi(connect_after_reads=None)
    _, text, _, _, _ = _wizard(api, timeout=10)
    assert _leaks(text + capsys.readouterr().err) == []
    assert text.count(AUTHORIZE_URL) == 1


def test_the_connect_tool_returns_the_url_and_nothing_else_the_route_sent(monkeypatch):
    opened = []
    monkeypatch.setattr(sc, "_open_browser", lambda url: opened.append(url) or True)
    reply = server._call(FakeApi(), "swarm_setup_connect", {})
    body = json.loads(reply)
    assert set(body) == {"authorize_url", "expires_in_seconds", "opened", "next"}
    assert body["authorize_url"] == AUTHORIZE_URL and body["opened"] is True
    assert opened == [AUTHORIZE_URL]
    assert _leaks(reply) == []
    assert reply.count(STATE) == 1


# -- the bridge tools --------------------------------------------------------------

SETUP_TOOLS = {"swarm_setup_status", "swarm_setup_connect", "swarm_setup_orgs",
               "swarm_setup_repos", "swarm_setup_grant", "swarm_setup_revoke",
               "swarm_setup_verify", "swarm_access"}


def test_the_tools_are_served_and_setup_md_grants_each_on_both_names():
    served = {tool["name"] for tool in server.TOOLS}
    assert SETUP_TOOLS <= served
    text = (Path(__file__).resolve().parents[3] / "plugin" / "commands" / "setup.md").read_text()
    front = text.split("---", 2)[1]
    for name in SETUP_TOOLS:
        assert f"mcp__swarmcloud__{name}" in front, name
        assert f"mcp__plugin_sc_swarmcloud__{name}" in front, name
    assert "Bash(" not in front, "/sc:setup reaches the flow through the tools, not a shell"


def test_the_connect_tool_says_as_whom_when_already_connected():
    api = FakeApi(connected=True)
    body = json.loads(server._call(api, "swarm_setup_connect", {"open_browser": False}))
    assert body["already_connected_as"] == LOGIN
    assert api.writes() == []


def test_the_status_tool_waits_for_github_and_drops_the_evidence(monkeypatch):
    api = FakeApi(connect_after_reads=2)
    api.authorized = True
    clock = Clock()
    real = sc.wait_for_github
    monkeypatch.setattr(sc, "wait_for_github", lambda client, timeout: real(
        client, timeout=timeout, sleep=clock.sleep, clock=clock))
    body = json.loads(server._call(api, "swarm_setup_status", {"wait_for_github_seconds": 60}))
    assert body["connected_as_you"] == LOGIN
    assert "not_connected" not in body
    assert all("evidence" not in step for step in body["steps"])
    assert "[x] github_connected" in body["checklist"]

    with pytest.raises(SwarmError, match="0-600"):
        server._call(api, "swarm_setup_status", {"wait_for_github_seconds": 601})


def test_the_tools_enable_list_grant_verify_and_revoke():
    api = FakeApi(connected=True)
    orgs = json.loads(server._call(api, "swarm_setup_orgs", {"enable": LOGIN}))
    assert orgs["enabled"]["owner"] == LOGIN
    repos = json.loads(server._call(api, "swarm_setup_repos", {"owner": LOGIN}))
    assert [r["repository"] for r in repos["repositories"]] == REPOS
    server._call(api, "swarm_setup_grant", {"repository": REPOS[0], "mode": "write"})
    verified = json.loads(server._call(api, "swarm_setup_verify", {}))
    assert verified["passed"] is True and verified["results"][0]["repository"] == REPOS[0]
    access = json.loads(server._call(api, "swarm_access", {}))
    assert REPOS[0] in access["text"]
    server._call(api, "swarm_setup_revoke", {"repository": REPOS[0]})
    assert api.grants == {}


def test_a_tool_refusal_carries_the_recovery_copy():
    api = FakeApi(connected=True)
    api.grant_error = SwarmError("PUT -> 422", status=422, detail={
        "failure_code": "PERMISSION_MISSING", "recovery": "Ask for write access."})
    with pytest.raises(SwarmError, match="^PERMISSION_MISSING: Ask for write access.$"):
        server._call(api, "swarm_setup_grant", {"repository": REPOS[0], "mode": "write"})


def test_a_grant_tool_call_with_an_argument_it_does_not_take_sends_nothing():
    api = FakeApi(connected=True)
    with pytest.raises(SwarmError, match="does not take"):
        server._call(api, "swarm_setup_grant",
                     {"repository": REPOS[0], "mode": "write", "token": "x"})
    assert api.sent == []
