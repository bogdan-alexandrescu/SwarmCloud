"""The text of each hunk a contract verdict cites, stored on the step.

The owner's pick on PR #3255 (jev-contract.md sections 5.4 and 8.2): the
stored `contract_acceptance` keeps the TEXT of each hunk a verdict cites
(`hunks`, by item), as JC2 was shown it -- cut to `HUNK_CHARS` and masked --
from the worker's verdict and from the control plane's re-ask, so the person
deciding reads what Jev read even after the published diff moves.
`test_contract_acceptance_served.py` holds where it is served.

Each assertion names the MUTATION it catches. Offline: the in-memory
Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import secrets

import pytest

from swarm_api import contractjudge as cj
from swarm_api import contractreask as cr

from .test_attemptstate import TASK as VERDICT_TASK
from .test_attemptstate import client, opened, worker_tokens  # noqa: F401 -- fixtures
from .test_contract_reask import TASK as REASK_TASK
from .test_contract_reask import Clock, Judge, minutes, seed_pending
from .test_contract_verdict import OP, body, setup, verdict

HUNK = "@@ -1,2 +1,3 @@\n def form():\n-    return None\n+    return Field('email')"


# --- the hunk text, stored -------------------------------------------------------


def test_the_verdict_route_stores_the_text_of_each_cited_hunk(client, db):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(
        contract, verdict("REQ-1", item="i1"), verdict("REQ-2", item="m0"),
        changed=["src/login/form.py", "tests/test_form.py"], hunks=["@@ uncited @@", HUNK]))
    assert response.status_code == 200, response.text
    acc = db.docs[f"tasks/{VERDICT_TASK}"][cr.ACCEPTANCE_FIELD]
    # MUTATION: storing the path only; storing every hunk sent, cited or not;
    # keying the text by path rather than by the item a verdict cites.
    assert acc[cr.HUNKS_FIELD] == {"i1": HUNK}
    assert acc["changed"] == ["src/login/form.py", "tests/test_form.py"]


def test_a_cited_hunk_is_masked_before_it_is_stored(client, db):
    worker, contract, _ = setup(client, db)
    leaked = "ghp_" + secrets.token_hex(18)
    response = worker.post(client, OP, body(
        contract, verdict("REQ-1", item="i0"), verdict("REQ-2", item="i0"),
        hunks=[f"+TOKEN = '{leaked}'\n"]))
    assert response.status_code == 200, response.text
    stored = db.docs[f"tasks/{VERDICT_TASK}"][cr.ACCEPTANCE_FIELD][cr.HUNKS_FIELD]["i0"]
    # MUTATION: storing the hunk unmasked, so the record serves a credential.
    assert leaked not in stored


@pytest.mark.parametrize("hunks, why", [
    (["a", "b", "c"], "names no changed item"),
    (["x" * (cj.HUNK_CHARS + 1)], "longer than the state cuts one"),
])
def test_hunks_the_state_could_not_have_sent_are_refused_with_nothing_written(
        client, db, hunks, why):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(contract, verdict("REQ-1"), verdict("REQ-2"),
                                                    hunks=hunks))
    # MUTATION: accepting more hunks than items, or an uncut hunk.
    assert response.status_code == 422 and why in response.text
    assert cr.ACCEPTANCE_FIELD not in db.docs[f"tasks/{VERDICT_TASK}"]


def test_a_not_run_verdict_stores_no_hunk(client, db):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(contract, outcome="not_run", reason="transport",
                                                    hunks=[HUNK]))
    assert response.status_code == 200, response.text
    # MUTATION: keeping hunks no verdict cites.
    assert db.docs[f"tasks/{VERDICT_TASK}"][cr.ACCEPTANCE_FIELD][cr.HUNKS_FIELD] == {}


def test_a_reask_stores_the_paths_and_cited_hunks_of_its_own_state(db):
    seed_pending(db)
    judged = cr.ReaskResult(cr.OUTCOME_JUDGED, verdicts=(
        cj.Verdict("REQ-1", cj.MET, "satisfied", "i0", 0.95),
        cj.Verdict("REQ-2", cj.MET, "satisfied", "i0", 0.95)),
        call_id="call-1", changed=("src/login/form.py", "README.md"), hunks=(HUNK, None),
        state_sha256="d" * 64)
    minutes(db, Judge(judged), Clock(), 5)
    acc = db.docs[f"tasks/{REASK_TASK}"][cr.ACCEPTANCE_FIELD]
    assert acc["status"] == cj.JUDGED_MET
    # MUTATION: the re-ask's verdicts citing items of the worker's state
    # (whose `changed` was empty), or dropping the text it read.
    assert acc["changed"] == ["src/login/form.py", "README.md"]
    assert acc[cr.HUNKS_FIELD] == {"i0": HUNK}
