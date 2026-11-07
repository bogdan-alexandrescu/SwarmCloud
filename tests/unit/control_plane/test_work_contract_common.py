"""Contract request 136, part A: the work contract's digest is one definition.

`swarm_common.workcontract` holds the purpose, the schema version, the
canonical projection (`WorkContract.to_document`) and `contract_digest`, moved
from `swarm_api.workcontract` unchanged in bytes. swarm-api imports them and
keeps the schema validation and the amendment protocol; a worker digests a
stored contract body with the same function before it enforces any of it.
"""

from __future__ import annotations

import dataclasses
import hashlib

from swarm_common import specsign
from swarm_common import workcontract as common

from swarm_api import workcontract as wc

from .test_work_contract import _doc, _strict_enforceable

#: The digests of the existing test vectors (`test_work_contract._doc` and
#: `_strict_enforceable`), as `swarm_api.workcontract.digest` computed them on
#: main (0e79d7a) before the move. A change here is a new schema, never an edit.
PINNED = (
    (_doc, "6c2594971f5e6439dca98f80fefb5e43b4ca63d9ae4f2b018e8efe75a6aabc0b"),
    (_strict_enforceable, "9a36acbe9c9334dd6a704e3c2d5e216eb1391873f96a4bfcfd0dbe896569de69"),
)

MINIMAL = {
    "schema": 1, "contract_id": "wc-pin", "tenant_id": "t", "revision": 1,
    "strictness": "strict", "requirements": [{"id": "REQ-1", "text": "x"}],
    "limits": {"runner_profile": "claude-code"}, "ownership": {"owner": "o@example.com"},
}

MINIMAL_BYTES = (
    b'{"contract":{"artifacts":[],"capabilities":{"plugins":{},"runtime":[]},'
    b'"contract_id":"wc-pin","limits":{"budget":null,"max_turns":null,"model_class":null,'
    b'"runner_profile":"claude-code","timeout_seconds":null},"ownership":{"depends_on":[],'
    b'"owner":"o@example.com","withdrawn":[]},"previous_digest":null,'
    b'"protected":{"interfaces":[],"paths":[]},"repositories":[],'
    b'"requirements":[{"id":"REQ-1","text":"x","verification":[]}],"revision":1,'
    b'"schema":1,"scope":{"read":[],"write":[]},"strictness":"strict","tenant_id":"t"},'
    b'"purpose":"swarm.work-contract/v1"}'
)


def test_the_digest_of_every_existing_vector_is_unchanged_by_the_move():
    for build, pinned in PINNED:
        c = wc.decode(build())
        assert wc.digest(c) == pinned
        assert common.contract_digest(c.to_document()) == pinned


def test_the_shared_canonical_bytes_are_schema_ones_pinned_bytes():
    c = wc.decode(MINIMAL)
    assert common.canonical_bytes(c.to_document()) == MINIMAL_BYTES
    assert wc.canonical_bytes(c) == MINIMAL_BYTES
    assert common.contract_digest(c.to_document()) == hashlib.sha256(MINIMAL_BYTES).hexdigest()


def test_swarm_api_imports_the_one_definition_rather_than_restating_it():
    assert wc.CONTRACT_PURPOSE is common.CONTRACT_PURPOSE == "swarm.work-contract/v1"
    assert wc.SCHEMA_VERSION is common.SCHEMA_VERSION
    assert common.SCHEMA_VERSION == 1
    # swarm-api's contract adds only what swarm-api reads, and `acceptance`:
    # the fields and the projection the digest covers are the shared class's,
    # and a strict contract (every contract written before `acceptance`)
    # projects to exactly the shared document.
    assert issubclass(wc.WorkContract, common.WorkContract)
    assert [f.name for f in dataclasses.fields(wc.WorkContract)] == \
        [f.name for f in dataclasses.fields(common.WorkContract)] + ["acceptance"]
    strict = wc.decode(_doc())
    assert strict.to_document() == common.WorkContract.to_document(strict)
    auto = wc.decode({**_doc(), "acceptance": "auto"})
    assert auto.to_document() == {**common.WorkContract.to_document(auto), "acceptance": "auto"}
    assert wc.Requirement is common.Requirement
    assert common.CONTRACT_PURPOSE not in (specsign.SPEC_PURPOSE, specsign.SETTINGS_PURPOSE)


def test_a_worker_digests_a_stored_body_as_swarm_api_signed_its_reference():
    """What the worker does: the body it fetched, as stored (`to_document`),
    against the reference's digest inside its verified spec."""
    c = wc.decode(_doc())
    stored = c.to_document()
    reference = wc.compile_contract(c, step_id="s1").reference
    assert common.contract_digest(stored) == reference["digest"]
    stored["scope"]["write"].append("**")
    assert common.contract_digest(stored) != reference["digest"]
