"""Three routes that served text around the redaction filter rather than through all of it.

Wave 2026-09-27, the redaction family. Each was measured by reading the code at
b0fae05 against the route it should have matched:

  * #207 -- `GET /v1/tasks/{id}/checkpoints/{n}/files/{path}` decoded a window
    with `errors="replace"`, so a Latin-1 byte became U+FFFD and nothing said
    so, and it read no look-back, so a page in the MIDDLE of a private key
    longer than a page held neither marker and was served in clear. The
    artifact route it is documented to match (`inspect.read_artifact`) counts
    the bytes (`_decode_window`) and looks back for an open key
    (`_enter_key`). The file read now does both, with the same helpers.
  * epic #227 -- a checkpoint's SYMLINK TARGET, in the listing and in the 422
    that refuses to open a link, was masked by the rules alone. A value the
    task's own metadata names as secret -- which every other route masks
    wherever it appears -- was served in the target.
  * epic #227 -- `/answer`'s `subtype`, `stop_reason` and `terminal_reason`
    were masked by the rules alone, beside a `content` masked with the task's
    literals; and a mask there was not counted.

MUTATIONS: decode the checkpoint window with `errors="replace"` again (the
count test goes red); drop `_enter_key` from `_serve_member` or read it no
look-back (the mid-key pages go red, and so does the window that starts
exactly on a key's first body line -- the issue's reproduction); drop `extra=` from either symlink call
(the listing or the 422 goes red); drop `literals` from `_text_or_none` (the
answer test goes red).
"""

from __future__ import annotations

import json

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant
from .test_checkpoint_content import put_checkpoint, tar_gz
from .test_log_redaction import KEY_LEAK, _pem_body, _pem_text
from .test_transcript_answer_literals_masked import LITERAL, a_task, answer, final_key


def _seed(db, objects, entries: list[tuple]) -> None:
    """A task whose METADATA names `LITERAL` as secret, and one checkpoint."""
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    doc["metadata"] = {"deploy_secret": LITERAL}
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB", "created_at": doc["created_at"],
    })
    put_checkpoint(objects, archive=tar_gz(entries))


def _file(client, path: str, **params):
    return client.get(
        f"/v1/tasks/task_a/checkpoints/ckpt-00001/files/{path}",
        params=params,
        headers=auth_header("alice"),
    )


# --------------------------------------------------------------------------
# #207: the checkpoint file read counts what it replaces
# --------------------------------------------------------------------------

def test_a_checkpoint_file_that_is_not_utf8_says_how_many_bytes_it_replaced(client, db, objects):
    """Shown as U+FFFD -- a JSON string cannot carry the byte -- and COUNTED,
    as `/logs` and `/artifacts/content` count it, so a mangled window is never
    taken for the agent's own text."""
    _seed(db, objects, [("cafe.txt", "file", b"caf\xe9 au lait\nplain line\n"),
                        ("clean.txt", "file", "plain line\n")])

    body = _file(client, "cafe.txt").json()
    assert body["status"] == "ok", body
    assert body["invalid_utf8_bytes"] == 1, body
    assert "�" in body["content"]
    assert "UTF-8" in (body["detail"] or ""), body["detail"]

    clean = _file(client, "clean.txt").json()
    assert clean["invalid_utf8_bytes"] == 0, "a clean window measured zero, not null"
    assert clean["detail"] is None


# --------------------------------------------------------------------------
# #207: a page in the middle of a private key knows it is inside one
# --------------------------------------------------------------------------

def test_a_key_longer_than_the_window_leaks_from_no_page_of_a_checkpoint_file(client, db, objects):
    """The `/logs` test of the same name, on a checkpoint member: a key longer
    than the smallest window (4 KiB) spans pages, and the pages in its middle
    hold neither marker. Each page now looks back past its own start."""
    before = "".join(f"line {n:03d} before\n" for n in range(40))
    after = "".join(f"line {n:03d} after\n" for n in range(40))
    text = before + _pem_text(_pem_body(200)) + "\n" + after
    _seed(db, objects, [("id_rsa.txt", "file", text)])

    pages, offset = [], 0
    while True:
        body = _file(client, "id_rsa.txt", offset=offset, limit_bytes=4096).json()
        assert body["status"] == "ok", body
        pages.append(body)
        if body["next_offset"] is None:
            break
        offset = body["next_offset"]
        assert len(pages) < 200, "the offset is not advancing"
    assert len(pages) > 3, "the key must span several pages for this to test anything"
    for page in pages:
        assert not KEY_LEAK.search(page["content"]), (page["offset"], page["content"][:300])
    seen = "".join(page["content"] for page in pages)
    assert seen.startswith(before), "the text before the key is whole"
    assert seen.endswith(after), "and so is the text after it"

    # Offsets a caller chooses, across the key and inside its BEGIN line.
    start = len(before)
    for offset in [*range(start, start + 13_000, 509), *range(start + 1, start + 40, 7)]:
        response = _file(client, "id_rsa.txt", offset=offset, limit_bytes=4096)
        assert response.status_code == 200, response.text
        assert not KEY_LEAK.search(response.json()["content"] or ""), offset


def test_a_window_starting_on_the_first_body_line_of_a_key_serves_no_key_material(client, db, objects):
    """The issue's own reproduction (#207): `offset` lands exactly on the byte
    after the BEGIN line's newline, so the window holds key body and no
    marker. Only the look-back knows it is inside a key; without it the first
    body line is the first thing served."""
    before = "".join(f"line {n:03d} before\n" for n in range(40))
    pem = _pem_text(_pem_body(200))
    text = before + pem + "\n" + "after the key\n"
    _seed(db, objects, [("id_rsa.txt", "file", text)])

    begin_line = pem.split("\n", 1)[0] + "\n"
    offset = len(before.encode()) + len(begin_line.encode())
    assert text.encode()[offset:].startswith(b"K0000"), "the offset must land on key body"

    response = _file(client, "id_rsa.txt", offset=offset, limit_bytes=4096)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok", body
    content = body["content"] or ""
    assert not KEY_LEAK.search(content), content[:300]
    assert not content.startswith("K0000"), content[:300]


# --------------------------------------------------------------------------
# #227: a symlink target is masked with the task's literals
# --------------------------------------------------------------------------

def test_a_symlink_target_holding_a_named_secret_is_masked_in_the_listing(client, db, objects):
    _seed(db, objects, [("state.json", "file", "{}\n"),
                        ("pointer", "symlink", f"/run/secrets/{LITERAL}"),
                        ("latest", "symlink", "state.json")])

    response = client.get("/v1/tasks/task_a/checkpoints/ckpt-00001/files",
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert LITERAL not in response.text, "the metadata's literal was served in a link target"
    rows = {row["path"]: row for row in response.json()["files"]}
    assert rows["pointer"]["link"] == f"/run/secrets/{MASK}"
    assert rows["latest"]["link"] == "state.json", "a target naming nothing secret is as written"


def test_a_symlink_target_holding_a_named_secret_is_masked_in_the_422(client, db, objects):
    _seed(db, objects, [("pointer", "symlink", f"/run/secrets/{LITERAL}")])

    response = _file(client, "pointer")
    assert response.status_code == 422, response.text
    assert LITERAL not in response.text
    assert response.json()["detail"]["link"] == f"/run/secrets/{MASK}"


# --------------------------------------------------------------------------
# #227: `/answer`'s reason fields are masked with the task's literals, and counted
# --------------------------------------------------------------------------

def test_the_answers_reason_fields_are_masked_with_the_tasks_literals(client, db, objects):
    result = {
        "type": "result", "is_error": True, "result": "stopped",
        "subtype": f"error_{LITERAL}",
        "stop_reason": LITERAL,
        "terminal_reason": f"aborted at {LITERAL}",
    }
    objects.put(final_key(), json.dumps(result) + "\n")
    a_task(db)

    body = answer(client).json()
    assert body["status"] == "ok", body
    assert LITERAL not in json.dumps(body), "the metadata's literal was served in a reason field"
    assert body["subtype"] == f"error_{MASK}"
    assert body["stop_reason"] == MASK
    assert body["terminal_reason"] == f"aborted at {MASK}"
    assert body["content"] == "stopped"
    assert body["redacted"] is True, "three masks, and the answer said it held none"
    assert body["redaction_count"] == 3


def test_the_control_ordinary_reason_fields_are_served_as_written(client, db, objects):
    result = {"type": "result", "is_error": False, "result": "done",
              "subtype": "success", "stop_reason": "end_turn", "terminal_reason": "completed"}
    objects.put(final_key(), json.dumps(result) + "\n")
    a_task(db)

    body = answer(client).json()
    assert (body["subtype"], body["stop_reason"], body["terminal_reason"]) == (
        "success", "end_turn", "completed")
    assert body["redaction_count"] == 0
    assert body["redacted"] is False
