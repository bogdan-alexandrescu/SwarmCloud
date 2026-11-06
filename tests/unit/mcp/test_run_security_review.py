"""`sc run --security-review` and `swarm_run_issue`'s `security_review` (owner, 2026-11-04).

Both build the issue-run request through `SwarmClient.create_run`, which
sends `security_review: true` only when asked, so a run that did not ask
sends exactly the body it sent before and the API's default (off) decides
(docs/design/security-review-step.md). No credentials, no network.
"""

from __future__ import annotations

import io

from swarm_mcp import sc, server

from test_issue_runs import FakeApi, _client


def test_create_run_sends_the_option_only_when_asked():
    api = FakeApi()
    _client(api).create_run(issue="o/r#5", security_review=True)
    _client(api).create_run(issue="o/r#5")
    asked, plain = (sent[2] for sent in api.posts())
    assert asked["security_review"] is True
    assert "security_review" not in plain


def test_sc_run_security_review_sends_it():
    parser = sc.build_parser()
    assert parser.parse_args(["run", "--issue", "o/r#5"]).security_review is False
    api = FakeApi()
    args = parser.parse_args(["run", "--issue", "o/r#5", "--security-review", "--json"])
    sc.cmd_run(_client(api), args, io.StringIO())
    (sent,) = api.posts()
    assert sent[2]["security_review"] is True


def test_the_tool_advertises_the_option_and_passes_it_strictly():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_run_issue"]
    prop = tool["inputSchema"]["properties"]["security_review"]
    assert prop["type"] == "boolean" and prop["default"] is False
    api = FakeApi()
    server._call(_client(api), "swarm_run_issue", {"issue": "o/r#5", "security_review": True})
    server._call(_client(api), "swarm_run_issue", {"issue": "o/r#5", "security_review": "false"})
    asked, refused = (sent[2] for sent in api.posts())
    assert asked["security_review"] is True
    assert "security_review" not in refused, "the string 'false' is not true"
