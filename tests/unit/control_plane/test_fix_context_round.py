"""A CI fix round asks for the TESTED CODE block of its OWN run's tenant (lane KG6, §4.4).

test_review_context.py holds what the block says; this holds the wiring: the
red reading's round passes the run's own tenant and the excerpt it stored to
`reviewcontext.read_fix_context`, and the round's prompt carries the answer
after the failing checks. No answer is today's prompt.
"""

from __future__ import annotations

from swarm_api import issueci

from .test_issue_run_ci import (  # noqa: F401 -- fixtures: the forge writes, the clock, the context
    SHA_A,
    _read,
    _rounds,
    _step_task,
    _stored,
    _to_checking,
    api_context,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_runs import _members_hold_grants  # noqa: F401 -- autouse fixture


def test_the_round_asks_for_its_own_tenants_block_and_carries_it(
    client, db, objects, writes, clock, monkeypatch
):
    asked: list[tuple[str, str, str]] = []

    def block(ctx, tenant_id, run, excerpt):
        asked.append((tenant_id, run.id, excerpt))
        return "=== TESTED CODE n s ===\n- `src/a.py#f` in `src/a.py`\n=== END TESTED CODE n ===\n"

    monkeypatch.setattr(issueci, "read_fix_context", block)
    running = _to_checking(client, db, objects, writes, clock)
    writes.check(SHA_A, "unit", "failure", output={"summary": "FAILED tests/a/test_f.py::test_f"})
    _read(client, clock, running["id"])

    stored = _stored(db, running["id"])
    assert asked == [(stored["tenant_id"], running["id"], stored["failure_excerpt"])]
    (round_one,) = _rounds(db, running["id"])
    prompt = _step_task(db, round_one, issueci.CI_FIX_STEP)["input"]["prompt"]
    assert prompt.index("=== TESTED CODE n s ===") > prompt.rindex("=== FAILING CHECKS")


def test_no_block_is_todays_round_prompt(client, db, objects, writes, clock, monkeypatch):
    monkeypatch.setattr(issueci, "read_fix_context", lambda *_a, **_k: None)
    running = _to_checking(client, db, objects, writes, clock)
    writes.check(SHA_A, "unit", "failure", output={"summary": "boom"})
    _read(client, clock, running["id"])
    (round_one,) = _rounds(db, running["id"])
    prompt = _step_task(db, round_one, issueci.CI_FIX_STEP)["input"]["prompt"]
    assert "TESTED CODE" not in prompt
    assert prompt.rstrip().endswith("===")  # the excerpt's closing nonce line ends it
