"""The worker actions' forge client: one host, no redirect, a bounded list (#295).

docs/merge-step.md §2.1b and §5. The merge and post-verdict actions hold an
installation token minted from a GitHub App key no agent has held, so the
client they talk through:

  * refuses any host but `api.github.com`, by exact equality;
  * NEVER follows a 3xx -- urllib's default opener does, and resends the
    Authorization header to wherever `Location` points;
  * follows `Link: rel="next"` only to the same host, and stops at a page and
    item cap with a refusal, never "checked and clean";
  * puts the token in the Authorization header and nowhere else.

MUTATIONS: build `_NO_REDIRECT_OPENER` with the default handlers -- the local
server test sees its second path hit. Drop the 3xx check in `_send` -- the
transport test reads a 302 as an answer. Drop the next-link host check -- the
off-host test follows it. Drop `max_items` -- the cap test reads 3000 files as
complete. Put the token in the query -- the header test sees it in the URL.
"""

from __future__ import annotations

import http.server
import threading
import urllib.request

import pytest

from agent_worker import forge

from fake_github import FakeGitHub, fresh_token


def _client(fake: FakeGitHub, token: str | None = None) -> forge.PinnedForgeClient:
    return forge.PinnedForgeClient(token=token or fresh_token(), transport=fake)


@pytest.mark.parametrize(
    "host",
    ["github.com", "api.github.com.example.invalid", "API.GITHUB.COM", "ghe.example.invalid",
     "api.github.com:443", ""],
)
def test_the_client_refuses_every_host_but_api_github_com(host):
    with pytest.raises(forge.ForgeHostRefused):
        forge.PinnedForgeClient(token=fresh_token(), host=host, transport=FakeGitHub())


def test_the_pinned_host_is_accepted():
    """The control for the refusal above."""
    client = forge.PinnedForgeClient(token=fresh_token(), transport=FakeGitHub())
    assert client.host == "api.github.com"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_not_followed(status):
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets",
               (status, {"Location": "https://elsewhere.invalid/steal"}, None))
    with pytest.raises(forge.ForgeRedirectRefused) as raised:
        _client(fake).get("/repos/acme/widgets")
    assert raised.value.code == "forge_redirect_refused"
    assert [s.url for s in fake.seen] == ["https://api.github.com/repos/acme/widgets"]


def test_the_real_opener_does_not_follow_a_redirect():
    """Against a real HTTP server: the 302's target is never requested.

    `_open` is the production transport; the server stands in for any host a
    `Location` header could name."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):  # noqa: D401
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        status, headers, _body = forge._open(urllib.request.Request(url))
    finally:
        server.shutdown()
    assert status == 302
    assert headers.get("Location") == "/stolen"
    assert hits == ["/start"], f"the redirect was followed: {hits}"


def test_the_token_is_sent_in_the_authorization_header_only():
    token = fresh_token()
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets", (404, {}, {"message": "Not Found"}))
    client = _client(fake, token)
    response = client.get("/repos/acme/widgets", query={"x": "1"})
    assert response.status == 404
    (seen,) = fake.seen
    assert seen.headers.get("Authorization") == f"Bearer {token}"
    assert token not in seen.url
    assert all(token not in v for k, v in seen.headers.items() if k != "Authorization")
    assert token not in repr(client)
    with pytest.raises(forge.ForgeAnswered) as raised:
        client.get_ok("/repos/acme/widgets")
    assert token not in str(raised.value)


def test_pagination_follows_next_to_the_end_on_the_same_host():
    fake = FakeGitHub()
    pages = {
        "1": (200, {"Link": '<https://api.github.com/repos/acme/widgets/pulls/1/files?page=2>; rel="next"'},
              [{"filename": "a"}]),
        "2": (200, {}, [{"filename": "b"}]),
    }
    fake.route("GET", "/repos/acme/widgets/pulls/1/files",
               lambda seen: pages[seen.query.get("page", ["1"])[0]])
    items = _client(fake).paginate("/repos/acme/widgets/pulls/1/files")
    assert [i["filename"] for i in items] == ["a", "b"]
    assert fake.seen[0].query["per_page"] == ["100"]


def test_a_next_page_on_another_host_is_refused():
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets/pulls/1/reviews",
               (200, {"Link": '<https://elsewhere.invalid/page2>; rel="next"'}, [{"id": 1}]))
    with pytest.raises(forge.ForgeHostRefused):
        _client(fake).paginate("/repos/acme/widgets/pulls/1/reviews")
    assert len(fake.seen) == 1


def test_the_item_cap_is_a_refusal_not_a_complete_list():
    """GitHub stops `pulls/{n}/files` at 3000 without saying so."""
    fake = FakeGitHub()

    def page(seen):
        number = int(seen.query.get("page", ["1"])[0])
        link = f'<https://api.github.com/repos/acme/widgets/pulls/1/files?page={number + 1}>; rel="next"'
        return 200, ({"Link": link} if number < 30 else {}), [{"filename": f"f{number}-{i}"} for i in range(100)]

    fake.route("GET", "/repos/acme/widgets/pulls/1/files", page)
    with pytest.raises(forge.PaginationCapReached):
        _client(fake).paginate("/repos/acme/widgets/pulls/1/files", max_items=3000)
    # One short of the cap reads completely: the control.
    assert len(_client(fake).paginate("/repos/acme/widgets/pulls/1/files", max_items=3001)) == 3000


def test_the_page_cap_stops_an_endless_list():
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets/pulls/1/reviews",
               (200, {"Link": '<https://api.github.com/repos/acme/widgets/pulls/1/reviews?page=2>; rel="next"'},
                [{"id": 1}]))
    with pytest.raises(forge.PaginationCapReached):
        _client(fake).paginate("/repos/acme/widgets/pulls/1/reviews", max_pages=3)
    assert len(fake.seen) == 3


def test_rules_for_branch_reads_the_one_effective_rules_endpoint():
    fake = FakeGitHub()
    rules = [
        {"type": "deletion"},
        {"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "ci-gate", "integration_id": 15368},
            {"context": "secret scan"},
        ]}},
    ]
    fake.route("GET", "/repos/acme/widgets/rules/branches/main", (200, {}, rules))
    read = _client(fake).rules_for_branch("acme", "widgets", "main")
    assert fake.seen[0].path == "/repos/acme/widgets/rules/branches/main"
    assert forge.required_status_checks(read) == [
        forge.RequiredCheck("ci-gate", 15368), forge.RequiredCheck("secret scan", None),
    ]


def test_an_outage_or_rate_limit_is_unavailable_with_its_retry_after():
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets", (429, {"Retry-After": "120"}, {"message": "slow down"}))
    with pytest.raises(forge.ForgeUnavailable) as raised:
        _client(fake).get("/repos/acme/widgets")
    assert raised.value.retry_after_seconds == 120


def test_the_job_forge_record_must_be_the_pinned_host():
    good = {"FORGE_HOST": "api.github.com", "FORGE_OWNER": "acme", "FORGE_REPO": "widgets",
            "REVIEW_APP_ID": "11", "REVIEW_APP_BOT_ID": "4242"}
    target = forge.forge_target_from_env(good, need_bot_id=True)
    assert (target.owner, target.repo, target.review_app_bot_id) == ("acme", "widgets", 4242)
    for broken in ({**good, "FORGE_HOST": "github.example.invalid"},
                   {**good, "FORGE_OWNER": "../x"},
                   {k: v for k, v in good.items() if k != "FORGE_HOST"}):
        with pytest.raises(forge.ForgeHostRefused):
            forge.forge_target_from_env(broken)
    with pytest.raises(forge.ForgeHostRefused):
        forge.forge_target_from_env({k: v for k, v in good.items() if k != "REVIEW_APP_BOT_ID"},
                                    need_bot_id=True)


def test_an_installation_token_is_minted_for_one_repository_and_never_shown():
    from fake_github import app_secret_payload

    fake = FakeGitHub()
    key = forge.parse_app_key(app_secret_payload(11))
    assert "PRIVATE" not in repr(key)
    token = forge.mint_installation_token(
        key=key, owner="acme", repo="widgets", permissions={"pull_requests": "write"},
        transport=fake,
    )
    assert token.token == fake.token and token.installation_id == 7
    assert fake.token not in repr(token)
    minted = fake.calls("POST", "/app/installations/7/access_tokens")[0]
    assert minted.body == {"repositories": ["widgets"], "permissions": {"pull_requests": "write"}}
    # The JWT went to the pinned host in the header, as three base64url parts.
    assert minted.headers["Authorization"].count(".") == 2
    assert forge.revoke_installation_token(_client(fake, token.token)) is True


def test_an_app_that_is_not_installed_is_rejected():
    from fake_github import app_secret_payload

    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets/installation", (404, {}, {"message": "Not Found"}))
    with pytest.raises(forge.AppRejected):
        forge.mint_installation_token(
            key=forge.parse_app_key(app_secret_payload(11)), owner="acme", repo="widgets",
            permissions={"pull_requests": "write"}, transport=fake,
        )
