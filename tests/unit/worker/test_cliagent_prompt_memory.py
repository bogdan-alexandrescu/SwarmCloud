"""The budget line tells the agent HOW to stay inside its memory (#2507).

#2504's line named the limit and the worker count and said to stop dev
servers. The OOM kills it was written for also came from the shape of the
work: two heavy commands at once, a test run backgrounded with `&` or
`nohup` and forgotten, a timed-out command whose children kept running while
the next one started, and `--max-old-space-size` set to the whole container,
which leaves nothing for the worker and the CLI that share it.

What holds now, for an 8 GiB and a 5 GiB container, in the one line the
first start's stdin prompt carries:

1. the limit in GiB;
2. run one heavy command at a time;
3. never background a test run with `&` or `nohup` -- said once, by the
   time-limit line (`cliagent.time_limit_line`), not by this one;
4. kill a timed-out command's process group before starting another;
5. `--max-old-space-size` at most the limit less 1.5 GiB (6656 MiB of 8 GiB,
   3584 of 5), and never the whole limit.

MUTATIONS: drop any one rule from `cliagent.budget_line` (2, 4), or 3 from
`cliagent.time_limit_line`; cap
`--max-old-space-size` at the whole limit again (5).
"""

from __future__ import annotations

import pytest

from agent_worker import metrics

from test_agent_env_resource_budget import _budget_lines, _run

GIB = 1 << 30
MIB = 1 << 20


@pytest.fixture(autouse=True)
def _no_container_budget_from_the_environment():
    """Overrides the worker conftest's fixture of this name: here the cgroup
    readers are the ones each test patches, so nothing is neutralised."""
    yield


def _line(tmp_path, monkeypatch, memory: int) -> str:
    monkeypatch.setattr(metrics, "cgroup_memory_max", lambda: memory)
    monkeypatch.setattr(metrics, "cgroup_cpu_limit_cores", lambda: 6.0)
    seen = _run(tmp_path, monkeypatch)
    lines = _budget_lines(seen["stdin"])
    assert len(lines) == 1, seen["stdin"]
    return lines[0]


@pytest.mark.parametrize(("memory", "gib"), [(8 * GIB, "8 GiB"), (5 * GIB, "5 GiB")])
def test_the_line_names_the_limit_in_gib(tmp_path, monkeypatch, memory, gib):
    assert gib in _line(tmp_path, monkeypatch, memory)


@pytest.mark.parametrize("memory", [8 * GIB, 5 * GIB])
def test_the_line_says_one_heavy_command_at_a_time(tmp_path, monkeypatch, memory):
    line = _line(tmp_path, monkeypatch, memory)

    assert "one heavy command" in line and "at a time" in line, line


@pytest.mark.parametrize("memory", [8 * GIB, 5 * GIB])
def test_the_background_rule_is_the_time_limit_lines_not_restated_here(tmp_path, monkeypatch,
                                                                         memory):
    """ONE-PLACE (2026-11-03): rule 3 is said once per first message, in
    `cliagent.time_limit_line`, which every start carries with or without a
    known memory limit."""
    from agent_worker.runners import cliagent

    line = _line(tmp_path, monkeypatch, memory)

    assert "background" not in line and "nohup" not in line, line
    limit = cliagent.time_limit_line(3600)
    assert "in the background (`&`, `nohup`)" in limit, limit


@pytest.mark.parametrize("memory", [8 * GIB, 5 * GIB])
def test_the_line_says_kill_a_timed_out_commands_process_group(tmp_path, monkeypatch, memory):
    line = _line(tmp_path, monkeypatch, memory)

    assert "times out" in line and "process group" in line, line
    assert "before starting another" in line, line


@pytest.mark.parametrize(("memory", "mib"), [(8 * GIB, 6656), (5 * GIB, 3584)])
def test_old_space_is_capped_at_the_limit_less_one_and_a_half_gib(
    tmp_path, monkeypatch, memory, mib
):
    line = _line(tmp_path, monkeypatch, memory)

    assert mib == (memory - GIB - GIB // 2) // MIB
    assert f"--max-old-space-size above {mib} " in line, line
    assert str(memory // MIB) not in line, line


# THE LIVE-BROWSER POD (#3104). Its worker container holds the dev server the
# browser renders, and its page compiles, inside this same limit: the line says
# so, caps the dev server's Node heap at the line's own old-space figure, and
# keeps it up while the browser renders -- stopped before tsc or the tests.
# The signal is the one `claude_code.live_browser_refusal` reads: SWARM_HANDOFF,
# which the lifecycle sets only for an attempt with a hand-off spool.
# MUTATIONS: drop the live branch from `cliagent.budget_line`; cap the dev
# server at another figure than old_space; tell the live agent to stop its dev
# server while the browser renders.


def _live_line(tmp_path, monkeypatch, memory: int) -> str:
    from agent_worker.handoff import HANDOFF_ENV

    spool = tmp_path / ".swarm-handoff"
    spool.mkdir()
    monkeypatch.setenv(HANDOFF_ENV, str(spool))
    return _line(tmp_path, monkeypatch, memory)


@pytest.mark.parametrize("memory", [8 * GIB, 5 * GIB])
def test_the_live_line_says_the_dev_server_shares_this_containers_limit(
    tmp_path, monkeypatch, memory
):
    line = _live_line(tmp_path, monkeypatch, memory)

    assert "dev server and its page compiles share this container's limit" in line, line


@pytest.mark.parametrize(("memory", "mib"), [(8 * GIB, 6656), (5 * GIB, 3584)])
def test_the_live_line_caps_the_dev_servers_heap_at_old_space(tmp_path, monkeypatch, memory,
                                                              mib):
    line = _live_line(tmp_path, monkeypatch, memory)

    assert f"NODE_OPTIONS=--max-old-space-size={mib} " in line, line
    assert f"--max-old-space-size above {mib} " in line, line


@pytest.mark.parametrize("memory", [8 * GIB, 5 * GIB])
def test_the_live_line_keeps_the_dev_server_up_while_the_browser_renders(
    tmp_path, monkeypatch, memory
):
    line = _live_line(tmp_path, monkeypatch, memory)

    assert "keep it up while the browser renders" in line, line
    assert "stop it before running tsc or the tests" in line, line
    assert "Stop any dev server you started" not in line, line


@pytest.mark.parametrize(("memory", "workers"), [(8 * GIB, 6), (5 * GIB, 3)])
def test_the_line_without_a_live_browser_is_unchanged(tmp_path, monkeypatch, memory, workers):
    from agent_worker.handoff import HANDOFF_ENV
    from agent_worker.runners import cliagent

    monkeypatch.delenv(HANDOFF_ENV, raising=False)
    line = _line(tmp_path, monkeypatch, memory)

    assert line == cliagent.budget_line(memory, workers), line
    assert "Stop any dev server you started (next dev, uvicorn, postgres) before running " \
        "tsc or the tests;" in line, line
    assert "NODE_OPTIONS" not in line and "browser" not in line, line
