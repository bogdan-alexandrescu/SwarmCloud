"""`sc admin purge`: the audited purge of failed history, from a terminal.

Owner request 2026-10-11. A dry run unless `--delete`; with it, the plan is
printed first and nothing is deleted until `purge` is typed AT A TERMINAL.
SWARM_ASSUME_YES is ignored. Each test fails without the command: there was
no `admin` subcommand, so every argv below was an argparse error.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from swarm_mcp import sc
from swarm_mcp.client import SwarmError

PATH = "/v1/admin/history:purge"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _page(ids, token=None, *, kind="workflows"):
    rows = [{"id": i, "tenant_id": "eng", "state": "FAILED", "task_ids": [f"t_{i}"],
             "attempts": 1, "leases": 0, "events": 2, "artifact_objects": 3} for i in ids]
    body = {"workflows": [], "tasks": [], "skipped": [], "failed": [],
            "next_page_token": token}
    body[kind] = rows
    return body


class FakeApi:
    def __init__(self):
        self.sent: list[dict] = []

    def request(self, method, path, payload=None, **kwargs):
        assert (method, path) == ("POST", PATH)
        self.sent.append(payload)
        if payload.get("page_token") is None:
            return _page(["wf_1"], token="w:wf_1")
        return {**_page(["t_9"], kind="tasks"),
                "skipped": [{"kind": "task", "id": "t_live", "reason": "live_lease",
                             "detail": "holds lease l1"}]}

    def deletes(self):
        return [p for p in self.sent if p["dry_run"] is False]


def run(argv, api, *, stdin=None):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    old = sys.stdin
    sys.stdin = stdin if stdin is not None else io.StringIO("")
    try:
        code = args.func(api, args, out)
    finally:
        sys.stdin = old
    return code, out.getvalue()


def test_the_default_is_a_dry_run_that_prints_the_whole_plan():
    api = FakeApi()
    code, out = run(["admin", "purge"], api)
    assert code == sc.EXIT_OK
    assert api.deletes() == []
    assert all(p["dry_run"] is True and "confirm" not in p for p in api.sent)
    assert [p.get("page_token") for p in api.sent] == [None, "w:wf_1"]
    assert api.sent[0]["states"] == ["FAILED"]
    assert "would delete: 1 workflow(s), 1 standalone task(s)" in out
    assert "wf_1" in out and "t_9" in out and "t_live" in out and "live_lease" in out
    assert "nothing was deleted" in out


def test_filters_reach_the_request():
    api = FakeApi()
    run(["admin", "purge", "--state", "FAILED", "--state", "CANCELLED", "--tenant", "eng",
         "--before", "2026-10-01T00:00:00Z", "--exclude", "wf_keep", "--limit", "20"], api)
    first = api.sent[0]
    assert first["states"] == ["FAILED", "CANCELLED"] and first["tenant_id"] == "eng"
    assert first["before"] == "2026-10-01T00:00:00Z" and first["exclude_ids"] == ["wf_keep"]
    assert first["limit"] == 20


def test_a_live_state_cannot_be_asked_for():
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["admin", "purge", "--state", "RUNNING"])


def test_delete_deletes_only_after_purge_is_typed_at_a_terminal(monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi()
    code, out = run(["admin", "purge", "--delete"], api, stdin=_Tty("purge\n"))
    assert code == sc.EXIT_OK
    deletes = api.deletes()
    assert deletes and all(p["confirm"] == "purge" for p in deletes)
    # The plan was printed, from a dry run, before anything was deleted.
    assert api.sent.index(deletes[0]) == 2
    assert "would delete:" in out and "deleted: 1 workflow(s)" in out


@pytest.mark.parametrize("typed", ["", "y\n", "yes\n", "PURGE\n", "delete\n"])
def test_anything_but_the_word_deletes_nothing_even_with_assume_yes(monkeypatch, typed):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        run(["admin", "purge", "--delete"], api, stdin=_Tty(typed))
    assert "nothing was deleted" in str(refused.value)
    assert api.deletes() == []


def test_no_terminal_means_nothing_is_deleted_even_with_the_word_piped_in(monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        run(["admin", "purge", "--delete"], api, stdin=io.StringIO("purge\n"))
    assert "terminal" in str(refused.value)
    assert api.deletes() == []


def test_json_dry_run_is_data():
    api = FakeApi()
    code, out = run(["admin", "purge", "--json"], api)
    body = json.loads(out)
    assert code == sc.EXIT_OK and body["complete"] is True
    assert [w["id"] for w in body["workflows"]] == ["wf_1"]
    assert api.deletes() == []
