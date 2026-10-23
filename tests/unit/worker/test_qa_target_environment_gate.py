"""The gate holds one leg of a target per attempt (QAENV-GATE; part of #2136).

docs/design/qa-environments.md §1.5 and §8's QAENV-GATE row, under BGD's Q1
(a) of §10: one target carries several `environments[]` entries (legs), so
the gate's `target_changed` refusal compares `(target_id, environment name)`.
The row's red-first tests, plus the review note's leg-switch case:

* a second hand-off with the same target id and another environment name is
  refused `target_changed`;
* a hand-off without the name (an old mark) is still accepted, and compares
  as today;
* a named hand-off followed by an unnamed one for the same target is refused
  (an absent name cannot be used to switch legs), and the reverse too.

The gate is the real `QaTarget`; the worker is the real `QaTargetSignIn`
through test_qa_target_signin's rig. No Chromium, network or cloud
credential. Every password is built at run time.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import qa_target
from agent_worker.livegate import qatarget as gate_qt

from test_qa_target_signin import (
    LOGIN_ORIGIN,
    TargetRoutes,
    _args,
    _password,
    _sign_in,
    _target_id,
    _username,
    _world,
)

STAGING_ORIGIN = "https://staging.example.com"


def _gate() -> gate_qt.QaTarget:
    return gate_qt.QaTarget(lambda e, f: None)


def _leg(target_id: str, password: str, name: str | None, **over: Any) -> dict[str, Any]:
    args = _args(target_id, password, **over)
    if name is not None:
        args["environment_name"] = name
    return args


def _staging(target_id: str, password: str, name: str | None = "staging") -> dict[str, Any]:
    """Another leg of the same target: its own origin, mark and write rule."""
    return _leg(target_id, password, name, base_url=f"{STAGING_ORIGIN}/home",
                origins=[STAGING_ORIGIN, LOGIN_ORIGIN], environment="non_production", mutating=True)


def _held(target: gate_qt.QaTarget) -> tuple[Any, ...]:
    return (target.target_id, target.origins, target.base_url, target.environment,
            target.mutating, target.credential())


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_another_environment_name_of_the_same_target_is_refused_target_changed():
    target = _gate()
    first, password = _target_id(), _password()
    target.handle(_leg(first, password, "production"))
    target.finished("signed_in")
    before = _held(target)
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_staging(first, _password()))
    # MUTATION: compare only target_id in QaTarget.handle and the staging leg's origins replace production's.
    assert refused.value.code == "target_changed"
    # MUTATION: assign the new leg's fields before the target_changed check and this fails.
    assert _held(target) == before == (first, before[1], before[2], "production", False, before[5])
    assert target.credential()[1] == password
    assert target.environment_name == "production"


def test_the_same_environment_name_signs_in_again():
    target = _gate()
    first = _target_id()
    target.handle(_staging(first, _password()))
    target.finished("signed_in")
    again = _password()
    # MUTATION: refuse every second hand-off naming an environment and `swarm-handoff sign-in` breaks.
    status = target.handle(_staging(first, again))
    assert status["qa_sign_in"] == "signing_in" and target.sign_ins == 2
    assert target.credential()[1] == again and target.environment_name == "staging"


def test_a_hand_off_without_the_name_is_accepted_and_compares_as_today():
    target = _gate()
    first = _target_id()
    # MUTATION: require environment_name in qatarget._parse and an old mark is refused.
    target.handle(_leg(first, _password(), None))
    target.finished("signed_in")
    assert target.environment_name is None
    status = target.handle(_leg(first, _password(), None))
    assert status["qa_sign_in"] == "signing_in" and target.sign_ins == 2
    target.finished("signed_in")
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_leg(_target_id(), _password(), None))
    # MUTATION: drop the target_id comparison when no name is held and another target is taken.
    assert refused.value.code == "target_changed"


def test_a_named_hand_off_followed_by_an_unnamed_one_for_another_leg_is_refused():
    target = _gate()
    first, password = _target_id(), _password()
    target.handle(_leg(first, password, "production"))
    target.finished("signed_in")
    before = _held(target)
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_staging(first, _password(), name=None))
    # MUTATION: skip the name comparison when the new hand-off has none and the absent name switches legs.
    assert refused.value.code == "target_changed"
    assert _held(target) == before and target.credential()[1] == password


def test_an_unnamed_hand_off_followed_by_a_named_one_is_refused():
    target = _gate()
    first, password = _target_id(), _password()
    target.handle(_leg(first, password, None))
    target.finished("signed_in")
    before = _held(target)
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_staging(first, _password()))
    # MUTATION: adopt a name when none is held and an old mark's attempt can switch to any leg.
    assert refused.value.code == "target_changed"
    assert _held(target) == before and target.credential()[1] == password


@pytest.mark.parametrize("name", ["", "Staging", "a" * 21, "stag ing", 7, None, ["staging"]])
def test_a_malformed_environment_name_is_refused(name):
    target = _gate()
    args = _args(_target_id(), _password())
    args["environment_name"] = name
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(args)
    # MUTATION: accept any environment_name in qatarget._parse and this fails.
    assert refused.value.code == "environment_name"
    assert not target.armed and not target.held


def test_the_record_names_the_leg_and_never_the_credential():
    events: list[tuple[str, dict[str, Any]]] = []
    target = gate_qt.QaTarget(lambda e, f: events.append((e, f)))
    password = _password()
    target.handle(_staging(_target_id(), password))
    ((event, fields),) = [(e, f) for e, f in events if e == "qa_target_set"]
    # MUTATION: drop environment_name from the qa_target_set record and this fails.
    assert fields["environment_name"] == "staging"
    assert password not in repr(events)


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


def _answer(name: Any = "staging") -> dict[str, Any]:
    answer: dict[str, Any] = {
        "target_id": _target_id(), "base_url": f"{STAGING_ORIGIN}/home",
        "origins": [STAGING_ORIGIN], "environment": "non_production",
        "sign_in": {"kind": "own_login", "login_url": f"{STAGING_ORIGIN}/login",
                    "signed_in_selector": "#a", "username": _username(), "password": _password()},
    }
    if name is not None:
        answer["environment_name"] = name
    return answer


def test_the_worker_hands_the_gate_the_legs_environment_name():
    # MUTATION: drop environment_name from qa_target.handoff_args and the gate compares the target id alone.
    assert qa_target.handoff_args(_answer("staging"))["environment_name"] == "staging"


def test_the_worker_hands_no_name_for_an_old_mark():
    # MUTATION: default environment_name in qa_target.handoff_args and an old mark reads as a named leg.
    assert "environment_name" not in qa_target.handoff_args(_answer(None))


@pytest.mark.parametrize("name", ["", "Production", 3, "x" * 21])
def test_the_worker_refuses_an_answer_with_a_malformed_environment_name(name):
    # MUTATION: pass environment_name through unchecked in qa_target.handoff_args and this fails.
    with pytest.raises(ValueError):
        qa_target.handoff_args(_answer(name))


class LegRoutes(TargetRoutes):
    """swarm-api answering the same target id under a leg name per ask."""

    def __init__(self, *args: Any, names: list[str | None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.names = names

    def call(self, method, path, *, body=None, headers=None):
        status, payload = super().call(method, path, body=body, headers=headers)
        if path.endswith("/qa-target") and status == 200:
            name = self.names.pop(0)
            if name is not None:
                payload["environment_name"] = name
        return status, payload


def _leg_world(tmp_path, db, monkeypatch, names: list[str | None]):
    made: dict[str, Any] = {}
    real = TargetRoutes

    def routes(*args: Any, **kwargs: Any) -> LegRoutes:
        made["routes"] = LegRoutes(*args, names=names, **kwargs)
        return made["routes"]

    import test_qa_target_signin as rig

    monkeypatch.setattr(rig, "TargetRoutes", routes)
    try:
        world = _world(tmp_path, db)
    finally:
        monkeypatch.setattr(rig, "TargetRoutes", real)
    return world


def test_the_worker_logs_the_leg_beside_the_target_id(tmp_path, db, monkeypatch):
    world = _leg_world(tmp_path, db, monkeypatch, ["staging"])
    (handed,) = world.gate.handoffs()
    assert handed["environment_name"] == "staging"
    assert world.path.qa_target.environment_name == "staging"
    line = [kw for msg, kw in world.log.lines if msg == "qa target handed to the gate"]
    # MUTATION: drop environment_name from the "qa target handed to the gate" log line and this fails.
    assert line and line[0]["environment_name"] == "staging"


def test_a_sign_in_again_that_comes_back_for_another_leg_is_refused_and_keeps_the_first(tmp_path, db, monkeypatch):
    world = _leg_world(tmp_path, db, monkeypatch, ["staging", "production"])
    first_password = world.routes.issued[0][1]
    assert world.gate.target.credential()[1] == first_password
    code, answer = _sign_in(world, monkeypatch)
    assert len(world.routes.target_calls()) == 2, "the worker asked swarm-api again"
    # MUTATION: compare only target_id in QaTarget.handle and the production leg's password is taken.
    assert world.gate.target.credential()[1] == first_password
    assert world.gate.target.environment_name == "staging"
    assert world.path.qa_target.cause == qa_target.GATE_REFUSED
    assert code != 0 or answer.get("signed_in") is not True

