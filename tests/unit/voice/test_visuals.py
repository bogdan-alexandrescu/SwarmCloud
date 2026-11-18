"""Visuals and notes are stored and served (docs/design/voice-it-own-speech.md §8;
build plan §14, L9; swarm_api/voice/visuals.py, swarm_api/routes/voice_visuals.py).

swarm-api is built as test_voice_grant builds it: the in-memory Firestore, a
fake swarm-voice, swarm-voice's identity as a static token. The visuals router
is mounted beside the app's own, as routes/voice.py will mount it (L6), the
bucket is `InMemoryVoiceBlobs` and the GitHub read is a fake reader that
records what it was asked. This holds that:

* a drawn visual's body lives under the tenant's own prefix,
  `tenants/<t>/voice/<p>/visuals/<v>/<version>.txt`, never rewritten, and is
  served back as text; another person's project is the same 404 as a missing one;
* a `code` visual is read by swarm-api, only through the person's repository
  grant in the session's tenant (no grant, or another tenant's repository:
  403 and GitHub is never asked), and is redacted before it is stored;
* anchors are checked against the visual: points and regions inside the frame,
  parts it has, lines inside its lines;
* a spoken note takes the page's fresh pointer, else a named part, else the
  whole visual;
* notes become a follow-up, an issue or a lane brief, each note then `work`;
* a grant reaches only the two writes it is listed for, and only its own
  session's; the pointer, the note edit and the notes-to-work route are the page's;
* `speak`'s `point_at` is a `focus` event on the session's stream;
* a mock-up is a visual of self-contained HTML frames, each frame a part: a frame
  that runs a script or fetches anything is refused, a credential in one is
  masked; an approval (by voice on the visual write, on the Stage on the
  approval route) is stored on the visual and served, names one of its frames,
  and another workspace's visual is the same 404 as a missing one.

No network, no cloud credential; every token and grant is built at runtime.
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.conftest import auth_header, db, group_map, objects, tokens  # noqa: E402,F401
from control_plane.test_voice_grant import _build, _grant_headers, _start  # noqa: E402
from control_plane.test_voice_sessions import FakeVoice  # noqa: E402

from swarm_api.access import GRANTS, grant_id_for  # noqa: E402
from swarm_api.errors import NotFound, ValidationFailed  # noqa: E402
from swarm_api.repositories import repo_id_for  # noqa: E402
from swarm_api.routes import voice_visuals as rvv  # noqa: E402
from swarm_api.voice import grant as vg  # noqa: E402
from swarm_api.voice import readback as rb  # noqa: E402
from swarm_api.voice import store as vstore  # noqa: E402
from swarm_api.voice import visuals as vvis  # noqa: E402
from swarm_common.states import EventType  # noqa: E402

ALICE = "alice@example.com"
SPEC = "# Overview\nWhat it is.\n\n## The API\nRoutes.\n\n```\n# not a heading\n```\n## The API\n"


class FakeReader:
    """The GitHub read, recording each call; answers a file of numbered lines."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.file = "\n".join(f"line {i}" for i in range(1, 51))

    def read_file(self, tenant_id, owner, repo, ref, path):
        self.calls.append(("file", tenant_id, owner, repo, ref, path))
        return self.file

    def read_diff(self, tenant_id, owner, repo, pull):
        self.calls.append(("diff", tenant_id, owner, repo, pull))
        return "diff --git a/x b/x\n+added\n-removed\n"


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


@pytest.fixture
def blobs() -> vstore.InMemoryVoiceBlobs:
    return vstore.InMemoryVoiceBlobs()


@pytest.fixture
def client(db, tokens, group_map, objects, voice, reader, blobs) -> TestClient:  # noqa: F811
    built = _build(db, tokens, group_map, objects, voice)
    built.app.state.voice_blobs = blobs
    built.app.state.voice_repo_reader = reader
    return built


def _tenant(db, call) -> str:  # noqa: F811
    return db.docs[f"voice_projects/{call.project_id}/sessions/{call.session_id}"]["tenant_id"]


def _draw(client, call, *, headers=None, **visual) -> Any:
    body = {"kind": "spec", "title": "The spec", "body": SPEC, "parts": None, "source": None,
            **visual}
    return client.post(f"/v1/voice/sessions/{call.session_id}/visuals", json={"visual": body},
                       headers=headers or _grant_headers(call))


def _events(db, call, kind: str) -> list[dict[str, Any]]:  # noqa: F811
    prefix = f"voice_projects/{call.project_id}/sessions/{call.session_id}/events/"
    return [row for path, row in sorted(db.docs.items())
            if path.startswith(prefix) and row.get("type") == kind]


def _grant_repo(db, tenant: str, repo: str = "acme/widgets", *, person: str = ALICE) -> str:  # noqa: F811
    owner, name = repo.split("/")
    repo_id = repo_id_for(tenant, owner, name)
    db.collection("repositories").document(repo_id).set(
        {"tenant_id": tenant, "owner": owner, "repo": name})
    db.collection(GRANTS).document(grant_id_for(tenant, person, repo_id)).set(
        {"tenant_id": tenant, "user": person, "repo_id": repo_id, "mode": "read"})
    return repo_id


# ---- the routes a grant reaches ---------------------------------------------


def test_the_grant_reaches_the_two_visual_writes_and_both_act_as_heard():
    routes = {(m, p) for m, p in vg.WRITE_ROUTES if "/visuals" in p}
    assert routes == {("POST", "/v1/voice/sessions/{session_id}/visuals"),
                      ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes")}
    assert all(rb.classify(m, p) == rb.AS_HEARD for m, p in routes)


def test_every_route_of_the_router_is_a_voice_route_the_design_names():
    table = {(m, r.path) for r in rvv.router.routes for m in r.methods}
    assert table == {
        ("GET", "/v1/voice/projects/{project_id}/visuals"),
        ("GET", "/v1/voice/projects/{project_id}/visuals/{visual_id}"),
        ("POST", "/v1/voice/sessions/{session_id}/visuals"),
        ("POST", "/v1/voice/sessions/{session_id}/pointer"),
        ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"),
        ("PATCH", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes/{note_id}"),
        ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes/work"),
        ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/approval"),
    }


# ---- a drawn visual: tenant prefix, versions, served as text -----------------


def test_a_drawn_spec_lives_under_the_tenants_prefix_and_is_served_as_text(client, db, voice, blobs):
    call = _start(client, voice)
    tenant = _tenant(db, call)
    first = _draw(client, call)
    assert first.status_code == 200, first.text
    visual = first.json()["visual"]
    vid = visual["visual_id"]
    assert visual["version"] == 1 and visual["created_by"] == "planner"
    assert visual["body_ref"] == f"tenants/{tenant}/voice/{call.project_id}/visuals/{vid}/000001.txt"
    assert blobs.objects[visual["body_ref"]] == SPEC.encode()
    assert blobs.content_types[visual["body_ref"]].startswith("text/plain")
    # Headings are the parts; a fenced line is not a heading; a repeat is numbered.
    assert visual["parts"] == [{"id": "overview", "label": "Overview"},
                               {"id": "the-api", "label": "The API"},
                               {"id": "the-api-2", "label": "The API"}]
    assert "tenant_id" not in visual and "person" not in visual
    # A new version is a new object; the first is never rewritten.
    second = _draw(client, call, visual_id=vid, body=SPEC + "More.\n")
    assert second.json()["visual"]["version"] == 2
    assert blobs.objects[visual["body_ref"]] == SPEC.encode()
    assert [e["visual"]["version"] for e in _events(db, call, vvis.VISUAL_UPSERT)] == [1, 2]
    served = client.get(f"/v1/voice/projects/{call.project_id}/visuals/{vid}",
                        headers=auth_header("alice"))
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("application/json")
    assert served.json()["body"] == SPEC + "More.\n"
    listed = client.get(f"/v1/voice/projects/{call.project_id}/visuals",
                        headers=auth_header("alice")).json()["visuals"]
    assert [v["visual_id"] for v in listed] == [vid]


def test_another_persons_project_is_the_same_404_as_a_missing_one(client, db, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    theirs = client.get(f"/v1/voice/projects/{call.project_id}/visuals/{vid}",
                        headers=auth_header("root"))
    missing = client.get(f"/v1/voice/projects/vp_{secrets.token_hex(16)}/visuals/{vid}",
                         headers=auth_header("alice"))
    assert theirs.status_code == missing.status_code == 404


def test_a_body_key_outside_the_tenants_prefix_is_refused():
    key = vstore.blob_key("eng", "vp_" + "a" * 32, vvis.VISUALS, "vv_" + "b" * 32, "000001.txt")
    assert key.startswith("tenants/eng/voice/vp_")
    assert vstore.key_refusal("research", key) is not None
    with pytest.raises(vstore.VoiceBlobKeyRefused):
        vstore.blob_key("eng", "vp_" + "a" * 32, vvis.VISUALS, "..", "x.txt")


def test_a_drawn_kind_takes_no_source_and_a_read_kind_takes_no_body(client, voice):
    call = _start(client, voice)
    with_source = _draw(client, call, source={"task_id": "t1", "step": None})
    assert with_source.status_code == 422
    log_with_body = _draw(client, call, kind="log", body="pasted", source={"task_id": "t1",
                                                                          "step": None})
    assert log_with_body.status_code == 422


def test_a_grant_draws_only_on_its_own_session(client, voice):
    mine = _start(client, voice)
    other = SimpleNamespace(session_id="vs_" + secrets.token_hex(16))
    response = _draw(client, other, headers=_grant_headers(mine))
    assert response.status_code == 403


# ---- a code visual: the repository grant scope --------------------------------


def _show(client, call, source, *, headers=None):
    return client.post(f"/v1/voice/sessions/{call.session_id}/visuals", json={"visual": {
        "kind": "code", "title": "widgets", "body": None, "parts": None, "source": source}},
        headers=headers or _grant_headers(call))


FILE = {"repo": "acme/widgets", "ref": "main", "path": "src/app.py", "from": 10, "to": 12}


def test_a_file_is_read_through_the_persons_repository_grant(client, db, voice, reader):
    call = _start(client, voice)
    tenant = _tenant(db, call)
    refused = _show(client, call, FILE)
    assert refused.status_code == 403 and reader.calls == []
    _grant_repo(db, tenant)
    shown = _show(client, call, FILE)
    assert shown.status_code == 200, shown.text
    visual = shown.json()["visual"]
    assert reader.calls == [("file", tenant, "acme", "widgets", "main", "src/app.py")]
    assert visual["lines"] == {"from": 10, "to": 12}
    assert visual["source"]["path"] == "src/app.py"
    body = client.get(f"/v1/voice/projects/{call.project_id}/visuals/{visual['visual_id']}",
                      headers=auth_header("alice")).json()["body"]
    assert body == "line 10\nline 11\nline 12"


def test_another_tenants_repository_and_another_persons_grant_are_no_grant(client, db, voice,
                                                                          reader):
    call = _start(client, voice)
    tenant = _tenant(db, call)
    _grant_repo(db, "research")
    assert _show(client, call, FILE).status_code == 403
    repo_id = repo_id_for(tenant, "acme", "widgets")
    db.collection("repositories").document(repo_id).set({"tenant_id": tenant})
    db.collection(GRANTS).document(grant_id_for(tenant, ALICE, repo_id)).set(
        {"tenant_id": tenant, "user": "bob@example.com", "repo_id": repo_id, "mode": "read"})
    assert _show(client, call, FILE).status_code == 403
    assert reader.calls == []


def test_a_diff_is_read_through_the_same_grant(client, db, voice, reader):
    call = _start(client, voice)
    tenant = _tenant(db, call)
    _grant_repo(db, tenant)
    shown = _show(client, call, {"repo": "acme/widgets", "pull": 42})
    assert shown.status_code == 200, shown.text
    assert reader.calls == [("diff", tenant, "acme", "widgets", 42)]
    assert shown.json()["visual"]["lines"] == {"from": 1, "to": 3}


def test_a_credential_in_a_file_is_redacted_before_it_is_stored(client, db, voice, reader, blobs):
    call = _start(client, voice)
    _grant_repo(db, _tenant(db, call))
    leaked = "ghp_" + secrets.token_hex(18)
    reader.file = f"TOKEN = '{leaked}'\nprint('hi')\n"
    shown = _show(client, call, {**FILE, "from": 1, "to": None})
    assert shown.status_code == 200, shown.text
    stored = blobs.objects[shown.json()["visual"]["body_ref"]].decode()
    assert leaked not in stored and "print('hi')" in stored


@pytest.mark.parametrize("source", [
    {**FILE, "repo": "../widgets"}, {**FILE, "path": "../etc/passwd"}, {**FILE, "path": "/abs"},
    {**FILE, "ref": "main..evil"}, {**FILE, "from": 0}, {**FILE, "from": 5, "to": 4},
    {"repo": "acme/widgets", "pull": 0}, {"repo": "acme/widgets", "pull": 3, "path": "x"},
])
def test_a_source_that_could_steer_the_read_is_refused(client, db, voice, reader, source):
    call = _start(client, voice)
    _grant_repo(db, _tenant(db, call))
    assert _show(client, call, source).status_code == 422
    assert reader.calls == []


class _FakeAnswer:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body, self.headers = status, body, {}


def test_the_github_reader_uses_the_installation_token_for_that_one_repository():
    minted = secrets.token_hex(20)
    asked: list[tuple[str, str, str]] = []
    sent: list[tuple[str, dict[str, str]]] = []

    class Installations:
        def token_for_repo(self, tenant_id, owner, repo):
            asked.append((tenant_id, owner, repo))
            return minted

    def send(method, url, headers, body, timeout):
        sent.append((url, headers))
        return _FakeAnswer(200, b"hello\n")

    gh = vvis.GitHubRepoReader(Installations(), send=send)
    assert gh.read_file("eng", "acme", "widgets", "main", "src/a b.py") == "hello\n"
    assert asked == [("eng", "acme", "widgets")]
    url, headers = sent[0]
    assert url == "https://api.github.com/repos/acme/widgets/contents/src/a%20b.py?ref=main"
    assert headers["Authorization"] == f"Bearer {minted}"
    gh.read_diff("eng", "acme", "widgets", 7)
    assert sent[1][0] == "https://api.github.com/repos/acme/widgets/pulls/7"
    assert sent[1][1]["Accept"] == "application/vnd.github.diff"


def test_without_an_installation_the_reader_refuses_and_never_falls_back():
    class NoInstallation:
        def token_for_repo(self, tenant_id, owner, repo):
            return None

    def send(*_args):
        raise AssertionError("GitHub was asked without an installation token")

    with pytest.raises(vvis.RepositoryNotCovered):
        vvis.GitHubRepoReader(NoInstallation(), send=send).read_file("eng", "a", "b", "main", "x")
    with pytest.raises(vvis.RepositoryNotCovered):
        vvis.GitHubRepoReader(None, send=send).read_diff("eng", "a", "b", 1)


def test_a_failed_github_read_names_no_body():
    class Installations:
        def token_for_repo(self, tenant_id, owner, repo):
            return secrets.token_hex(20)

    def send(*_args):
        return _FakeAnswer(500, b"secret-ish body")

    with pytest.raises(vvis.RepositoryReadFailed) as caught:
        vvis.GitHubRepoReader(Installations(), send=send).read_file("eng", "a", "b", "main", "x")
    assert "secret-ish" not in str(caught.value)


# ---- a log visual -------------------------------------------------------------


def _event(kind: EventType, attempt: str, error: str | None, minute: int) -> Any:
    return SimpleNamespace(type=kind, attempt_id=attempt, detail={"error": error} if error else {},
                           at=datetime(2026, 11, 5, 6, minute, tzinfo=timezone.utc))


class FakeTaskStore:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.asked: list[tuple[str, str, str | None]] = []

    def get_task(self, tenant_id, task_id, *, submitted_by):
        self.asked.append((tenant_id, task_id, submitted_by))
        if tenant_id != "eng":
            raise NotFound("no such task")
        return SimpleNamespace(task_id=task_id)

    def list_events(self, tenant_id, task_id, *, limit, descending, task):
        assert descending and limit == vvis.LOG_EVENTS_SCAN
        return SimpleNamespace(items=list(reversed(self.events)), next_page_token=None)


def test_a_log_is_the_tasks_failure_tails_in_the_sessions_tenant():
    store = FakeTaskStore([
        _event(EventType.RUNNING, "a1", None, 1),
        _event(EventType.RETRYING, "a1", "first failure", 2),
        _event(EventType.FAILED, "a2", "Traceback\nboom", 3),
    ])
    text = vvis.log_tail(store, tenant_id="eng", submitted_by=None,
                         source=vvis.LogSource(task_id="t1", step=None))
    assert text.index("first failure") < text.index("boom")
    only = vvis.log_tail(store, tenant_id="eng", submitted_by=ALICE,
                         source=vvis.LogSource(task_id="t1", step="a2"))
    assert "boom" in only and "first failure" not in only
    assert store.asked[-1] == ("eng", "t1", ALICE)
    with pytest.raises(NotFound):
        vvis.log_tail(store, tenant_id="research", submitted_by=None,
                      source=vvis.LogSource(task_id="t1", step=None))


def test_a_task_with_no_failed_step_has_no_log_to_show():
    store = FakeTaskStore([_event(EventType.SUCCEEDED, "a1", None, 1)])
    with pytest.raises(NotFound):
        vvis.log_tail(store, tenant_id="eng", submitted_by=None,
                      source=vvis.LogSource(task_id="t1", step=None))


# ---- anchors ------------------------------------------------------------------


VISUAL = {"parts": [{"id": "header", "label": "Header"}], "lines": {"from": 10, "to": 20}}


@pytest.mark.parametrize("anchor", [
    {"type": "point", "x": 0, "y": 1},
    {"type": "region", "x": 0.25, "y": 0.5, "w": 0.75, "h": 0.5},
    {"type": "part", "part_id": "header"},
    {"type": "lines", "from": 10, "to": 20},
    None,
])
def test_an_anchor_inside_the_visual_is_kept(anchor):
    assert vvis.check_anchor(anchor, VISUAL) == anchor


@pytest.mark.parametrize("anchor", [
    {"type": "point", "x": 1.5, "y": 0},
    {"type": "point", "x": True, "y": 0},
    {"type": "point", "x": 0.1, "y": 0.1, "w": 0.2},
    {"type": "region", "x": 0.5, "y": 0.5, "w": 0.6, "h": 0.1},
    {"type": "region", "x": 0.1, "y": 0.1, "w": 0, "h": 0.1},
    {"type": "part", "part_id": "footer"},
    {"type": "lines", "from": 9, "to": 12},
    {"type": "lines", "from": 15, "to": 14},
    {"type": "circle", "x": 0, "y": 0},
    "here",
])
def test_an_anchor_outside_the_visual_is_refused(anchor):
    with pytest.raises(ValidationFailed):
        vvis.check_anchor(anchor, VISUAL)


# ---- notes ----------------------------------------------------------------------


def _note(client, call, vid, *, headers=None, **note):
    body = {"text": "make this bigger", "via": "voice", "anchor": None, "part": None, **note}
    return client.post(f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes",
                       json={"note": body}, headers=headers or _grant_headers(call))


def _point(client, call, vid, anchor):
    return client.post(f"/v1/voice/sessions/{call.session_id}/pointer",
                       json={"visual_id": vid, "anchor": anchor}, headers=auth_header("alice"))


def test_a_spoken_note_takes_the_fresh_pointer_else_a_part_else_the_whole_visual(client, db,
                                                                               voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    region = {"type": "region", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert _point(client, call, vid, region).status_code == 200
    pinned = _note(client, call, vid)
    assert pinned.status_code == 200, pinned.text
    note = pinned.json()["note"]
    assert note["anchor"] == region and note["via"] == "voice" and note["state"] == "open"
    assert note["visual_version"] == 1 and "tenant_id" not in note
    named = _note(client, call, vid, part="the api").json()["note"]
    assert named["anchor"] == {"type": "part", "part_id": "the-api"}
    # A pointer older than five seconds is not where the person is pointing now.
    pointer = f"voice_projects/{call.project_id}/pointers/{call.session_id}"
    db.docs[pointer]["at"] = db.docs[pointer]["at"] - timedelta(
        seconds=vvis.POINTER_MAX_AGE_SECONDS + 1)
    whole = _note(client, call, vid).json()["note"]
    assert whole["anchor"] is None
    assert len(_events(db, call, vvis.NOTE_UPSERT)) == 3


def test_a_typed_note_carries_its_own_anchor_and_a_bad_one_is_refused(client, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    point = {"type": "point", "x": 0.5, "y": 0.5}
    typed = _note(client, call, vid, via="tap", anchor=point, headers=auth_header("alice"))
    assert typed.status_code == 200 and typed.json()["note"]["anchor"] == point
    bad = _note(client, call, vid, via="typed", headers=auth_header("alice"),
                anchor={"type": "part", "part_id": "nope"})
    assert bad.status_code == 422
    # The planner pins spoken notes only.
    assert _note(client, call, vid, via="typed").status_code == 422


def test_a_note_with_a_credential_is_refused(client, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    assert _note(client, call, vid, text="use ghp_" + secrets.token_hex(18)).status_code == 422


def test_a_grant_reaches_only_its_own_projects_notes_and_not_the_pages_routes(client, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    other = SimpleNamespace(project_id="vp_" + secrets.token_hex(16))
    assert _note(client, other, vid, headers=_grant_headers(call)).status_code == 403
    note_id = _note(client, call, vid).json()["note"]["note_id"]
    base = f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes"
    grant = _grant_headers(call)
    assert client.post(f"/v1/voice/sessions/{call.session_id}/pointer",
                       json={"visual_id": vid, "anchor": None}, headers=grant).status_code == 403
    assert client.patch(f"{base}/{note_id}", json={"state": "resolved"},
                        headers=grant).status_code == 403
    assert client.post(f"{base}/work", json={"note_ids": [note_id], "kind": "issue"},
                       headers=grant).status_code == 403


def test_the_person_resolves_a_note_and_a_note_that_became_work_stays_work(client, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    first = _note(client, call, vid).json()["note"]["note_id"]
    base = f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes"
    resolved = client.patch(f"{base}/{first}", json={"state": "resolved"},
                            headers=auth_header("alice"))
    assert resolved.status_code == 200 and resolved.json()["note"]["state"] == "resolved"
    second = _note(client, call, vid).json()["note"]["note_id"]
    made = client.post(f"{base}/work", json={"note_ids": [second], "kind": "follow_up"},
                       headers=auth_header("alice"))
    assert made.status_code == 200, made.text
    again = client.patch(f"{base}/{second}", json={"state": "open"}, headers=auth_header("alice"))
    assert again.status_code == 409


# ---- notes become work ----------------------------------------------------------


def test_notes_become_a_follow_up_on_the_projects_open_list(client, db, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    ids = [_note(client, call, vid, text=t).json()["note"]["note_id"] for t in ("one", "two")]
    made = client.post(f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes",
                       json={"to_work": {"note_ids": ids, "kind": "follow_up", "title": None}},
                       headers=_grant_headers(call))
    assert made.status_code == 200, made.text
    work = made.json()["work"]
    assert work["kind"] == "follow_up" and work["ref"].startswith("brief.open/vf_")
    brief = db.docs[f"voice_projects/{call.project_id}"]["brief"]
    assert brief["open"][-1]["notes"] == ["one", "two"]
    assert {n["state"] for n in made.json()["notes"]} == {"work"}
    again = client.post(f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes",
                        json={"to_work": {"note_ids": ids[:1], "kind": "issue", "title": None}},
                        headers=_grant_headers(call))
    assert again.status_code == 409


def test_notes_become_an_issue_card_and_a_lane_brief_over_the_visuals_files(client, db, voice):
    call = _start(client, voice)
    _grant_repo(db, _tenant(db, call))
    vid = _show(client, call, FILE).json()["visual"]["visual_id"]
    first = _note(client, call, vid, anchor={"type": "lines", "from": 11, "to": 11}).json()
    assert first["note"]["anchor"] == {"type": "lines", "from": 11, "to": 11}
    second = _note(client, call, vid, text="rename this").json()["note"]["note_id"]
    url = f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes"
    issue = client.post(url, json={"to_work": {"note_ids": [first["note"]["note_id"]],
                                               "kind": "issue", "title": "Bigger"}},
                        headers=_grant_headers(call))
    assert issue.status_code == 200, issue.text
    card_id = issue.json()["work"]["ref"].removeprefix("card/")
    card = db.docs[f"voice_projects/{call.project_id}/cards/{card_id}"]
    assert card["kind"] == "work" and card["body"]["body"]["target_kind"] == "issue"
    assert card["body"]["body"]["repository"] == "acme/widgets"
    lane = client.post(url, json={"to_work": {"note_ids": [second], "kind": "lane_brief",
                                              "title": None}}, headers=_grant_headers(call))
    assert lane.status_code == 200, lane.text
    plan_id = lane.json()["work"]["ref"].removeprefix("card/")
    plan = db.docs[f"voice_projects/{call.project_id}/cards/{plan_id}"]
    assert plan["kind"] == "plan"
    assert plan["body"]["body"]["waves"][0]["lanes"][0]["territory"] == ["src/app.py"]


# ---- speak's point_at -----------------------------------------------------------


def test_speaks_point_at_is_a_focus_on_the_sessions_stream(client, db, voice):
    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    anchor = {"type": "part", "part_id": "overview"}
    sent = client.post(f"/v1/voice/sessions/{call.session_id}/visuals",
                       json={"focus": {"visual_id": vid, "anchor": anchor,
                                       "caption": "The overview is here."}},
                       headers=_grant_headers(call))
    assert sent.status_code == 200, sent.text
    [focus] = _events(db, call, vvis.FOCUS)
    assert focus["visual_id"] == vid and focus["anchor"] == anchor
    assert focus["caption"] == "The overview is here."
    null_visual = client.post(f"/v1/voice/sessions/{call.session_id}/visuals",
                       json={"focus": {"visual_id": vid}, "visual": None},
                       headers=_grant_headers(call))
    assert null_visual.status_code == 200
    neither = client.post(f"/v1/voice/sessions/{call.session_id}/visuals", json={},
                          headers=_grant_headers(call))
    assert neither.status_code == 422


# ---- the stream carries them ------------------------------------------------------


def test_the_stream_serves_a_visual_a_focus_and_a_note_in_sequence_and_on_replay(client, db,
                                                                                  voice):
    from control_plane.test_voice_sessions import _end
    from control_plane.test_voice_stream import _frames

    call = _start(client, voice)
    vid = _draw(client, call).json()["visual"]["visual_id"]
    anchor = {"type": "part", "part_id": "overview"}
    assert client.post(f"/v1/voice/sessions/{call.session_id}/visuals",
                       json={"focus": {"visual_id": vid, "anchor": anchor, "caption": "Here."}},
                       headers=_grant_headers(call)).status_code == 200
    note = _note(client, call, vid, part="overview").json()["note"]
    resolved = client.patch(
        f"/v1/voice/projects/{call.project_id}/visuals/{vid}/notes/{note['note_id']}",
        json={"state": "resolved", "session_id": call.session_id}, headers=auth_header("alice"))
    assert resolved.status_code == 200, resolved.text
    assert _end(client, "alice", call.session_id).status_code == 200
    url = f"/v1/voice/sessions/{call.session_id}/events"

    frames = _frames(client.get(url, headers=auth_header("alice")).text)

    ours = [f for f in frames if f["event"] in (vvis.VISUAL_UPSERT, vvis.FOCUS, vvis.NOTE_UPSERT)]
    assert [f["event"] for f in ours] == [vvis.VISUAL_UPSERT, vvis.FOCUS, vvis.NOTE_UPSERT,
                                          vvis.NOTE_UPSERT]
    assert [f["id"] for f in ours] == sorted(f["id"] for f in ours)
    visual = client.get(f"/v1/voice/projects/{call.project_id}/visuals/{vid}",
                        headers=auth_header("alice")).json()
    # Each frame carries what the visual and note reads serve, and no more.
    assert ours[0]["data"]["visual"] == visual["visual"] and "body" not in ours[0]["data"]["visual"]
    assert {k: ours[1]["data"][k] for k in ("visual_id", "anchor", "caption")} == {
        "visual_id": vid, "anchor": anchor, "caption": "Here."}
    assert ours[2]["data"]["note"]["note_id"] == note["note_id"]
    assert ours[2]["data"]["note"]["state"] == "open"
    assert ours[3]["data"]["note"] == visual["notes"][0]
    assert ours[3]["data"]["note"]["state"] == "resolved"
    for f in ours:
        assert "tenant_id" not in json.dumps(f["data"]) and '"person"' not in json.dumps(f["data"])

    replay = _frames(client.get(url, headers={**auth_header("alice"),
                                              "Last-Event-ID": str(ours[0]["id"])}).text)
    # Each grant call also writes a masked work step (voice/workfeed.py).
    assert [(f["id"], f["event"]) for f in replay if f["id"] <= ours[-1]["id"]
            and f["event"] != "work.step"] == [(f["id"], f["event"]) for f in ours[1:]]


# ---- a mock-up: frames, approval ---------------------------------------------


FRAMES = [{"id": "frame-1", "label": "Cards",
           "html": "<!doctype html><style>h1{color:#333}</style><h1>Shop</h1>"},
          {"id": "frame-2", "label": "List",
           "html": '<h1>Shop</h1><img src="data:image/png;base64,AAAA" alt="">'}]


def _mockup(client, call, frames=None, **visual):
    return _draw(client, call, kind="mockup", title="The shop page",
                 body=json.dumps({"frames": FRAMES if frames is None else frames}), **visual)


def _approve(client, call, vid, frame, *, headers=None):
    return client.post(f"/v1/voice/sessions/{call.session_id}/visuals",
                       json={"approve": {"visual_id": vid, "frame": frame}},
                       headers=headers or _grant_headers(call))


def test_a_mockup_is_a_visual_of_frames_each_frame_a_part(client, db, voice, blobs):
    call = _start(client, voice)
    drawn = _mockup(client, call)
    assert drawn.status_code == 200, drawn.text
    visual = drawn.json()["visual"]
    assert visual["kind"] == "mockup" and visual["approved"] is None
    assert visual["parts"] == [{"id": "frame-1", "label": "Cards"},
                               {"id": "frame-2", "label": "List"}]
    stored = json.loads(blobs.objects[visual["body_ref"]])
    assert [f["html"] for f in stored["frames"]] == [f["html"] for f in FRAMES]


@pytest.mark.parametrize("html", [
    "<script>alert(1)</script>",
    '<img src="x" onerror="alert(1)">',
    '<a href="javascript:alert(1)">go</a>',
    '<img src="https://example.com/x.png">',
    '<img src="//example.com/x.png">',
    '<img src="logo.png">',
    "<style>body{background:url(https://example.com/x.png)}</style>",
    "<style>@import 'https://example.com/x.css';</style>",
    '<link rel="stylesheet" href="https://example.com/x.css">',
    '<iframe srcdoc="x"></iframe>',
    '<meta http-equiv="refresh" content="0;url=https://example.com">',
    '<form action="https://example.com"><input></form>',
])
def test_a_frame_that_runs_code_or_fetches_anything_is_refused(client, voice, blobs, html):
    call = _start(client, voice)
    refused = _mockup(client, call, frames=[{"id": "frame-1", "label": "One", "html": html}])
    assert refused.status_code == 422, html
    assert blobs.objects == {}


def test_a_mockup_body_that_is_not_frames_is_refused(client, voice):
    call = _start(client, voice)
    assert _draw(client, call, kind="mockup", body="<h1>loose</h1>").status_code == 422
    assert _mockup(client, call, frames=[]).status_code == 422
    two = [{"id": "a", "label": "A", "html": "<p>a</p>"}, {"id": "a", "label": "B", "html": "<p>b</p>"}]
    assert _mockup(client, call, frames=two).status_code == 422


def test_a_credential_in_a_frame_is_masked_before_it_is_stored(client, voice, blobs):
    call = _start(client, voice)
    token = "ghp_" + secrets.token_hex(18)
    drawn = _mockup(client, call, frames=[{"id": "frame-1", "label": "One",
                                           "html": f"<p>key {token}</p>"}])
    assert drawn.status_code == 200, drawn.text
    stored = blobs.objects[drawn.json()["visual"]["body_ref"]].decode()
    assert token not in stored and "<p>key " in stored


def test_an_approval_by_voice_is_stored_on_the_visual_and_served(client, db, voice):
    call = _start(client, voice)
    vid = _mockup(client, call).json()["visual"]["visual_id"]
    approved = _approve(client, call, vid, "List")
    assert approved.status_code == 200, approved.text
    mark = approved.json()["visual"]["approved"]
    assert mark["frame_id"] == "frame-2" and mark["label"] == "List"
    assert mark["by"] == ALICE and mark["via"] == "voice" and mark["at"]
    served = client.get(f"/v1/voice/projects/{call.project_id}/visuals/{vid}",
                        headers=auth_header("alice")).json()["visual"]
    assert served["approved"] == mark
    listed = client.get(f"/v1/voice/projects/{call.project_id}/visuals",
                        headers=auth_header("alice")).json()["visuals"]
    assert listed[0]["approved"] == mark
    # The page hears it on the session's stream.
    assert _events(db, call, vvis.VISUAL_UPSERT)[-1]["visual"]["approved"] == mark
    # The planner reads what was approved, on the same write the grant reaches.
    read = client.post(f"/v1/voice/sessions/{call.session_id}/visuals",
                       json={"approved": True}, headers=_grant_headers(call))
    assert read.status_code == 200, read.text
    assert read.json()["approved"] == [{"visual_id": vid, "title": "The shop page",
                                        "version": 1, **mark}]


def test_an_approval_names_one_of_the_mockups_frames_and_only_a_mockup(client, voice):
    call = _start(client, voice)
    vid = _mockup(client, call).json()["visual"]["visual_id"]
    assert _approve(client, call, vid, "frame-9").status_code == 422
    spec = _draw(client, call).json()["visual"]["visual_id"]
    assert _approve(client, call, spec, "overview").status_code == 422


def test_the_person_approves_a_frame_on_the_stage(client, db, voice):
    call = _start(client, voice)
    vid = _mockup(client, call).json()["visual"]["visual_id"]
    url = f"/v1/voice/projects/{call.project_id}/visuals/{vid}/approval"
    approved = client.post(url, json={"frame": "frame-1", "session_id": call.session_id},
                           headers=auth_header("alice"))
    assert approved.status_code == 200, approved.text
    mark = approved.json()["visual"]["approved"]
    assert mark["frame_id"] == "frame-1" and mark["via"] == "stage" and mark["by"] == ALICE
    # The approval route is the page's: a grant does not reach it.
    assert client.post(url, json={"frame": "frame-1"},
                       headers=_grant_headers(call)).status_code == 403


def test_another_workspaces_visual_is_not_approved(client, db, voice):
    call = _start(client, voice)
    vid = _mockup(client, call).json()["visual"]["visual_id"]
    url = f"/v1/voice/projects/{call.project_id}/visuals/{vid}/approval"
    theirs = client.post(url, json={"frame": "frame-1"}, headers=auth_header("root"))
    missing = client.post(f"/v1/voice/projects/vp_{secrets.token_hex(16)}/visuals/{vid}/approval",
                          json={"frame": "frame-1"}, headers=auth_header("alice"))
    assert theirs.status_code == missing.status_code == 404
    # A grant approves only on its own session.
    other = SimpleNamespace(session_id="vs_" + secrets.token_hex(16))
    assert _approve(client, other, vid, "frame-1", headers=_grant_headers(call)).status_code == 403
    doc = db.docs[f"voice_projects/{call.project_id}/visuals/{vid}"]
    assert doc.get("approved") is None


def test_a_mockup_drawn_again_drops_the_approval_of_its_old_frames(client, voice):
    call = _start(client, voice)
    vid = _mockup(client, call).json()["visual"]["visual_id"]
    assert _approve(client, call, vid, "frame-1").status_code == 200
    again = _mockup(client, call, visual_id=vid).json()["visual"]
    assert again["version"] == 2 and again["approved"] is None


# ---- a refused write is logged by its code, field and reason, never its body --


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "swarm_api.voice.refused" and r.levelname == "WARNING"]


def test_a_refused_mockup_logs_its_code_field_and_reason_and_none_of_the_body(client, voice, caplog):
    call = _start(client, voice)
    marker = "hero-" + secrets.token_hex(6)
    html = f'<h1>{marker}</h1><img src="https://images.example.com/{marker}.png">'
    caplog.set_level("WARNING", logger="swarm_api.voice.refused")
    refused = _mockup(client, call, frames=[{"id": "frame-1", "label": "One", "html": html}])
    assert refused.status_code == 422
    # MUTATION: leave the refusal to the request log and its reason cannot be read.
    (line,) = _refusals(caplog)
    assert "route=/v1/voice/sessions/{session_id}/visuals" in line and "status=422" in line
    assert "code=validation_failed" in line and "field=frames[0].html" in line
    assert "reason=frames[0].html names an address" in line
    assert marker not in caplog.text and "images.example.com" not in caplog.text


def test_a_body_the_request_model_refuses_logs_where_and_why_never_the_input(client, voice, caplog):
    call = _start(client, voice)
    title = "t-" + secrets.token_hex(80)
    caplog.set_level("WARNING", logger="swarm_api.voice.refused")
    assert _draw(client, call, title=title).status_code == 422
    (line,) = _refusals(caplog)
    assert "code=validation_failed" in line and "field=body.visual.title" in line
    assert "at most 120 characters" in line and title not in caplog.text


def test_a_refused_card_logs_its_field_and_reason_and_a_credential_never(client, voice, caplog):
    call = _start(client, voice)
    token = "ghp_" + secrets.token_hex(18)
    card = {"kind": "mockup", "state": "proposed", "title": "Landing", "created_by": "planner",
            "references": {}, "summary": f"key {token}",
            "body": {"drawn_by": "planner", "frames": [{"label": "One", "html": "<p>a</p>"}]}}
    caplog.set_level("WARNING", logger="swarm_api.voice.refused")
    refused = client.post(f"/v1/voice/sessions/{call.session_id}/cards", json={"card": card},
                          headers=_grant_headers(call))
    assert refused.status_code == 422
    (line,) = _refusals(caplog)
    assert "route=/v1/voice/sessions/{session_id}/cards" in line
    assert "field=body.frames[0].label" in line and "is not a field of this card" in line
    assert token not in caplog.text and "<p>a</p>" not in caplog.text


def test_a_drawn_mockup_logs_no_refusal(client, voice, caplog):
    call = _start(client, voice)
    caplog.set_level("WARNING", logger="swarm_api.voice.refused")
    assert _mockup(client, call).status_code == 200
    assert _refusals(caplog) == []
