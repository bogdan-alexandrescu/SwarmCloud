"""The theater's timeline records what people and the agent did (lane LB-V3,
part of #1030).

MEASURED 2026-10-12 05:48 PDT on SwarmCloud (visual QA pass 5): a person took
control, switched the device to iPhone 15, rotated it to landscape and
returned control. `GET /v1/tasks/{id}/live/actions` served two lines, which
the theater drew as "05:48:09 A person took control" and "05:48:09 refused".
The relay's action log held only its own `control_taken` and the gate's
`refused`; the device, the rotation, the return and the hand-off were written
to the task's `live_audit` (§5.8), which the route never read.

So the route merges the audit into the timeline, with who did each thing and
`you` for the reader, and drops the relay's nameless copy of the same take.
The B6 sequence is replayed here through the real routes.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from swarm_api import live as live_module

from .test_live_api import (  # noqa: F401  (pytest fixtures, used by name)
    ATTEMPT,
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
)
from .test_live_wire_shapes import fits, fixture

REC = f"tenants/eng/tasks/{TASK}/attempts/{ATTEMPT}/live/"

#: Everything a timeline line may carry. A key outside it is a field the
#: masking rules (design §5) were not asked about.
WIRE_KEYS = {"at", "kind", "method", "url", "length", "event", "cause", "outcome",
             "by", "you", "preset", "orientation", "touch"}


def _timeline(client, user: str) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/{TASK}/live/actions", headers=bearer(user))
    assert response.status_code == 200, response.text
    return response.json()


def _post(client, method: str, path: str, user: str, body: Any = None) -> None:
    response = client.request(method, f"/v1/tasks/{TASK}{path}", headers=bearer(user), json=body)
    assert response.status_code == 200, (path, response.text)


def _requested(ldb, clock) -> None:
    """The worker's hand-off request, as `handoff._audit` writes it."""
    _ask_for_help(ldb)
    ldb.docs["live_audit/la_request"] = {
        "id": "la_request", "entry": "handoff_requested", "task_id": TASK, "tenant_id": "eng",
        "attempt_id": ATTEMPT, "generation": 1, "email": None, "at": clock(),
        "detail": {"reason": "Sign in to GitHub", "expect": "github.com",
                   "request_id": "signin-1", "attempt_id": ATTEMPT, "generation": 1},
    }


def _b6(client, ldb, clock, objects) -> None:
    """Take control, the gate refuses the page, iPhone 15, rotate, touch off,
    return -- one second apart, as the person did it."""
    _requested(ldb, clock)
    clock.advance(1)
    _post(client, "POST", "/live/control", "alice", {})
    taken = clock().timestamp()
    # What the relay wrote for the same second: its own nameless copy of the
    # take, the gate's grant, then the gate's refusal and an agent method.
    lines = [
        {"t": taken, "kind": "control", "event": "control_taken"},
        {"t": taken, "kind": "control", "event": "granted"},
        {"t": taken + 0.5, "kind": "control", "event": "refused", "cause": "insecure_connection"},
    ]
    objects.put(REC + "actions-0000000000000.live.jsonl",
                "\n".join(json.dumps(x) for x in lines) + "\n", updated=clock())
    clock.advance(1)
    _post(client, "POST", "/live/device", "alice",
          {"preset": "iphone-15", "orientation": "portrait", "touch": True})
    clock.advance(1)
    _post(client, "POST", "/live/device", "alice", {"preset": "iphone-15", "orientation": "landscape"})
    clock.advance(1)
    _post(client, "POST", "/live/device", "alice", {"preset": "iphone-15", "touch": False})
    clock.advance(1)
    _post(client, "DELETE", "/live/control", "alice")


def test_the_b6_sequence_is_on_the_timeline_with_who_and_what(client, ldb, live, clock, objects):
    _b6(client, ldb, clock, objects)
    served = _timeline(client, "alice")
    assert served["attempt_id"] == ATTEMPT
    events = [a.get("event") for a in served["actions"]]
    assert events == [
        "handoff_requested", "control_taken", "refused", "device_changed", "rotated",
        "touch_toggled", "control_returned", "handoff_resolved",
    ]
    by_event = {a["event"]: a for a in served["actions"]}
    assert by_event["control_taken"]["by"] == "alice@saga.xyz"
    assert by_event["control_taken"]["you"] is True
    assert by_event["refused"]["cause"] == "insecure_connection"
    assert {k: by_event["device_changed"][k] for k in ("preset", "orientation", "touch")} == {
        "preset": "iphone-15", "orientation": "portrait", "touch": True}
    assert by_event["rotated"]["orientation"] == "landscape"
    assert by_event["rotated"]["touch"] is True, "a rotation leaves the toggle as it was"
    assert by_event["touch_toggled"]["touch"] is False
    assert by_event["touch_toggled"]["orientation"] == "landscape"
    assert by_event["control_returned"]["cause"] == "returned"
    assert by_event["handoff_resolved"]["outcome"] == "returned"
    assert all(datetime.fromisoformat(a["at"]) for a in served["actions"])
    assert fits(served, fixture("actions.json")) == []


def test_another_member_reads_the_name_not_you(client, ldb, live, clock, objects):
    _b6(client, ldb, clock, objects)
    served = _timeline(client, "root")
    taken = next(a for a in served["actions"] if a.get("event") == "control_taken")
    assert taken["by"] == "alice@saga.xyz" and taken["you"] is False


def test_the_relays_nameless_copy_of_a_take_is_not_shown_twice(client, ldb, live, clock, objects):
    """The control for the merge: the relay's line IS in the log, and the audit
    holds the same take; the timeline shows it once, with the name."""
    _b6(client, ldb, clock, objects)
    served = _timeline(client, "alice")
    taken = [a for a in served["actions"] if a.get("event") == "control_taken"]
    assert len(taken) == 1 and "by" in taken[0]
    assert not [a for a in served["actions"] if a.get("event") == "granted"]
    assert len(audit(ldb, "control_taken")) == 1


def test_no_line_carries_a_reason_a_host_a_count_or_a_field_outside_the_wire(
        client, ldb, live, clock, objects):
    """The audit's detail holds the hand-off's reason, the request id and
    counts; none of it is a timeline field (design §5)."""
    _b6(client, ldb, clock, objects)
    served = _timeline(client, "alice")
    for line in served["actions"]:
        assert set(line) <= WIRE_KEYS, line
    text = json.dumps(served)
    for leaked in ("Sign in to GitHub", "signin-1", "held_seconds", "episode_id", "pointer_events"):
        assert leaked not in text


def test_with_no_recording_the_audit_alone_is_the_timeline(client, ldb, live, clock):
    """Recording off (or the relay's segment not yet closed): what people did
    is still on the timeline, on the attempt the audit names."""
    _requested(ldb, clock)
    clock.advance(1)
    _post(client, "POST", "/live/control", "alice", {})
    served = _timeline(client, "alice")
    assert served["attempt_id"] == ATTEMPT
    assert [a["event"] for a in served["actions"]] == ["handoff_requested", "control_taken"]


def test_another_tenants_member_still_gets_404(client, ldb, live, clock, objects):
    _b6(client, ldb, clock, objects)
    assert client.get(f"/v1/tasks/{TASK}/live/actions", headers=bearer("bob")).status_code == 404


def test_a_device_choice_reads_as_a_change_a_rotation_or_a_toggle():
    """The derivation on its own: against the choice before it, on the same
    preset; a new preset is always a change."""
    at = "2026-10-12T12:48:09+00:00"

    def entry(**detail: Any) -> dict[str, Any]:
        return {"entry": "device_requested", "email": "alice@saga.xyz", "at": at, "detail": detail}

    # Looked up by name, so the route tests above fail on their own without it.
    lines = live_module.audit_lines([
        entry(preset="iphone-15", orientation="portrait", touch=True),
        entry(preset="iphone-15", orientation="landscape", touch=None),
        entry(preset="iphone-15", orientation=None, touch=False),
        entry(preset="iphone-15", orientation="portrait", touch=True),
        entry(preset="pixel-8", orientation="landscape", touch=True),
        entry(preset="1440x900", orientation="portrait", touch=None),
    ], "alice@saga.xyz")
    assert [x["event"] for x in lines] == [
        "device_changed", "rotated", "touch_toggled", "device_changed", "device_changed"]
    assert lines[2]["orientation"] == "landscape"
    assert all(x["you"] is True for x in lines)


def test_the_device_steps_are_read_in_time_order_not_the_order_firestore_streams(
        client, ldb, live, clock, objects):
    """Firestore streams an equality query in document-ID order, and a
    `live_audit` ID is random, so the stream need not be the writing order.
    The control: the same B6 sequence, with the audit stored newest first, so
    the fake streams the landscape choice before the portrait one."""
    _b6(client, ldb, clock, objects)
    entries = [(k, v) for k, v in ldb.docs.items() if k.startswith("live_audit/")]
    assert [v["entry"] for _, v in entries].count("device_requested") == 3
    for key, _ in entries:
        del ldb.docs[key]
    ldb.docs.update(reversed(entries))
    served = _timeline(client, "alice")
    events = [a.get("event") for a in served["actions"]]
    assert events == [
        "handoff_requested", "control_taken", "refused", "device_changed", "rotated",
        "touch_toggled", "control_returned", "handoff_resolved",
    ]
    by_event = {a["event"]: a for a in served["actions"]}
    assert by_event["device_changed"]["orientation"] == "portrait"
    assert by_event["rotated"]["orientation"] == "landscape"
    assert by_event["touch_toggled"]["orientation"] == "landscape"
    assert by_event["touch_toggled"]["touch"] is False
