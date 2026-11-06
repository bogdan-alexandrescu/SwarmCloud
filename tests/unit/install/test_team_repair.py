"""`swarm team repair <team>` (DAYONE-TEAM-REPAIR, part of #2688): one team's
tenant render, applied again through the path that made it, on fakes.

What has to hold:

  * a team whose dispatcher Role was deleted gets it back: the command starts
    swarm-workspace-apply's `limits` run (the ceiling written again unchanged),
    which re-runs A7, the whole tenant render, waits for the run to end, puts
    the namespace's verify keys back as team add does, and ends only when the
    dispatcher and reaper Roles and RoleBindings are in the namespace;
  * a stopped workspace is retried instead (its create run re-applies all of it);
  * nothing changes without the team typed (SWARM_ASSUME_YES is not read);
  * a run that failed, a publish that did not go out, or a Role still absent
    afterwards is never success.

No credentials, no emulator, no cluster: swarm-api, the job, the cluster and
the clock are fakes.
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
TEAM = "eng"  # swarm_common.identity.tenant_id_for_group(GROUP)
NAMESPACE = f"swarm-tenant-{TEAM}"
WORKSPACE = "w-3f9a2c"
PROJECT = "acme-swarm-1"

#: The dispatcher RBAC the tenant render holds (kubernetes/rbac/dispatcher-rbac.yaml).
RENDER = [("Role", "swarm-dispatcher"), ("RoleBinding", "swarm-dispatcher"),
          ("Role", "swarm-reaper"), ("RoleBinding", "swarm-reaper")]


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


class Cluster:
    """The install cluster: the team's namespace, its tenant render, with the
    dispatcher Role deleted."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], dict] = {("Namespace", "", NAMESPACE): {}}
        for kind, name in RENDER:
            self.objects[(kind, NAMESPACE, name)] = {"kind": kind, "metadata": {"name": name}}
        del self.objects[("Role", NAMESPACE, "swarm-dispatcher")]

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None:
        return self.objects.get((kind, namespace, name))

    def apply(self, manifest: str) -> None:
        for doc in yaml.safe_load_all(manifest):
            meta = doc["metadata"]
            self.objects[(doc["kind"], meta.get("namespace", ""), meta["name"])] = doc


class Api:
    """swarm-api and the swarm-workspace-apply job behind it: a `limits` run
    (or a retry's create run) is claimed on the next read, and ends one read
    later having applied the tenant render into the cluster."""

    def __init__(self, cluster: Cluster, *, state: str = "ready", published: bool = True,
                 run_failure: Mapping[str, Any] | None = None, renders: bool = True,
                 limits: Mapping[str, int] | None = None) -> None:
        self.cluster = cluster
        self.calls: list[str] = []
        self.published = published
        self.run_failure = run_failure
        self.renders = renders
        self.record: dict[str, Any] = {
            "workspace_id": WORKSPACE, "kind": "group", "principal": GROUP, "state": state,
            "limits": dict(limits or {"max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64}),
            "run": {"build_id": "exec-1", "mode": "create", "claimed_at": "2026-10-30T10:00:00Z",
                    "finished_at": "2026-10-30T10:04:00Z"},
            "failure": None,
        }
        self.pending: str | None = None
        self.reads = 0

    def workspaces(self) -> Sequence[Mapping[str, Any]]:
        self.calls.append("list")
        other = {"workspace_id": "w-000001", "kind": "personal", "principal": "bob@example.com", "state": "ready"}
        return [other, dict(self.record)]

    def workspace(self, workspace_id: str) -> Mapping[str, Any]:
        assert workspace_id == WORKSPACE
        self.reads += 1
        if self.pending == "queued":
            self.pending = "claimed"
            mode = "create" if self.record["state"] == "approved" else "limits"
            self.record["run"] = {"build_id": f"exec-{self.reads}", "mode": mode,
                                  "claimed_at": "2026-10-31T03:00:00Z", "finished_at": None}
        elif self.pending == "claimed":
            self.pending = None
            if self.renders and not self.run_failure:
                self.cluster.apply("\n---\n".join(
                    yaml.safe_dump({"kind": k, "metadata": {"name": n, "namespace": NAMESPACE}})
                    for k, n in RENDER))
            self.record["run"] = {**self.record["run"], "finished_at": "2026-10-31T03:03:00Z"}
            self.record["failure"] = self.run_failure
            if self.record["state"] == "approved":
                self.record["state"] = "failed" if self.run_failure else "ready"
        return dict(self.record)

    def set_ceiling(self, workspace_id: str, max_active: int) -> Mapping[str, Any]:
        assert workspace_id == WORKSPACE
        self.calls.append(f"limits {max_active}")
        if self.published:
            self.pending = "queued"
        dispatch = {"published": self.published, "mode": "limits",
                    "reason": None if self.published else "publishing_off"}
        return {"workspace": dict(self.record), "dispatch": dispatch}

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        self.calls.append(f"retry {workspace_id}")
        self.record["state"] = "approved"
        self.pending = "queued"
        return {"workspace": dict(self.record)}


def _flow(api: Api, cluster: Cluster, *, typed: str | None = TEAM) -> tuple[team_mod.TeamRepair, list[str]]:
    out = io.StringIO()
    clock = Clock()
    asked: list[str] = []

    def confirm(prompt: str) -> str:
        asked.append(prompt)
        return "" if typed is None else typed

    flow = team_mod.TeamRepair(api=api, cluster=lambda: cluster, spec_settings=_settings, confirm=confirm,
                               clock=clock, events=Events(clock.now, out=out), out=out,
                               resume=f"swarm team repair {TEAM} --project {PROJECT}")
    return flow, asked


def test_a_deleted_dispatcher_role_is_recreated() -> None:
    cluster = Cluster()
    api = Api(cluster)
    assert cluster.get("Role", "swarm-dispatcher", NAMESPACE) is None
    flow, asked = _flow(api, cluster)

    result = flow.run(TEAM)

    assert cluster.get("Role", "swarm-dispatcher", NAMESPACE) is not None
    assert api.calls == ["list", "limits 8"], "the ceiling is written again unchanged: a limits run, no retry"
    assert asked and f"Type {TEAM}" in asked[0]
    assert cluster.get("ConfigMap", team_mod.VERIFY_KEYS_CONFIG_MAP, NAMESPACE) is not None
    assert result == {"team": TEAM, "workspace": WORKSPACE, "run": api.record["run"]["build_id"]}


def test_the_team_resolves_from_its_group_too() -> None:
    cluster = Cluster()
    flow, _ = _flow(Api(cluster), cluster, typed=GROUP)
    assert flow.run(GROUP)["team"] == TEAM


def test_nothing_changes_unless_the_team_is_typed() -> None:
    cluster = Cluster()
    api = Api(cluster)
    flow, _ = _flow(api, cluster, typed="yes")
    with pytest.raises(team_mod.RepairRefused):
        flow.run(TEAM)
    assert api.calls == ["list"]
    assert cluster.get("Role", "swarm-dispatcher", NAMESPACE) is None


def test_an_unknown_team_is_refused_before_anything() -> None:
    cluster = Cluster()
    api = Api(cluster)
    flow, asked = _flow(api, cluster)
    with pytest.raises(team_mod.TeamError, match="no workspace"):
        flow.run("research")
    assert asked == [] and api.calls == ["list"]


def test_a_stopped_workspace_is_retried() -> None:
    cluster = Cluster()
    api = Api(cluster, state="failed")
    flow, _ = _flow(api, cluster)
    flow.run(TEAM)
    assert api.calls == ["list", f"retry {WORKSPACE}"]
    assert cluster.get("Role", "swarm-dispatcher", NAMESPACE) is not None


def test_a_failed_run_is_not_success() -> None:
    cluster = Cluster()
    api = Api(cluster, run_failure={"code": "APPLY_FAILED", "step": "A7"})
    flow, _ = _flow(api, cluster)
    with pytest.raises(team_mod.TeamError, match="A7"):
        flow.run(TEAM)


def test_a_publish_that_did_not_go_out_is_not_success() -> None:
    cluster = Cluster()
    api = Api(cluster, published=False)
    flow, _ = _flow(api, cluster)
    with pytest.raises(team_mod.TeamError, match="publishing_off"):
        flow.run(TEAM)
    assert api.reads == 0, "nothing is waited for when nothing was sent"


def test_a_role_still_absent_after_the_run_is_not_success() -> None:
    cluster = Cluster()
    flow, _ = _flow(Api(cluster, renders=False), cluster)
    with pytest.raises(team_mod.TeamError, match="swarm-dispatcher"):
        flow.run(TEAM)


def test_limits_the_ceiling_route_changed_are_said() -> None:
    # The ceiling route writes the ceiling's own quota; a record whose quota
    # was set apart from its ceiling comes back changed, and the repair says
    # so rather than pass over it.
    cluster = Cluster()
    api = Api(cluster, limits={"max_active": 8, "capacity_units": 8, "quota_pods": 24, "quota_cpu": 64})
    real = api.set_ceiling

    def set_ceiling(workspace_id: str, max_active: int) -> Mapping[str, Any]:
        api.record["limits"] = {"max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64}
        return real(workspace_id, max_active)

    api.set_ceiling = set_ceiling  # type: ignore[method-assign]
    flow, asked = _flow(api, cluster)
    flow.run(TEAM)
    assert "quota_pods" in asked[0], "the confirmation shows the limits the route writes again"
    said = flow.out.getvalue()
    assert "quota_pods 24 -> 16" in said


def test_the_verb_parses_team_repair() -> None:
    parser = argparse.ArgumentParser()
    discover()["team"].add_arguments(parser)
    args = parser.parse_args(["repair", TEAM, "--project", PROJECT])
    assert (args.action, args.team, args.project) == ("repair", TEAM, PROJECT)


def test_the_verb_command_is_the_one_the_doctor_prints() -> None:
    from swarm_install import doctor

    verb = discover()["team"]
    args = argparse.Namespace(team=TEAM, project=PROJECT, context=None)
    assert verb.repair_command(args) == doctor.team_repair_command(TEAM, PROJECT)


@pytest.mark.parametrize(("typed", "code"), [(TEAM, ExitCode.OK), ("", ExitCode.REFUSED)])
def test_the_verb_exits_refused_without_the_typed_team(monkeypatch, typed: str, code: ExitCode) -> None:
    from swarm_install.state import InstallState
    from swarm_install.steps import infra

    verb = discover()["team"]
    cluster = Cluster()
    api = Api(cluster)
    monkeypatch.setattr(verb, "read_state", lambda project: InstallState(project=PROJECT, environment="dev",
                                                                          region="us-central1"))
    monkeypatch.setattr(verb, "verb_context", lambda project, s, events: object())
    monkeypatch.setattr(infra.REACH, "close_after", lambda ctx, name: "")
    monkeypatch.setattr("swarm_install.verbs.install._typed", lambda prompt: typed)

    def build_repair(args: Any, s: Any, events: Any, ctx: Any = None) -> team_mod.TeamRepair:
        from swarm_install.verbs import install

        clock = Clock()
        return team_mod.TeamRepair(api=api, cluster=lambda: cluster, spec_settings=_settings,
                                   confirm=install._typed, clock=clock, events=events, out=io.StringIO())

    monkeypatch.setattr(verb, "build_repair", build_repair)
    args = argparse.Namespace(action="repair", team=TEAM, project=PROJECT, context=None, json_events=False)
    assert verb.run(args) == int(code)


def test_swarm_api_reads_a_workspace_out_of_its_envelope() -> None:
    # GET /v1/admin/workspaces/{id} answers {"workspace": {...}}
    # (swarm_api.routes.people.get_workspace_record); read as it is, the state
    # was always "" and `swarm team add` waited out its bound on a ready team.
    class Client:
        def request(self, method: str, path: str, payload: Any = None) -> Any:
            assert (method, path) == ("GET", f"/v1/admin/workspaces/{WORKSPACE}")
            return {"workspace": {"workspace_id": WORKSPACE, "state": "ready"}}

    api = team_mod.SwarmApi()
    api._clients[""] = Client()
    assert api.workspace(WORKSPACE)["state"] == "ready"
