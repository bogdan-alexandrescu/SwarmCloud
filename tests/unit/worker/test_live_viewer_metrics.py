"""The gate measures every viewer connection's stream (LIVE-MEASURE; part of #1030).

Owner decision 2026-10-13: Phase 1 of the live-browser plan is "measure", and
the warm-pool question is decided from the numbers. So the gate logs, per
viewer connection, when it closes and every `METRICS_INTERVAL_SECONDS` while
it is open, ONE structured line (`event=live_viewer_metrics`) that Cloud
Logging parses into jsonPayload and scripts/live-report.sh reads:

  * frames captured and frames changed while the connection was open;
  * updates sent, and the update rate in the seconds the page changed;
  * the longest a changed frame waited for an update;
  * how long the published frames carried a reason -- the hub's #555 fill
    before the first capture, the browser closed after the attempt -- and
    that time and its longest single stretch per reason (#1344). Since
    LIVE-NOMASK (#2298) there is no page-blanked fill and no mask reason;
  * input-to-frame p50/p95, ONLY for inputs a changed frame followed: a key
    that changed nothing is not timed to some later, unrelated change;
  * key and pointer counts. Never a keysym, a coordinate or pasted text.

`ViewerMetrics` is fed explicit timestamps, so the arithmetic is tested here
without a socket; the last tests run a real `RfbSession` over a socket pair.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest
from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import mask, rfb
from test_live_rfb import Gate, _frame

METRICS_LOGGER = "agent_worker.livegate.metrics"


def _metrics(now: float = 100.0, reason: str | None = None) -> rfb.ViewerMetrics:
    return rfb.ViewerMetrics("v-1", now=now, reason=reason)


def _lines(caplog: pytest.LogCaptureFixture) -> list[dict]:
    out = []
    for record in caplog.records:
        if record.name == METRICS_LOGGER:
            out.append(json.loads(record.getMessage()))
    return out


# --------------------------------------------------------------------------
# The arithmetic, with explicit clocks
# --------------------------------------------------------------------------


def test_fps_is_measured_only_over_the_seconds_the_page_changed() -> None:
    m = _metrics(now=0.0)
    # Two seconds of a changing page, 10 updates a second.
    for i in range(20):
        t = i * 0.1
        m.frame(t, changed=True, at=t)
        m.update(t + 0.01, sent_at=t)
    # Then eight seconds of a static page: frames captured, nothing changed,
    # nothing sent. They must not dilute the rate.
    for i in range(80):
        t = 2.0 + i * 0.1
        m.frame(t, changed=False, at=t)
    line = m.snapshot(10.0, rfb.SessionCounts(), final=True)
    assert line["frames_captured"] == 100
    assert line["frames_changed"] == 20
    assert line["updates"] == 20
    assert line["changing_seconds"] == 2
    assert line["updates_while_changing"] == 20
    assert line["fps_while_changing"] == pytest.approx(10.0)


def test_a_stalled_stream_reads_as_zero_fps_and_a_long_gap() -> None:
    m = _metrics(now=0.0)
    m.frame(0.0, changed=True, at=0.0)
    m.update(0.05, sent_at=0.0)
    # The page keeps changing for 6 s and the viewer is sent nothing.
    for i in range(1, 60):
        t = i * 0.1
        m.frame(t, changed=True, at=t)
    m.update(6.5, sent_at=6.4)
    line = m.snapshot(7.0, rfb.SessionCounts(), final=True)
    # From the first change it had not been sent (0.1 s) to the update (6.5 s).
    assert line["longest_gap_ms"] == pytest.approx(6400, abs=1)
    assert line["fps_while_changing"] < 1.0


def test_a_static_page_is_no_gap() -> None:
    """Thirty quiet seconds between two updates are not a stall: nothing was owed."""
    m = _metrics(now=0.0)
    m.frame(0.0, changed=True, at=0.0)
    m.update(0.02, sent_at=0.0)
    m.frame(30.0, changed=True, at=30.0)
    m.update(30.05, sent_at=30.0)
    line = m.snapshot(31.0, rfb.SessionCounts(), final=True)
    assert line["longest_gap_ms"] == pytest.approx(50, abs=1)


def test_an_open_gap_counts_in_a_line_written_while_it_is_open() -> None:
    m = _metrics(now=0.0)
    m.frame(1.0, changed=True, at=1.0)
    line = m.snapshot(9.0, rfb.SessionCounts(), final=False)
    assert line["longest_gap_ms"] == pytest.approx(8000, abs=1)
    assert line["final"] is False


def test_blank_time_is_the_time_the_frames_carried_a_reason_and_no_page_is_blanked() -> None:
    # The hub starts on its "not captured" fill until the first capture.
    m = _metrics(now=0.0, reason=rfb.REASON_NOT_CAPTURED)
    m.frame(3.0, changed=True, at=3.0)
    m.frame(5.0, changed=True, at=5.0)
    m.frame(8.0, changed=True, at=8.0, reason=mask.REASON_BROWSER_CLOSED)
    line = m.snapshot(10.0, rfb.SessionCounts(), final=True)
    assert line["unavailable_ms"] == pytest.approx(5000, abs=1)  # 0-3 and 8-10
    assert line["blank_ms"] == line["unavailable_ms"]
    assert "page_blanked_ms" not in line


def test_unavailable_time_is_split_by_the_reason_the_frames_carried() -> None:
    # Issue #1344: the line says which reason its time went to, each its own key.
    m = rfb.ViewerMetrics("v-1", now=0.0, reason=rfb.REASON_NOT_CAPTURED)
    m.frame(1.0, changed=True, at=1.0)
    m.frame(6.0, changed=True, at=6.0, reason=mask.REASON_BROWSER_CLOSED)
    m.frame(6.5, changed=False, at=6.5, reason=mask.REASON_BROWSER_CLOSED)
    line = m.snapshot(9.0, rfb.SessionCounts(), final=True)
    by_reason = line["unavailable_ms_by_reason"]
    assert by_reason == {
        "not_captured": pytest.approx(1000, abs=1),
        "browser_closed": pytest.approx(3000, abs=1),  # one stretch across an unchanged frame, still open
    }
    # The split adds up to the total it splits.
    assert sum(by_reason.values()) == pytest.approx(line["unavailable_ms"], abs=1)
    longest = line["unavailable_longest_ms_by_reason"]
    assert set(longest) == set(by_reason)


def test_an_open_unavailable_stretch_counts_as_the_longest_while_it_lasts() -> None:
    m = _metrics(now=0.0)
    m.frame(1.0, changed=True, at=1.0, reason=mask.REASON_BROWSER_CLOSED)
    m.frame(2.0, changed=True, at=2.0)
    m.frame(3.0, changed=True, at=3.0, reason=mask.REASON_BROWSER_CLOSED)
    # A periodic line, 13.2 s into the second stretch.
    line = m.snapshot(16.2, rfb.SessionCounts(), final=False)
    assert line["unavailable_longest_ms_by_reason"] == {"browser_closed": pytest.approx(13200, abs=1)}
    # Reading it closed nothing: the stretch goes on.
    later = m.snapshot(20.0, rfb.SessionCounts(), final=True)
    assert later["unavailable_longest_ms_by_reason"]["browser_closed"] == pytest.approx(17000, abs=1)
    assert later["unavailable_ms_by_reason"]["browser_closed"] == pytest.approx(18000, abs=1)


def test_a_reason_the_gate_does_not_name_is_counted_as_other() -> None:
    # Every reason is words the gate wrote, never page text, but only the known
    # ones get a key: a log field per new phrase is not a schema a query can rely on.
    m = _metrics(now=0.0)
    m.frame(1.0, changed=True, at=1.0, reason="something new")
    m.frame(2.0, changed=True, at=2.0, reason=mask.REASON_BROWSER_CLOSED)
    line = m.snapshot(2.5, rfb.SessionCounts(), final=True)
    assert line["unavailable_ms_by_reason"] == {
        "other": pytest.approx(1000, abs=1), "browser_closed": pytest.approx(500, abs=1)}


def test_every_reason_a_frame_can_carry_has_its_own_key() -> None:
    keys = [rfb.unavailable_reason_key(reason) for reason in (rfb.REASON_NOT_CAPTURED,
                                                              mask.REASON_BROWSER_CLOSED)]
    assert keys == ["not_captured", "browser_closed"]
    assert set(rfb.UNAVAILABLE_REASON_KEYS.values()) == {"not_captured", "browser_closed"}


def test_the_hub_carries_its_reasons_into_a_connections_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=METRICS_LOGGER)

    async def main() -> None:
        gate = Gate()
        viewer = await gate.connect("v-0003")  # opens on the hub's "not captured" fill
        viewer.request(incremental=False)
        await viewer.read_update()
        # The browser closed after the attempt: the capture, with that reason.
        gate.hub.publish(_frame(rgb=(1, 2, 3)), reason=mask.REASON_BROWSER_CLOSED)
        await viewer.drain_server()
        await asyncio.wait_for(asyncio.gather(*gate.tasks, return_exceptions=True), 5)

    asyncio.run(main())
    [line] = [line for line in _lines(caplog) if line["final"]]
    assert set(line["unavailable_ms_by_reason"]) == {"not_captured", "browser_closed"}
    assert set(line["unavailable_longest_ms_by_reason"]) == {"not_captured", "browser_closed"}


def test_input_to_frame_is_timed_only_for_inputs_a_change_followed() -> None:
    m = _metrics(now=0.0)
    # A key that changes the page 40 ms later, shown 60 ms after the key.
    m.input(1.0)
    m.frame(1.04, changed=True, at=1.04)
    m.update(1.06, sent_at=1.04)
    # A key that changes nothing (a dead area of the page) ...
    m.input(2.0)
    # ... and an unrelated animation ten seconds later. Under the old rule the
    # key would be "10 s from input to frame".
    m.frame(12.0, changed=True, at=12.0)
    m.update(12.02, sent_at=12.0)
    line = m.snapshot(13.0, rfb.SessionCounts(), final=True)
    assert line["input_samples"] == 1
    assert line["inputs_unchanged"] == 1
    assert line["input_to_frame_p50_ms"] == pytest.approx(60, abs=1)
    assert line["input_to_frame_p95_ms"] == pytest.approx(60, abs=1)


def test_a_change_shown_late_counts_its_full_wait() -> None:
    """The input is attributed at capture time; the wait is to the update the
    viewer got. A stalled stream is exactly what this number must show."""
    m = _metrics(now=0.0)
    m.input(1.0)
    m.frame(1.05, changed=True, at=1.05)
    m.update(9.0, sent_at=8.9)
    line = m.snapshot(10.0, rfb.SessionCounts(), final=True)
    assert line["input_samples"] == 1
    assert line["input_to_frame_p95_ms"] == pytest.approx(8000, abs=1)


def test_an_update_from_a_frame_older_than_the_change_does_not_end_the_wait() -> None:
    m = _metrics(now=0.0)
    m.input(1.0)
    m.frame(1.05, changed=True, at=1.05)
    m.update(1.06, sent_at=0.9)  # rendered from a frame grabbed before the change
    assert m.snapshot(1.1, rfb.SessionCounts(), final=False)["input_samples"] == 0
    m.update(1.2, sent_at=1.05)
    line = m.snapshot(2.0, rfb.SessionCounts(), final=True)
    assert line["input_to_frame_p50_ms"] == pytest.approx(200, abs=1)


def test_the_line_carries_counts_and_names_the_role() -> None:
    m = rfb.ViewerMetrics("rec-18f", now=0.0)
    counts = rfb.SessionCounts(key_events=41, pointer_events=9, pastes=1, dropped=2,
                               updates=7, bytes_sent=1234, stalls=1)
    line = m.snapshot(60.0, counts, final=True)
    assert line["event"] == rfb.VIEWER_METRICS_EVENT == "live_viewer_metrics"
    assert line["conn"] == "rec-18f"
    assert line["role"] == "recorder"
    assert (line["keys"], line["pointer"], line["pastes"], line["dropped"], line["bytes"],
            line["stalls"]) == (41, 9, 1, 2, 1234, 1)
    assert line["seconds"] == pytest.approx(60.0)
    assert rfb.ViewerMetrics("v-ab", now=0.0).snapshot(
        1.0, counts, final=True)["role"] == "viewer"
    # Every value is a number, a bool, a short name or null: nothing read off the page.
    for key, value in line.items():
        if isinstance(value, list):
            assert all(isinstance(v, int) for v in value), key
            continue
        if isinstance(value, dict):
            # Per reason: a fixed key the gate chose, never the reason's words.
            known = {*rfb.UNAVAILABLE_REASON_KEYS.values(), rfb.UNAVAILABLE_REASON_OTHER}
            assert set(value) <= known and all(isinstance(v, float) for v in value.values()), key
            continue
        assert value is None or isinstance(value, (int, float, bool)) or key in (
            "event", "conn", "role", "message", "severity"), key


def test_input_to_frame_is_also_a_histogram_that_pools_across_connections() -> None:
    """A p95 per connection cannot be averaged into a p95 across them; counts
    in fixed buckets can be added. 200 ms is a bound: the Phase-2 target."""
    assert 200 in rfb.INPUT_TO_FRAME_BOUNDS_MS
    m = _metrics(now=0.0)
    for start, wait in ((1.0, 0.04), (3.0, 0.18), (5.0, 0.25), (8.0, 6.0)):
        m.input(start)
        m.frame(start + 0.01, changed=True, at=start + 0.01)
        m.update(start + wait, sent_at=start + 0.01)
    line = m.snapshot(20.0, rfb.SessionCounts(), final=True)
    bounds, hist = line["input_to_frame_bounds_ms"], line["input_to_frame_hist"]
    assert bounds == list(rfb.INPUT_TO_FRAME_BOUNDS_MS)
    assert len(hist) == len(bounds) + 1 and sum(hist) == 4
    assert hist[bounds.index(50)] == 1      # 40 ms
    assert hist[bounds.index(200)] == 1     # 180 ms
    assert hist[bounds.index(300)] == 1     # 250 ms
    assert hist[-1] == 1                    # 6 s: above every bound


# --------------------------------------------------------------------------
# A real connection logs it
# --------------------------------------------------------------------------


def test_a_closed_connection_logs_one_metrics_line_with_its_input(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=METRICS_LOGGER)

    async def main() -> None:
        gate = Gate()
        viewer = await gate.connect("v-0001")
        await gate.control.grant("v-0001")
        viewer.request(incremental=False)
        await viewer.read_update()
        viewer.key(0x61, True)
        viewer.key(0x61, False)
        viewer.pointer(1, 5, 5)
        viewer.pointer(0, 5, 5)
        await viewer.flush()
        # The page reacts: a changed frame, after the input.
        gate.hub.publish(_frame(rgb=(200, 0, 0)))
        viewer.request(incremental=True)
        await viewer.read_update()
        await viewer.drain_server()
        await asyncio.wait_for(asyncio.gather(*gate.tasks, return_exceptions=True), 5)

    asyncio.run(main())
    lines = [line for line in _lines(caplog) if line["final"]]
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["conn"] == "v-0001" and line["role"] == "viewer"
    assert line["keys"] == 2 and line["pointer"] == 2
    # Every key read in control was injected: the "100% keys" target's two sides.
    assert line["keys_read"] == 2
    assert line["updates"] == 2
    assert line["frames_changed"] == 1
    assert line["input_samples"] == 1
    assert line["input_to_frame_p50_ms"] is not None
    assert line["severity"] == "INFO"


def test_an_open_connection_logs_every_interval(caplog: pytest.LogCaptureFixture,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.INFO, logger=METRICS_LOGGER)
    monkeypatch.setattr(rfb, "METRICS_INTERVAL_SECONDS", 0.05)

    async def main() -> None:
        gate = Gate()
        viewer = await gate.connect("v-0002")  # held: a dropped viewer closes its socket
        await asyncio.sleep(0.3)
        await gate.close()
        viewer.writer.close()

    asyncio.run(main())
    periodic = [line for line in _lines(caplog) if not line["final"]]
    assert len(periodic) >= 2, periodic
    assert all(line["conn"] == "v-0002" for line in periodic)


def test_the_gate_writes_the_metrics_line_as_bare_json_for_cloud_logging() -> None:
    """Cloud Logging parses a container's stderr line into jsonPayload only
    when the WHOLE line is JSON: no timestamp or level prefix in front."""
    stream = io.StringIO()
    logger = logging.getLogger(METRICS_LOGGER)
    saved = (list(logger.handlers), logger.propagate)
    try:
        gate_main.configure_metrics_logging(stream)
        line = _metrics(now=0.0).snapshot(1.0, rfb.SessionCounts(), final=True)
        rfb.log_viewer_metrics(line)
    finally:
        logger.handlers, logger.propagate = saved
    written = stream.getvalue().strip().splitlines()
    assert len(written) == 1
    payload = json.loads(written[0])
    assert payload["event"] == "live_viewer_metrics"
    assert payload["severity"] == "INFO"
    assert payload["message"].startswith("live viewer metrics")
