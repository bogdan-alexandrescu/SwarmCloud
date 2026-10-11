"""The schedule and approval tools of docs/schedules.md §7.2, and `sc schedules` / `sc approvals` (§7.3).

Lane S8. The routes (§7.1) were merged by S3 and S5; nothing in the bridge
called them. These hold the bridge to three rules:

* A CALLER NAMES A TYPE, NEVER A BACKEND (invariant 10). `swarm_schedule_create`
  refuses `image`, `command` and every other backend field -- top level or
  inside `params` -- and sends nothing.
* AN APPROVAL SENDS THE DIGEST IT SHOWED. `sc approvals approve` prints the
  item and its digest and asks for `approve` typed back; `swarm_schedule_approve`
  takes the digest from its caller. A digest that went stale comes back said
  plainly, with nothing retried.
* A READ-ONLY GRANT REACHES NO VERB THAT WRITES. `/sc` and the `sc` skill may
  be granted `sc schedules`, `sc schedules show|preview` and `sc approvals`,
  and nothing that creates, pauses, resumes, runs or decides.

The fake records every request it is sent, so "nothing was sent" is the
measurement of an empty list.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from swarm_api import scheduletypes
from test_plugin_commands import _READ_ONLY_SURFACES, _allowed, _granted_rules, _reached

from swarm_mcp import schedules, sc, server
from swarm_mcp.client import SwarmClient, SwarmError

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"

SID = "sch_0123456789ab"
OTHER = "sch_ba9876543210"
PARAMS_DIGEST = "sha256:" + "c" * 64
PLAN_DIGEST = "sha256:" + "d" * 64


def _schedule(sid=SID, name="nightly sweep", **extra):
    doc = {
        "schedule_id": sid,
        "tenant_id": "acme",
        "name": name,
        "type": "issue-sweep",
        "scope": {"mode": "repos", "repo_ids": ["repo_1"]},
        "cron": "0 9 * * 1-5",
        "timezone": "Europe/London",
        "words": "at 09:00, Monday to Friday",
        "state": "enabled",
        "revision": 3,
        "owner": "dev@example.com",
        "gate": {"run": "auto", "plan": "approve", "merge": "approve", "approvers": "members"},
        "budget": {"per_run_usd": 5.0, "per_day_usd": 20.0, "max_concurrent": 2},
        "next_run_at": "2026-10-12T08:00:00+00:00",
        "last_firing": {"firing_id": f"{sid}:1", "state": "done", "outcome": "succeeded",
                        "slot": "2026-10-10T08:00:00+00:00"},
        "spend_today": {"day": "2026-10-11", "reported_usd": 1.25, "unreported_attempts": 1,
                        "coverage": "partial"},
        "pending_approvals": 1,
        "tier": "plan",
    }
    doc.update(extra)
    return doc


def _firing(n, state="done"):
    return {
        "firing_id": f"{SID}:{n}", "schedule_id": SID, "slot": f"2026-10-{n:02d}T08:00:00+00:00",
        "state": state, "trigger": "slot", "outcome": "succeeded" if state == "done" else None,
        "work": [{"kind": "task", "id": f"task_{n}"}], "cost": {"reported_usd": 0.5},
    }


def _approval(item_id="run_" + SID + "_7", kind="run", digest=PARAMS_DIGEST, **extra):
    doc = {
        "approval_id": item_id, "tenant_id": "acme", "kind": kind,
        "subject": {"schedule_id": SID, "firing_id": f"{SID}:7"},
        "digest": digest, "summary": "nightly sweep: 3 candidate issues",
        "approvers": "members", "state": "pending",
        "requested_at": "2026-10-11T08:00:00+00:00", "expires_at": "2026-10-12T08:00:00+00:00",
    }
    doc.update(extra)
    return doc


class FakeApi:
    """Answers the schedule and approval routes; records everything it is sent."""

    def __init__(self, *, approval=None, approve_error=None, patch_error=None,
                 registrations=None, firings=12):
        self.sent: list[tuple[str, str, object]] = []
        self.approval = approval or _approval()
        self.approve_error = approve_error
        self.patch_error = patch_error
        self.registrations = registrations if registrations is not None else [
            {"repo_id": "repo_1", "owner": "o", "repo": "r"},
        ]
        self.firings = [_firing(n) for n in range(firings, 0, -1)]

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/schedule-types"):
            return {"types": [{"name": "issue-sweep", "description": "sweep", "available": True,
                               "min_interval_minutes": 60, "creatable_scopes": ["repos"],
                               "default_gate": {"run": "auto"}},
                              {"name": "pr-shepherd", "description": "shepherd", "available": False,
                               "disabled_reason": "no executor yet", "creatable_scopes": ["repos"]}],
                    "tenant_id": "acme"}
        if (method, path) == ("GET", "/v1/schedules"):
            return {"schedules": [_schedule(), _schedule(OTHER, "weekly index", type="repo-index-refresh")],
                    "tenant_id": "acme"}
        if (method, path) == ("POST", "/v1/schedules"):
            return {"schedule": _schedule(name=payload["name"], type=payload["type"]), "created": True}
        if (method, path) == ("POST", "/v1/schedules:preview"):
            return {"preview": {"words": "at 09:00, Monday to Friday",
                                "next": ["2026-10-12T09:00:00+01:00", "2026-10-13T09:00:00+01:00"],
                                "min_gap_minutes": 1440, "refusal": None}}
        if method == "GET" and path.startswith("/v1/repositories?"):
            return {"repositories": self.registrations, "next_page_token": None}
        if method == "GET" and path == f"/v1/schedules/{SID}":
            return {"schedule": _schedule(), "firings": self.firings}
        if method == "PATCH" and path == f"/v1/schedules/{SID}":
            if self.patch_error is not None:
                raise self.patch_error
            return {"schedule": _schedule(revision=payload["revision"] + 1)}
        if method == "POST" and path.startswith("/v1/schedules/sch_") and path.endswith(":pause"):
            return {"schedule": _schedule(state="paused", pause={"reason": (payload or {}).get("reason")})}
        if method == "POST" and path.startswith("/v1/schedules/sch_") and path.endswith(":resume"):
            return {"schedule": _schedule()}
        if method == "POST" and path.startswith("/v1/schedules/sch_") and path.endswith(":run"):
            return {"firing": _firing(13, "created" if not payload.get("dry_run") else "skipped")}
        if method == "GET" and path.startswith("/v1/approvals?") or (method, path) == ("GET", "/v1/approvals"):
            return {"approvals": [self.approval], "tenant_id": "acme"}
        if method == "GET" and path.startswith("/v1/approvals/"):
            return {"approval": self.approval}
        if method == "POST" and path.endswith(":approve") and path.startswith("/v1/approvals/"):
            if self.approve_error is not None:
                raise self.approve_error
            return {"approval": {**self.approval, "state": "approved", "decided_by": "dev@example.com"}}
        if method == "POST" and path.endswith(":reject") and path.startswith("/v1/approvals/"):
            return {"approval": {**self.approval, "state": "rejected", "reason": payload["reason"]}}
        raise AssertionError(f"unexpected request {method} {path}")

    def posts(self):
        return [s for s in self.sent if s[0] in ("POST", "PATCH", "DELETE")]


def _client(api: FakeApi) -> SwarmClient:
    """A SwarmClient whose transport is `api`: the methods under test are real."""
    client = object.__new__(SwarmClient)
    client.request = api.request  # type: ignore[method-assign]
    return client


def _call(api, name, args):
    return json.loads(server._call(_client(api), name, args))


@pytest.fixture()
def typed(monkeypatch):
    asked: list[str] = []

    def install(answer: str):
        def _ask(prompt):
            asked.append(prompt)
            return answer

        monkeypatch.setattr(sc, "_ask", _ask)
        return asked

    return install


def _sc(argv):
    return sc.build_parser().parse_args(argv + ["--width", "100", "--ascii"])


# --------------------------------------------------------------------------
# the MCP tool list (§7.2)
# --------------------------------------------------------------------------

SCHEDULE_TOOLS = {
    "swarm_schedule_types": set(),
    "swarm_schedules": set(),
    "swarm_schedule_create": {"name", "type", "cron"},
    "swarm_schedule_update": {"id", "revision"},
    "swarm_schedule_pause": {"id"},
    "swarm_schedule_resume": {"id"},
    "swarm_schedule_run_now": {"id"},
    "swarm_approvals": set(),
    "swarm_schedule_approve": {"approval_id", "digest"},
    "swarm_schedule_reject": {"approval_id", "reason"},
}

#: Invariant 10's backend fields, and the credentials no tool takes.
BACKEND = {"image", "command", "args", "entrypoint", "runner_profile", "resources", "env",
           "token", "forge_token"}


def test_the_tool_list_includes_every_schedule_tool():
    names = {t["name"] for t in server.TOOLS}
    missing = sorted(set(SCHEDULE_TOOLS) - names)
    assert not missing, f"§7.2 tools not advertised: {missing}"


@pytest.mark.parametrize("name,required", sorted(SCHEDULE_TOOLS.items()))
def test_each_tool_is_advertised_with_its_schema_and_no_backend_field(name, required):
    tool = next(t for t in server.TOOLS if t["name"] == name)
    schema = tool["inputSchema"]
    assert set(schema.get("required") or []) == required
    assert not BACKEND & set(schema["properties"]), f"{name} takes a backend field"


def test_the_create_description_names_every_catalogue_type_and_refuses_images():
    text = next(t for t in server.TOOLS if t["name"] == "swarm_schedule_create")["description"]
    for entry in scheduletypes.TYPES:
        assert f"`{entry.name}`" in text, f"the create tool never names {entry.name}"
    assert "no image or command" in text.lower()
    assert "swarm_schedule_types" in text


@pytest.mark.parametrize("field", ["image", "command", "runner_profile"])
def test_swarm_schedule_create_refuses_a_backend_field_and_sends_nothing(field):
    api = FakeApi()
    with pytest.raises(SwarmError, match="does not take"):
        server._call(_client(api), "swarm_schedule_create", {
            "name": "n", "type": "issue-sweep", "cron": "0 9 * * *", "repos": ["o/r"],
            field: "ghcr.io/x/y:latest",
        })
    assert api.sent == []


def test_swarm_schedule_create_refuses_an_image_inside_params_and_sends_nothing():
    api = FakeApi()
    with pytest.raises(SwarmError, match="image"):
        server._call(_client(api), "swarm_schedule_create", {
            "name": "n", "type": "issue-sweep", "cron": "0 9 * * *", "repos": ["o/r"],
            "params": {"image": "ghcr.io/x/y:latest"},
        })
    assert api.sent == []


@pytest.mark.parametrize("name", sorted(SCHEDULE_TOOLS))
def test_an_unknown_argument_is_refused_and_nothing_is_sent(name):
    api = FakeApi()
    with pytest.raises(SwarmError, match="does not take"):
        server._call(_client(api), name, {"id": SID, "approval_id": "a", "token": "x"})
    assert api.sent == []


# --------------------------------------------------------------------------
# the MCP tools, against the routes
# --------------------------------------------------------------------------

def test_swarm_schedule_create_resolves_repos_and_sends_data_only():
    api = FakeApi()
    answer = _call(api, "swarm_schedule_create", {
        "name": "nightly sweep", "type": "issue-sweep", "cron": "0 9 * * 1-5",
        "timezone": "Europe/London", "repos": ["O/R"], "params": {"max_issues": 5},
        "dry_run": True,
    })
    method, path, body = api.posts()[0]
    assert (method, path) == ("POST", "/v1/schedules")
    assert body["scope"] == {"mode": "repos", "repo_ids": ["repo_1"]}
    assert body["params"] == {"max_issues": 5}
    assert body["policy"] == {"dry_run": True}
    assert body["timezone"] == "Europe/London"
    assert body["client_request_id"], "a retried create returns the first schedule"
    assert not BACKEND & set(body)
    assert answer["schedule"]["schedule_id"] == SID and answer["created"] is True


def test_swarm_schedule_create_with_an_unregistered_repo_sends_nothing():
    api = FakeApi(registrations=[])
    with pytest.raises(SwarmError, match="not registered"):
        server._call(_client(api), "swarm_schedule_create", {
            "name": "n", "type": "issue-sweep", "cron": "0 9 * * *", "repos": ["o/r"],
        })
    assert api.posts() == []


def test_swarm_schedule_create_with_repos_scope_and_no_repo_sends_nothing():
    api = FakeApi()
    with pytest.raises(SwarmError, match="repos"):
        server._call(_client(api), "swarm_schedule_create",
                     {"name": "n", "type": "issue-sweep", "cron": "0 9 * * *"})
    assert api.sent == []


def test_swarm_schedules_lists_or_reads_one():
    api = FakeApi()
    listing = _call(api, "swarm_schedules", {})
    assert [s["schedule_id"] for s in listing["schedules"]] == [SID, OTHER]
    one = _call(api, "swarm_schedules", {"id": "Nightly Sweep"})
    assert one["schedule"]["schedule_id"] == SID and len(one["firings"]) == 12
    assert api.sent[-1][:2] == ("GET", f"/v1/schedules/{SID}")


def test_a_name_no_schedule_has_is_refused_and_nothing_is_written():
    api = FakeApi()
    with pytest.raises(SwarmError, match="no schedule"):
        server._call(_client(api), "swarm_schedule_pause", {"id": "nope"})
    assert api.posts() == []


def test_swarm_schedule_update_sends_the_revision_and_only_the_fields_given():
    api = FakeApi()
    _call(api, "swarm_schedule_update", {"id": SID, "revision": 3, "cron": "0 10 * * *"})
    assert api.posts() == [("PATCH", f"/v1/schedules/{SID}", {"revision": 3, "cron": "0 10 * * *"})]


def test_a_merge_auto_edit_is_refused_in_words_naming_the_switch():
    api = FakeApi(patch_error=SwarmError("422", status=422, code="use_merge_switch"))
    with pytest.raises(SwarmError) as exc:
        server._call(_client(api), "swarm_schedule_update",
                     {"id": SID, "revision": 3, "gate": {"merge": "auto"}})
    text = server._tool_error_text(exc.value)
    assert "merge switch" in text and "Nothing changed" in text


def test_a_stale_revision_is_refused_in_words():
    api = FakeApi(patch_error=SwarmError("409", status=409, code="schedule_changed"))
    with pytest.raises(SwarmError) as exc:
        server._call(_client(api), "swarm_schedule_update", {"id": SID, "revision": 2, "name": "x"})
    assert "read it again" in server._tool_error_text(exc.value)


def test_pause_resume_and_run_now_call_their_verbs():
    api = FakeApi()
    assert _call(api, "swarm_schedule_pause", {"id": SID, "reason": "holiday"})["schedule"]["state"] == "paused"
    _call(api, "swarm_schedule_resume", {"id": SID})
    _call(api, "swarm_schedule_run_now", {"id": SID, "dry_run": True})
    assert api.posts() == [
        ("POST", f"/v1/schedules/{SID}:pause", {"reason": "holiday"}),
        ("POST", f"/v1/schedules/{SID}:resume", None),
        ("POST", f"/v1/schedules/{SID}:run", {"dry_run": True}),
    ]


def test_run_now_passes_dry_run_strictly():
    api = FakeApi()
    _call(api, "swarm_schedule_run_now", {"id": SID, "dry_run": "false"})
    assert api.posts()[0][2] == {"dry_run": False}, "the string 'false' is not true"


def test_swarm_approvals_reads_the_inbox_with_its_kind():
    api = FakeApi()
    answer = _call(api, "swarm_approvals", {"kind": "merge"})
    assert api.sent == [("GET", "/v1/approvals?kind=merge", None)]
    assert answer["approvals"][0]["digest"] == PARAMS_DIGEST
    assert "THAT digest" in answer["next"]


def test_swarm_schedule_approve_sends_the_callers_digest_and_reads_nothing_first():
    api = FakeApi()
    answer = _call(api, "swarm_schedule_approve", {"approval_id": "run:run_9", "digest": PLAN_DIGEST})
    assert api.sent == [("POST", "/v1/approvals/run:run_9:approve", {"digest": PLAN_DIGEST})]
    assert answer["approval"]["state"] == "approved"


def test_a_merge_digest_object_is_sent_as_given():
    api = FakeApi()
    digest = {"head_sha": "abc123", "verdict": "approve"}
    _call(api, "swarm_schedule_approve", {"approval_id": "merge_x", "digest": digest})
    assert api.sent[0][2] == {"digest": digest}


@pytest.mark.parametrize("code", ["approval_changed", "merge_changed", "plan_changed"])
def test_a_stale_digest_reaches_the_caller_in_words_and_is_not_retried(code):
    api = FakeApi(approve_error=SwarmError("409", status=409, code=code))
    with pytest.raises(SwarmError) as exc:
        server._call(_client(api), "swarm_schedule_approve", {"approval_id": "a_1", "digest": PARAMS_DIGEST})
    text = server._tool_error_text(exc.value)
    assert "changed since it was shown" in text and "Nothing was done" in text
    assert len(api.posts()) == 1


def test_swarm_schedule_reject_sends_the_reason():
    api = FakeApi()
    _call(api, "swarm_schedule_reject", {"approval_id": "a_1", "reason": "not today"})
    assert api.posts() == [("POST", "/v1/approvals/a_1:reject", {"reason": "not today"})]


def test_an_id_with_a_slash_never_reaches_another_route():
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(_client(api), "swarm_schedule_reject", {"approval_id": "../tasks/x", "reason": "r"})
    assert api.sent == []


# --------------------------------------------------------------------------
# `sc schedules` and `sc approvals` (§7.3)
# --------------------------------------------------------------------------

def test_the_parser_reaches_every_section_7_3_command():
    p = sc.build_parser()
    assert p.parse_args(["schedules"]).func is sc.cmd_schedules
    assert p.parse_args(["schedules", "show", SID]).func is sc.cmd_schedules_show
    new = p.parse_args(["schedules", "new", "issue-sweep", "--repo", "o/r", "--cron", "0 9 * * 1-5",
                        "--tz", "Europe/London", "--param", "max_issues=5", "--param", "label=bug",
                        "--dry-run"])
    assert new.func is sc.cmd_schedules_new
    assert (new.type, new.repo, new.cron, new.tz, new.param, new.dry_run) == (
        "issue-sweep", ["o/r"], "0 9 * * 1-5", "Europe/London", ["max_issues=5", "label=bug"], True)
    assert p.parse_args(["schedules", "pause", SID, "--reason", "x"]).func is sc.cmd_schedules_pause
    assert p.parse_args(["schedules", "resume", SID]).func is sc.cmd_schedules_resume
    assert p.parse_args(["schedules", "run", SID, "--dry-run"]).func is sc.cmd_schedules_run
    preview = p.parse_args(["schedules", "preview", "0 9 * * 1-5", "--tz", "America/New_York"])
    assert preview.func is sc.cmd_schedules_preview and preview.tz == "America/New_York"
    assert p.parse_args(["approvals"]).func is sc.cmd_approvals
    assert p.parse_args(["approvals", "approve", "a_1"]).func is sc.cmd_approvals_approve
    assert p.parse_args(["approvals", "reject", "a_1", "--reason", "no"]).func is sc.cmd_approvals_reject
    with pytest.raises(SystemExit):
        p.parse_args(["approvals", "reject", "a_1"])  # --reason is required
    with pytest.raises(SystemExit):
        p.parse_args(["schedules", "new", "issue-sweep", "--repo", "o/r"])  # --cron is required


def test_sc_schedules_lists_like_the_console_table():
    out = io.StringIO()
    assert sc.cmd_schedules(_client(FakeApi()), _sc(["schedules"]), out) == sc.EXIT_OK
    printed = out.getvalue()
    for text in ("nightly sweep", "weekly index", "issue-sweep", "at 09:00, Monday to Friday",
                 "enabled", "2026-10-12T08:00:00+00:00", "1 pending", "$1.25", "partial"):
        assert text in printed, text


def test_sc_schedules_show_prints_the_last_ten_firings():
    out = io.StringIO()
    sc.cmd_schedules_show(_client(FakeApi(firings=12)), _sc(["schedules", "show", "nightly sweep"]), out)
    printed = out.getvalue()
    assert f"{SID}:12" in printed and f"{SID}:3" in printed
    assert f"{SID}:2 " not in printed and f"{SID}:1 " not in printed, "only the last 10"
    assert "Europe/London" in printed and "revision 3" in printed


def test_sc_schedules_new_parses_params_and_sends_one_create():
    api = FakeApi()
    out = io.StringIO()
    args = _sc(["schedules", "new", "issue-sweep", "--repo", "o/r", "--cron", "0 9 * * 1-5",
                "--param", "max_issues=5", "--param", "label=bug", "--param", "draft=true",
                "--dry-run"])
    assert sc.cmd_schedules_new(_client(api), args, out) == sc.EXIT_OK
    [(method, path, body)] = api.posts()
    assert (method, path) == ("POST", "/v1/schedules")
    assert body["params"] == {"max_issues": 5, "label": "bug", "draft": True}
    assert body["type"] == "issue-sweep" and body["name"] == "issue-sweep o/r"
    assert body["timezone"] == "UTC" and body["policy"] == {"dry_run": True}


@pytest.mark.parametrize("param", ["image=ghcr.io/x/y", "command=rm -rf /", "novalue"])
def test_sc_schedules_new_refuses_a_backend_or_malformed_param_and_sends_nothing(param):
    api = FakeApi()
    args = _sc(["schedules", "new", "issue-sweep", "--repo", "o/r", "--cron", "0 9 * * *",
                "--param", param])
    with pytest.raises(SwarmError):
        sc.cmd_schedules_new(_client(api), args, io.StringIO())
    assert api.posts() == []


def test_sc_schedules_pause_resume_and_run_resolve_a_name():
    api = FakeApi()
    sc.cmd_schedules_pause(_client(api), _sc(["schedules", "pause", "weekly index", "--reason", "r"]),
                           io.StringIO())
    assert api.posts()[-1] == ("POST", f"/v1/schedules/{OTHER}:pause", {"reason": "r"})
    api = FakeApi()
    sc.cmd_schedules_run(_client(api), _sc(["schedules", "run", SID, "--dry-run"]), io.StringIO())
    assert api.posts() == [("POST", f"/v1/schedules/{SID}:run", {"dry_run": True})]
    assert [m for m, _, _ in api.sent] == ["POST"], "an id is used as given, with no list read"


def test_sc_schedules_preview_prints_the_words_and_the_next_slots():
    api = FakeApi()
    out = io.StringIO()
    sc.cmd_schedules_preview(_client(api), _sc(["schedules", "preview", "0 9 * * 1-5", "--tz",
                                                "Europe/London"]), out)
    assert api.sent == [("POST", "/v1/schedules:preview", {"cron": "0 9 * * 1-5", "timezone": "Europe/London"})]
    assert "Monday to Friday" in out.getvalue() and "2026-10-13T09:00:00+01:00" in out.getvalue()


def test_sc_approvals_prints_the_inbox_and_how_to_decide():
    out = io.StringIO()
    sc.cmd_approvals(_client(FakeApi()), _sc(["approvals"]), out)
    printed = out.getvalue()
    assert "nightly sweep: 3 candidate issues" in printed and PARAMS_DIGEST in printed
    assert "sc approvals approve" in printed


def test_sc_approvals_approve_prints_the_item_and_sends_the_digest_it_printed(typed):
    asked = typed("approve")
    api = FakeApi()
    out = io.StringIO()
    sc.cmd_approvals_approve(_client(api), _sc(["approvals", "approve", "a_1"]), out)
    assert PARAMS_DIGEST in out.getvalue() and PARAMS_DIGEST in asked[0]
    assert api.sent[0][:2] == ("GET", "/v1/approvals/a_1")
    assert api.posts() == [("POST", "/v1/approvals/a_1:approve", {"digest": PARAMS_DIGEST})]


def test_sc_approvals_approve_sends_nothing_unless_approve_is_typed(typed, monkeypatch):
    typed("yes")
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")  # ignored: the confirmation is typed
    api = FakeApi()
    with pytest.raises(SwarmError, match="nothing was sent"):
        sc.cmd_approvals_approve(_client(api), _sc(["approvals", "approve", "a_1"]), io.StringIO())
    assert api.posts() == []


def test_sc_approvals_approve_with_digest_neither_reads_nor_asks(typed):
    asked = typed("never read")
    api = FakeApi()
    sc.cmd_approvals_approve(_client(api), _sc(["approvals", "approve", "a_1", "--digest", PLAN_DIGEST]),
                             io.StringIO())
    assert asked == []
    assert api.sent == [("POST", "/v1/approvals/a_1:approve", {"digest": PLAN_DIGEST})]


def test_sc_approvals_approve_refuses_an_item_already_decided(typed):
    typed("approve")
    api = FakeApi(approval=_approval(state="approved"))
    with pytest.raises(SwarmError, match="Nothing was sent"):
        sc.cmd_approvals_approve(_client(api), _sc(["approvals", "approve", "a_1"]), io.StringIO())
    assert api.posts() == []


def test_sc_approvals_reject_sends_the_reason():
    api = FakeApi()
    sc.cmd_approvals_reject(_client(api), _sc(["approvals", "reject", "a_1", "--reason", "no"]),
                            io.StringIO())
    assert api.posts() == [("POST", "/v1/approvals/a_1:reject", {"reason": "no"})]


# --------------------------------------------------------------------------
# the grants: a read-only surface reaches no schedule verb that writes
# --------------------------------------------------------------------------

#: The `sc` handlers that create a schedule, move one, run one or decide an
#: approval. No read-only surface may be granted them.
SCHEDULE_WRITES = frozenset({
    "sc.cmd_schedules_new", "sc.cmd_schedules_pause", "sc.cmd_schedules_resume",
    "sc.cmd_schedules_run", "sc.cmd_approvals_approve", "sc.cmd_approvals_reject",
})


def test_the_write_probes_reach_every_schedule_write_handler():
    reached = {
        _reached(f"sc {line}") for line in (
            "schedules new issue-sweep --repo o/r --cron x", "schedules pause x",
            "schedules resume x", "schedules run x", "approvals approve x",
            "approvals reject x --reason y",
        )
    }
    assert reached == SCHEDULE_WRITES


@pytest.mark.parametrize("path", _READ_ONLY_SURFACES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_read_only_surface_is_granted_a_schedule_verb_that_writes(path):
    offences = [
        f"Bash({rule}) allows `{command}`"
        for rule in _granted_rules(path)
        for command in _allowed(rule, SCHEDULE_WRITES)
    ]
    assert not offences, f"{path} promises to be read-only and grants: {offences}"


def test_slash_sc_is_granted_the_schedule_and_approval_views():
    rules = _granted_rules(_PLUGIN / "commands" / "sc.md")
    for view in ("uv run sc schedules", "uv run sc schedules show:*", "uv run sc schedules preview:*",
                 "uv run sc approvals"):
        assert view in rules, f"/sc is not granted {view}"
