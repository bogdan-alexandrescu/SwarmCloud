"""How many index versions a repository keeps, and who says which (lane INDEX-RETENTION, part of #1693).

Measured 2026-10-20 on ai-studio (repo_d754779d0cf31b44): retention never
ran. The worker read the registration's `index_versions` from Firestore
itself (`agent_worker.indexrun.kept_commits`), a tenant not switched to the
state routes got PermissionDenied on every run, and the sweep was then given
no keep set and retired nothing: 163 manifests and about 880 MB were held
where swarm-api keeps 20 versions. What is held here:

* `index.keep_versions` is a repository setting: 1-100, default 20 (today's
  `KEPT_VERSIONS`), refused outside the range or as a string, written through
  `PATCH /v1/repositories/{repo_id}` like `full_every_days`, and served;
* `_prune_versions` keeps the repository's value, not the constant, and the
  promoted version on top;
* swarm-api, not the worker, decides the kept commits -- the newest
  `keep_versions` versions, the promoted `current_sha` and the run's own
  commit -- and names them in the SIGNED prompt (`KEEP_LINE`), the way it
  names an incremental run's base (`BASE_LINE`);
* the worker's sweep report reaches the run and the registration, so the
  repository page says what was deleted and kept, or why nothing was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from swarm_api import repoindex, repositories

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE, TWO = sha("one"), sha("two")
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(
        db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=github
    )


@pytest.fixture
def repo_id(client) -> str:
    created = client.post(
        "/v1/repositories", json={"repository": REPOSITORY}, headers=auth_header("alice")
    )
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _patch(client, repo_id, index):
    return client.patch(f"/v1/repositories/{repo_id}", json={"index": index},
                        headers=auth_header("alice"))


def _index_now(client, repo_id):
    started = client.post(f"/v1/repositories/{repo_id}/index", json={"kind": "full"},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    return started.json()["run"]["task_id"]


def _seed_versions(db, repo_id, count):
    shas = [sha(f"v{n}") for n in range(count)]
    for n, commit in enumerate(shas):
        db.docs[f"repositories/{repo_id}/index_versions/{commit}"] = {
            "commit_sha": commit, "task_id": f"task_{n}", "recorded_at": T0 + timedelta(hours=n),
        }
    return shas  # oldest first


def _versions(db, repo_id):
    prefix = f"repositories/{repo_id}/index_versions/"
    return {path[len(prefix):] for path in db.docs if path.startswith(prefix)}


def _service(db):
    service = repoindex.RepoIndex.__new__(repoindex.RepoIndex)
    service._db = db
    return service


# --------------------------------------------------------------------------
# the setting
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, 101, "20", -1, 2.5])
def test_keep_versions_outside_one_to_a_hundred_or_not_an_integer_is_refused(bad):
    # MUTATION: drop the Field(ge=, le=) or the StrictInt on keep_versions and this passes nothing.
    with pytest.raises(ValidationError):
        repositories.IndexSettings(keep_versions=bad)


def test_keep_versions_range_and_default_are_today_s_twenty():
    # MUTATION: a default other than 20 changes retention for every repository that never set it.
    assert repositories.KEEP_VERSIONS_DEFAULT == 20 == repoindex.KEPT_VERSIONS
    assert repositories.KEEP_VERSIONS_RANGE == (1, 100)
    assert repositories.IndexSettings(keep_versions=1).changes() == {"keep_versions": 1}
    assert repositories.IndexSettings(keep_versions=100).changes() == {"keep_versions": 100}


def test_keep_versions_of_reads_the_setting_or_the_default():
    # MUTATION: return the constant instead of the stored value.
    assert repositories.keep_versions_of({"keep_versions": 5}) == 5
    assert repositories.keep_versions_of({}) == 20
    assert repositories.keep_versions_of({"keep_versions": "5"}) == 20
    assert repositories.keep_versions_of({"keep_versions": 0}) == 20


def test_a_new_registration_serves_the_default(client, repo_id):
    # MUTATION: leave keep_versions out of `to_api`'s index and the console reads "not served".
    body = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice")).json()
    assert body["repository"]["index"]["keep_versions"] == 20


def test_keep_versions_is_stored_and_served_through_the_patch(client, db, repo_id):
    # MUTATION: drop keep_versions from IndexSettings and the PATCH is refused as an extra key.
    patched = _patch(client, repo_id, {"keep_versions": 5})
    assert patched.status_code == 200, patched.text
    assert patched.json()["repository"]["index"]["keep_versions"] == 5
    assert db.docs[f"repositories/{repo_id}"]["index"]["keep_versions"] == 5
    # The rest of the schedule is left as it was: the PATCH merges `index` field by field.
    assert db.docs[f"repositories/{repo_id}"]["index"]["full_every_days"] == 7


@pytest.mark.parametrize("bad", [0, 101, "20"])
def test_a_patch_outside_the_range_is_refused_and_stores_nothing(client, db, repo_id, bad):
    # MUTATION: validate after storing, and the bad value reaches the document.
    refused = _patch(client, repo_id, {"keep_versions": bad})
    assert refused.status_code == 422, refused.text
    assert "keep_versions" not in db.docs[f"repositories/{repo_id}"]["index"] or \
        db.docs[f"repositories/{repo_id}"]["index"]["keep_versions"] == 20


# --------------------------------------------------------------------------
# the prune
# --------------------------------------------------------------------------

def test_the_prune_keeps_the_repositorys_value_and_the_promoted_one(db, repo_id):
    shas = _seed_versions(db, repo_id, 12)
    promoted = shas[0]  # the oldest, as a superseded-then-promoted sha could be
    # MUTATION: prune by KEPT_VERSIONS instead of keep_versions, and all 12 stay.
    _service(db)._prune_versions(repo_id, keep=promoted, keep_versions=5)
    assert _versions(db, repo_id) == set(shas[-5:]) | {promoted}


def test_the_prune_default_is_still_twenty(db, repo_id):
    shas = _seed_versions(db, repo_id, 25)
    # MUTATION: a default other than KEPT_VERSIONS.
    _service(db)._prune_versions(repo_id, keep=None)
    assert _versions(db, repo_id) == set(shas[-20:])


def test_raising_keep_versions_resurrects_nothing(db, repo_id):
    shas = _seed_versions(db, repo_id, 8)
    _service(db)._prune_versions(repo_id, keep=None, keep_versions=3)
    _service(db)._prune_versions(repo_id, keep=None, keep_versions=50)
    # MUTATION: nothing here can bring a deleted version back; a prune that wrote would.
    assert _versions(db, repo_id) == set(shas[-3:])


def test_promotion_prunes_with_the_registrations_value(client, db, objects, repo_id,
                                                       monkeypatch):
    seen = []
    real = repoindex.RepoIndex._prune_versions

    def spy(self, repo, keep, keep_versions=repoindex.KEPT_VERSIONS):
        seen.append(keep_versions)
        return real(self, repo, keep, keep_versions)

    monkeypatch.setattr(repoindex.RepoIndex, "_prune_versions", spy)
    assert _patch(client, repo_id, {"keep_versions": 3}).status_code == 200
    task_id = _index_now(client, repo_id)
    finish_index_task(db, objects, task_id, fixture_index(ONE))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    # MUTATION: `_promote_from` calling `_prune_versions(repo_id, keep=...)` without the setting.
    assert seen == [3]


# --------------------------------------------------------------------------
# the kept commits, named by swarm-api in the signed prompt
# --------------------------------------------------------------------------

def test_kept_commits_are_the_newest_versions_the_promoted_one_and_the_runs_own():
    rows = [{"commit_sha": sha(f"v{n}"), "recorded_at": T0 + timedelta(hours=n)}
            for n in range(25)]
    current, own = sha("v0"), sha("own")
    kept = repoindex.kept_commits(rows, keep_versions=20, current_sha=current, own=own)
    # MUTATION: keep the OLDEST twenty, or forget the promoted or the run's own commit.
    assert kept == sorted({r["commit_sha"] for r in rows[5:]} | {current, own})


def test_a_version_that_names_no_commit_makes_no_keep_set():
    rows = [{"commit_sha": sha("a"), "recorded_at": T0}, {"recorded_at": T0}]
    # MUTATION: skipping the broken row would let the sweep delete the objects it stands for.
    assert repoindex.kept_commits(rows, keep_versions=20, current_sha=None, own=sha("x")) is None


def test_the_signed_prompt_names_the_kept_commits(client, db, objects, repo_id, github):
    first = _index_now(client, repo_id)
    # The very first run keeps only its own commit: there is nothing else yet.
    prompt = db.docs[f"tasks/{first}"]["input"]["prompt"]
    assert f"\n{repoindex.KEEP_LINE}{ONE}\n" in prompt
    finish_index_task(db, objects, first, fixture_index(ONE))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))

    github.heads["main"] = TWO
    second = _index_now(client, repo_id)
    task = db.docs[f"tasks/{second}"]
    lines = [line for line in task["input"]["prompt"].splitlines()
             if line.startswith(repoindex.KEEP_LINE)]
    # MUTATION: a keep line in `metadata` (unsigned) instead of the prompt (signed `input`).
    assert lines == [f"{repoindex.KEEP_LINE}{' '.join(sorted({ONE, TWO}))}"]
    assert repoindex.KEEP_LINE.strip() not in str(task.get("metadata"))


def test_the_kept_commits_follow_keep_versions(client, db, objects, repo_id, github):
    shas = _seed_versions(db, repo_id, 6)
    assert _patch(client, repo_id, {"keep_versions": 2}).status_code == 200
    task = db.docs[f"tasks/{_index_now(client, repo_id)}"]
    [line] = [line for line in task["input"]["prompt"].splitlines()
              if line.startswith(repoindex.KEEP_LINE)]
    # MUTATION: KEPT_VERSIONS in place of the repository's keep_versions.
    assert line.split(": ", 1)[1].split() == sorted({*shas[-2:], ONE})


def test_the_worker_and_the_api_name_the_same_keep_line():
    from agent_worker import indexrun

    # MUTATION: either side renaming its prefix alone.
    assert repoindex.KEEP_LINE == indexrun.KEEP_LINE


# --------------------------------------------------------------------------
# what the sweep did, on the run and on the registration
# --------------------------------------------------------------------------

def _sweep_summary(**record):
    return {"repo_index_phases": [
        {"phase": "graph_write", "status": "ok", "seconds": 90.0},
        {"phase": "index_sweep", **record},
    ]}


def test_retention_is_read_from_the_sweep_phase():
    summary = _sweep_summary(status="ok", seconds=12.5, retention={
        "kept_commits": 21, "deleted_copies": 140, "deleted_indexes": 140, "deleted_blobs": 900,
        "deleted_bytes": 700_000_000, "held_copies": 23, "held_bytes": 180_000_000,
        "truncated": True, "secret": "not a count",
    })
    got = repoindex.retention_of(summary)
    # MUTATION: serving the record as written, an unknown key and all.
    assert got == {"status": "ok", "reason": None, "kept_commits": 21, "deleted_copies": 140,
                   "deleted_indexes": 140, "deleted_blobs": 900,
                   "deleted_bytes": 700_000_000, "held_copies": 23,
                   "held_bytes": 180_000_000, "truncated": True}
    skipped = repoindex.retention_of(_sweep_summary(
        status="skipped", seconds=0.0, reason="the signed prompt names no kept commits"))
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "the signed prompt names no kept commits"
    assert repoindex.retention_of({"repo_index_phases": []}) is None
    assert repoindex.retention_of(None) is None


def test_the_run_and_the_registration_say_what_the_sweep_did(client, db, objects, repo_id):
    task_id = _index_now(client, repo_id)
    finish_index_task(db, objects, task_id, fixture_index(ONE))
    db.docs[f"tasks/{task_id}"]["result_summary"].update(_sweep_summary(
        status="ok", seconds=12.5,
        retention={"kept_commits": 1, "deleted_copies": 162, "deleted_bytes": 860_000_000,
                   "held_copies": 1, "held_bytes": 5_000_000}))

    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))

    runs = client.get(f"/v1/repositories/{repo_id}/index/runs",
                      headers=auth_header("alice")).json()["runs"]
    # MUTATION: `run_to_api` without `retention`, or `_settle_run` not recording it.
    assert runs[0]["retention"]["deleted_copies"] == 162
    assert runs[0]["retention"]["deleted_bytes"] == 860_000_000
    served = client.get(f"/v1/repositories/{repo_id}",
                        headers=auth_header("alice")).json()["repository"]["index"]
    assert served["retention"]["held_copies"] == 1
    assert served["retention"]["task_id"] == task_id
