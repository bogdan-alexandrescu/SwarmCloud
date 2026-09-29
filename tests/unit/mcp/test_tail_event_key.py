"""Epic #227: `swarm tail` keyed an event without an `event_id` as `at` +
`type`, so two events of one type stored in the same second collided and the
second was dropped as already printed. Such an event is now keyed by its whole
content, so only a true duplicate is suppressed.

AGAINST THE REAL swarm-api, as `test_tail_artifacts_and_cursor.py` builds it,
with `event_id` taken off each event on its way to `tail` -- the real route
always sends one, so the id-less case is the shape of an API that does not.
Offline: no credentials, no emulator, no network.
"""

from __future__ import annotations

import pytest

from swarm_mcp import cli

from test_swarm_says_what_happened import AT, World, _args, swarm, world  # noqa: F401


def _without_event_ids(swarm, monkeypatch) -> None:
    """Serve every event page with `event_id` removed from each event."""
    real = swarm.events_page

    def _page(task_id, **kwargs):
        events, token, paged = real(task_id, **kwargs)
        return [{k: v for k, v in e.items() if k != "event_id"} for e in events], token, paged

    monkeypatch.setattr(swarm, "events_page", _page)


def _printed(out: str, task_id: str, kind: str) -> list[str]:
    return [line for line in out.splitlines() if task_id[-8:] in line and f"· {kind}" in line]


def test_two_same_second_events_of_one_type_with_different_detail_are_both_printed(
    swarm, world: World, monkeypatch, capsys
):
    task_id = world.task("task_00000000000000samesec", state="SUCCEEDED")
    world.event(task_id, "ev_0001", "checkpoint_completed", AT, detail={"step": 1})
    world.event(task_id, "ev_0002", "checkpoint_completed", AT, detail={"step": 2})
    _without_event_ids(swarm, monkeypatch)

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    printed = _printed(capsys.readouterr().out, task_id, "checkpoint_completed")
    assert len(printed) == 2, f"tail printed {len(printed)} of 2 distinct same-second events: {printed}"


def test_an_exact_repeat_of_an_event_without_an_id_is_printed_once(swarm, world: World, monkeypatch, capsys):
    task_id = world.task("task_000000000000000repeat", state="SUCCEEDED")
    world.event(task_id, "ev_0001", "checkpoint_completed", AT, detail={"step": 1})
    world.event(task_id, "ev_0002", "checkpoint_completed", AT, detail={"step": 1})
    _without_event_ids(swarm, monkeypatch)

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    printed = _printed(capsys.readouterr().out, task_id, "checkpoint_completed")
    assert len(printed) == 1, f"an exact repeat was printed {len(printed)} times: {printed}"


@pytest.mark.parametrize(
    "a, b, same",
    [
        # Key order is not content: the same event serialised differently is one event.
        ({"at": "t", "type": "x", "detail": {"a": 1, "b": 2}}, {"type": "x", "detail": {"b": 2, "a": 1}, "at": "t"}, True),
        ({"at": "t", "type": "x", "detail": {"a": 1}}, {"at": "t", "type": "x", "detail": {"a": 2}}, False),
        ({"at": "t", "type": "x", "attempt_id": "at_1"}, {"at": "t", "type": "x", "attempt_id": "at_2"}, False),
    ],
)
def test_an_event_without_an_id_is_keyed_by_its_whole_content(a, b, same):
    assert (cli._event_key(a) == cli._event_key(b)) is same


def test_an_event_with_an_id_is_keyed_by_its_id():
    assert cli._event_key({"event_id": "ev_1", "at": "t", "type": "x"}) == "ev_1"
    assert cli._event_key({"event_id": "ev_1", "detail": {"a": 1}}) == cli._event_key(
        {"event_id": "ev_1", "detail": {"a": 2}}
    )
