"""The live routes' WIRE, held from both ends (lane LB-SEAM, part of #1030).

MEASURED 2026-10-11 on SwarmCloud: a claude-code-browser task had a working
live path, and `GET /v1/tasks/{id}/live/session` answered 200 with
`{"available": true, "session_id": ..., "you": {"may_take": ...}}`. The
console reads `{"live_browser": ..., "session": {..., "you": {"can_control":
...}}}`, found no `live_browser`, and drew no Browser tab. Lanes L9 (API) and
L11 (UI) were built in parallel against different shapes, and the only seam
check (test_runtimes_screen.py) compares PATHS, never bodies.

So the shape now lives once, as JSON, in tests/fixtures/live/, and both ends
read the same files:

  * here, every fixture is validated by the API's own model (`extra="forbid"`,
    so a key either side renames fails), and the REAL routes are driven over
    the in-memory Firestore and their answers compared, key for key, with the
    fixture for that route;
  * apps/swarm-ui/src/__tests__/live.seam.test.ts feeds the same files through
    the console's own loaders (`loadLiveSession` and the rest) and asserts
    what the Browser tab is drawn from: `live_browser: true` -> a Browser tab,
    `you.can_control`, and so on.

Every key, token and ticket here is built at runtime: nothing is a credential.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from swarm_api import live as live_module
from swarm_api.main import create_app  # noqa: F401  (the routes' module graph loads)

from .test_live_api import (  # noqa: F401  (pytest fixtures, used by name)
    ATTEMPT,
    READY,
    SOCKET,
    TASK,
    _ask_for_help,
    audit,
    bearer,
    client,
    clock,
    groups,
    ldb,
    live,
    people,
    relay,
    session_doc,
    ticket_for,
    wait_until,
)
from .test_live_relay_wire import relay_report_text

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "live"
INDEX = json.loads((FIXTURES / "index.json").read_text())["fixtures"]

#: Looked up by name, so a model that is missing fails the tests that use it
#: rather than the collection of this whole file.
MODELS: dict[str, type[BaseModel] | None] = {
    name: getattr(live_module, name, None)
    for name in ("LiveRead", "LiveTicketView", "TakeControl", "SetDevice", "RecordingsView",
                 "LiveActionsView")
}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def fits(served: Any, wire: Any, where: str = "$") -> list[str]:
    """Where `served` does not have the fixture's shape. A null on either side
    is accepted (the fixtures show a reported `origin`; the API serves null
    until the gate reports one); otherwise the same keys, recursively, and the
    same JSON type."""
    if served is None or wire is None:
        return []
    if isinstance(wire, dict):
        if not isinstance(served, dict):
            return [f"{where}: served {type(served).__name__}, wire is an object"]
        wrong = [f"{where}: keys {sorted(set(served) ^ set(wire))} on one side only"] \
            if set(served) != set(wire) else []
        for key in set(served) & set(wire):
            wrong += fits(served[key], wire[key], f"{where}.{key}")
        return wrong
    if isinstance(wire, list):
        if not isinstance(served, list):
            return [f"{where}: served {type(served).__name__}, wire is a list"]
        wrong = []
        for i, item in enumerate(served if wire else []):
            # An item fits when it has the shape of ANY item the fixture shows
            # (an action line carries `method` or `event`, not both).
            tries = [fits(item, w, f"{where}[{i}]") for w in wire]
            if all(tries):
                wrong += tries[0]
        return wrong
    if isinstance(wire, bool) or isinstance(served, bool):
        return [] if isinstance(wire, bool) and isinstance(served, bool) else [
            f"{where}: served {served!r}, wire {wire!r}"]
    if isinstance(wire, (int, float)):
        return [] if isinstance(served, (int, float)) else [f"{where}: not a number"]
    return [] if type(served) is type(wire) else [f"{where}: served {served!r}, wire {wire!r}"]


# --------------------------------------------------------------------------
# Every fixture is the API's model
# --------------------------------------------------------------------------


def test_the_index_names_every_fixture_file_and_no_other():
    named = {row["file"] for row in INDEX}
    on_disk = {p.name for p in FIXTURES.glob("*.json")} - {"index.json"}
    assert named == on_disk


@pytest.mark.parametrize("row", INDEX, ids=lambda r: f"{r['method']} {r['path']} {r['file']}")
def test_each_fixture_is_valid_against_the_routes_model(row):
    model = MODELS[row["model"]]
    assert model is not None, f"swarm_api.live has no {row['model']}"
    parsed = model.model_validate(fixture(row["file"]))
    # Lossless: nothing in the fixture was dropped or defaulted away. An
    # action line's absent fields stay absent on the wire
    # (`response_model_exclude_none`), as the console's `LiveAction` has them.
    dumped = parsed.model_dump(mode="json", exclude_none=row["model"] == "LiveActionsView")
    assert dumped == fixture(row["file"])


@pytest.mark.parametrize("row", INDEX, ids=lambda r: f"{r['method']} {r['path']}")
def test_each_route_in_the_index_is_served_with_that_model(row):
    """The route exists with that method and declares that model, so the model
    validated above is what the route actually answers through."""
    from fastapi.routing import APIRoute

    from swarm_api.routes.live import router

    matches = [
        r for r in router.routes
        if isinstance(r, APIRoute) and r.path == row["path"] and row["method"] in r.methods
    ]
    assert matches, f"swarm-api serves no {row['method']} {row['path']}"
    if row["direction"] == "response":
        assert matches[0].response_model is MODELS[row["model"]]


def test_a_fixture_with_an_unknown_key_is_refused():
    """The control: `extra="forbid"` is what makes the check above a shape
    check. The old answer's keys are refused by name."""
    old = {"available": True, "session_id": "live_att_x", "state": "open",
           "close_cause": None, "controller": None, "open_to_members": False,
           "human_wait": None, "you": {"may_view": True, "may_take": True}}
    model = MODELS["LiveRead"]
    assert model is not None
    with pytest.raises(Exception):
        model.model_validate(old)
    wrong_you = fixture("session.open.json")
    wrong_you["session"]["you"] = {"may_take": True, "in_control": False}
    with pytest.raises(Exception):
        model.model_validate(wrong_you)


# --------------------------------------------------------------------------
# The real routes answer in the fixtures' shape
# --------------------------------------------------------------------------


def _served(client, method: str, path: str, user: str, body: Any = None) -> Any:
    response = client.request(method, path.replace("{task_id}", TASK),
                              headers=bearer(user), json=body)
    assert response.status_code == 200, (method, path, response.text)
    return response.json()


def test_the_session_read_serves_live_browser_and_a_session(client, ldb, live):
    """THE DEFECT: the console needs `live_browser` and `session`."""
    _ask_for_help(ldb)
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")
    assert served["live_browser"] is True
    session = served["session"]
    assert session["session_id"] == "live_" + ATTEMPT
    assert session["state"] == "open" and session["closed_reason"] is None
    assert session["you"] == {"email": "alice@saga.xyz", "can_control": True,
                              "can_preempt": False, "can_open": True,
                              "can_switch_device": False}
    assert session["human_wait"]["expect"] == "github.com"
    # Nothing reported by the gate yet: null, never invented.
    assert session["origin"] is None and session["device"] is None
    assert [p["name"] for p in session["presets"]] == [
        "desktop", "iphone-15", "pixel-8", "iphone-se", "ipad-7"]
    assert fits(served, fixture("session.open.json")) == []
    assert fits(served, fixture("session.reported.json")) == []


def test_a_live_browser_task_before_its_tunnel_is_up_is_live_with_no_session(client, ldb, live):
    ldb.docs[f"tasks/{TASK}"]["current_lease_id"] = None
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")
    assert served == fixture("session.pending.json")


def test_a_task_on_another_profile_is_not_live(client, ldb, live):
    ldb.docs[f"tasks/{TASK}"]["runner_profile"] = "claude-code"
    ldb.docs[f"tasks/{TASK}"]["current_lease_id"] = None
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")
    assert served == fixture("session.not-live.json")


def test_a_closed_session_says_why(client, ldb, live):
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")
    assert served["session"]["state"] == "closed"
    assert served["session"]["closed_reason"] == "cancelled"
    assert fits(served, fixture("session.closed.json")) == []


def test_a_member_who_is_not_the_submitter_is_told_they_may_watch_only(client, ldb, live):
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "root")
    assert served["session"]["you"] == {"email": "root@saga.xyz", "can_control": False,
                                        "can_preempt": False, "can_open": False,
                                        "can_switch_device": False}


def test_the_ticket_route_serves_the_tickets_shape(client, ldb, live):
    served = _served(client, "POST", "/v1/tasks/{task_id}/live/ticket", "alice")
    assert fits(served, fixture("ticket.json")) == []
    assert served["socket_path"] == f"/v1/live/{served['session_id']}/stream"


def test_the_console_take_body_is_accepted_and_its_mismatch_audited(client, ldb, live):
    """`takeLiveControl` sends `confirmed_mismatch`; the route used to drop it."""
    _ask_for_help(ldb)
    body = fixture("control.take.request.json")
    body["preempt"] = False
    served = _served(client, "POST", "/v1/tasks/{task_id}/live/control", "alice", body)
    assert fits(served, fixture("control.taken.json")) == []
    assert served["session"]["controller"]["email"] == "alice@saga.xyz"
    taken = audit(ldb, "control_taken")
    assert [r["detail"].get("confirmed_mismatch") for r in taken] == [
        {"expected": "github.com", "actual": "github-signin.example"}]


def test_a_take_body_with_a_key_the_console_never_sends_is_refused(client, ldb, live):
    response = client.post(f"/v1/tasks/{TASK}/live/control", headers=bearer("alice"),
                           json={"preempt": False, "width": 4000})
    assert response.status_code in (400, 422), response.text
    assert session_doc(ldb)["controller"] is None


def test_renew_and_open_to_members_answer_as_the_session_read(client, ldb, live):
    assert _served(client, "POST", "/v1/tasks/{task_id}/live/control", "alice", {})
    renewed = _served(client, "POST", "/v1/tasks/{task_id}/live/control/renew", "alice")
    assert fits(renewed, fixture("control.taken.json")) == []
    opened = _served(client, "POST", "/v1/tasks/{task_id}/live/open-to-members", "alice")
    assert opened["session"]["open_to_members"] is True
    assert fits(opened, fixture("session.open.json")) == []


def test_delete_control_is_the_consoles_return(client, ldb, live):
    """`returnLiveControl` sends DELETE .../live/control; the API served only
    POST .../control/return, so Return control answered 405."""
    _ask_for_help(ldb)
    assert _served(client, "POST", "/v1/tasks/{task_id}/live/control", "alice", {})
    returned = _served(client, "DELETE", "/v1/tasks/{task_id}/live/control", "alice")
    assert returned["session"]["controller"] is None
    assert fits(returned, fixture("session.open.json")) == []
    assert session_doc(ldb)["controller"] is None
    assert ldb.docs[f"tasks/{TASK}"]["human_wait"] is None
    assert [r["detail"]["cause"] for r in audit(ldb, "control_returned")] == ["returned"]


def test_the_device_route_answers_as_the_session_read_and_takes_the_consoles_body(
        client, ldb, live, relay):
    """LB-DEV-B: `setLiveDevice` sends device.request.json; the route answers
    in device.switched.json's shape, and says the controller may switch."""
    _ask_for_help(ldb)
    assert _served(client, "POST", "/v1/tasks/{task_id}/live/control", "alice", {})
    served = _served(client, "POST", "/v1/tasks/{task_id}/live/device", "alice",
                     fixture("device.request.json"))
    assert fits(served, fixture("device.switched.json")) == []
    assert served["session"]["you"]["can_switch_device"] is True
    assert len(relay.devices) == 1


# --------------------------------------------------------------------------
# The origin and the device, as the gate reported them (LB-DEV-A)
# --------------------------------------------------------------------------

GITHUB = ("https://github.com/login", "github.com", "iphone-15", "portrait")


def _watch(client, relay, user: str, *texts: str) -> list[Any]:
    """Open `user`'s console socket with the relay double answering `ready`,
    then `texts`, then going away; everything the console received before
    swarm-api closed the socket (1012, "the relay went away")."""
    relay._frames, relay._refuse = [READY, *texts], True
    ticket = ticket_for(client, user)
    received: list[Any] = []
    with client.websocket_connect(SOCKET, headers=bearer(user)) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                received.append(ws.receive_json())
    assert closed.value.code == 1012
    return received


def test_a_reported_origin_and_device_are_stored_and_served_as_the_fixture_shows(client, ldb, live, relay):
    """THE GAP (LB-SEAM stated it): origin and device were always null. The
    gate's report, through the real relay's text, reaches swarm-api on the
    viewer link it already proxies; swarm-api validates it, records it on
    `live_sessions/{id}`, and the session read serves it."""
    _ask_for_help(ldb)
    assert _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]["origin"] is None
    _watch(client, relay, "alice", relay_report_text(*GITHUB))
    session = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    reported = fixture("session.reported.json")["session"]
    assert session["origin"] == reported["origin"]
    assert session["device"] == reported["device"]
    assert fits(session, reported) == []
    # Served to another member too: it is the session's, not the viewer's.
    assert _served(client, "GET", "/v1/tasks/{task_id}/live/session", "root")["session"]["device"] == {
        "preset": "iphone-15", "orientation": "portrait", "touch": True}


def test_the_report_is_recorded_not_forwarded_to_the_console(client, ldb, live, relay):
    received = _watch(client, relay, "alice", relay_report_text(*GITHUB))
    assert all(m.get("type") != "report" for m in received)


def _report(origin: Any = None, device: Any = None, **extra: Any) -> str:
    return json.dumps({"type": "report", "origin": origin, "device": device, **extra})


ORIGIN = {"origin": "https://github.com", "host": "github.com", "display_host": "github.com",
          "registrable_domain": "github.com", "scheme": "https:", "ip_literal": False, "cert_error": None,
          "path": "/"}
DEVICE = {"preset": "iphone-15", "orientation": "portrait", "touch": True}


@pytest.mark.parametrize("origin", [
    {**ORIGIN, "scheme": "javascript:"},
    {**ORIGIN, "origin": "https://evil.example", "host": "github.com"},
    {**ORIGIN, "host": "git hub.com", "origin": "https://git hub.com"},
    {**ORIGIN, "registrable_domain": "evil.example"},
    {**ORIGIN, "registrable_domain": "hub.com"},
    {**ORIGIN, "display_host": "github.com\u202egnp.exe"},
    {**ORIGIN, "display_host": "x" * 300},
    {**ORIGIN, "ip_literal": "no"},
    {**ORIGIN, "host": "h" * 254 + ".com", "origin": "https://" + "h" * 254 + ".com"},
    "https://github.com",
])
def test_an_origin_that_does_not_hold_together_is_not_stored(origin):
    """Origin is untrusted page data, and a strip that shows a forged one is
    the phishing defence failing (§5.7): every field is checked against the
    others before anything is stored, and a report that fails is null."""
    assert live_module.parse_report(_report(origin, DEVICE)) == {"origin": None, "device": DEVICE}


@pytest.mark.parametrize("device", [
    {**DEVICE, "preset": "galaxy-fold"},
    {**DEVICE, "orientation": "upside-down"},
    {"preset": "desktop", "orientation": "landscape", "touch": True},
    {**DEVICE, "touch": "yes"},
    {**DEVICE, "width": 393},
    "iphone-15",
])
def test_a_device_that_is_not_a_preset_by_name_is_not_stored(device):
    """Invariant 10: a device is a preset NAME; a report carrying dimensions,
    an unknown name or a touch the preset does not have is refused."""
    assert live_module.parse_report(_report(ORIGIN, device)) == {"origin": ORIGIN, "device": None}


def test_a_touch_preset_with_the_persons_touch_turned_off_is_stored():
    """LB-DEV-B: the controller's touch toggle. The gate reports a phone with
    touch off as touch false; that is a device the preset can be, not a forgery."""
    off = {**DEVICE, "touch": False}
    assert live_module.parse_report(_report(ORIGIN, off)) == {"origin": ORIGIN, "device": off}


@pytest.mark.parametrize("text", [
    "not json", json.dumps({"type": "ready"}), json.dumps([1]),
    _report(ORIGIN, DEVICE, url="https://github.com/login"),
    _report({**ORIGIN, "display_host": "x" * 5000}, DEVICE),
])
def test_a_message_that_is_not_a_report_or_is_too_large_is_none(text):
    assert live_module.parse_report(text) is None


def test_an_ip_literal_and_punycode_are_accepted_as_the_gate_reports_them():
    ip = {"origin": "https://[2001:db8::1]:8443", "host": "[2001:db8::1]", "display_host": "[2001:db8::1]",
          "registrable_domain": "", "scheme": "https:", "ip_literal": True, "cert_error": None,
          "path": "/"}
    puny = {**ORIGIN, "origin": "https://www.xn--bcher-kva.example", "host": "www.xn--bcher-kva.example",
            "display_host": "www.b\u00fccher.example", "registrable_domain": "xn--bcher-kva.example"}
    for origin in (ip, puny):
        assert live_module.parse_report(_report(origin, None)) == {"origin": origin, "device": None}


def test_a_stored_value_that_fails_the_check_is_served_as_null(client, ldb, live):
    """Read back through the same check: a document written by anything but
    this path still never reaches the strip unchecked."""
    session_doc(ldb).update({"origin": {**ORIGIN, "registrable_domain": "evil.example"},
                             "device": {**DEVICE, "preset": "galaxy-fold"}})
    session = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    assert session["origin"] is None and session["device"] is None


# --------------------------------------------------------------------------
# The presets are the gate's
# --------------------------------------------------------------------------


def test_the_served_presets_are_the_gates_fixed_list():
    from agent_worker.livegate.presets import PRESETS

    gate = [{"name": p.name, "width": p.width, "height": p.height, "scale": p.scale,
             "mobile": p.mobile} for p in PRESETS.values()]
    assert list(getattr(live_module, "LIVE_PRESETS", ())) == gate


# --------------------------------------------------------------------------
# Recordings and the action timeline (§7): served at all, in the console's shape
# --------------------------------------------------------------------------

REC = f"tenants/eng/tasks/{TASK}/attempts/{ATTEMPT}/live/"
WRITTEN = datetime(2026, 10, 11, 23, 41, tzinfo=timezone.utc)


def _record(objects) -> None:
    objects.put(REC + "rec-0000000000000.live.webm", b"\x1a\x45\xdf\xa3" + b"v" * 60,
                updated=WRITTEN)
    objects.put(REC + "rec-0000000000001.live.webm", b"\x1a\x45\xdf\xa3" + b"w" * 90,
                updated=WRITTEN.replace(minute=42))
    lines = [
        {"t": WRITTEN.timestamp(), "kind": "cdp", "method": "Page.navigate",
         "url": "https://github.com/login"},
        {"t": WRITTEN.timestamp() + 10, "kind": "cdp", "method": "Input.insertText", "length": 12},
        {"t": WRITTEN.timestamp() + 20, "kind": "control", "event": "control_taken"},
        {"t": "not a time", "kind": "cdp"},
    ]
    objects.put(REC + "actions-0000000000000.live.jsonl",
                "\n".join(json.dumps(x) for x in lines) + "\nnot json\n", updated=WRITTEN)
    # Neither a recording nor an action log: never listed.
    objects.put(f"tenants/eng/tasks/{TASK}/attempts/{ATTEMPT}/logs/agent_stdout.log", b"x")
    objects.put(f"tenants/research/tasks/{TASK}/attempts/{ATTEMPT}/live/rec-0000000000009.live.webm",
                b"theirs")


def test_the_recordings_list_is_served_in_both_of_the_consoles_shapes(client, ldb, live, objects):
    _record(objects)
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/recordings", "alice")
    assert fits(served, fixture("recordings.json")) == []
    assert [r["seq"] for r in served["segments"]] == [0, 1]
    assert [r["name"] for r in served["recordings"]] == [
        "rec-0000000000000.live.webm", "rec-0000000000001.live.webm"]
    assert all(r["object"].startswith(REC) and r["key"] == r["object"] for r in served["segments"])
    assert served["tenant_id"] == "eng" and served["task_id"] == TASK


def test_a_segment_streams_by_name_and_by_number_and_nothing_else(client, ldb, live, objects):
    _record(objects)
    by_name = client.get(f"/v1/tasks/{TASK}/live/recordings/raw",
                         params={"attempt_id": ATTEMPT, "name": "rec-0000000000001.live.webm"},
                         headers=bearer("alice"))
    assert by_name.status_code == 200, by_name.text
    assert by_name.headers["content-type"] == "video/webm"
    assert by_name.content.endswith(b"w" * 90)
    by_seq = client.get(f"/v1/tasks/{TASK}/attempts/{ATTEMPT}/live/recordings/1",
                        headers=bearer("alice"))
    assert by_seq.status_code == 200 and by_seq.content == by_name.content
    for name in ("../logs/agent_stdout.log", "actions-0000000000000.live.jsonl",
                 "rec-0000000000007.live.webm"):
        refused = client.get(f"/v1/tasks/{TASK}/live/recordings/raw",
                             params={"attempt_id": ATTEMPT, "name": name},
                             headers=bearer("alice"))
        assert refused.status_code == 404, name
    for attempt in ("..", "a/b", ".hidden"):
        refused = client.get(f"/v1/tasks/{TASK}/live/recordings/raw",
                             params={"attempt_id": attempt, "name": "rec-0000000000001.live.webm"},
                             headers=bearer("alice"))
        assert refused.status_code == 404, attempt


def test_another_tenants_member_gets_404_on_every_recording_route(client, ldb, live, objects):
    _record(objects)
    for path in (f"/v1/tasks/{TASK}/live/recordings",
                 f"/v1/tasks/{TASK}/live/recordings/raw?attempt_id={ATTEMPT}"
                 "&name=rec-0000000000001.live.webm",
                 f"/v1/tasks/{TASK}/attempts/{ATTEMPT}/live/recordings/1",
                 f"/v1/tasks/{TASK}/live/actions"):
        assert client.get(path, headers=bearer("bob")).status_code == 404, path
        assert client.get(path, headers=bearer("alice")).status_code == 200, path


def test_the_action_timeline_is_served_with_lengths_never_text(client, ldb, live, objects):
    _record(objects)
    served = _served(client, "GET", "/v1/tasks/{task_id}/live/actions", "alice")
    assert fits(served, fixture("actions.json")) == []
    assert served["attempt_id"] == ATTEMPT
    # The relay's nameless `control_taken` is not served: the audit holds the
    # same take with the person's name (LB-V3, test_live_timeline.py).
    assert [a.get("method") or a.get("event") for a in served["actions"]] == [
        "Page.navigate", "Input.insertText"]
    assert served["actions"][1]["length"] == 12
    assert served["actions"][0]["at"] == WRITTEN.isoformat()


def test_a_task_with_nothing_recorded_lists_nothing(client, ldb, live):
    assert _served(client, "GET", "/v1/tasks/{task_id}/live/recordings", "alice")["segments"] == []
    assert _served(client, "GET", "/v1/tasks/{task_id}/live/actions", "alice") == {
        "actions": [], "truncated": False}


# --------------------------------------------------------------------------
# Whether the agent is using this browser (LIVE-UNUSED; part of #1030)
# --------------------------------------------------------------------------
#
# MEASURED 2026-10-13 (task_7cda0d5335f049b4b442): the agent worked in a
# browser of its own while the live view stayed blank, and the console had no
# way to know. The gate's CDP proxy knows; its report carries it as `agent`,
# through the same relay text and `record_report` as the origin.

FRAME_AT = 1_791_900_000.0  # epoch seconds in October 2026, as the gate sends them
CONNECTED_AT = FRAME_AT + 7.5


def _agent(connected_at: Any = None, connections: Any = 0, first_frame_at: Any = FRAME_AT) -> dict[str, Any]:
    from agent_worker.livegate.report import agent_report

    return agent_report(connected_at=connected_at, connections=connections, first_frame_at=first_frame_at)


def test_a_report_with_no_agent_reads_exactly_as_before():
    """A gate from before LIVE-UNUSED sends two keys: nothing about it changes."""
    assert live_module.parse_report(_report(ORIGIN, DEVICE)) == {"origin": ORIGIN, "device": DEVICE}


def test_the_agent_crosses_the_real_relay_and_is_read_with_utc_times():
    text = relay_report_text(*GITHUB, agent=_agent(CONNECTED_AT, 1))
    parsed = live_module.parse_report(text)
    assert parsed is not None
    assert parsed["agent"] == {
        "connected_at": datetime.fromtimestamp(CONNECTED_AT, tz=timezone.utc),
        "connections": 1,
        "first_frame_at": datetime.fromtimestamp(FRAME_AT, tz=timezone.utc),
    }


@pytest.mark.parametrize("agent", [
    {"connected_at": None, "connections": 1, "first_frame_at": FRAME_AT},  # connected now, never connected
    {"connected_at": None, "connections": -1, "first_frame_at": FRAME_AT},
    {"connected_at": None, "connections": True, "first_frame_at": FRAME_AT},
    {"connected_at": None, "connections": 10_000, "first_frame_at": FRAME_AT},
    {"connected_at": "soon", "connections": 0, "first_frame_at": FRAME_AT},
    {"connected_at": None, "connections": 0, "first_frame_at": 1e30},
    {"connected_at": None, "connections": 0},
    {"connected_at": None, "connections": 0, "first_frame_at": FRAME_AT, "url": "https://x.test/"},
    "connected",
])
def test_an_agent_reading_that_does_not_hold_together_is_none_and_the_rest_is_kept(agent):
    assert live_module.parse_report(_report(ORIGIN, DEVICE, agent=agent)) == {
        "origin": ORIGIN, "device": DEVICE, "agent": None}


def test_the_agent_is_stored_and_served_and_its_first_connection_is_kept(client, ldb, live, relay):
    _ask_for_help(ldb)
    before = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    # Nothing reported yet: unknown, never "not connected".
    assert (before["agent_connected_at"], before["agent_connections"], before["first_frame_at"]) == (None, None, None)

    _watch(client, relay, "alice", relay_report_text(*GITHUB, agent=_agent()))
    waiting = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    assert waiting["agent_connected_at"] is None and waiting["agent_connections"] == 0
    assert waiting["first_frame_at"] == datetime.fromtimestamp(FRAME_AT, tz=timezone.utc).isoformat()

    _watch(client, relay, "alice", relay_report_text(*GITHUB, agent=_agent(CONNECTED_AT, 1)))
    using = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    connected = datetime.fromtimestamp(CONNECTED_AT, tz=timezone.utc).isoformat()
    assert using["agent_connected_at"] == connected and using["agent_connections"] == 1
    assert fits(using, fixture("session.reported.json")["session"]) == []

    # A relaunched gate's report has not seen the agent yet: the first
    # connection stays on the session, and the count is what it is now.
    _watch(client, relay, "alice", relay_report_text(*GITHUB, agent=_agent(None, 0)))
    later = _served(client, "GET", "/v1/tasks/{task_id}/live/session", "alice")["session"]
    assert later["agent_connected_at"] == connected and later["agent_connections"] == 0
