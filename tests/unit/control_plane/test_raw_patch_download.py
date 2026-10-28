"""The owner-only raw patch: `GET /v1/tasks/{id}/artifacts/raw?redaction=none` (#1857).

BGD's decision, 2026-10-27: a served `.patch` is redacted at read time, so its
credential-SHAPED test fixtures come back masked and the patch no longer
applies. The masking rules stay as they are; the person who SUBMITTED the task
may download the patch as stored. What is pinned here:

  * the submitter gets the stored bytes of a patch the default read masks;
  * nobody else does -- another member, a tenant admin, a platform admin --
    and a name that is not a top-level `.patch` is refused, each a 403 naming
    the rule, with no byte of the body;
  * another tenant's task is the same 404 a missing one is;
  * the answer is `text/x-diff`, always an attachment, `no-store`, and says
    it is not redacted;
  * every such read writes one audit record, which holds no content, and a
    read that cannot be recorded is not served.

Every fixture value is built at runtime: nothing here may look like a
credential to the publish scan. Offline: FakeFirestore and the in-memory
object reader.
"""

from __future__ import annotations

import secrets

import pytest

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, auth_header
from .test_artifact_outputs import a_finished_task
from .test_roles_store import grant

SUBMITTER = "alice@example.com"

#: Where `agent_output._audit_raw_patch` writes; spelled here so this file
#: fails test by test, not at import, on a tree without the route.
RAW_PATCH_AUDIT = "artifact_audit"


def _fixture_values() -> dict[str, str]:
    """Credential-shaped test fixtures, each assembled here, never one literal."""
    return {
        "hash": "pbkdf2_sha256$" + secrets.token_hex(24),
        "token": "tok_" + secrets.token_hex(20),
        "key": "fake" + secrets.token_hex(12),
    }


def _patch(values: dict[str, str]) -> str:
    """A diff whose added lines are the fixture shapes #1857 saw masked."""
    pw = "password" + "_hash"
    tok = "to" + "ken"
    key = "api" + "_key"
    return (
        "diff --git a/tests/test_login.py b/tests/test_login.py\n"
        "--- a/tests/test_login.py\n"
        "+++ b/tests/test_login.py\n"
        "@@ -1,2 +1,5 @@\n"
        " import pytest\n"
        f'+USER = dict({pw}="{values["hash"]}")\n'
        f'+BODY = {{"{tok}": "{values["token"]}"}}\n'
        f"+client = Client({key}={values['key']})\n"
        " # end\n"
    )


@pytest.fixture
def group_map():
    """`alice` submits; `dave` is a member of eng; `erin` is eng's tenant admin
    (a signed grant below); `root` is a platform admin who is in eng; `bob` is
    another tenant."""
    return {
        SUBMITTER: (ENG_GROUP,),
        "dave@example.com": (ENG_GROUP,),
        "erin@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "carol@example.com": (),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def api_context(db, tokens, group_map, objects):
    context = build_context(
        settings=api_settings(settings_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    grant(context.roles, "erin@example.com", "admin")
    return context


@pytest.fixture
def values():
    return _fixture_values()


@pytest.fixture
def patch_task(db, objects, values):
    body = _patch(values)
    a_finished_task(
        db, objects, files={"fix-1.patch": body, "notes.md": body, "deep/fix-2.patch": body}
    )
    db.docs["tasks/task_a"]["submitted_by"] = SUBMITTER
    return body


def raw(client, name="fix-1.patch", user="alice", task="task_a", **params):
    return client.get(
        f"/v1/tasks/{task}/artifacts/raw",
        params={"name": name, **params},
        headers=auth_header(user),
    )


def audits(db) -> list[dict]:
    return list(db.dump(f"{RAW_PATCH_AUDIT}/").values())


def test_the_default_read_masks_the_fixtures_so_the_patch_would_not_apply(
    client, patch_task, values
):
    response = raw(client)
    assert response.status_code == 200, response.text
    assert response.headers["x-swarm-redaction"] == "applied"
    assert response.text != patch_task
    assert values["token"] not in response.text


def test_the_submitter_gets_the_stored_bytes_as_an_unredacted_attachment(
    client, db, patch_task, values
):
    response = raw(client, redaction="none")

    assert response.status_code == 200, response.text
    assert response.content == patch_task.encode()
    for value in values.values():
        assert value in response.text
    assert response.headers["content-type"] == "text/x-diff; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="fix-1.patch"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-swarm-redaction"] == "not-applied"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_inline_disposition_is_still_an_attachment(client, patch_task):
    response = raw(client, redaction="none", disposition="inline")

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment;")


def test_the_explicit_default_is_todays_answer(client, patch_task):
    default = raw(client)
    explicit = raw(client, redaction="applied")

    assert explicit.status_code == 200, explicit.text
    assert explicit.content == default.content
    assert explicit.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    "user",
    [
        pytest.param("dave", id="another-member"),
        pytest.param("erin", id="tenant-admin"),
        pytest.param("root", id="platform-admin"),
    ],
)
def test_nobody_but_the_submitter_gets_the_raw_patch(client, db, patch_task, values, user):
    # The redacted read is theirs, as today: only the unredacted one is not.
    assert raw(client, user=user).status_code == 200

    response = raw(client, user=user, redaction="none")

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "forbidden"
    assert body["detail"]["rule"] == "submitter_only"
    for value in values.values():
        assert value not in response.text
    assert audits(db) == []


def test_the_tenant_admin_really_is_one(api_context):
    """The control for the tenant-admin case: the grant above took."""
    newest = api_context.roles.newest_valid("eng", "erin@example.com", fresh=True)
    assert newest is not None and newest.role == "admin"


@pytest.mark.parametrize(
    "name", ["notes.md", "deep/fix-2.patch"], ids=["not-a-patch", "a-path"]
)
def test_only_a_top_level_patch_is_served_unredacted(client, db, patch_task, values, name):
    assert raw(client, name=name).status_code == 200

    response = raw(client, name=name, redaction="none")

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["rule"] == "patch_only"
    for value in values.values():
        assert value not in response.text
    assert audits(db) == []


def test_another_tenants_task_is_the_same_404_as_today(client, db, patch_task, values):
    today = raw(client, user="bob")
    response = raw(client, user="bob", redaction="none")

    assert today.status_code == 404
    assert response.status_code == 404, response.text
    assert response.json()["code"] == today.json()["code"]
    assert response.json()["message"] == today.json()["message"]
    assert values["token"] not in response.text
    assert audits(db) == []


def test_a_redaction_that_is_neither_applied_nor_none_is_a_422(client, patch_task):
    response = raw(client, redaction="off")

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_every_raw_read_is_audited_and_the_record_holds_no_content(
    client, db, patch_task, values
):
    assert raw(client, redaction="none").status_code == 200
    assert raw(client).status_code == 200  # a redacted read is not this record's
    assert raw(client, redaction="none").status_code == 200

    rows = audits(db)
    assert len(rows) == 2
    row = rows[0]
    assert row["action"] == "artifact_raw_unredacted"
    assert row["by"] == SUBMITTER
    assert row["task_id"] == "task_a"
    assert row["tenant_id"] == "eng"
    assert row["name"] == "fix-1.patch"
    assert row["attempt_id"] == "att_1"
    assert row["at"] is not None
    flat = repr(rows)
    for value in values.values():
        assert value not in flat
    assert "diff --git" not in flat


def test_a_read_that_cannot_be_audited_is_not_served(client, db, patch_task, values, monkeypatch):
    real = db.collection

    def refusing(path, *args, **kwargs):
        if path == RAW_PATCH_AUDIT:
            raise RuntimeError("audit store down")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(db, "collection", refusing)

    response = raw(client, redaction="none")

    assert response.status_code == 503, response.text
    for value in values.values():
        assert value not in response.text


def test_no_log_line_carries_the_body(client, patch_task, values, caplog):
    with caplog.at_level("DEBUG"):
        assert raw(client, redaction="none").status_code == 200

    assert "unredacted patch read" in caplog.text
    for value in values.values():
        assert value not in caplog.text


def test_a_listed_patch_whose_object_is_gone_is_a_410_and_not_audited(
    client, db, objects, values
):
    a_finished_task(db, objects, files={"fix-1.patch": _patch(values)}, store_objects=False)
    db.docs["tasks/task_a"]["submitted_by"] = SUBMITTER

    response = raw(client, redaction="none")

    assert response.status_code == 410, response.text
    assert audits(db) == []
