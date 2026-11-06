"""Deleting a Voice it project takes its visuals, notes and pointers with it
(swarm_api/voice/store.py `VoiceStore.delete_project`; swarm_api/voice/visuals.py
names the collections).

A project's visuals live at `voice_projects/{p}/visuals/{v}`, each visual's
notes under it, and the page's pointer at `voice_projects/{p}/pointers/{s}`.
The delete removes them in the same batches as the project's cards, turns and
sessions, and nothing of another project goes with them: per-tenant isolation
(CONTRACT.md invariant 9) holds after a delete as it does before one.

The in-memory Firestore and bucket; no network, no cloud credential.
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.conftest import db  # noqa: E402,F401

from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import store as vstore  # noqa: E402
from swarm_api.voice import visuals as vvis  # noqa: E402


def _seed(db, project: str, *, tenant: str, person: str, visuals: int = 2) -> None:  # noqa: F811
    owner = {"tenant_id": tenant, "person": person}
    project_ref = db.collection(vs.PROJECTS).document(project)
    project_ref.set({**owner, "title": "T"})
    session = "vs_" + secrets.token_hex(16)
    project_ref.collection(vs.SESSIONS).document(session).set({**owner, "state": vs.ENDED})
    for _ in range(visuals):
        visual = project_ref.collection(vvis.VISUALS).document("vv_" + secrets.token_hex(16))
        visual.set({**owner, "kind": "diagram", "version": 1})
        for _ in range(2):
            visual.collection(vvis.NOTES).document("vn_" + secrets.token_hex(16)).set(
                {**owner, "text": "make this bigger", "state": "open"})
    project_ref.collection(vvis.POINTERS).document(session).set(
        {**owner, "visual_id": "vv_x", "anchor": None})


def _under(db, project: str) -> list[str]:  # noqa: F811
    return [p for p in db.docs if p.startswith(f"{vs.PROJECTS}/{project}/")]


def _store(db, blobs=None) -> vstore.VoiceStore:  # noqa: F811
    return vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc), blobs=blobs)


def test_a_deleted_project_keeps_no_visual_note_or_pointer(db):  # noqa: F811
    project = "vp_" + secrets.token_hex(16)
    _seed(db, project, tenant="eng", person="alice@example.com")
    assert [p for p in _under(db, project) if f"/{vvis.NOTES}/" in p]

    result = _store(db, vstore.InMemoryVoiceBlobs()).delete_project(
        tenant_id="eng", person="alice@example.com", project_id=project, confirm="T")

    assert result["deleted"] is True
    assert _under(db, project) == []
    assert f"{vs.PROJECTS}/{project}" not in db.docs
    # 1 session, 2 visuals with 2 notes each, 1 pointer, and the project.
    assert result["documents"] == 1 + 2 + 4 + 1 + 1


def test_another_projects_visuals_notes_and_pointers_are_untouched(db):  # noqa: F811
    project = "vp_" + secrets.token_hex(16)
    mine = "vp_" + secrets.token_hex(16)
    theirs = "vp_" + secrets.token_hex(16)
    _seed(db, project, tenant="eng", person="alice@example.com")
    _seed(db, mine, tenant="eng", person="alice@example.com")
    _seed(db, theirs, tenant="research", person="bob@example.com")
    kept = {p: dict(db.docs[p]) for p in _under(db, mine) + _under(db, theirs)}

    _store(db).delete_project(tenant_id="eng", person="alice@example.com",
                              project_id=project, confirm="T")

    assert {p: db.docs.get(p) for p in kept} == kept
    for other in (mine, theirs):
        paths = _under(db, other)
        assert [p for p in paths if f"/{vvis.NOTES}/" in p]
        assert [p for p in paths if f"/{vvis.POINTERS}/" in p]


def test_a_retried_delete_finishes_what_a_failed_batch_left(db, monkeypatch):  # noqa: F811
    project = "vp_" + secrets.token_hex(16)
    _seed(db, project, tenant="eng", person="alice@example.com", visuals=3)
    store = _store(db)
    monkeypatch.setattr(vstore, "_BATCH", 4)
    real_batch = db.batch
    calls = {"n": 0}

    def failing_second_batch():
        batch = real_batch()
        calls["n"] += 1
        if calls["n"] == 2:
            def refuse():
                raise RuntimeError("firestore unavailable")
            batch.commit = refuse
        return batch

    monkeypatch.setattr(db, "batch", failing_second_batch)
    with pytest.raises(RuntimeError):
        store.delete_project(tenant_id="eng", person="alice@example.com",
                             project_id=project, confirm="T")
    assert f"{vs.PROJECTS}/{project}" in db.docs

    monkeypatch.setattr(db, "batch", real_batch)
    store.delete_project(tenant_id="eng", person="alice@example.com",
                         project_id=project, confirm="T")

    assert _under(db, project) == []
    assert f"{vs.PROJECTS}/{project}" not in db.docs
