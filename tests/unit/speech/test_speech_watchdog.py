"""The speech machine's own watchdog (docs/design/voice-it-own-speech.md §4.5,
stop 2; swarm_speech/watchdog.py and activity.py).

It powers the machine off ten minutes after the last open authenticated
socket, counted from the boot when none has opened; a socket the server says
is open holds it up only while the server keeps refreshing that claim, so a
crashed server cannot keep a GPU running.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import speech_fakes  # noqa: F401 -- puts apps/swarm-speech on sys.path
from swarm_speech import activity, watchdog as w

BOOT = 1_000_000.0


@pytest.mark.parametrize("now_after_boot, state, off", [
    (w.IDLE_SECONDS - 1, None, False),                       # nothing ever opened, not yet ten minutes
    (w.IDLE_SECONDS, None, True),                            # ten minutes from boot with no socket
    (w.IDLE_SECONDS + 5, {"open": 0, "at": BOOT - 50}, True),  # a file from before this boot counts for nothing
    (w.IDLE_SECONDS + 5, {"open": 0, "at": BOOT + 60}, False),  # closed 545 s ago
    (60 + w.IDLE_SECONDS, {"open": 0, "at": BOOT + 60}, True),  # closed exactly ten minutes ago
    (3600, {"open": 1, "at": BOOT + 3600 - 30}, False),      # open and freshly refreshed
    (3600, {"open": 1, "at": BOOT + 3600 - w.FRESH_SECONDS}, False),  # stale, but only just: idle from then
    (3600, {"open": 1, "at": BOOT + 3600 - w.IDLE_SECONDS}, True),  # a server that died with a socket open
])
def test_power_off_after_ten_minutes_with_no_open_socket(now_after_boot, state, off):
    assert w.should_power_off(BOOT + now_after_boot, BOOT, state) is off


def test_the_watchdog_reads_the_file_the_server_writes(tmp_path):
    clock = iter([BOOT + 10, BOOT + 20, BOOT + 50]).__next__
    act = activity.Activity(tmp_path, clock=clock)
    act.opened()
    path = tmp_path / activity.FILENAME
    assert w.read_state(path) == {"open": 1, "at": BOOT + 10}
    act.refresh()
    assert w.read_state(path)["at"] == BOOT + 20
    act.closed()
    assert w.read_state(path) == {"open": 0, "at": BOOT + 50}
    assert not w.should_power_off(BOOT + 50 + w.IDLE_SECONDS - 1, BOOT, w.read_state(path))
    assert w.should_power_off(BOOT + 50 + w.IDLE_SECONDS, BOOT, w.read_state(path))


def test_a_refresh_with_nothing_open_does_not_move_the_idle_clock(tmp_path):
    act = activity.Activity(tmp_path, clock=lambda: BOOT + 99)
    act.refresh()
    assert not (tmp_path / activity.FILENAME).exists()


@pytest.mark.parametrize("content", ["", "{", "[]", json.dumps({"open": "x", "at": 1})])
def test_an_unreadable_file_counts_from_the_boot(tmp_path, content):
    path = tmp_path / activity.FILENAME
    path.write_text(content)
    assert w.read_state(path) is None
    assert w.should_power_off(BOOT + w.IDLE_SECONDS, BOOT, w.read_state(path))


def test_boot_time_is_now_less_the_uptime(tmp_path):
    (tmp_path / "uptime").write_text("120.50 300.00\n")
    assert w.boot_time(tmp_path, clock=lambda: BOOT) == BOOT - 120.5


def test_the_server_refreshes_well_inside_the_watchdogs_freshness():
    assert activity.REFRESH_SECONDS * 3 <= w.FRESH_SECONDS


def test_the_watchdog_needs_only_the_hosts_python_and_standard_library():
    """It runs with the VM's Python 3.10 when the container cannot: no import
    from the package or from anything outside the standard library."""
    source = Path(w.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import would need the package on the host"
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "json", "logging", "subprocess", "sys", "time", "pathlib"}
    ast.parse(source, feature_version=(3, 10))
