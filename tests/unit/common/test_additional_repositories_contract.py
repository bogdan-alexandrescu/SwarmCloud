"""`Task.additional_repositories` and spec format 4 (contract requests 95 and 96).

docs/design/multi-repository-steps.md §5 requests A and B, accepted by the
owner 2026-10-17 on PR 1461 with one change: the operative count limit is the
platform setting `max_additional_repositories` (default 5, 1-10), read by
swarm-api at submission. The contract keeps only a hard SHAPE ceiling of 10,
a guard on the document, so these tests hold the ceiling and never a limit.

Held here, red before the change:

  * a malformed entry raises (a name of "..", a `git-r-` slot, a short
    commit, a duplicate name or repo_id, more entries than the ceiling), and
    the message names the key, never the value;
  * a document written before the field decodes with None;
  * format 3 has no canonical form for a set list, so adding one to a task
    signed without it fails verification instead of slipping outside the bytes;
  * at format 4, a rewritten commit (or any rewrite of the list) changes the digest;
  * a task without the list keeps signing at its old format, byte for byte.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from swarm_common import specsign
from swarm_common.models import (
    ADDITIONAL_FORGE_CREDENTIAL,
    ADDITIONAL_REPOSITORIES_CEILING,
    ADDITIONAL_REPOSITORY_KEYS,
    ADDITIONAL_REPOSITORY_NAME,
    ADDITIONAL_REPOSITORY_SOURCES,
    Task,
    check_additional_repositories,
)
from swarm_common.specsign import SpecNotCanonical, canonical_step_spec, spec_digest
from swarm_common.states import TaskState

TASK_ID = "task_0123456789abcdef"
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def _entry(i: int = 0, **over: object) -> dict:
    entry = {
        "name": f"web{i}",
        "repo_id": "repo_" + f"{i:016x}",
        "repository": f"acme/web{i}",
        "repository_url": f"https://github.com/acme/web{i}",
        "ref": "main",
        "commit": f"{i:040x}",
        "forge_credential": "git-u-" + "0123456789abcdef",
        "forge_credential_source": "person",
        "size_kib": 48213,
    }
    entry.update(over)
    return entry


def _task(**over: object) -> Task:
    kwargs: dict = {
        "id": TASK_ID,
        "tenant_id": "eng",
        "created_at": NOW,
        "updated_at": NOW,
        "state": TaskState.QUEUED,
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "input": {"prompt": "which service owns /v1/orders?"},
        "submitted_by": "alice@saga.xyz",
        "repository_url": "https://github.com/acme/api",
    }
    kwargs.update(over)
    return Task(**kwargs)


# ---------------------------------------------------------------------------
# Request A: the shape
# ---------------------------------------------------------------------------


def test_the_shape_constants():
    """The ceiling is a shape guard, never the operative limit (a setting, 1-10)."""
    assert ADDITIONAL_REPOSITORIES_CEILING == 10
    assert ADDITIONAL_REPOSITORY_KEYS == (
        "name", "repo_id", "repository", "repository_url", "ref", "commit",
        "forge_credential", "forge_credential_source", "size_kib",
    )
    assert ADDITIONAL_REPOSITORY_SOURCES == ("tenant", "owner", "person")
    for good in ("web", "a", "web.v2", "my_repo-1", "0" * 63):
        assert ADDITIONAL_REPOSITORY_NAME.fullmatch(good), good
    for bad in ("", ".", "..", "Web", "-web", ".web", "a/b", "a b", "0" * 64):
        assert not ADDITIONAL_REPOSITORY_NAME.fullmatch(bad), bad
    for good in ("git", "git-u-" + "0123456789abcdef"):
        assert ADDITIONAL_FORGE_CREDENTIAL.fullmatch(good), good
    for bad in ("git-r-" + "0123456789abcdef", "git-u-123", "gh", "git-refresh"):
        assert not ADDITIONAL_FORGE_CREDENTIAL.fullmatch(bad), bad


def test_a_well_formed_list_is_accepted_and_written_as_stored():
    entries = [_entry(0), _entry(1, forge_credential="git", forge_credential_source="tenant")]
    task = _task(additional_repositories=entries)
    assert task.additional_repositories == entries
    assert task.to_firestore()["additional_repositories"] == entries


def test_none_and_an_empty_list_are_accepted():
    check_additional_repositories(None)
    check_additional_repositories([])
    assert _task().additional_repositories is None


def test_an_old_document_decodes_with_none():
    """A document written before the field has no such key."""
    doc = _task().to_firestore()
    del doc["additional_repositories"]
    doc["state"] = TaskState(doc["state"])
    assert Task(**doc).additional_repositories is None


def test_the_ceiling_holds_and_one_more_raises():
    entries = [_entry(i) for i in range(ADDITIONAL_REPOSITORIES_CEILING)]
    _task(additional_repositories=entries)
    with pytest.raises(ValueError, match="additional_repositories"):
        _task(additional_repositories=entries + [_entry(ADDITIONAL_REPOSITORIES_CEILING)])


SECRETISH = "s3cr3t-" + "x" * 12


MALFORMED: dict[str, object] = {
    "name_dotdot": [_entry(name="..")],
    "name_dot": [_entry(name=".")],
    "name_upper": [_entry(name="Web")],
    "name_slash": [_entry(name="a/b")],
    "repo_slot": [_entry(forge_credential="git-r-" + "0123456789abcdef")],
    "short_commit": [_entry(commit="abc1234")],
    "upper_commit": [_entry(commit="A" * 40)],
    "duplicate_name": [_entry(0), _entry(1, name="web0")],
    "duplicate_repo_id": [_entry(0), _entry(1, repo_id=_entry(0)["repo_id"])],
    "bad_repo_id": [_entry(repo_id="repo_XYZ")],
    "url_not_repository": [_entry(repository_url="https://github.com/acme/other")],
    "url_repository": [_entry(repository="https://github.com/acme/web0")],
    "bad_source": [_entry(forge_credential_source="admin")],
    "size_bool": [_entry(size_kib=True)],
    "size_negative": [_entry(size_kib=-1)],
    "size_float": [_entry(size_kib=1.0)],
    "empty_ref": [_entry(ref="")],
    "option_ref": [_entry(ref="--upload-pack=x")],
    "missing_key": [{k: v for k, v in _entry().items() if k != "commit"}],
    "extra_key": [{**_entry(), "token": SECRETISH}],
    "entry_not_a_map": ["acme/web"],
    "not_a_list": {"web": _entry()},
    "a_string": "acme/web",
}


@pytest.mark.parametrize("value", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_list_raises_naming_the_key_never_the_value(value):
    with pytest.raises(ValueError) as caught:
        _task(additional_repositories=copy.deepcopy(value))
    message = str(caught.value)
    assert message.startswith("additional_repositories"), message
    assert SECRETISH not in message
    assert "acme/web" not in message and "--upload-pack" not in message


# ---------------------------------------------------------------------------
# Request B: spec format 4
# ---------------------------------------------------------------------------


def _doc(**over: object) -> dict:
    return _task(**over).to_firestore()


def test_format_4_is_known():
    assert specsign.SPEC_FORMAT == 4
    assert specsign.SPEC_FORMATS == (1, 2, 3, 4)


def test_a_task_without_the_list_keeps_its_old_format_and_bytes():
    """A worker built before format 4 must keep verifying every task that does
    not use the feature: the signer signs those at 1-3 exactly as before."""
    plain = _doc()
    assert specsign.signing_format(plain) == 1
    assert specsign.signing_format({**plain, "additional_repositories": []}) == 1
    forge = _doc(forge_credential="git", forge_access="read")
    assert specsign.signing_format(forge) == 3
    for doc, fmt in ((plain, 1), (forge, 3)):
        without = {k: v for k, v in doc.items() if k != "additional_repositories"}
        assert canonical_step_spec(doc, task_id=TASK_ID, spec_format=fmt) == canonical_step_spec(
            without, task_id=TASK_ID, spec_format=fmt
        )


def test_a_task_with_the_list_signs_at_format_4():
    doc = _doc(additional_repositories=[_entry()])
    assert specsign.signing_format(doc) == 4
    canonical = canonical_step_spec(doc, task_id=TASK_ID, spec_format=4)
    assert b'"format":4' in canonical
    assert b'"additional_repositories":[{' in canonical
    assert _entry()["commit"].encode() in canonical


@pytest.mark.parametrize("spec_format", [1, 2, 3])
def test_an_older_format_has_no_canonical_form_for_a_set_list(spec_format):
    """A tenant's agent can write its task documents. A list added to a task
    signed at 1-3 must fail verification, not sit outside the signed bytes."""
    doc = _doc(additional_repositories=[_entry()])
    with pytest.raises(SpecNotCanonical, match="additional_repositories"):
        canonical_step_spec(doc, task_id=TASK_ID, spec_format=spec_format)


def test_a_rewritten_list_fails_a_format_4_verification():
    entries = [_entry(0), _entry(1)]
    doc = _doc(additional_repositories=entries)
    signed = spec_digest(canonical_step_spec(doc, task_id=TASK_ID, spec_format=4))
    rewrites = {
        "commit": [_entry(0, commit="f" * 40), _entry(1)],
        "added": entries + [_entry(2)],
        "removed": entries[:1],
        "reordered": entries[::-1],
        "slot": [_entry(0, forge_credential="git"), _entry(1)],
        "emptied": [],
        "dropped": None,
    }
    for label, value in rewrites.items():
        rewritten = {**doc, "additional_repositories": value}
        digest = spec_digest(canonical_step_spec(rewritten, task_id=TASK_ID, spec_format=4))
        assert digest != signed, label


def test_a_format_4_signature_verifies_and_a_rewrite_does_not():
    """End to end with a real P-256 key, as swarm-api signs and the worker verifies."""
    pytest.importorskip("cryptography")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    key = ec.generate_private_key(ec.SECP256R1())
    prehashed = ec.ECDSA(utils.Prehashed(hashes.SHA256()))
    doc = _doc(additional_repositories=[_entry()])
    fmt = specsign.signing_format(doc)
    signature = key.sign(spec_digest(canonical_step_spec(doc, task_id=TASK_ID, spec_format=fmt)), prehashed)
    key.public_key().verify(
        signature, spec_digest(canonical_step_spec(doc, task_id=TASK_ID, spec_format=fmt)), prehashed
    )
    doc["additional_repositories"][0]["commit"] = "e" * 40
    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            signature, spec_digest(canonical_step_spec(doc, task_id=TASK_ID, spec_format=fmt)), prehashed
        )
