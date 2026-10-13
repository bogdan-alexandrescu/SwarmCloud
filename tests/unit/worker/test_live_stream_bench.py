"""The live stream's benchmark still runs (STREAM-SMOOTH; part of #1030).

`python -m agent_worker.livegate.bench` is how the before and after in the
STREAM-SMOOTH pull request were measured, and how the next change to the
gate's capture, mask or encoder is measured. A benchmark nobody runs rots
until it is wrong, so this runs it -- briefly, at the phone size, asserting
its shape rather than its numbers, which belong to the machine it runs on.
"""

from __future__ import annotations

import asyncio

from agent_worker.livegate import bench


def test_the_benchmark_measures_every_part_it_reports() -> None:
    result = asyncio.run(bench.run(seconds=0.4, frames=2, sizes=("iphone",), levels=(1,)))
    frames, session, levels = result["frames"], result["session"], result["levels"]
    assert set(frames) == {f"iphone/{s}/{v}" for s in bench.SCENARIOS for v in ("novnc", "recorder")}
    assert set(session) == {"iphone/idle/control", "iphone/idle/watch", "iphone/scroll/control",
                            "iphone/scroll/watch", "iphone/typing/control"}
    assert set(levels) == {"desktop/scroll/novnc level 1", "desktop/typing/novnc level 1"}
    for row in frames.values():
        assert row["grab_ms"] > 0 and row["mask_ms"] > 0
    assert frames["iphone/scroll/novnc"]["bytes_per_update"] > 0
    assert frames["iphone/static/novnc"]["bytes_per_update"] == 0
    assert session["iphone/scroll/control"]["updates_per_s"] > 0
    assert "| session: size/scenario/mode |" in bench._table(result)
