"""`swarm team add <group>` (DAYONE-TEAM-1, part of #2688): one path with the
console's team creation, ending with a green smoke agent, on fakes.

What has to hold (docs/design/day-one-plan.md, lane TEAM-1):

  * lesson 20: swarm-api's group identity is probed BEFORE anything is
    registered, and an unreadable group stops the verb with nothing written;
  * the team's `-git` value goes in only through the one-time link the verb
    prints; it never reads the terminal for a forge token (CLAUDE.md);
  * the verb ends with the smoke agent SUCCEEDED, after the workspace is
    ready and the namespace holds its verify keys;
  * review-fix finding 1: the smoke agent starts only once a pool account is
    owned by or lent to the team, and the wait says which (stage 12's accounts
    for the first team, a loan for any later one).

No credentials, no emulator, no cluster: swarm-api, the cluster and the clock
are fakes.
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import yaml

from swarm_install import team as team_mod
from swarm_install.events import Events
from swarm_install.exitcodes import ExitCode
from swarm_install.verbs import discover

GROUP = "eng@example.com"
TEAM = "g-eng"
WORKSPACE = "w-3f9a2c"
ADMIN = "alice@example.com"
PROJECT = "acme-swarm-1"


def _pem() -> str:
    return f"-----BEGIN PUBLIC KEY-----\n{secrets.token_hex(32)}\n-----END PUBLIC KEY-----\n"


def _settings() -> dict[str, str]:
    key = f"projects/{PROJECT}/locations/us-central1/keyRings/swarm-prod-specs/cryptoKeys/step-spec"
    return {"SPEC_VERIFY_KEYS": json.dumps({f"{key}/cryptoKeyVersions/1": _pem()}), "SPEC_SIGNING_KEY": key}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Api:
    """swarm-api, recording every call in order."""

    def __init__(self, *, probe: Mapping[str, Any] | None = None, teams: Sequence[str] = (),
                 workspace_states: Sequence[str] = ("approved", "provisioning", "ready"),
                 accounts_after: int = 0, smoke_states: Sequence[str] = ("QUEUED", "RUNNING", "SUCCEEDED")) -> None:
        self.calls: list[str] = []
        self._probe = probe if probe is not None else {"groups": [{"group": "admins@example.com", "state": "met"}],
                                                       "met": True}
        self._teams = list(teams)
        self._ws = list(workspace_states)
        self._accounts_after = accounts_after
        self._smoke = list(smoke_states)
        self.link_token = secrets.token_hex(16)

    def groups_probe(self) -> Mapping[str, Any]:
        self.calls.append("probe")
        return self._probe

    def team_ids(self) -> Sequence[str]:
        self.calls.append("teams")
        return list(self._teams)

    def create_team(self, group: str, *, display_name: str | None, first_admin: str | None) -> Mapping[str, Any]:
        self.calls.append(f"create {group}")
        return {"team": TEAM, "workspace": {"workspace_id": WORKSPACE, "state": "approved"}}

    def workspace(self, workspace_id: str) -> Mapping[str, Any]:
        self.calls.append("workspace")
        state = self._ws.pop(0) if len(self._ws) > 1 else self._ws[0]
        return {"workspace_id": workspace_id, "state": state}

    def team_git_link(self, team: str, owner: str) -> Mapping[str, Any]:
        self.calls.append(f"git-link {team} {owner}")
        return {"url": f"https://console.example.com/setup/link/{self.link_token}",
                "expires_at": "2026-10-29T23:00:00+00:00"}

    def pool_accounts(self, team: str) -> Sequence[Mapping[str, Any]]:
        self.calls.append("accounts")
        if self._accounts_after > 0:
            self._accounts_after -= 1
            return []
        return [{"id": "acct-1", "state": "AVAILABLE"}]

    def start_smoke(self, team: str) -> str:
        self.calls.append(f"smoke {team}")
        return "t-smoke-1"

    def task_state(self, team: str, task_id: str) -> str:
        self.calls.append("task")
        return self._smoke.pop(0) if len(self._smoke) > 1 else self._smoke[0]


class Cluster:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], dict] = {("Namespace", "", f"swarm-tenant-{TEAM}"): {}}

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None:
        return self.objects.get((kind, namespace, name))

    def apply(self, manifest: str) -> None:
        for doc in yaml.safe_load_all(manifest):
            meta = doc["metadata"]
            self.objects[(doc["kind"], meta.get("namespace", ""), meta["name"])] = doc


def _flow(api: Api, cluster: Cluster | None = None) -> tuple[team_mod.TeamAdd, io.StringIO, Events]:
    out = io.StringIO()
    clock = Clock()
    events = Events(clock.now, out=out)
    the_cluster = cluster or Cluster()
    flow = team_mod.TeamAdd(api=api, cluster=lambda: the_cluster, spec_settings=_settings,
                            clock=clock, events=events, out=out)
    return flow, out, events


@pytest.fixture(autouse=True)
def _no_terminal(monkeypatch) -> None:
    """Any read of the terminal fails the test: the verb never prompts."""
    def refuse(*_a: Any, **_k: Any) -> str:
        raise AssertionError("swarm team add read the terminal")

    monkeypatch.setattr("builtins.input", refuse)
    monkeypatch.setattr("getpass.getpass", refuse)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


# ---------------------------------------------------------------------------
# lesson 20: the probe comes first
# ---------------------------------------------------------------------------


def test_group_probe_before_registration() -> None:
    api = Api()
    flow, _out, _ = _flow(api)
    flow.run(GROUP, first_admin=ADMIN)
    assert api.calls[0] == "probe", api.calls
    assert api.calls.index("probe") < api.calls.index(f"create {GROUP}"), api.calls

    # An unreadable group stops the verb and registers nothing, naming both causes.
    refused = Api(probe={"groups": [{"group": "admins@example.com", "state": "unmet", "reason": "unreadable"}],
                         "met": False})
    flow, _out, _ = _flow(refused)
    with pytest.raises(team_mod.TeamError) as caught:
        flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("create") for c in refused.calls), refused.calls
    assert "signJwt" in str(caught.value) and "unauthorized_client" in str(caught.value)

    # An answer that names no group proves nothing, and nothing is registered.
    empty = Api(probe={"groups": [], "met": True})
    flow, _out, _ = _flow(empty)
    with pytest.raises(team_mod.TeamError):
        flow.run(GROUP)
    assert not any(c.startswith("create") for c in empty.calls), empty.calls


# ---------------------------------------------------------------------------
# the team's -git value: only through the link
# ---------------------------------------------------------------------------


def test_team_git_value_only_through_link() -> None:
    api = Api()
    flow, out, events = _flow(api)
    flow.run(GROUP, first_admin=ADMIN)
    text = out.getvalue()
    assert f"git-link {TEAM} {ADMIN}" in api.calls, api.calls
    # The link is printed whole, once, for the person to open.
    assert text.count(f"https://console.example.com/setup/link/{api.link_token}") == 1, text
    assert "never typed here" in text
    # No event carries it: events reach a Claude session (/sc:install).
    assert api.link_token not in json.dumps(events.record)
    # The module asks for no token anywhere (the autouse fixture fails any read).
    source = (team_mod.__file__ and open(team_mod.__file__).read()) or ""
    assert "input(" not in source and "getpass" not in source


def test_git_owner_defaults_to_the_first_admin_and_can_be_named() -> None:
    api = Api()
    flow, _out, _ = _flow(api)
    flow.run(GROUP, first_admin=ADMIN, git_owner="bob@example.com")
    assert f"git-link {TEAM} bob@example.com" in api.calls


# ---------------------------------------------------------------------------
# the end: a green smoke agent
# ---------------------------------------------------------------------------


def test_ends_with_smoke_green() -> None:
    api = Api()
    cluster = Cluster()
    flow, out, events = _flow(api, cluster)
    result = flow.run(GROUP, first_admin=ADMIN)
    assert result == {"team": TEAM, "workspace": WORKSPACE, "smoke_task": "t-smoke-1"}
    # In order: probe, create, the job to ready, keys, link, account, smoke.
    order = [c.split()[0] for c in api.calls]
    assert order.index("create") < order.index("workspace") < order.index("git-link") \
        < order.index("accounts") < order.index("smoke") < order.index("task"), api.calls
    assert ("ConfigMap", f"swarm-tenant-{TEAM}", "swarm-spec-verify-keys") in cluster.objects
    done = [e["step"] for e in events.of_kind("step_done")]
    assert done[-1] == "team.smoke", done
    assert "a green smoke agent (t-smoke-1)" in out.getvalue()


def test_a_failed_smoke_is_not_success() -> None:
    api = Api(smoke_states=("RUNNING", "FAILED"))
    flow, _out, _ = _flow(api)
    with pytest.raises(team_mod.TeamError, match="ended FAILED"):
        flow.run(GROUP, first_admin=ADMIN)


def test_a_workspace_the_job_stopped_stops_the_verb_before_any_smoke() -> None:
    api = Api(workspace_states=("provisioning", "needs_owner"))
    flow, _out, _ = _flow(api)
    with pytest.raises(team_mod.TeamError, match="needs_owner"):
        flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("smoke") for c in api.calls), api.calls


# ---------------------------------------------------------------------------
# review-fix finding 1: the smoke waits for a pool account
# ---------------------------------------------------------------------------


def test_smoke_waits_for_a_pool_account() -> None:
    api = Api(accounts_after=3)
    flow, out, events = _flow(api)
    flow.run(GROUP, first_admin=ADMIN)
    accounts = [i for i, c in enumerate(api.calls) if c == "accounts"]
    smoke = api.calls.index(f"smoke {TEAM}")
    assert len(accounts) == 4, api.calls
    assert all(i < smoke for i in accounts), "the smoke started before a pool account was there"
    # The first team (no team before it): stage 12's accounts, owned by it.
    text = out.getvalue()
    assert "first team" in text and "stage 12" in text, text
    waits = [e for e in events.of_kind("wait_countdown") if e["step"] == "team.pool-account"]
    assert len(waits) == 3 and "stage 12" in waits[0]["reason"], waits

    # A later team is told to borrow one.
    later = Api(teams=["g-first"], accounts_after=1)
    flow, out, _ = _flow(later)
    flow.run(GROUP, first_admin=ADMIN)
    assert "lend one of the pool's accounts" in out.getvalue()


def test_an_account_that_is_not_available_does_not_start_the_smoke() -> None:
    api = Api()
    api.pool_accounts = lambda team: [{"id": "acct-1", "state": "PAUSED"}]  # type: ignore[method-assign]
    flow, _out, _ = _flow(api)
    from swarm_install.waits import WaitGaveUp

    with pytest.raises(WaitGaveUp):
        flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("smoke") for c in api.calls)


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------


def test_the_verb_is_discovered_and_parses_team_add() -> None:
    verbs = discover()
    assert "team" in verbs
    parser = argparse.ArgumentParser()
    verbs["team"].add_arguments(parser)
    args = parser.parse_args(["add", GROUP, "--project", PROJECT, "--first-admin", ADMIN])
    assert (args.action, args.group, args.project, args.first_admin) == ("add", GROUP, PROJECT, ADMIN)


def test_the_verb_exits_fail_with_the_reason(monkeypatch, capsys) -> None:
    verb = discover()["team"]
    monkeypatch.setattr(verb, "read_state", lambda project: (_ for _ in ()).throw(
        team_mod.TeamError("no install state")))
    args = argparse.Namespace(project=PROJECT, group=GROUP, display_name=None, first_admin=None,
                              git_owner=None, context=None, json_events=False)
    assert verb.run(args) == int(ExitCode.FAIL)
    assert "no install state" in capsys.readouterr().err


def test_the_keys_are_the_ones_the_scheduler_hands_its_jobs() -> None:
    settings = _settings()

    class Gcp:
        def run_service(self, project: str, region: str, name: str) -> dict:
            assert (project, region, name) == (PROJECT, "us-central1", "swarm-scheduler")
            env = [{"name": k, "value": v} for k, v in settings.items()]
            env.append({"name": "UNRELATED", "value": "x"})
            return {"template": {"containers": [{"env": env}]}}

    assert team_mod.scheduler_spec_settings(Gcp(), PROJECT, "us-central1") == settings
