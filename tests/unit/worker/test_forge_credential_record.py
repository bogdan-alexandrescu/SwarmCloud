"""Each run records WHICH KIND of GitHub credential it cloned and pushed with.

Lesson 43 (docs/design/day-one/lessons.md): a verification run must use a
GitHub App installation token, never a personal access token. Stage 14 of
`swarm install` checks it, so the worker writes `result_summary.forge_credential`:

  * `kind`: `app_installation`, `app_user`, `pat` or `none` (`unknown` for a
    value whose shape GitHub documents no prefix for), classified in memory;
  * `source`: the secret's NAME (`swarm-tenant-<tenant>-git`), never its value.

Never a token, a prefix of one or a hash of one. Every fake token here is
built at runtime (`secrets.token_hex`), never written as one literal.
"""

from __future__ import annotations

import hashlib
import json
import secrets

import pytest

from agent_worker import secrets as secrets_mod
from agent_worker import workspace as workspace_mod

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt

GITHUB_URL = "https://github.com/example-org/widgets.git"
TENANT_SECRET = f"swarm-tenant-{TENANT}-git"


def _fake(prefix_letters: str) -> str:
    """A token of GitHub's documented shape for `prefix_letters`, built now."""
    return prefix_letters + "_" + secrets.token_hex(18)


def installation_token() -> str:
    return _fake("gh" + "s")


def app_user_token() -> str:
    return _fake("gh" + "u")


def classic_pat() -> str:
    return _fake("gh" + "p")


def fine_grained_pat() -> str:
    return _fake("github" + "_pat")


def _tenant_worker(db, worker_factory, value: str | None):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    client = FakeSecretClient({TENANT_SECRET: value} if value is not None else {})
    worker, config, _ = worker_factory(secret_client=client)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": None}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL
    return worker, config


def _token_shaped(text: str, values: list[str]) -> list[str]:
    """Every way a recorded string could carry one of `values`."""
    found = []
    for value in values:
        head = value.split("_", 1)[0] + "_"
        body = value[len(head):]
        for piece in (value, body, body[:8], body[-4:], hashlib.sha256(value.encode()).hexdigest()[:12]):
            if piece and piece in text:
                found.append(piece)
        if head in text:
            found.append("a token prefix")
    return found


# --------------------------------------------------------------------------
# The classification, in memory
# --------------------------------------------------------------------------


@pytest.mark.parametrize("make, kind", [
    (installation_token, "app_installation"),
    (app_user_token, "app_user"),
    (classic_pat, "pat"),
    (fine_grained_pat, "pat"),
    (lambda: _fake("gh" + "o"), "pat"),
])
def test_the_kind_is_read_from_githubs_documented_prefix(make, kind):
    assert secrets_mod.forge_credential_kind(make()) == kind


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_token_is_none(value):
    assert secrets_mod.forge_credential_kind(value) == "none"


def test_a_value_of_no_documented_shape_is_unknown_never_an_app_token():
    assert secrets_mod.forge_credential_kind(secrets.token_hex(20)) == "unknown"


# --------------------------------------------------------------------------
# What the run records
# --------------------------------------------------------------------------


def test_a_run_with_an_app_installation_token_records_app_installation(db, worker_factory):
    value = installation_token()
    worker, _ = _tenant_worker(db, worker_factory, value)

    assert worker._git_token() == value
    assert worker._forge_credential_summary() == {"kind": "app_installation", "source": TENANT_SECRET}


def test_a_run_with_a_pat_records_pat(db, worker_factory):
    value = classic_pat()
    worker, _ = _tenant_worker(db, worker_factory, value)

    worker._git_token()
    assert worker._forge_credential_summary() == {"kind": "pat", "source": TENANT_SECRET}


def test_a_json_payloads_git_token_is_what_is_classified(db, worker_factory):
    value = fine_grained_pat()
    worker, _ = _tenant_worker(db, worker_factory, json.dumps({"GIT_TOKEN": value}))

    worker._git_token()
    assert worker._forge_credential_summary() == {"kind": "pat", "source": TENANT_SECRET}


def test_a_tenant_without_a_git_slot_records_none_and_no_source(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory(secret_client=FakeSecretClient())
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": None}
    worker._forge_signed = True

    assert worker._git_token() is None
    assert worker._forge_credential_summary() == {"kind": "none", "source": None}


def test_a_run_that_never_read_a_credential_records_nothing(db, worker_factory):
    worker, _ = _tenant_worker(db, worker_factory, installation_token())
    assert worker._forge_credential_summary() is None


def test_reads_of_two_kinds_in_one_attempt_are_mixed_and_list_both(db, worker_factory):
    """The clone and the push each read the latest version: a slot rotated
    from an installation token to a PAT between them is not reported as an
    installation token."""
    worker, _ = _tenant_worker(db, worker_factory, installation_token())
    worker._git_token()
    worker.secret_client.values[TENANT_SECRET] = classic_pat()
    worker._git_token()

    assert worker._forge_credential_summary() == {
        "kind": "mixed", "kinds": ["app_installation", "pat"], "source": TENANT_SECRET,
    }


def test_the_final_upload_carries_the_record(db, worker_factory):
    value = installation_token()
    worker, config = _tenant_worker(db, worker_factory, value)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._git_token()

    summary = worker._upload_outputs()

    assert summary["forge_credential"] == {"kind": "app_installation", "source": TENANT_SECRET}


@pytest.mark.parametrize("make", [installation_token, app_user_token, classic_pat, fine_grained_pat])
def test_no_recorded_field_holds_anything_token_shaped(db, worker_factory, make):
    """Not the value, not its prefix, not a piece of it, not a hash of it --
    in the summary or in any log line the read wrote."""
    value = make()
    worker, config = _tenant_worker(db, worker_factory, value)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._git_token()

    recorded = json.dumps(worker._upload_outputs()["forge_credential"])
    assert _token_shaped(recorded, [value]) == []
    assert secrets_mod.FORGE_CREDENTIAL_KINDS >= {json.loads(recorded)["kind"]}
