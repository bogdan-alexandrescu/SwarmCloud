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
                 accounts_after: int = 0, smoke_states: Sequence[str] = ("QUEUED", "RUNNING", "SUCCEEDED"),
                 create_state: str = "approved", caller: str = ADMIN) -> None:
        self.calls: list[str] = []
        self._create_state = create_state
        self._caller = caller
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

    def caller(self) -> str:
        self.calls.append("me")
        return self._caller

    def create_team(self, group: str, *, display_name: str | None, first_admin: str | None) -> Mapping[str, Any]:
        """A re-run answers the workspace already standing, in whatever state
        it is (routes/people.py: 200, nothing new written)."""
        self.calls.append(f"create {group}")
        return {"team": TEAM, "workspace": {"workspace_id": WORKSPACE, "state": self._create_state}}

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        """The real route refuses anything but failed or needs_owner (409
        WorkspaceWrongState, `People.retry`); so does this fake."""
        self.calls.append(f"retry {workspace_id}")
        if self._create_state not in ("failed", "needs_owner"):
            raise AssertionError(f"409: workspace {workspace_id} is {self._create_state}; it cannot be retried")
        self._create_state = "approved"
        return {"workspace_id": workspace_id, "state": "approved"}

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


CONSOLE = "https://console.example.com"


class GitSecret:
    """Secret Manager's `swarm-tenant-<team>-git`: it has an enabled version
    only once the link's owner has opened it, `polls` reads after the link
    was minted (never before: no link, no value)."""

    def __init__(self, api: Api, *, polls: int = 1, standing: bool = False) -> None:
        self.api = api
        self.polls = polls
        self.standing = standing
        self.reads: list[str] = []

    def __call__(self, team: str) -> bool:
        self.reads.append(team)
        if self.standing:
            return True
        if not any(c.startswith("git-link") for c in self.api.calls):
            return False
        self.polls -= 1
        return self.polls < 0


class Pages:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def __call__(self, url: str, *, why: str, say: Any = print) -> None:
        self.opened.append((url, why))
        say(f"{why}\n  {url}")


def _flow(api: Api, cluster: Cluster | None = None, *, git: GitSecret | None = None,
          pages: Pages | None = None) -> tuple[team_mod.TeamAdd, io.StringIO, Events]:
    out = io.StringIO()
    clock = Clock()
    events = Events(clock.now, out=out)
    the_cluster = cluster or Cluster()
    flow = team_mod.TeamAdd(api=api, cluster=lambda: the_cluster, spec_settings=_settings,
                            git_secret=git if git is not None else GitSecret(api), console=CONSOLE,
                            clock=clock, events=events, out=out, open_page=pages or Pages())
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


def _team_args() -> argparse.Namespace:
    return argparse.Namespace(project=PROJECT, group=GROUP, display_name=None, first_admin=None,
                              git_owner=None, context=None, json_events=False)


@pytest.mark.parametrize("fails", [False, True])
def test_the_verb_closes_the_reach_its_kubectl_reopened(monkeypatch, capsys, fails: bool) -> None:
    """Finding 16: after the closing pass, team add's kubectl reopens the control
    plane through an engine-shaped context; the verb closes it when it finishes,
    whether the add succeeded or not, and says so when the close did not apply."""
    from swarm_install.state import InstallState
    from swarm_install.steps import infra

    verb = discover()["team"]
    state = InstallState(project=PROJECT, environment="dev", region="us-central1")
    marker = object()
    monkeypatch.setattr(verb, "read_state", lambda project: state)
    monkeypatch.setattr(verb, "verb_context", lambda project, s, events: marker)

    class Flow:
        def run(self, group: str, **kwargs: Any) -> dict:
            if fails:
                raise team_mod.TeamError("swarm-api refused")
            return {"team": TEAM}

    built: list[Any] = []

    def build(args: Any, s: Any, events: Any, ctx: Any = None) -> Flow:
        built.append(ctx)
        return Flow()

    closed: list[tuple[Any, str]] = []

    def close_after(ctx: Any, name: str) -> str:
        closed.append((ctx, name))
        return "swarm team add: the closing pass did not apply"

    monkeypatch.setattr(verb, "build", build)
    monkeypatch.setattr(infra.REACH, "close_after", close_after)
    code = verb.run(_team_args())
    assert code == int(ExitCode.FAIL if fails else ExitCode.OK)
    assert built == [marker], "kube_for reads the same context the close does"
    assert closed == [(marker, "swarm team add")]
    assert "the closing pass did not apply" in capsys.readouterr().err


def test_the_verb_context_can_apply_a_pass(monkeypatch) -> None:
    from swarm_install.state import InstallState
    from swarm_install.steps import cluster, infra

    verb = discover()["team"]
    monkeypatch.setattr(verb, "GcsObjectStore", lambda bucket: f"gs://{bucket}")
    monkeypatch.setattr("swarm_install.verbs.install.holder_identity", lambda: ADMIN)
    state = InstallState(project=PROJECT, environment="dev", region="us-central1")
    ctx = verb.verb_context(PROJECT, state, Events(lambda: 0.0, out=io.StringIO()))
    assert isinstance(ctx, infra.VerbContext) and ctx.engine.store.bucket.startswith("gs://")
    assert (ctx.project, ctx.holder, ctx.state) == (PROJECT, ADMIN, state)
    assert cluster.kube_for(ctx).reach is not None, "kubectl reopens the control plane after the close"


# ---------------------------------------------------------------------------
# audit 4e F12: "ready" only once the team's -git value exists; pages opened
# ---------------------------------------------------------------------------


def test_ready_only_once_the_git_secret_has_an_enabled_version() -> None:
    api = Api()
    git = GitSecret(api, polls=3)
    flow, out, events = _flow(api, git=git)
    flow.run(GROUP, first_admin=ADMIN)
    assert git.reads and set(git.reads) == {TEAM}, git.reads
    waits = [e for e in events.of_kind("wait_countdown") if e["step"] == "team.git-link"]
    assert len(waits) == 3, waits
    assert f"swarm-tenant-{TEAM}-git" in waits[0]["reason"], waits[0]
    done = [e["step"] for e in events.of_kind("step_done")]
    assert done.index("team.git-link") < done.index("team.smoke"), done
    assert "is ready" in out.getvalue()


def test_a_git_value_that_never_arrives_is_not_ready() -> None:
    from swarm_install.waits import WaitGaveUp

    api = Api()
    flow, out, _ = _flow(api, git=GitSecret(api, polls=10**6))
    with pytest.raises(WaitGaveUp) as gave_up:
        flow.run(GROUP, first_admin=ADMIN)
    assert gave_up.value.step == "team.git-link"
    assert "is ready" not in out.getvalue()
    assert not any(c.startswith("smoke") for c in api.calls), api.calls


def test_the_git_owner_defaults_to_the_caller_and_the_link_opens() -> None:
    api = Api()
    pages = Pages()
    flow, out, _ = _flow(api, pages=pages)
    flow.run(GROUP)
    assert f"git-link {TEAM} {ADMIN}" in api.calls, api.calls
    link = f"https://console.example.com/setup/link/{api.link_token}"
    assert [u for u, _ in pages.opened if u == link] == [link], pages.opened
    assert "later in Admin › Setup" not in out.getvalue()


def test_another_owner_gets_the_link_and_the_caller_gets_admin_setup() -> None:
    api = Api()
    pages = Pages()
    flow, out, _ = _flow(api, pages=pages)
    flow.run(GROUP, git_owner="bob@example.com")
    link = f"https://console.example.com/setup/link/{api.link_token}"
    # The link redeems for bob only: it is printed for him, never opened here.
    assert link not in [u for u, _ in pages.opened], pages.opened
    assert out.getvalue().count(link) == 1
    assert (f"{CONSOLE}/admin/setup", ) == tuple(u for u, _ in pages.opened if u.endswith("/admin/setup"))[:1]


def test_a_standing_git_value_mints_no_link() -> None:
    api = Api()
    pages = Pages()
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), pages=pages)
    flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("git-link") for c in api.calls), api.calls


@pytest.mark.parametrize(("teams", "page"), [((), "/admin/setup"), (("g-first",), "/admin/people")])
def test_the_pool_account_wait_opens_its_page(teams: tuple[str, ...], page: str) -> None:
    api = Api(teams=teams, accounts_after=2)
    pages = Pages()
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), pages=pages)
    flow.run(GROUP, first_admin=ADMIN)
    assert f"{CONSOLE}{page}" in [u for u, _ in pages.opened], pages.opened

    # An account already there opens nothing.
    ready = Api(teams=teams)
    pages = Pages()
    flow, _out, _ = _flow(ready, git=GitSecret(ready, standing=True), pages=pages)
    flow.run(GROUP, first_admin=ADMIN)
    assert pages.opened == [], pages.opened


# ---------------------------------------------------------------------------
# audit 4e F13: the re-run resumes, and the waits name the verb that does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stopped", ["failed", "needs_owner"])
def test_a_rerun_retries_a_stopped_workspace(stopped: str) -> None:
    api = Api(teams=(TEAM,), create_state=stopped, workspace_states=("approved", "ready"))
    flow, _out, _ = _flow(api)
    flow.run(GROUP, first_admin=ADMIN)
    assert f"retry {WORKSPACE}" in api.calls, api.calls
    assert api.calls.index(f"create {GROUP}") < api.calls.index(f"retry {WORKSPACE}") \
        < api.calls.index("workspace"), api.calls
    # The re-run of the first team is still the first team.
    assert any(c.startswith("smoke") for c in api.calls)


def test_a_rerun_of_a_standing_team_does_not_retry() -> None:
    api = Api(teams=(TEAM,), create_state="ready")
    flow, out, _ = _flow(api)
    flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("retry") for c in api.calls), api.calls
    # The team itself is not a team before it: it is still the first team.
    assert "first team" in out.getvalue()


def test_a_denied_workspace_is_not_retried() -> None:
    api = Api(create_state="denied")
    flow, _out, _ = _flow(api)
    with pytest.raises(team_mod.TeamError, match="denied"):
        flow.run(GROUP, first_admin=ADMIN)
    assert not any(c.startswith("retry") for c in api.calls), api.calls


def test_a_stopped_workspace_says_the_rerun_retries_it() -> None:
    api = Api(workspace_states=("provisioning", "failed"))
    flow, _out, _ = _flow(api)
    flow.resume = f"swarm team add {GROUP} --project {PROJECT}"
    with pytest.raises(team_mod.TeamError) as stopped:
        flow.run(GROUP, first_admin=ADMIN)
    assert f"swarm team add {GROUP} --project {PROJECT}" in str(stopped.value)


def test_a_team_wait_names_swarm_team_add_to_resume() -> None:
    from swarm_install.waits import WaitGaveUp

    api = Api(workspace_states=("provisioning",))
    flow, _out, _ = _flow(api)
    flow.resume = f"swarm team add {GROUP} --project {PROJECT}"
    with pytest.raises(WaitGaveUp) as gave_up:
        flow.run(GROUP, first_admin=ADMIN)
    text = str(gave_up.value)
    assert f"Run swarm team add {GROUP} --project {PROJECT} again to resume." in text, text
    assert "swarm install" not in text, text


def test_the_verb_builds_its_resume_command_and_console(monkeypatch) -> None:
    from swarm_install.state import InstallState

    verb = discover()["team"]
    state = InstallState(project=PROJECT, environment="dev", region="us-central1",
                         hostname="swarm.example.com")
    flow = verb.build(_team_args(), state, Events(lambda: 0.0, out=io.StringIO()), ctx=object())
    assert flow.resume == f"swarm team add {GROUP} --project {PROJECT}"
    assert flow.console == "https://swarm.example.com"


def test_the_git_secret_is_read_from_secret_manager() -> None:
    class Gcp:
        def __init__(self, secret: bool, version: int | None) -> None:
            self._secret, self._version = secret, version

        def secret(self, project: str, secret_id: str) -> dict | None:
            assert (project, secret_id) == (PROJECT, f"swarm-tenant-{TEAM}-git")
            return {"name": secret_id} if self._secret else None

        def enabled_version(self, project: str, secret_id: str) -> int | None:
            return self._version

    assert team_mod.git_secret_ready(Gcp(True, 1), PROJECT, TEAM) is True
    assert team_mod.git_secret_ready(Gcp(True, None), PROJECT, TEAM) is False
    assert team_mod.git_secret_ready(Gcp(False, None), PROJECT, TEAM) is False
