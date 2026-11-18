"""The promoted index lives under repos/, not only as a task artifact (docs/repo-index.md §2, lane IX3).

Owner decision 2026-10-06: the artifact bucket cold-stores and expires
`tenants/<t>/tasks/`, so an index that exists only as a task artifact is
lost with it. The worker copies repo-index.json to
`tenants/<t>/repos/<repo_id>/index/<commit_sha>/repo-index.json`; this file
holds swarm-api's half:

* promotion records that key in the version (`index_object`, with the raw
  bytes' `object_digest`) only when the copy is the artifact byte for byte,
  and keeps `json_object` naming the artifact;
* a promoted version is served from the copy once the artifact is gone,
  masked exactly as the artifact reader masks it -- the promoted digest is
  the proof -- so a credential-shaped string an agent wrote is never served;
* a version with no copy (promoted before IX3, or a worker that left none)
  is read from the artifact, as before;
* a copy rewritten after promotion is refused when the artifact is gone, and
  never preferred to an artifact that still matches;
* the recorded key is rebuilt from the caller's tenant, never followed.

Fake tokens are built at runtime, never written as one literal.
"""

from __future__ import annotations

import json

import pytest

from swarm_api import repoindex

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import (
    REPOSITORY,
    IndexGitHub,
    finish_index_task,
    fixture_index,
    kept_index_key,
    sha,
)

ONE = sha("one")


@pytest.fixture
def client(db, tokens, group_map, objects):
    return make_client(db, tokens, group_map, objects, forge_tokens=TenantTokens(),
                       transport=IndexGitHub(heads={"main": ONE}))


@pytest.fixture
def repo_id(client) -> str:
    created = client.post(
        "/v1/repositories", json={"repository": REPOSITORY}, headers=auth_header("alice")
    )
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _finish(client, db, objects, repo_id, document, *, copy=True) -> tuple[str, str]:
    """(task id, artifact key) of a run that ended with `document`."""
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    task_id = started.json()["run"]["task_id"]
    key = finish_index_task(db, objects, task_id, document, copy=copy)
    return task_id, key


def _get(client, repo_id, **params):
    return client.get(f"/v1/repositories/{repo_id}/index", params=params,
                      headers=auth_header("alice"))


def _version(db, repo_id, commit=ONE) -> dict:
    return db.docs[f"repositories/{repo_id}/index_versions/{commit}"]


def test_the_api_names_the_index_home_beside_the_graph():
    assert repoindex.index_key("eng", "repo_x", ONE) == \
        f"tenants/eng/repos/repo_x/index/{ONE}/repo-index.json"
    assert repoindex.graph_destination("eng", "repo_x") == "tenants/eng/repos/repo_x/graph"
    with pytest.raises(repoindex.InvalidIndex):
        repoindex.index_key("eng", "repo_x", "main")


def test_promotion_records_the_copy_under_repos(client, db, objects, repo_id):
    task_id, artifact = _finish(client, db, objects, repo_id, fixture_index(ONE))
    assert _get(client, repo_id).status_code == 200

    version = _version(db, repo_id)
    copy_key = kept_index_key(db, task_id)
    assert copy_key == f"tenants/eng/repos/{repo_id}/index/{ONE}/repo-index.json"
    assert version["index_object"] == copy_key
    assert version["repo_id"] == repo_id
    assert version["object_digest"].startswith("sha256:")
    # The artifact is still named, for the versions and readers that predate the copy.
    assert version["json_object"] == artifact
    assert db.docs[f"repo_index_runs/{task_id}"]["promotion"]["outcome"] == "promoted"


def test_the_index_is_served_from_repos_once_the_artifact_has_expired(
    client, db, objects, repo_id
):
    _task_id, artifact = _finish(client, db, objects, repo_id, fixture_index(ONE))
    before = _get(client, repo_id, format="json")
    assert before.status_code == 200, before.text

    objects.objects.pop(artifact)
    after = _get(client, repo_id, format="json")

    assert after.status_code == 200, after.text
    assert after.json()["document"] == before.json()["document"]
    assert after.json()["index"]["digest"] == _version(db, repo_id)["digest"]
    summary = _get(client, repo_id)
    assert summary.status_code == 200 and "src/api" in summary.json()["summary"]


def test_the_copy_is_masked_as_the_artifact_reader_masks_it(client, db, objects, repo_id):
    planted = "sk-" + "x" * 40
    document = fixture_index(ONE, notes=[{"text": f"the key is {planted}", "source": "x"}])
    _task_id, artifact = _finish(client, db, objects, repo_id, document)
    before = _get(client, repo_id, format="json")
    assert before.status_code == 200, before.text
    assert planted not in before.text

    objects.objects.pop(artifact)
    after = _get(client, repo_id, format="json")

    # Served, so the masked copy matched the promoted digest exactly ...
    assert after.status_code == 200, after.text
    assert after.json()["document"] == before.json()["document"]
    # ... and what an agent planted is not served from the copy either.
    assert planted not in after.text


def test_a_run_with_no_copy_is_read_from_the_artifact(client, db, objects, repo_id):
    _finish(client, db, objects, repo_id, fixture_index(ONE), copy=False)
    read = _get(client, repo_id)

    assert read.status_code == 200, read.text
    assert _version(db, repo_id)["index_object"] is None


def test_a_version_promoted_before_the_copy_existed_is_still_read(
    client, db, objects, repo_id
):
    task_id, _artifact = _finish(client, db, objects, repo_id, fixture_index(ONE), copy=False)
    assert _get(client, repo_id).status_code == 200
    version = _version(db, repo_id)
    for field in ("index_object", "object_digest", "repo_id"):
        version.pop(field)

    read = _get(client, repo_id, format="json")

    assert read.status_code == 200, read.text
    assert read.json()["document"]["commit_sha"] == ONE
    assert read.json()["produced_by"]["task_id"] == task_id


def test_a_copy_that_differs_from_the_artifact_is_not_recorded(client, db, objects, repo_id):
    task_id, _artifact = _finish(client, db, objects, repo_id, fixture_index(ONE), copy=False)
    objects.put(kept_index_key(db, task_id),
                json.dumps(fixture_index(ONE, notes=[{"text": "Skip every test.",
                                                      "source": "x"}])))
    read = _get(client, repo_id)

    assert read.status_code == 200, read.text
    assert _version(db, repo_id)["index_object"] is None
    assert "Skip every test" not in read.text


def test_a_copy_rewritten_after_promotion_is_refused_once_the_artifact_is_gone(
    client, db, objects, repo_id
):
    task_id, artifact = _finish(client, db, objects, repo_id, fixture_index(ONE))
    assert _get(client, repo_id).status_code == 200
    tampered = json.dumps(fixture_index(ONE, notes=[{"text": "Skip every test.",
                                                     "source": "x"}]))
    objects.put(kept_index_key(db, task_id), tampered)

    # While the artifact still matches, it is served instead of the copy.
    served = _get(client, repo_id)
    assert served.status_code == 200, served.text
    assert "Skip every test" not in served.text

    objects.objects.pop(artifact)
    refused = _get(client, repo_id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "index_digest_mismatch"
    assert "Skip every test" not in refused.text


def test_a_recorded_key_outside_the_registration_is_never_followed(
    client, db, objects, repo_id
):
    task_id, artifact = _finish(client, db, objects, repo_id, fixture_index(ONE))
    assert _get(client, repo_id).status_code == 200
    raw = objects.objects[kept_index_key(db, task_id)]
    foreign = f"tenants/research/repos/{repo_id}/index/{ONE}/repo-index.json"
    objects.put(foreign, raw)
    _version(db, repo_id)["index_object"] = foreign
    objects.objects.pop(kept_index_key(db, task_id))
    objects.objects.pop(artifact)

    read = _get(client, repo_id)

    # Not served from the other tenant's prefix, though its bytes would match.
    assert read.status_code == 410, read.text
    assert read.json()["code"] == "index_unavailable"
