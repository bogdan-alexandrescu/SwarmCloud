"""`ChildPath.adopt`: the child path signs with the key `state/open` registered.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §4.3, §4.6
"`children.py`: uses the key `state/open` registered instead of registering
its own"). With the tenant switched, one key per attempt signs both kinds of
request, and the child path is offered exactly as its own registration
would have offered it: a `child`-purpose key is the child path; a key from an
unprotected heap (state only, owner decision Q4) is today's tombstone; no key
is no child path. Never raises, and the note says why.
"""

from __future__ import annotations

import io
import secrets
from types import SimpleNamespace

from agent_worker.children import WORKER_UNPROTECTED, AttemptKey, ChildPath, request_message
from agent_worker.logs import build_logger


def _path() -> ChildPath:
    cfg = SimpleNamespace(child_nonce="nonce-" + secrets.token_hex(4),
                          swarm_api_url="https://swarm-api.test", swarm_api_audience=None,
                          tenant_id="eng", task_id="task_1", attempt_id="att_1",
                          lease_id="lease_1", generation=1, child_submit_retry_seconds=5)
    logger = build_logger(task_id="task_1", attempt_id="att_1", tenant_id="eng",
                          generation=1, runner_profile="mock", stream=io.StringIO())
    return ChildPath(cfg, logger=logger, memory=None, api=object())


def test_a_child_purpose_key_is_the_child_path():
    path, key, notes = _path(), AttemptKey(), []

    path.adopt(key, {"registered": "key", "purpose": ["child", "state"],
                     "unprotected": False}, notes.append)

    assert path.keyed and path.offered and path.unavailable is None
    assert notes == [] and path.search_flag is False
    # The child routes' proof is the same key's, under the child purpose.
    headers = path._key.headers("POST", "/v1/tasks/t/children", b"{}", now=17.0)
    assert headers["X-Swarm-Attempt-Proof"] == key.sign(
        request_message("POST", "/v1/tasks/t/children", b"{}", "17"))


def test_a_state_only_key_is_the_tombstone_of_an_unprotected_heap():
    path, notes = _path(), []

    path.adopt(AttemptKey(), {"registered": "key", "purpose": ["state"], "unprotected": True},
               notes.append)

    assert not path.keyed and not path.offered
    assert path.unavailable == WORKER_UNPROTECTED
    assert notes == [{"phase": "child_key", "child_path": "child_key_tombstone",
                      "reason": WORKER_UNPROTECTED}]


def test_no_key_is_no_child_path_and_says_so():
    path, notes = _path(), []

    path.adopt(None, None, notes.append)

    assert not path.keyed and path.unavailable == "child_key_unregistered"
    assert notes and notes[0]["child_path"] == "child_key_unregistered"


def test_adopt_enables_search_when_state_open_says_so():
    """Lane ISO-SEARCH: `adopt` reads `search_tool` exactly as `register` does."""
    path = _path()

    path.adopt(AttemptKey(), {"registered": "key", "purpose": ["child", "state"],
                              "unprotected": False, "search_tool": True}, [].append)

    assert path.keyed and path.search_flag is True


def test_adopt_enables_search_only_on_a_literal_true():
    for answer in ({}, {"search_tool": False}, {"search_tool": "true"}, {"search_tool": 1}):
        path = _path()
        path.adopt(AttemptKey(), {"registered": "key", "purpose": ["child", "state"],
                                  "unprotected": False, **answer}, [].append)
        assert path.keyed and path.search_flag is False, answer


def test_a_tombstone_adopts_no_search_tool_whatever_the_answer_says():
    path = _path()

    path.adopt(AttemptKey(), {"registered": "key", "purpose": ["state"], "unprotected": True,
                              "search_tool": True}, [].append)

    assert not path.keyed and path.search_flag is False
