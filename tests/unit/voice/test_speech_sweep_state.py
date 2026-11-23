"""The speech sweep's state line: the one signal both Voice it alerts read
(swarm_api/routes/voice_speech.py `sweep`; terraform/infra/voice_alerts.tf).

Each sweep, every two minutes, logs one structured line, event
`voice_sweep_state`, after its work, with:

  * `voice_live_idle_seconds`: the most seconds any session still `live`
    after the sweep has gone without activity (0 when none is live);
  * `speech_machine_unheld`: whether the sweep found the speech machine not at
    rest with nothing holding it (no live session's lease, no warm lease
    inside its idle time).

The sweep's answer is unchanged: the line is a log line, not a field. Firestore
is the in-memory fake, Compute a fake, the clock a fake: no network, no cloud
credential.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_sessions_engine import TENANT  # noqa: E402
from test_speech_machine import ALICE, CONFIG  # noqa: E402
from test_speech_routes import SWEEP, Rig  # noqa: E402

from swarm_api.routes import voice_speech as rvs  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402

ENGINE_ON = sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP})
ENGINE_OFF = sm.SpeechConfig(**SWEEP)
ANSWER_KEYS = {"enabled", "lease", "ended", "stopped", "instance", "abandoned", "budget_ended"}


def _seed_live(rig: Rig, *, person: str, session_id: str, at: Any) -> None:
    project_id = "vp_" + session_id[3:]
    rig.db.docs[f"{vs.PROJECTS}/{project_id}"] = {"tenant_id": TENANT, "person": person}
    rig.db.docs[f"{vs.PROJECTS}/{project_id}/{vs.SESSIONS}/{session_id}"] = {
        "tenant_id": TENANT, "person": person, "project_id": project_id, "state": vs.LIVE,
        "mode": vs.MODE_TEXT, "created_at": at, "live_at": at, "grant_hash": "h"}
    rig.db.docs[f"{vs.LIVE_MARKERS}/{vs.marker_id('', person)}"] = {
        "tenant_id": TENANT, "project_id": project_id, "session_id": session_id, "at": at}


def _sweep_state(rig: Rig, caplog: pytest.LogCaptureFixture) -> tuple[dict[str, Any], Any]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=rvs.log.name):
        reply = rig.sweep(rig.verifier.token)
    assert reply.status_code == 200, reply.text
    lines = [r for r in caplog.records if getattr(r, "event", None) == rvs.SWEEP_STATE_EVENT]
    assert len(lines) == 1, "the sweep logs exactly one state line per run"
    return reply.json(), lines[0]


def test_the_event_name_is_the_one_the_alerts_filter_on():
    # terraform/infra/voice_alerts.tf filters on this literal.
    assert rvs.SWEEP_STATE_EVENT == "voice_sweep_state"


@pytest.mark.parametrize("config", [ENGINE_OFF, ENGINE_ON], ids=["engine-off", "engine-on"])
def test_nothing_live_and_a_machine_at_rest_report_zero_and_false(config, caplog):
    rig = Rig(config=config)
    answer, line = _sweep_state(rig, caplog)
    assert line.voice_live_idle_seconds == 0
    assert line.speech_machine_unheld is False
    assert set(answer) == ANSWER_KEYS, "the sweep's answer gained or lost a field"


def test_the_longest_idle_live_session_is_reported_in_whole_seconds(caplog):
    rig = Rig(config=ENGINE_ON)
    start = rig.clock.now()
    _seed_live(rig, person="a@example.com", session_id="vs_" + "a" * 32, at=start)
    rig.clock.advance(600)
    _seed_live(rig, person="b@example.com", session_id="vs_" + "b" * 32, at=rig.clock.now())
    rig.clock.advance(5)
    _, line = _sweep_state(rig, caplog)
    assert line.voice_live_idle_seconds == 605
    assert isinstance(line.voice_live_idle_seconds, int)


def test_a_session_the_sweep_ends_as_abandoned_is_not_counted(caplog):
    rig = Rig(config=ENGINE_ON)
    _seed_live(rig, person="a@example.com", session_id="vs_" + "a" * 32, at=rig.clock.now())
    rig.clock.advance(vs.LIVE_STALE_SECONDS + 1)
    answer, line = _sweep_state(rig, caplog)
    assert answer["abandoned"] == 1
    assert line.voice_live_idle_seconds == 0


def test_a_running_machine_nothing_holds_is_reported_unheld_until_it_is_at_rest(caplog):
    rig = Rig(config=ENGINE_ON)
    rig.compute.status = sm.INSTANCE_RUNNING
    answer, line = _sweep_state(rig, caplog)
    assert line.speech_machine_unheld is True
    assert answer["stopped"] is True and set(answer) == ANSWER_KEYS
    # The stop took: the next sweep finds it at rest.
    _, line = _sweep_state(rig, caplog)
    assert line.speech_machine_unheld is False


def test_a_machine_whose_stop_does_not_take_stays_unheld_on_every_sweep(caplog):
    rig = Rig(config=ENGINE_ON)
    rig.compute.status = sm.INSTANCE_RUNNING
    for _ in range(3):
        _, line = _sweep_state(rig, caplog)
        assert line.speech_machine_unheld is True
        rig.compute.status = sm.INSTANCE_RUNNING  # restarted, or the stop never landed


def test_a_machine_a_live_session_holds_is_not_unheld(caplog):
    rig = Rig(config=ENGINE_ON)
    rig.session(ALICE)
    rig.take(ALICE)
    rig.clock.advance(sm.HEARTBEAT_STALE_SECONDS - sm.HEARTBEAT_SECONDS)
    _, line = _sweep_state(rig, caplog)
    assert rig.compute.status == sm.INSTANCE_RUNNING
    assert line.speech_machine_unheld is False
