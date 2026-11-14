"""The worker passes the opt-in latency measurement to the graph write (part of #1840).

swarm-api names a registration's `index.measure_latency_hours` in the SIGNED
prompt, on a line of its own (`MEASURE_LATENCY_LINE`); the worker reads it
from there -- never from metadata, which the spec signature does not cover --
and adds `--measure-latency-hours N` to the graph write. No line, two lines,
or a value the settings would refuse: no flag, and the write is unmeasured.
"""

from __future__ import annotations

import pytest

from agent_worker import indexrun
from swarm_api import repoindex
from swarm_api.repositories import MEASURE_LATENCY_HOURS_RANGE

from worker_seeds import TENANT
from test_index_run_incremental import (  # noqa: F401 - fixtures
    HEAD,
    URL,
    _order,
    _seed,
    checkout,
    index_tools,
)

FLAG = "--measure-latency-hours"


def _prompt(*lines: str) -> str:
    return "index it\n" + "".join(f"{line}\n" for line in lines)


def _line(value: object) -> str:
    return f"{indexrun.MEASURE_LATENCY_LINE}{value}"


def test_the_worker_reads_the_line_swarm_api_writes():
    assert indexrun.MEASURE_LATENCY_LINE == repoindex.MEASURE_LATENCY_LINE
    assert indexrun.MEASURE_LATENCY_HOURS_RANGE == MEASURE_LATENCY_HOURS_RANGE
    record = {"repo_id": "repo_0123456789abcdef", "tenant_id": TENANT, "owner": "eng",
              "repo": "app", "default_branch": "main", "repository_url": URL,
              "index": {"measure_latency_hours": 24}}
    task = repoindex.indexer_task(record, HEAD)
    assert indexrun.measure_latency_hours(task.input) == 24


@pytest.mark.parametrize(("task_input", "hours"), [
    ({"prompt": _prompt(_line(24))}, 24),
    ({"prompt": _prompt(_line(1))}, 1),
    ({"prompt": _prompt(_line(168))}, 168),
    ({"prompt": _prompt()}, None),
    ({"prompt": _prompt(_line(24), _line(24))}, None),
    ({"prompt": _prompt(_line(0))}, None),
    ({"prompt": _prompt(_line(169))}, None),
    ({"prompt": _prompt(_line("1.5"))}, None),
    ({"prompt": _prompt(_line("24 --tenant other"))}, None),
    ({"prompt": _prompt(_line(""))}, None),
    ({"prompt": None}, None),
    (None, None),
])
def test_the_hours_are_read_from_the_signed_prompt_only(task_input, hours):
    # MUTATION: believing a doubled or malformed line passes a value
    # swarm-api never wrote to the indexer's command line.
    assert indexrun.measure_latency_hours(task_input) == hours


def _where() -> indexrun.Target:
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    return where


def test_graph_write_argv_builds_the_flag_only_when_set(tmp_path):
    kwargs = dict(work=tmp_path, artifacts=tmp_path, where=_where(), bucket="bucket")
    bare = indexrun.graph_write_argv("swarm-repo-graph", **kwargs)
    assert FLAG not in bare
    measured = indexrun.graph_write_argv("swarm-repo-graph", measure_latency_hours=24, **kwargs)
    at = measured.index(FLAG)
    assert measured[at + 1] == "24"
    assert measured[:at] == bare


def _run_write(db, worker_factory, index_tools) -> list[str]:
    db.doc("tasks/task_1")["repository_ref"] = HEAD
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=1800,
                                  repository_url=URL)
    worker.run()
    [write] = [row for name, row in _order(index_tools) if name == "graph_write"]
    return write["argv"]


def test_the_index_run_passes_the_signed_hours_to_the_graph_write(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full", prompt=_prompt(_line(24)))
    argv = _run_write(db, worker_factory, index_tools)
    # MUTATION: lifecycle not passing `measure_latency_hours=` to
    # graph_write_argv and the registration's setting is never acted on.
    assert argv[argv.index(FLAG) + 1] == "24"


def test_an_index_run_without_the_line_stays_unmeasured(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    db.doc("tasks/task_1")["metadata"]["measure_latency_hours"] = 24  # unsigned: ignored
    argv = _run_write(db, worker_factory, index_tools)
    assert FLAG not in argv
