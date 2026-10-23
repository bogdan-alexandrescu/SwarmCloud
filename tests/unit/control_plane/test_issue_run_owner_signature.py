"""An issue run's owner fields are signed, and nothing submits on a run that does not verify (#1569).

A tenant's worker can update any Firestore document it can name, and an issue
run's planner knows its run id. Before RUN-OWNER-SIG the planner could rewrite
its own run's `created_by`, `on_behalf_of` or `plan_approval`, and the
auto-approval then submitted the plan AS the rewritten member, resolving that
member's forge credential (`run_owner_auth` -> `submit_workflow` ->
`_resolve_forge`).

What these tests hold:

  1. A run is created carrying `owner_signature`, made with the step-spec
     signer (the KMS key workers hold only the public half of), and an
     untouched run submits exactly as it did before.
  2. A run whose `created_by`, `on_behalf_of` or `plan_approval` was rewritten
     after creation submits NOTHING -- not at the auto-approval, not at a
     person's approval -- and no forge credential is resolved for it.
  3. A signature copied from another run, or deleted, does not verify.
  4. The refusal's log line names the run and the field, and carries neither
     the signature nor a token.

No credentials, no network, no emulator: the signer is a local P-256 key.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, issueruns
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import Forbidden
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRun
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import runs as runs_routes
from swarm_api.service import SubmissionService
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_issue_runs import PLAN, _approve, _create, _docs, _finish_planner, _run

#: Another member of `eng` (conftest's group map): the identity an agent would
#: rewrite its run to, so the membership check alone still passes.
OTHER_MEMBER = "root@example.com"


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def forge_tokens():
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def api_context(db, tokens, group_map, objects, forge_tokens, signer):
    github = forge_fakes.GitHub(
        issues=[forge_fakes.issue(42, "Widgets cannot be sorted")],
        pulls=[forge_fakes.pull(9, "Sort helpers")],
        files={9: ["src/widgets/sort.py"]},
    )
    return build_context(
        settings=api_settings(
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: signer.public_pem()}),
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
        signer=signer,
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture
def resolved(monkeypatch) -> list[list[str]]:
    """Every `_resolve_forge` call, by the submitters of the tasks it was handed."""
    calls: list[list[str]] = []
    original = SubmissionService._resolve_forge

    def spy(self, tenant, tasks, *, service):
        calls.append([t.submitted_by for t in tasks])
        return original(self, tenant, tasks, service=service)

    monkeypatch.setattr(SubmissionService, "_resolve_forge", spy)
    return calls


def _stored(db, run_id: str) -> dict:
    return db.docs[f"issue_runs/{run_id}"]


def _created(client, db, objects, **body) -> dict:
    response = _create(client, **body)
    assert response.status_code == 201, response.text
    run = response.json()["run"]
    _finish_planner(db, objects, run, PLAN)
    return run


# --------------------------------------------------------------------------
# an untouched run
# --------------------------------------------------------------------------

def test_a_run_is_created_carrying_a_signature_over_its_owner_fields(client, db, signer):
    response = _create(client, plan_approval="auto")
    assert response.status_code == 201, response.text
    stored = _stored(db, response.json()["run"]["id"])
    signature = stored.get(issueruns.OWNER_SIGNATURE)
    # MUTATION: drop the signing in `start_run` and the run is stored with no signature.
    assert isinstance(signature, dict) and signature.get("signature")
    # MUTATION: sign with any other key and the version names that key instead.
    assert signature["key_version"] == KEY_VERSION
    # MUTATION: sign without the run's own id and a copy would verify on any run.
    assert signature["fields"]["run_id"] == stored["id"]
    assert signature["fields"]["created_by"] == "alice@example.com"
    # The signature is never served: GET /v1/runs/{id} shows no part of it.
    served = _run(client, stored["id"]).json()["run"]
    # MUTATION: add `owner_signature` to `to_api` and the response carries it.
    assert issueruns.OWNER_SIGNATURE not in served
    assert signature["signature"] not in json.dumps(served)


def test_an_untouched_auto_run_submits_as_its_creator_exactly_as_before(
        client, db, objects, resolved):
    run = _created(client, db, objects, plan_approval="auto")
    resolved.clear()

    advanced = _run(client, run["id"]).json()["run"]

    # MUTATION: refuse every run in `verify_run_owner` and this stays PLANNED or FAILS.
    assert advanced["state"] == "RUNNING", advanced.get("error")
    stored = _stored(db, run["id"])
    tasks = [d for d in _docs(db, "tasks").values()
             if d.get("workflow_id") == stored["workflow_id"]]
    assert tasks and {t["submitted_by"] for t in tasks} == {"alice@example.com"}
    # MUTATION: verify after the submission and this would still hold, but
    # the tampered tests below would not.
    assert resolved and {who for call in resolved for who in call} == {"alice@example.com"}


def test_an_untouched_required_run_is_approved_by_a_person_exactly_as_before(
        client, db, objects):
    run = _created(client, db, objects)
    planned = _run(client, run["id"]).json()["run"]

    response = _approve(client, run["id"], planned["plan_digest"])

    # MUTATION: refuse every manual approval and this is a 403.
    assert response.status_code == 200, response.text
    assert response.json()["run"]["state"] == "RUNNING"


def test_a_run_the_api_narrowed_to_required_still_verifies(client, db, objects):
    """Jev's duplicate hold flips an auto run to `required` (`_from_planner`):
    narrower than what was signed, so it is not a forgery."""
    run = _created(client, db, objects, plan_approval="auto")
    _stored(db, run["id"])["plan_approval"] = "required"
    planned = _run(client, run["id"]).json()["run"]
    assert planned["state"] == "PLANNED"

    response = _approve(client, run["id"], planned["plan_digest"])

    # MUTATION: compare plan_approval exactly and the narrowed run is refused.
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# a rewritten run submits nothing
# --------------------------------------------------------------------------

REWRITES = [
    pytest.param({"created_by": OTHER_MEMBER}, "created_by", id="created_by"),
    pytest.param({"on_behalf_of": OTHER_MEMBER}, "on_behalf_of", id="on_behalf_of"),
]


@pytest.mark.parametrize("rewrite, field", REWRITES)
def test_a_rewritten_owner_is_refused_at_the_auto_approval(
        client, db, objects, resolved, rewrite, field, caplog):
    run = _created(client, db, objects, plan_approval="auto")
    _stored(db, run["id"]).update(rewrite)
    resolved.clear()
    workflows_before = set(_docs(db, "workflows"))

    with caplog.at_level(logging.WARNING):
        advanced = _run(client, run["id"]).json()["run"]

    # MUTATION: skip `verify_run_owner` in `run_owner_auth` and the run goes RUNNING as root.
    assert advanced["state"] == "FAILED"
    # MUTATION: submit before verifying and a workflow is stored.
    assert set(_docs(db, "workflows")) == workflows_before
    # MUTATION: resolve the forge before verifying and a call is recorded.
    assert resolved == []
    # MUTATION: log no field and the operator cannot tell which field failed.
    assert any(run["id"] in r.getMessage() and field in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("rewrite, field", REWRITES)
def test_a_rewritten_owner_is_refused_at_a_persons_approval(
        client, db, objects, resolved, rewrite, field):
    run = _created(client, db, objects)
    planned = _run(client, run["id"]).json()["run"]
    _stored(db, run["id"]).update(rewrite)
    resolved.clear()

    response = _approve(client, run["id"], planned["plan_digest"])

    # MUTATION: verify only in `run_owner_auth` and a person's approval submits.
    assert response.status_code == 403, response.text
    # MUTATION: raise a plain Forbidden and the code is `forbidden`.
    assert response.json()["code"] == "run_owner_unverified"
    assert not _docs(db, "workflows")
    # MUTATION: verify after `submit_workflow` and the forge is resolved.
    assert resolved == []
    assert _stored(db, run["id"])["state"] == "PLANNED"


def test_plan_approval_flipped_to_auto_is_refused_and_nothing_is_submitted(
        client, db, objects, resolved):
    run = _created(client, db, objects)  # created `required`
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    _stored(db, run["id"])["plan_approval"] = "auto"
    resolved.clear()

    advanced = _run(client, run["id"]).json()["run"]

    # MUTATION: leave plan_approval out of the signed fields and the tick submits it.
    assert advanced["state"] == "FAILED"
    assert not _docs(db, "workflows")
    assert resolved == []


def test_a_signature_copied_from_another_run_does_not_verify(client, db, objects, resolved):
    victim = _created(client, db, objects, plan_approval="auto")
    # root's own run: a valid signature naming root, on another run id.
    donor = _create(client, user="root", plan_approval="auto").json()["run"]
    stored = _stored(db, victim["id"])
    stored["created_by"] = OTHER_MEMBER
    stored[issueruns.OWNER_SIGNATURE] = dict(_stored(db, donor["id"])[issueruns.OWNER_SIGNATURE])
    resolved.clear()

    advanced = _run(client, victim["id"]).json()["run"]

    # MUTATION: leave run_id out of the signed fields and the copy verifies as root.
    assert advanced["state"] == "FAILED"
    assert not any(d.get("tenant_id") == "eng" and d.get("submitted_by") == OTHER_MEMBER
                   and d.get("workflow_id") for d in _docs(db, "tasks").values())
    assert resolved == []


def test_a_deleted_signature_is_refused_never_trusted_as_an_old_run(
        client, db, objects, resolved):
    run = _created(client, db, objects, plan_approval="auto")
    del _stored(db, run["id"])[issueruns.OWNER_SIGNATURE]
    resolved.clear()

    advanced = _run(client, run["id"]).json()["run"]

    # MUTATION: treat an unsigned run as a pre-change run and trust it, and it submits.
    assert advanced["state"] == "FAILED"
    assert resolved == []


def test_a_tampered_signature_value_does_not_verify(client, db, objects, resolved):
    run = _created(client, db, objects, plan_approval="auto")
    signature = _stored(db, run["id"])[issueruns.OWNER_SIGNATURE]
    # The signed copy rewritten too, so the field comparison alone passes.
    signature["fields"] = {**signature["fields"], "created_by": OTHER_MEMBER}
    _stored(db, run["id"])["created_by"] = OTHER_MEMBER
    resolved.clear()

    advanced = _run(client, run["id"]).json()["run"]

    # MUTATION: compare fields without checking the signature and this submits as root.
    assert advanced["state"] == "FAILED"
    assert resolved == []


def test_run_owner_auth_refuses_a_rewritten_run_for_every_later_submission(
        client, db, objects, api_context):
    """The CI fix rounds and the merge (`issueci`) submit through
    `run_owner_auth` too: it is the check, not only the tick's caller."""
    run = _created(client, db, objects, plan_approval="auto")
    stored = dict(_stored(db, run["id"]))
    stored["created_by"] = OTHER_MEMBER

    # MUTATION: verify in `_advance_state` only and `run_owner_auth` answers root.
    with pytest.raises(runs_routes.RunOwnerUnverified) as refused:
        runs_routes.run_owner_auth(api_context, IssueRun.from_firestore(stored))
    assert isinstance(refused.value, Forbidden)
    assert refused.value.code == "run_owner_unverified"


def test_the_refusal_log_holds_neither_the_signature_nor_a_token(
        client, db, objects, forge_tokens, caplog):
    run = _created(client, db, objects, plan_approval="auto")
    stored = _stored(db, run["id"])
    signature = stored[issueruns.OWNER_SIGNATURE]["signature"]
    stored["created_by"] = OTHER_MEMBER

    with caplog.at_level(logging.DEBUG):
        _run(client, run["id"])

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "run_owner_unverified" in logged or "do not verify" in logged
    # MUTATION: log the stored signature map and the signature is in the line.
    assert signature not in logged
    # MUTATION: log the resolved credential and an issued token is in the line.
    for token in forge_tokens.issued.values():
        assert token not in logged
    assert OTHER_MEMBER not in logged
