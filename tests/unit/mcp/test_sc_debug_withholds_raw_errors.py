"""`sc debug` prints no API text the API did not say it masked (#453 boxes 84, 85).

`_log_tail` printed the logs route's `detail` for an unreadable stream, and
`debug_report` printed `str(SwarmError)` for any section it could not read --
which, for an HTTP failure, quotes the API's or the edge's error body. Neither
comes with a redaction count, and `_shown`'s rule is that a string the API did
not say it masked is withheld. Both are now: an unreadable stream is reported
by name, and a failed read by its HTTP status, edge flag and error code, never
by its body. The fake credential is assembled at runtime.
"""

from __future__ import annotations

import io
import json

from swarm_mcp import sc
from swarm_mcp.client import SwarmError

#: Unique, so its absence is measurable; not shaped like any provider's key.
LEAK = "fake" + "-unmasked-" + "body" + "-0451"


class Api:
    """A task whose every log stream is unreadable, with a detail no one masked."""

    def __init__(self, fail: dict[str, SwarmError] | None = None):
        self.fail = fail or {}

    def _maybe(self, what):
        if what in self.fail:
            raise self.fail[what]

    def task(self, task_id):
        self._maybe("task")
        return {"id": task_id, "state": "FAILED", "runner_profile": "claude-code",
                "last_error": None, "input_redaction_count": 0, "metadata_redaction_count": 0}

    def attempts(self, task_id, *, limit=20):
        self._maybe("attempts")
        return [{"attempt_id": "att_1", "generation": 1, "error": None}]

    def events_page(self, task_id, *, limit=50, newest_first=False, page_token=None):
        self._maybe("events")
        return [], None, True

    def logs(self, task_id, *, attempt_id=None, stream=None, source="auto", offset=0,
             limit_bytes=None, timeout=60):
        self._maybe("logs")
        return {"attempt_id": "att_1", "streams": [
            {"stream": stream, "status": "unreadable", "detail": f"bucket said {LEAK}"},
        ]}


def _debug(api) -> str:
    out = io.StringIO()
    args = sc.build_parser().parse_args(["debug", "task_1", "--json"])
    args.func(api, args, out)
    text = out.getvalue()
    json.loads(text)
    return text


def test_an_unreadable_streams_detail_is_withheld():
    text = _debug(Api())
    assert LEAK not in text
    shown = json.loads(text)["log_tail"]
    assert shown["value"] is None
    assert "agent_stderr: unreadable" in shown["not_read_because"]
    assert "withheld" in shown["not_read_because"]


def test_an_unreadable_streams_detail_with_a_count_is_shown():
    class Counted(Api):
        def logs(self, task_id, **kwargs):
            served = super().logs(task_id, **kwargs)
            served["streams"][0].update(detail="bucket said ********", detail_redaction_count=1)
            return served

    shown = json.loads(_debug(Counted()))["log_tail"]
    assert "bucket said ********" in shown["not_read_because"]


def test_a_failed_reads_error_body_is_withheld():
    raw = SwarmError(f"GET /v1/tasks/task_1/attempts -> 500: {LEAK}", status=500, code="internal")
    edge = SwarmError(f"GET /v1/tasks/task_1 -> 403: IAP refused -- {LEAK}", status=403, edge=True)
    text = _debug(Api(fail={"attempts": raw, "task": edge, "events": raw, "logs": raw}))
    assert LEAK not in text
    shown = json.loads(text)
    assert "500" in shown["attempts"]["not_read_because"]
    assert "internal" in shown["attempts"]["not_read_because"]
    assert "403" in shown["task"]["not_read_because"]
    assert "edge" in shown["task"]["not_read_because"]


def test_a_reason_the_bridge_wrote_itself_is_still_printed():
    """A SwarmError with no HTTP status is the bridge's own sentence (a
    transport failure, `could not reach ...`): it quotes no response body."""
    text = _debug(Api(fail={"events": SwarmError("could not reach https://swarm.example.test: timed out")}))
    assert "could not reach https://swarm.example.test" in json.loads(text)["events"]["not_read_because"]
