"""`sc`'s I/O half: what it fetches, where it sends its credential, and what
it exits with.

WHY THIS FILE EXISTS SEPARATELY FROM test_render.py. `render.py` is pure and
was tested exhaustively; everything on the other side of that boundary -- the
fetch fallbacks, the exit codes, the `--json` dump and the argument parser --
had no test at all, and that is precisely where the defects were. A screen
that is right and a command that exits 0 however broken the cluster is add up
to a status tool nothing can be built on.

Offline by construction, like its neighbours. The control plane here is a dict
and a fake client: no gcloud, no network, no credentials. The one thing that
must never happen in this layer -- forwarding a bearer token minted for
swarm-api to some other host -- is checked by making the construction of a
second client an error.
"""

from __future__ import annotations

import io
import json

import pytest

from swarm_mcp import cli, sc, server
from swarm_mcp.client import SwarmError

HEALTHY = {
    # The route's real shape: the tenant is NESTED (#88, SC-F3/F4).
    "/v1/tenants/me": {"tenant": {"tenant_id": "acme"}, "principal": {"email": "a@acme.test"}},
    "/v1/stats": {"dispatch_paused": False, "tasks_by_state": {}},
    "/v1/capacity": {"pools": [], "runner_profiles": {}},
    "/v1/tasks": {"tasks": []},
    "/v1/accounts": {"accounts": []},
}


class FakeClient:
    """A control plane that answers from a dict, and counts what was asked."""

    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, routes=None):
        self.routes = dict(HEALTHY if routes is None else routes)
        self.asked: list[str] = []

    def request(self, method, path, **kwargs):
        self.asked.append(path)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise SwarmError(f"{method} {path} -> 500: no such route", status=500)


def no_route_404(path="/v1/accounts"):
    return SwarmError(f"GET {path} -> 404: Not Found", status=404)


def run(command, argv, client):
    """One command, its exit code and what it wrote."""
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    return command(client, args, out), out.getvalue()


# ==========================================================================
# Where the credential goes
# ==========================================================================


class TestTheCredentialNeverLeavesTheApi:
    @pytest.fixture(autouse=True)
    def _no_second_client(self, monkeypatch):
        """Building a second client is the bug, so make it an error.

        The removed fallback constructed `SwarmClient(base_url=$SWARM_BROKER_URL)`
        and reused this process's ID token -- one minted for swarm-api -- as a
        bearer against whatever host that variable named, where it could be
        replayed back at swarm-api. That is the cross-audience mistake commit
        411d086 fixed one layer down.
        """

        def forbidden(*args, **kwargs):  # pragma: no cover - the assert is the point
            raise AssertionError("sc built a second client and sent its token elsewhere")

        monkeypatch.setattr(sc, "SwarmClient", forbidden)

    def test_a_broker_url_in_the_environment_is_not_a_second_host_to_try(self, monkeypatch):
        monkeypatch.setenv("SWARM_BROKER_URL", "http://127.0.0.1:8931")
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        with pytest.raises(SwarmError):
            sc.fetch_accounts(client)
        assert client.asked == ["/v1/accounts"]

    def test_every_fetch_goes_through_the_one_client_it_was_given(self, monkeypatch):
        monkeypatch.setenv("SWARM_BROKER_URL", "http://127.0.0.1:8931")
        client = FakeClient()
        snap = sc.collect(client, sc.NEEDS["overview"])
        assert snap.accounts == []
        assert sorted(client.asked)[0].startswith("/v1/")


# ==========================================================================
# An absent route is not a broken cluster
# ==========================================================================


class TestAnAbsentAccountsRoute:
    def test_a_404_from_the_api_is_recorded_as_absent_not_as_a_failure(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        snap = sc.collect(client, sc.NEEDS["overview"])
        assert snap.accounts is None, "an absent route is not an empty pool"
        assert snap.accounts_absent is True
        assert "404" in snap.accounts_error
        assert "no /v1/accounts route" in snap.accounts_error

    def test_googles_html_404_is_an_ingress_refusal_not_an_absent_route(self):
        # A 404 from the edge means "this caller may not reach the service",
        # which is a real failure and must keep its severity.
        edge = SwarmError("GET /v1/accounts -> 404: an HTML 404 from Google's edge", status=404, edge=True)
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": edge}))
        snap = sc.collect(client, sc.NEEDS["overview"])
        assert snap.accounts_absent is False

    def test_any_other_failure_keeps_its_severity(self):
        broken = SwarmError("GET /v1/accounts -> 500: boom", status=500)
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": broken}))
        snap = sc.collect(client, sc.NEEDS["overview"])
        assert snap.accounts_absent is False
        assert "500" in snap.accounts_error

    def test_a_deployment_without_the_route_is_not_reported_as_down(self):
        # The backwards-compatibility bar: an unchanged, healthy cluster that
        # simply predates the accounts proxy must not come back red.
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        code, out = run(sc.cmd_trouble, ["trouble"], client)
        assert code == sc.EXIT_OK
        assert "404" in out

    def test_the_overview_says_so_and_still_exits_clean(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        code, out = run(sc.cmd_overview, [], client)
        assert code == sc.EXIT_OK
        assert "could not be read" in out

    def test_the_accounts_view_itself_still_fails(self):
        # That view is ABOUT the pool and has nothing else to show, so "I
        # could not read it" is the honest answer for it.
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        code, _ = run(sc.cmd_accounts, ["accounts"], client)
        assert code == sc.EXIT_FAIL


# ==========================================================================
# Exit codes
# ==========================================================================


UNREACHABLE = {}


class TestExitCodes:
    def test_a_healthy_cluster_is_zero(self):
        assert run(sc.cmd_overview, [], FakeClient())[0] == sc.EXIT_OK

    def test_the_default_view_does_not_call_an_unreadable_cluster_a_success(self):
        # `sc` with no arguments is what `/sc` runs and what a wrapper wraps.
        # It used to return 0 while printing five `down` findings.
        code, out = run(sc.cmd_overview, [], FakeClient(routes=UNREACHABLE))
        assert code == sc.EXIT_FAIL
        assert "could not be" in out

    def test_the_three_whole_cluster_views_agree_on_one_snapshot(self):
        client = FakeClient(routes=UNREACHABLE)
        assert run(sc.cmd_overview, [], client)[0] == sc.EXIT_FAIL
        assert run(sc.cmd_trouble, ["trouble"], FakeClient(routes=UNREACHABLE))[0] == sc.EXIT_FAIL
        assert run(sc.cmd_accounts, ["accounts"], FakeClient(routes=UNREACHABLE))[0] == sc.EXIT_FAIL

    def test_unreadable_is_one_and_down_is_three(self):
        # Two different alerts: "I could not ask" is about the path to the
        # cluster, "it is broken" is about the cluster.
        paused = FakeClient(routes=dict(HEALTHY, **{"/v1/stats": {"dispatch_paused": True}}))
        assert run(sc.cmd_trouble, ["trouble"], paused)[0] == sc.EXIT_TROUBLE
        assert run(sc.cmd_overview, [], paused)[0] == sc.EXIT_TROUBLE

    def test_a_warning_is_not_worth_an_exit_code(self):
        one_account = dict(HEALTHY, **{"/v1/accounts": {"accounts": [
            {"account_id": "acme:main", "state": "PAUSED", "reason": "by hand",
             "windows": {}, "observed_at": None, "stale": True},
        ]}})
        code, out = run(sc.cmd_trouble, ["trouble"], FakeClient(routes=one_account))
        assert code == sc.EXIT_OK
        assert "paused" in out

    def test_a_narrow_view_fails_only_on_what_it_is_about(self):
        # `sc agents` must not fail because the account pool is unreadable.
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        assert run(sc.cmd_agents, ["agents"], client)[0] == sc.EXIT_OK
        assert "/v1/accounts" not in client.asked

    def test_the_json_path_exits_the_same_way_the_screen_does(self):
        assert run(sc.cmd_overview, ["--json"], FakeClient(routes=UNREACHABLE))[0] == sc.EXIT_FAIL
        assert run(sc.cmd_accounts, ["accounts", "--json"], FakeClient(routes=UNREACHABLE))[0] == sc.EXIT_FAIL
        paused = FakeClient(routes=dict(HEALTHY, **{"/v1/stats": {"dispatch_paused": True}}))
        assert run(sc.cmd_trouble, ["--json", "trouble"], paused)[0] == sc.EXIT_TROUBLE


# ==========================================================================
# What each view fetches
# ==========================================================================


class TestFetching:
    def test_a_view_fetches_what_it_prints_and_no_more(self):
        # The narrow views used to fetch `/v1/tenants/me` and render none of
        # it: a round trip whose failure was invisible on screen.
        client = FakeClient()
        run(sc.cmd_accounts, ["accounts"], client)
        assert client.asked == ["/v1/accounts"]

    def test_a_failure_is_never_an_empty_list(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/tasks": SwarmError("nope")}))
        snap = sc.collect(client, sc.NEEDS["agents"])
        assert snap.tasks is None
        assert snap.tasks_error == "nope"

    def test_an_accounts_response_without_the_field_is_a_failure(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": {"items": []}}))
        with pytest.raises(SwarmError):
            sc.fetch_accounts(client)

    def test_an_empty_pool_is_a_measurement(self):
        snap = sc.collect(FakeClient(), sc.NEEDS["accounts"])
        assert snap.accounts == [] and snap.accounts_error is None


# ==========================================================================
# --json
# ==========================================================================


SMUGGLED = {"accounts": [{
    "account_id": "acme:main",
    "owner_tenant": "acme",
    "label": "main",
    "provider": "anthropic",
    "state": "AVAILABLE",
    "reason": "",
    "lend_to": [],
    "assigned": 1,
    "windows": {"five_hour": {"utilization": 0.12, "resets_at": None, "reset": False}},
    "observed_at": None,
    "stale": True,
    "access_token": "sk-ant-secret",
    "access_token_len": 108,
}]}


class TestJson:
    def test_it_dumps_no_key_material_even_if_the_payload_carries_some(self):
        # The screen is defended by construction and by a test; this path
        # dumps what it fetched, so it is the one that would show a token
        # first if the broker's shape ever widened.
        _, out = run(sc.cmd_accounts, ["accounts", "--json"], FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": SMUGGLED})))
        assert "sk-ant-secret" not in out

        # AN ALLOWLIST, NOT A SEARCH FOR THE TWO FIELDS THIS FIXTURE SMUGGLES.
        #
        # This was `assert "108" not in out`, for `access_token_len: 108`, and
        # it was flaky by construction: a bare three-digit needle searched
        # across the whole document matches the microseconds of the timestamp
        # the dump generates. It failed in CI on `...2:51:35.651089+00:00`,
        # where `651089` contains `1089`. The clock decides whether that
        # assertion passes.
        #
        # Asserting the key SET is both unflakeable and strictly stronger. Two
        # substring checks could only ever catch the two fields someone thought
        # to smuggle in the fixture; this fails on ANY key the broker starts
        # returning that this tool has not been taught is safe -- which is the
        # actual risk the comment above describes, "if the broker's shape ever
        # widened".
        payload = json.loads(out)
        account = payload["accounts"][0]
        assert set(account) == {
            "account_id",
            "owner_tenant",
            "label",
            "provider",
            "state",
            "reason",
            "lend_to",
            "assigned",
            "windows",
            "observed_at",
            "stale",
        }, (
            "the JSON dump carried a field this tool does not know is safe: "
            f"{sorted(set(account) - {'account_id', 'owner_tenant', 'label', 'provider', 'state', 'reason', 'lend_to', 'assigned', 'windows', 'observed_at', 'stale'})}"
        )
        assert account["account_id"] == "acme:main"
        assert account["windows"]["five_hour"]["utilization"] == 0.12

    def test_unreadable_is_null_and_not_an_empty_list(self):
        _, out = run(sc.cmd_accounts, ["accounts", "--json"], FakeClient(routes=UNREACHABLE))
        payload = json.loads(out)
        assert payload["accounts"] is None
        assert payload["accounts_error"]

    def test_an_absent_route_is_visible_in_the_json_too(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        _, out = run(sc.cmd_overview, ["--json"], client)
        assert json.loads(out)["accounts_absent"] is True


# ==========================================================================
# The parser
# ==========================================================================


class TestParser:
    @pytest.mark.parametrize("argv", [["--json", "trouble"], ["trouble", "--json"]])
    def test_json_works_on_either_side_of_the_subcommand(self, argv):
        # A subparser writes ITS defaults over the namespace the root parser
        # already filled, so `sc --json trouble` used to parse as json=False
        # and print a human table -- em dashes and `~12%` -- to a script.
        assert sc.build_parser().parse_args(argv).json is True

    @pytest.mark.parametrize("argv", [["--width", "120", "accounts"], ["accounts", "--width", "120"]])
    def test_width_works_on_either_side_of_the_subcommand(self, argv):
        assert sc.build_parser().parse_args(argv).width == 120

    @pytest.mark.parametrize("argv", [["--ascii", "agents"], ["agents", "--ascii"]])
    def test_ascii_works_on_either_side_of_the_subcommand(self, argv):
        assert sc.build_parser().parse_args(argv).ascii is True

    @pytest.mark.parametrize("argv", [["--no-color", "capacity"], ["capacity", "--no-color"]])
    def test_no_color_works_on_either_side_of_the_subcommand(self, argv):
        assert sc.build_parser().parse_args(argv).color is False

    def test_a_flag_after_the_subcommand_still_wins_when_both_are_given(self):
        assert sc.build_parser().parse_args(["--width", "120", "accounts", "--width", "60"]).width == 60

    def test_every_attribute_exists_however_the_line_was_written(self):
        args = sc.build_parser().parse_args([])
        assert (args.json, args.width, args.ascii, args.color) == (False, None, None, None)

    def test_no_arguments_is_the_overview(self):
        assert sc.build_parser().parse_args([]).func is sc.cmd_overview

    def test_a_flag_before_the_subcommand_reaches_the_renderer(self):
        _, out = run(sc.cmd_accounts, ["--ascii", "accounts"], FakeClient(routes=UNREACHABLE))
        out.encode("ascii")


# ==========================================================================
# `swarm sc ...` and `swarm accounts ...`
# ==========================================================================


class TestTheSwarmSpelling:
    @pytest.fixture()
    def delegated(self, monkeypatch):
        seen: list[list[str]] = []
        monkeypatch.setattr(sc, "main", lambda argv=None, out=None: seen.append(list(argv)) or 0)
        return seen

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["accounts"], ["accounts"]),
            (["accounts", "--width", "60"], ["accounts", "--width", "60"]),
            (["sc"], []),
            (["sc", "trouble", "--json"], ["trouble", "--json"]),
        ],
    )
    def test_both_spellings_hand_the_line_to_sc_verbatim(self, delegated, argv, expected):
        assert cli.main(argv) == 0
        assert delegated == [expected]

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["accounts"], ["accounts"]),
            (["sc", "accounts", "--width", "60"], ["accounts", "--width", "60"]),
        ],
    )
    def test_the_argparse_route_runs_the_same_delegation(self, delegated, argv, expected):
        # It is unreachable while `main()` routes on the first token, and it
        # exists so `swarm --help` lists these -- but a second, divergent
        # implementation of the routing is how the two start disagreeing.
        args = cli.build_parser().parse_args(argv)
        assert args.no_client is True
        assert args.func(None, args) == 0
        assert delegated == [expected]

    def test_a_leading_option_is_why_main_routes_before_argparse(self):
        # `nargs=REMAINDER` still matches a LEADING option against the
        # subparser, so this spelling can only work by being routed on the
        # first token -- which is what `main()` does, and what the test above
        # deliberately does not rely on.
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["accounts", "--width", "60"])

    def test_there_is_no_second_implementation_of_it(self):
        assert not hasattr(cli, "_run_sc")
        assert not hasattr(cli, "_sc_alias")

    def test_swarms_own_subcommands_are_untouched(self):
        assert cli.build_parser().parse_args(["status", "t_1"]).task_ids == ["t_1"]


# ==========================================================================
# The same five views, as MCP tools
# ==========================================================================


class TestTheMcpViews:
    """Every one of these goes through `sc.collect` and `render`, so the tool
    and the terminal command cannot drift apart. These tests are what says so.
    """

    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("swarm_overview", "ACCOUNTS"),
            ("swarm_accounts", "ACCOUNTS"),
            ("swarm_capacity", "CAPACITY"),
            ("swarm_agents", "AGENTS"),
            ("swarm_trouble", "TROUBLE"),
        ],
    )
    def test_each_view_returns_the_screen_it_names(self, tool, expected):
        text = server._call(FakeClient(), tool, {"width": 80})
        assert expected in text
        for line in text.splitlines():
            assert len(line) <= 80, line

    def test_a_tool_reports_an_unreadable_area_rather_than_an_empty_one(self):
        # A host that got a blank section would tell the session the pool is
        # empty, which is the failure the whole surface exists to prevent.
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": no_route_404()}))
        text = server._call(client, "swarm_accounts", {})
        assert "could not be read" in text
        assert "UNKNOWN, not empty" in text

    def test_a_tool_carries_no_key_material_out_of_a_payload_that_had_some(self):
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": SMUGGLED}))
        text = server._call(client, "swarm_accounts", {})
        assert "sk-ant-secret" not in text
        assert "108" not in text

    def test_the_marks_survive_the_trip_through_a_tool(self):
        stale = {"accounts": [{
            "account_id": "acme:main", "state": "AVAILABLE", "assigned": 0,
            "windows": {"five_hour": {"utilization": 0.12, "resets_at": None, "reset": False}},
            "observed_at": None, "stale": True,
        }]}
        client = FakeClient(routes=dict(HEALTHY, **{"/v1/accounts": stale}))
        text = server._call(client, "swarm_accounts", {})
        assert "~12%" in text, "a stale reading must not arrive as a bare number"

    def test_every_advertised_view_is_one_this_module_can_answer(self):
        advertised = {t["name"] for t in server.TOOLS} & set(server._SC_VIEWS)
        assert advertised == set(server._SC_VIEWS)
        for name in server._SC_VIEWS:
            assert server._SC_VIEWS[name][1] in sc.NEEDS

