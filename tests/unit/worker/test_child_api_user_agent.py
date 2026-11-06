"""The worker's swarm-api client names itself with its version (BGD's pick,
2026-11-06): the request log tells an agent worker from the plugin bridge."""

from __future__ import annotations

import io

from agent_worker import __version__
from agent_worker import children as children_mod


class _Response:
    status = 200

    def __init__(self) -> None:
        self._body = io.BytesIO(b"{}")

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_every_child_api_request_carries_the_worker_user_agent():
    sent = []

    def _open(req, timeout=None):  # noqa: ARG001
        sent.append(req)
        return _Response()

    api = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id", opener=_open)
    api.call("GET", "/v1/tasks/t/children")
    api.call("POST", "/v1/tasks/t/children", body=b"{}")
    assert {req.get_header("User-agent") for req in sent} == {f"agent-worker/{__version__} (children)"}
