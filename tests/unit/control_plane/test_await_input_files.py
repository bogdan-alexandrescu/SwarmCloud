"""An answer's files, and the size gate in front of them (docs/design/human-steps.md §4.3; HW-API-ANSWER).

What is refused and when: a text file holding a credential (422, naming the
line and the rule, never the match), an archive or an executable (415), a
file whose name lies about its bytes (415), too many files, a body over
16 MiB (413) -- by its `Content-Length` before it is read, and by the
counting reader when the header lies -- and a fifth multipart answer while
four are held (429). A GCS failure writes nothing to Firestore. Every URI the
answer records, its attachments' included, passes the worker's own
`artifact_key` (imported, not restated), and `inputwriter` refuses a key
outside the grant's condition before it calls GCS.

The fixtures are test_await_input_api.py's: the same signed workflow, parked
by the scheduler's own window. Every fake credential is built at run time.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from typing import Any

import pytest

from agent_worker.inputs import ATTACHMENTS_DIR_SUFFIX as WORKER_SUFFIX
from agent_worker.inputs import artifact_key
from agent_worker.inputs import attachments_dir as worker_attachments_dir
from swarm_api import awaitinput, inputwriter
from swarm_api.inputwriter import InMemoryInputWriter, key_refusal
from swarm_api.routes import await_input as route

from .conftest import auth_header
from .test_await_input_api import (  # noqa: F401 - fixtures
    api_context,
    await_enabled,
    block,
    client,
    fake_token,
    group_map,
    launch,
    park,
    snapshot,
    tokens,
    url,
    waiting,
    writer,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
PDF = b"%PDF-1.7\n" + bytes(range(256))
ZIP = b"PK\x03\x04" + bytes(range(256))
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 64


def form(answer_id: str = "f1", verdict: str = "NOT_YET") -> dict[str, Any]:
    return {"answer_id": answer_id, "payload": {"verdict": verdict, "findings": ["see draft.md"]}}


def multipart(answer: dict[str, Any], files: list[tuple[str, bytes]]) -> list[tuple[str, Any]]:
    parts: list[tuple[str, Any]] = [("answer", ("answer.json", json.dumps(answer), "application/json"))]
    parts += [("file", (name, data, "application/octet-stream")) for name, data in files]
    return parts


def post_files(client, ids, files, answer=None, user="alice"):
    return client.post(url(ids), headers=auth_header(user), files=multipart(answer or form(), files))


# --------------------------------------------------------------------------
# what is taken
# --------------------------------------------------------------------------

def test_files_are_stored_beside_the_form_and_listed_as_its_attachments(
    client, db, objects, waiting
):
    draft = "# Draft\n\nRevised section 3.\n".encode()
    response = post_files(client, waiting, [("draft.md", draft), ("shot.png", PNG),
                                            ("spec.pdf", PDF)])
    assert response.status_code == 200, response.text
    assert [f["name"] for f in response.json()["files"]] == [
        "review.files/draft.md", "review.files/shot.png", "review.files/spec.pdf"]
    entry = db.docs[f"tasks/{waiting['review']}"]["result_summary"]["artifacts"][0]
    types = {a["name"]: a["content_type"] for a in entry["attachments"]}
    assert types == {"review.files/draft.md": "text/plain; charset=utf-8",
                     "review.files/shot.png": "image/png",
                     "review.files/spec.pdf": "application/pdf"}
    summary = db.docs[f"tasks/{waiting['review']}"]["result_summary"]
    assert summary["input"]["files"] == 3
    assert summary["artifact_bytes"] == entry["bytes"] + len(draft) + len(PNG) + len(PDF)
    # Byte for byte: never rewritten.
    by_name = {a["name"]: a for a in entry["attachments"]}
    key = artifact_key(by_name["review.files/shot.png"]["uri"], tenant_id="eng",
                       upstream_task_id=waiting["review"], filename="review.files/shot.png")
    assert objects.objects[key] == PNG


def test_every_recorded_uri_passes_the_workers_artifact_key_unchanged(
    client, db, objects, waiting
):
    response = post_files(client, waiting, [("notes.txt", b"plain notes\n"), ("a.json", b"{}")])
    assert response.status_code == 200, response.text
    entry = db.docs[f"tasks/{waiting['review']}"]["result_summary"]["artifacts"][0]
    recorded = [(entry["name"], entry["uri"])] + [(a["name"], a["uri"]) for a in entry["attachments"]]
    assert len(recorded) == 3
    for name, uri in recorded:
        key = artifact_key(uri, tenant_id="eng", upstream_task_id=waiting["review"], filename=name)
        assert key in objects.objects, name
        assert key_refusal(key) is None, key
        assert uri == f"gs://{objects.bucket}/{key}"


def test_the_attachments_directory_is_the_workers():
    assert inputwriter.ATTACHMENTS_DIR_SUFFIX == WORKER_SUFFIX
    for artifact in ("review.json", "verdict.json", "a.b.json", "answers", "input.json"):
        assert inputwriter.attachments_dir(artifact) == worker_attachments_dir(artifact)


def test_max_files_bounds_the_count(client, db, objects, await_enabled):
    ids = launch(client, db, block(max_files=0))
    park(db, ids["review"])
    before = snapshot(db, objects, ids["review"])
    response = post_files(client, ids, [("a.txt", b"x")])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "too_many_files"
    assert snapshot(db, objects, ids["review"]) == before
    assert post_files(client, ids, []).status_code == 200


def test_an_eleventh_file_is_refused(client, waiting):
    files = [(f"f{i}.txt", b"x") for i in range(11)]
    response = post_files(client, waiting, files)
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "too_many_files"


@pytest.mark.parametrize("name", ["../x.md", ".hidden.md", "a b.md", "dir/x.md", "x" * 129 + ".md"])
def test_a_name_that_could_traverse_or_hide_is_refused(client, db, objects, waiting, name):
    before = snapshot(db, objects, waiting["review"])
    response = post_files(client, waiting, [(name, b"x")])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "invalid_file_name"
    assert snapshot(db, objects, waiting["review"]) == before


def test_two_files_of_one_name_are_refused(client, waiting):
    response = post_files(client, waiting, [("a.md", b"x"), ("A.md", b"y")])
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_file_name"


# --------------------------------------------------------------------------
# what is refused by its bytes
# --------------------------------------------------------------------------

def test_a_text_file_holding_a_credential_is_422_naming_the_line_and_rule(
    client, db, objects, waiting
):
    token = fake_token()
    text = f"line one\nline two\nexport GH={token}\n".encode()
    before = snapshot(db, objects, waiting["review"])
    response = post_files(client, waiting, [("draft.md", text)])
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "file_contains_credential"
    assert detail["file"] == "draft.md" and detail["line"] == 3
    assert detail["rule"] == "github_token"
    assert token not in response.text and token[:12] not in response.text
    assert snapshot(db, objects, waiting["review"]) == before


def test_the_scan_names_the_first_matching_rule_and_line():
    key = "AKIA" + secrets.token_hex(8).upper()
    assert awaitinput.credential_hit(f"a\n\nb {key}\n") == (3, "aws_access_key_id")
    assert awaitinput.credential_hit("nothing to see\n") is None


@pytest.mark.parametrize("name, data", [
    ("bundle.zip", ZIP),
    ("bundle.md", ZIP),
    ("tool", ELF),
    ("tool.sh", ELF),
    ("data.tar.gz", b"\x1f\x8b\x08" + bytes(range(256))),
    ("notes.md", b"\xff\xfe not utf-8 \x80"),
    ("notes.exe", b"MZ" + b"\x00" * 64),
])
def test_an_archive_or_an_executable_is_415(client, db, objects, waiting, name, data):
    before = snapshot(db, objects, waiting["review"])
    response = post_files(client, waiting, [(name, data)])
    assert response.status_code == 415, response.text
    assert response.json()["detail"] == {"reason": "file_type_refused", "file": name}
    assert snapshot(db, objects, waiting["review"]) == before


@pytest.mark.parametrize("name, data", [
    ("shot.md", PNG),
    ("spec.txt", PDF),
    ("shot.jpg", PNG),
    ("notes.png", b"just text\n"),
])
def test_a_file_whose_name_lies_about_its_bytes_is_refused(client, waiting, name, data):
    response = post_files(client, waiting, [(name, data)])
    assert response.status_code == 415, response.text
    assert response.json()["detail"]["reason"] == "file_type_refused"


def test_the_clients_content_type_decides_nothing(client, db, waiting):
    parts = [("answer", ("a.json", json.dumps(form()), "application/json")),
             ("file", ("shot.png", PNG, "text/markdown"))]
    response = client.post(url(waiting), headers=auth_header("alice"), files=parts)
    assert response.status_code == 200, response.text
    entry = db.docs[f"tasks/{waiting['review']}"]["result_summary"]["artifacts"][0]
    assert entry["attachments"][0]["content_type"] == "image/png"


# --------------------------------------------------------------------------
# the size gate
# --------------------------------------------------------------------------

def test_a_body_over_the_bound_is_413_by_its_length_before_anything_is_read(
    client, db, objects, writer, waiting
):
    before = snapshot(db, objects, waiting["review"])
    response = client.post(
        url(waiting),
        headers={**auth_header("alice"), "Content-Type": "multipart/form-data; boundary=x",
                 "Content-Length": str(route.MAX_MULTIPART_BYTES + 1)},
        content=b"--x--\r\n",
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["reason"] == "answer_too_large"
    assert writer.created == []
    assert snapshot(db, objects, waiting["review"]) == before


def test_an_answer_of_16_mib_and_one_byte_is_413_before_any_gcs_call(
    client, db, objects, writer, waiting
):
    form_bytes = len(awaitinput._form_bytes(form()["payload"]))
    file_bytes = awaitinput.MAX_ANSWER_BYTES - form_bytes + 1
    before = snapshot(db, objects, waiting["review"])
    response = post_files(client, waiting, [("big.pdf", b"%PDF-" + b"\0" * (file_bytes - 5))])
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["reason"] == "answer_too_large"
    assert writer.created == []
    assert snapshot(db, objects, waiting["review"]) == before
    # Exactly 16 MiB is taken.
    exact = post_files(client, waiting, [("big.pdf", b"%PDF-" + b"\0" * (file_bytes - 6))])
    assert exact.status_code == 200, exact.text


def test_a_multipart_answer_without_a_length_is_411(client, writer, waiting):
    def body():
        # A generator body is sent chunked, with no Content-Length.
        yield b"--x\r\n"

    response = client.post(url(waiting), content=body(), headers={
        **auth_header("alice"), "Content-Type": "multipart/form-data; boundary=x"})
    assert response.status_code == 411, response.text
    assert response.json()["detail"]["reason"] == "length_required"
    assert writer.created == []


class _Stream:
    """A request whose body is more than its `Content-Length` claims."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read = 0

    async def stream(self):
        for chunk in self.chunks:
            self.read += len(chunk)
            yield chunk


def test_a_lying_content_length_is_caught_by_the_counting_reader():
    stream = _Stream([b"x" * 1024] * 100)
    with pytest.raises(awaitinput.AnswerTooLarge):
        asyncio.run(route.read_counted(stream, 10 * 1024))
    # It stopped at the bound, not at the end of the stream.
    assert stream.read <= 11 * 1024
    assert asyncio.run(route.read_counted(_Stream([b"ab", b"cd"]), 4)) == b"abcd"


def test_a_lying_content_length_through_the_route_is_413(client, db, objects, writer, waiting):
    before = snapshot(db, objects, waiting["review"])
    big = b"--x\r\n" + b"y" * (route.MAX_MULTIPART_BYTES + 10)

    def body():
        yield big

    response = client.post(url(waiting), content=body(), headers={
        **auth_header("alice"), "Content-Type": "multipart/form-data; boundary=x",
        "Content-Length": "100"})
    assert response.status_code == 413, response.text
    assert writer.created == []
    assert snapshot(db, objects, waiting["review"]) == before


def test_a_fifth_concurrent_multipart_answer_is_429(client, db, writer, await_enabled):
    workflows = []
    for _ in range(5):
        ids = launch(client, db)
        park(db, ids["review"])
        workflows.append(ids)
    entered = threading.Semaphore(0)
    release = threading.Event()

    held = {ids["review"] for ids in workflows[:4]}

    def hold(key: str) -> None:
        # The four answers wait inside their write, holding their slots.
        if key.split("/")[3] in held:
            entered.release()
            assert release.wait(30)

    writer.before_create = hold
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda ids=ids: results.append(
            post_files(client, ids, [("a.txt", b"x")]).status_code))
        for ids in workflows[:4]
    ]
    for thread in threads:
        thread.start()
    try:
        for _ in range(4):
            assert entered.acquire(timeout=30), "four answers did not reach the writer"
        fifth = post_files(client, workflows[4], [("a.txt", b"x")])
        assert fifth.status_code == 429, fifth.text
        assert fifth.json()["detail"]["reason"] == "answer_busy"
        assert fifth.headers["Retry-After"]
        # A JSON answer, which holds no files, is not counted.
        assert client.post(url(workflows[4]), headers=auth_header("alice"),
                           json=form()).status_code == 200
    finally:
        writer.before_create = None
        release.set()
        for thread in threads:
            thread.join(30)
    assert results == [200, 200, 200, 200]


# --------------------------------------------------------------------------
# a failed write
# --------------------------------------------------------------------------

def test_a_gcs_failure_writes_nothing_to_firestore_and_a_retry_succeeds(
    client, db, objects, writer, waiting
):
    before = snapshot(db, objects, waiting["review"])
    writer.fail = True
    response = post_files(client, waiting, [("a.txt", b"x")])
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "store_unavailable"
    assert snapshot(db, objects, waiting["review"]) == before
    writer.fail = False
    retried = post_files(client, waiting, [("a.txt", b"x")])
    assert retried.status_code == 200, retried.text


def test_a_failure_after_some_files_leaves_only_orphans(client, db, objects, writer, waiting):
    calls: list[str] = []

    def second_fails(key: str) -> None:
        calls.append(key)
        writer.fail = len(calls) >= 2

    writer.before_create = second_fails
    response = post_files(client, waiting, [("a.txt", b"x"), ("b.txt", b"y")])
    writer.before_create, writer.fail = None, False
    assert response.status_code == 503
    doc = db.docs[f"tasks/{waiting['review']}"]
    assert doc["state"] == "PARKED" and doc.get("result_summary") is None
    assert "answer" not in doc["metadata"]["await_input"]
    # The one object that was created is named by no manifest.
    assert len(writer.created) == 1


# --------------------------------------------------------------------------
# the writer refuses what the grant's condition would refuse
# --------------------------------------------------------------------------

#: tests/terraform/await_input.tftest.hcl's cases, as object keys.
ADMITTED = [
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.files/draft.md",
    "tenants/u-carol/tasks/task_01abc/attempts/input-fedcba987654/artifacts/input.json",
]
REFUSED = [
    "tenants/a/tasks/t/attempts/att_x/artifacts/review.json",
    "tenants/a/tasks/t/attempts/input-0123456789ab/checkpoints/c/x.tar.gz",
    "tenants/a/tasks/t/attempts/input-0123456789ab/live/x",
    "tenants/a/tasks/t/attempts/input-0123456789ab/logs/x",
    "other/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "tenants/a/b/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "tenants/tasks/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "tenants/a/tasks/t/attempts/input-0123456789AB/artifacts/review.json",
    "tenants/a/tasks/t/attempts/input-0123456789a/artifacts/review.json",
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/",
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/../x",
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.files/a/b.md",
    "tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/other/draft.md",
    "tenants/a/tasks/t/attempts/att_x/artifacts/attempts/input-0123456789ab/artifacts/x",
    "/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
]


@pytest.mark.parametrize("key", ADMITTED)
def test_the_writer_creates_a_key_of_the_conditions_shape(key):
    writer = InMemoryInputWriter()
    assert key_refusal(key) is None
    assert writer.create(key, b"{}", "application/json") == f"gs://{writer.bucket}/{key}"


@pytest.mark.parametrize("key", REFUSED)
def test_the_writer_refuses_a_key_outside_the_conditions_shape(key):
    writer = InMemoryInputWriter()
    calls: list[str] = []
    writer.before_create = calls.append
    assert key_refusal(key) is not None
    with pytest.raises(inputwriter.InputKeyRefused):
        writer.create(key, b"{}", "application/json")
    assert calls == [] and writer.objects == {}


def test_the_gcs_writer_refuses_before_it_builds_a_client():
    class NoClient:
        def bucket(self, name):
            raise AssertionError("GCS was called for a refused key")

    gcs = inputwriter.GcsInputWriter("b", client=NoClient())
    with pytest.raises(inputwriter.InputKeyRefused):
        gcs.create(REFUSED[0], b"{}", "application/json")


def test_the_gcs_writer_creates_only(monkeypatch):
    uploads: list[dict[str, Any]] = []

    class Blob:
        custom_time = None

        def upload_from_string(self, data, *, content_type, if_generation_match):
            uploads.append({"data": data, "content_type": content_type,
                            "if_generation_match": if_generation_match,
                            "custom_time": self.custom_time})

    class Bucket:
        def blob(self, key):
            return Blob()

    class Client:
        def bucket(self, name):
            return Bucket()

    gcs = inputwriter.GcsInputWriter("b", client=Client())
    assert gcs.create(ADMITTED[0], b"{}", "application/json") == f"gs://b/{ADMITTED[0]}"
    assert uploads[0]["if_generation_match"] == 0 and uploads[0]["custom_time"] is not None


def test_the_nonce_is_fresh_and_of_the_conditions_shape():
    nonces = {inputwriter.new_input_attempt() for _ in range(50)}
    assert len(nonces) == 50
    for nonce in nonces:
        assert key_refusal(inputwriter.input_key("a", "t", nonce, "review.json")) is None
