"""Voice it storage (docs/design/voice-it.md §3.7, §3.11, §10 phase 2).

Another tenant's project, or another person's, reads 404; the blob path
builder refuses anything outside the tenant's voice prefix; deleting a project
removes its documents and its prefix and nothing else; a preference edit keeps
the previous value, and Undo puts it back. Firestore is the in-memory fake,
the bucket an `InMemoryVoiceBlobs`, swarm-voice the session tests' fake.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.errors import NotFound
from swarm_api.voice import preferences as vprefs
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_sessions import FakeVoice, _client, _end, _start


@pytest.fixture
def blobs() -> vstore.InMemoryVoiceBlobs:
    return vstore.InMemoryVoiceBlobs()


@pytest.fixture
def client(db, tokens, group_map, objects, blobs) -> TestClient:
    # alice is in eng, bob in research, both allowed; carol is not.
    c = _client(db, tokens, group_map, objects, FakeVoice(),
                voice_url="http://voice.internal:8080",
                voice_people=("alice@example.com", "bob@example.com"),
                child_key=secrets.token_hex(32))
    c.app.state.voice_blobs = blobs
    return c


def _project(client: TestClient, user: str) -> str:
    started = _start(client, user).json()
    assert _end(client, user, started["session_id"]).status_code == 200
    return started["project_id"]


def _store(db, blobs=None) -> vstore.VoiceStore:
    return vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc), blobs=blobs)


# ---- the tenant boundary ---------------------------------------------------


def test_another_tenants_project_reads_404_on_every_route(client):
    theirs = _project(client, "bob")  # research

    missing = "vp_" + secrets.token_hex(16)
    for method, body in (("get", None), ("patch", {"title": "mine now"}), ("delete", None)):
        response = client.request(method, f"/v1/voice/projects/{theirs}", json=body,
                                  headers=auth_header("alice"))
        absent = client.request(method, f"/v1/voice/projects/{missing}", json=body,
                                headers=auth_header("alice"))
        # The same refusal as a project that does not exist (invariant 9).
        assert response.status_code == absent.status_code == 404, (method, response.text)
        assert response.json()["code"] == absent.json()["code"]
        assert response.json()["message"].replace(theirs, missing) == absent.json()["message"]

    assert client.get(f"/v1/voice/projects/{theirs}",
                      headers=auth_header("bob")).status_code == 200


def test_another_tenants_project_is_not_listed(client):
    mine = _project(client, "alice")
    _project(client, "bob")

    listed = client.get("/v1/voice/projects", headers=auth_header("alice"))

    assert listed.status_code == 200, listed.text
    assert [p["project_id"] for p in listed.json()["projects"]] == [mine]


def test_another_persons_project_in_the_same_tenant_reads_404(db, blobs):
    store = _store(db, blobs)
    db.collection(vs.PROJECTS).document("vp_" + "a" * 32).set(
        {"tenant_id": "eng", "person": "dana@example.com", "title": "Hers"})

    with pytest.raises(NotFound):
        store.get_project(tenant_id="eng", person="alice@example.com",
                          project_id="vp_" + "a" * 32)


def test_projects_list_newest_first(db):
    store = _store(db)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for n in range(3):
        db.collection(vs.PROJECTS).document(f"vp_{n:032d}").set(
            {"tenant_id": "eng", "person": "alice@example.com", "title": f"p{n}",
             "updated_at": base + timedelta(hours=n)})

    rows = store.list_projects(tenant_id="eng", person="alice@example.com")

    assert [r["title"] for r in rows] == ["p2", "p1", "p0"]
    assert rows[0]["tag"] == f"voice:vp_{2:032d}"


def test_the_list_and_preferences_are_403_for_anyone_not_on_the_list(client):
    assert client.get("/v1/voice/projects", headers=auth_header("carol")).status_code == 403
    assert client.get("/v1/voice/preferences", headers=auth_header("carol")).status_code == 403


def test_rename_changes_the_title_and_audits(client, db):
    mine = _project(client, "alice")

    response = client.patch(f"/v1/voice/projects/{mine}", json={"title": "  Billing rewrite "},
                            headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Billing rewrite"
    assert db.docs[f"{vs.PROJECTS}/{mine}"]["title"] == "Billing rewrite"
    assert any(row.get("action") == vstore.AUDIT_RENAMED for path, row in db.docs.items()
               if path.startswith(vs.AUDIT + "/"))


def test_rename_refuses_a_blank_title(client):
    mine = _project(client, "alice")

    response = client.patch(f"/v1/voice/projects/{mine}", json={"title": "   "},
                            headers=auth_header("alice"))

    assert response.status_code == 422


# ---- the blob prefix -------------------------------------------------------


@pytest.mark.parametrize("parts", [
    ("plans", ".."),
    ("..", "..", "research", "voice", "vp_x", "plans", "1.md"),
    ("plans", "a/../../b.md"),
    ("plans", "/etc/passwd"),
    ("plans", ""),
    ("plans", "."),
    ("plans", "a\\b.md"),
    ("plans", ".hidden"),
])
def test_the_path_builder_refuses_anything_outside_the_project(parts):
    with pytest.raises(vstore.VoiceBlobKeyRefused):
        vstore.blob_key("eng", "vp_" + "1" * 32, *parts)


@pytest.mark.parametrize("key", [
    "/tenants/eng/voice/vp_1/plans/1.md",
    "tenants/research/voice/vp_1/plans/1.md",
    "tenants/eng/voice/vp_1/../../../research/voice/vp_1/plans/1.md",
    "tenants/eng/voice/vp_1/plans/../1.md",
    "tenants/eng/voice/vp_1//1.md",
    "tenants/eng/voice/plans.md",
    "tenants/eng/voice/../tasks/t/result.json",
    "tenants/eng/tasks/t/attempts/a/artifacts/x.json",
    "tenants/engx/voice/vp_1/plans/1.md",
    "",
])
def test_key_refusal_refuses_another_tenant_and_traversal(key):
    assert vstore.key_refusal("eng", key) is not None


def test_a_tenant_id_that_is_not_one_segment_is_refused():
    for tenant in ("", "eng/../research", "../eng", "eng/x"):
        with pytest.raises(vstore.VoiceBlobKeyRefused):
            vstore.voice_prefix(tenant)


def test_the_path_builder_admits_the_designs_names():
    p = "vp_" + "1" * 32
    assert vstore.blob_key("eng", p, "plans", "3.md") == f"tenants/eng/voice/{p}/plans/3.md"
    assert vstore.transcript_key("eng", p, "vs_" + "2" * 32, 4) == \
        f"tenants/eng/voice/{p}/transcripts/vs_{'2' * 32}/000004.jsonl"
    assert vstore.key_refusal("eng", f"tenants/eng/voice/{p}/mockups/vc_1/2.html") is None


def test_the_blob_store_refuses_another_tenants_key_before_the_bucket(blobs):
    key = vstore.blob_key("research", "vp_" + "1" * 32, "plans", "1.md")
    with pytest.raises(vstore.VoiceBlobKeyRefused):
        blobs.create("eng", key, b"x", "text/markdown")
    with pytest.raises(vstore.VoiceBlobKeyRefused):
        blobs.delete_keys("eng", [key])
    with pytest.raises(vstore.VoiceBlobKeyRefused):
        blobs.list_keys("eng", "tenants/research/voice/")
    assert blobs.objects == {}


def test_a_blob_is_created_once_and_never_overwritten(db, blobs):
    store = _store(db, blobs)
    project = "vp_" + "3" * 32
    db.collection(vs.PROJECTS).document(project).set(
        {"tenant_id": "eng", "person": "alice@example.com"})

    key = store.create_blob(tenant_id="eng", person="alice@example.com", project_id=project,
                            parts=("plans", "1.md"), data=b"one", content_type="text/markdown")

    with pytest.raises(vstore.VoiceBlobFailed):
        store.create_blob(tenant_id="eng", person="alice@example.com", project_id=project,
                          parts=("plans", "1.md"), data=b"two", content_type="text/markdown")
    assert blobs.objects[key] == b"one"
    with pytest.raises(NotFound):
        store.create_blob(tenant_id="research", person="alice@example.com", project_id=project,
                          parts=("plans", "2.md"), data=b"x", content_type="text/markdown")


# ---- the documents ---------------------------------------------------------


def _seed(db, blobs, project: str, session: str, *, tenant="eng", person="alice@example.com"):
    store = _store(db, blobs)
    db.collection(vs.PROJECTS).document(project).set(
        {"tenant_id": tenant, "person": person, "title": "T"})
    db.collection(vs.PROJECTS).document(project).collection(vs.SESSIONS).document(session).set(
        {"tenant_id": tenant, "person": person, "state": vs.ENDED})
    kw = dict(tenant_id=tenant, person=person, project_id=project)
    store.put_card(**kw, card_id="vc_" + secrets.token_hex(4), kind="proposal", body={"a": 1})
    store.record_turn(**kw, turn_id="vt_" + secrets.token_hex(4), fields={"state": "done"})
    store.append_event(**kw, session_id=session, event={"type": "card"})
    store.append_event(**kw, session_id=session, event={"type": "caption"})
    store.record_start(**kw, session_id=session, kind="agent", target_id="t1", day="2026-11-02")
    store.create_blob(**kw, parts=("plans", "1.md"), data=b"plan", content_type="text/markdown")
    blobs.objects[vstore.transcript_key(tenant, project, session, 0)] = b"{}"
    return store


def test_events_take_the_next_sequence_from_the_session(db, blobs):
    project, session = "vp_" + "4" * 32, "vs_" + "4" * 32
    store = _seed(db, blobs, project, session)

    events = store.events_after(tenant_id="eng", person="alice@example.com",
                                project_id=project, session_id=session, after=1)

    assert [(e["seq"], e["type"]) for e in events] == [(2, "caption")]
    session_doc = db.docs[f"{vs.PROJECTS}/{project}/{vs.SESSIONS}/{session}"]
    assert session_doc["event_seq"] == 2


def test_a_cards_version_counts_up(db, blobs):
    project, session = "vp_" + "5" * 32, "vs_" + "5" * 32
    store = _seed(db, blobs, project, session)
    kw = dict(tenant_id="eng", person="alice@example.com", project_id=project)

    store.put_card(**kw, card_id="vc_same", kind="diagram", body={"v": 1})
    card = store.put_card(**kw, card_id="vc_same", kind="diagram", body={"v": 2})

    assert card["version"] == 2 and card["body"] == {"v": 2}


def test_a_retried_turn_keeps_the_actions_it_recorded(db, blobs):
    project, session = "vp_" + "6" * 32, "vs_" + "6" * 32
    store = _seed(db, blobs, project, session)
    kw = dict(tenant_id="eng", person="alice@example.com", project_id=project)

    store.record_turn(**kw, turn_id="vt_retry", fields={"actions": {"k1": {"state": "done"}}})
    store.record_turn(**kw, turn_id="vt_retry", fields={"actions": {"k2": {"state": "started"}}})

    turn = store.get_turn(**kw, turn_id="vt_retry")
    assert set(turn["actions"]) == {"k1", "k2"}


def test_deleting_a_project_removes_its_documents_and_its_prefix(client, db, blobs):
    project, session = "vp_" + "7" * 32, "vs_" + "7" * 32
    other, other_session = "vp_" + "8" * 32, "vs_" + "8" * 32
    _seed(db, blobs, project, session)
    _seed(db, blobs, other, other_session)
    _seed(db, blobs, "vp_" + "9" * 32, "vs_" + "9" * 32, tenant="research",
          person="bob@example.com")
    before = dict(blobs.objects)

    response = client.delete(f"/v1/voice/projects/{project}", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True and body["objects"] == 2
    assert not [p for p in db.docs if p.startswith(f"{vs.PROJECTS}/{project}")]
    assert not [d for p, d in db.docs.items()
                if p.startswith(vstore.STARTS + "/") and d.get("project_id") == project]
    prefix = vstore.project_prefix("eng", project)
    assert not [k for k in blobs.objects if k.startswith(prefix)]
    # Nothing of another project or another tenant went with it.
    assert {k: v for k, v in before.items() if not k.startswith(prefix)} == blobs.objects
    assert f"{vs.PROJECTS}/{other}" in db.docs
    assert [d for p, d in db.docs.items()
            if p.startswith(vstore.STARTS + "/") and d.get("project_id") == other]
    assert client.get(f"/v1/voice/projects/{project}",
                      headers=auth_header("alice")).status_code == 404


def test_a_failed_bucket_delete_keeps_the_documents_for_a_retry(db, blobs):
    project, session = "vp_" + "a" * 32, "vs_" + "a" * 32
    store = _seed(db, blobs, project, session)
    blobs.fail = True

    with pytest.raises(vstore.VoiceBlobFailed):
        store.delete_project(tenant_id="eng", person="alice@example.com", project_id=project)

    assert f"{vs.PROJECTS}/{project}" in db.docs


def test_deleting_a_project_with_a_live_session_is_409(client, db, blobs):
    started = _start(client, "alice").json()

    response = client.delete(f"/v1/voice/projects/{started['project_id']}",
                             headers=auth_header("alice"))

    assert response.status_code == 409, response.text
    assert f"{vs.PROJECTS}/{started['project_id']}" in db.docs


# ---- preferences -----------------------------------------------------------


def test_preferences_read_as_the_defaults_before_any_edit(client):
    response = client.get("/v1/voice/preferences", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json() == {"preferences": vprefs.DEFAULTS, "revision": 0,
                               "can_undo": False, "previous": None}


def test_a_preference_edit_keeps_the_previous_value_and_undo_restores_it(client, db):
    first = client.patch("/v1/voice/preferences", json={"changes": {"stacks": ["Postgres"]}},
                         headers=auth_header("alice"))
    assert first.status_code == 200, first.text

    second = client.patch("/v1/voice/preferences",
                          json={"changes": {"stacks": ["SQLite"], "options": 2}, "revision": 1},
                          headers=auth_header("alice"))

    body = second.json()
    assert body["preferences"]["stacks"] == ["SQLite"] and body["preferences"]["options"] == 2
    assert body["previous"]["stacks"] == ["Postgres"] and body["previous"]["options"] == 3
    assert body["can_undo"] is True and body["revision"] == 2
    assert body["changed"] == ["options", "stacks"]

    undone = client.post("/v1/voice/preferences/undo", json={"revision": 2},
                         headers=auth_header("alice"))

    assert undone.status_code == 200, undone.text
    assert undone.json()["preferences"]["stacks"] == ["Postgres"]
    assert undone.json()["preferences"]["options"] == 3
    # The document holds no email: its id is an HMAC of it.
    (path,) = [p for p in db.docs if p.startswith(vprefs.COLLECTION + "/")]
    assert "alice" not in path and "alice" not in str(db.docs[path])
    assert path.split("/")[1].startswith(vprefs.ID_PREFIX)


def test_preferences_are_each_persons_own(client):
    client.patch("/v1/voice/preferences", json={"changes": {"voice": "cedar"}},
                 headers=auth_header("alice"))

    assert client.get("/v1/voice/preferences",
                      headers=auth_header("bob")).json()["preferences"]["voice"] == "marin"


def test_a_stale_revision_or_nothing_to_undo_is_409(client):
    assert client.post("/v1/voice/preferences/undo", json={},
                       headers=auth_header("alice")).status_code == 409
    client.patch("/v1/voice/preferences", json={"changes": {"brevity": "terse"}},
                 headers=auth_header("alice"))

    stale = client.patch("/v1/voice/preferences",
                         json={"changes": {"brevity": "detailed"}, "revision": 0},
                         headers=auth_header("alice"))

    assert stale.status_code == 409
    assert client.get("/v1/voice/preferences",
                      headers=auth_header("alice")).json()["preferences"]["brevity"] == "terse"


@pytest.mark.parametrize("changes", [
    {"voice": "robot"},
    {"options": 9},
    {"grace_seconds": -1},
    {"stacks": "Postgres"},
    {"reservations": {"huge": 10}},
    {"secret": "x"},
    {},
])
def test_a_bad_preference_is_422_and_nothing_is_written(client, db, changes):
    response = client.patch("/v1/voice/preferences", json={"changes": changes},
                            headers=auth_header("alice"))

    assert response.status_code == 422, response.text
    assert not [p for p in db.docs if p.startswith(vprefs.COLLECTION + "/")]


def test_preferences_without_a_settings_key_are_503(db, tokens, group_map, objects):
    c = _client(db, tokens, group_map, objects, FakeVoice(),
                voice_people=("alice@example.com",))

    response = c.get("/v1/voice/preferences", headers=auth_header("alice"))

    assert response.status_code == 503
    assert not [p for p in db.docs if p.startswith(vprefs.COLLECTION + "/")]
