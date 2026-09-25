"""Outputs, as the Artifacts tab reads them: kinds and roles in the listing, and the raw bytes.

#184, owner decisions 2026-09-25. The drawer listed artifacts as file names
with "copy gsutil"; nothing rendered an image, and the viewer was guessed from
the name in the browser. What is pinned here:

  1. THE LISTING says, per entry, the attempt it came from, a `kind` and a
     `content_type` from the one name table, and a `role` naming the agent's
     own stdout, stderr and transcript -- from `result_summary.agent_streams`,
     or the runner's naming convention for an attempt made before it existed.
     It reads no object.
  2. `GET /v1/tasks/{id}/artifacts/raw` serves the BYTES through the API --
     never a signed URL -- so the tenant check and read-time redaction apply:
     an image as stored, text as text/plain and redacted whatever its name,
     anything else as an attachment. Every error is the JSON envelope, sent
     before the first byte; a listed artifact whose object is gone is a 410,
     not a 404.

Offline: FakeFirestore and the in-memory object reader. No credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api.redaction import redact

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"

#: A credential shape the redaction table recognises (`github_token`).
GH_TOKEN = "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"

#: A PNG: its magic, then bytes that include a NUL and a token-shaped run. An
#: image passes through AS STORED (owner's decision), so the token must survive.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 16 + GH_TOKEN.encode()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 32
ZIP = b"PK\x03\x04" + b"\x00" * 64

#: Sentinel: leave `result_summary.agent_streams` out entirely (a pre-#184 attempt).
UNSET = object()


def prefix(tenant="eng", task="task_a", attempt="att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}"


def artifact_key(name, **kw) -> str:
    return f"{prefix(**kw)}/artifacts/{name}"


def a_finished_task(
    db,
    objects,
    *,
    files: dict[str, bytes | str],
    runner_profile: str = "claude-code",
    agent_streams=UNSET,
    tenant: str = "eng",
    task: str = "task_a",
    attempt: str = "att_1",
    store_objects: bool = True,
) -> dict:
    """A terminal task whose manifest lists `files`, as `_upload_outputs` writes it."""
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED", runner_profile=runner_profile)
    entries = []
    for name, body in files.items():
        raw = body.encode("utf-8") if isinstance(body, str) else body
        key = artifact_key(name, tenant=tenant, task=task, attempt=attempt)
        if store_objects:
            objects.put(key, raw)
        entries.append({"name": name, "bytes": len(raw), "uri": f"gs://{BUCKET}/{key}"})
    summary: dict = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
    }
    if agent_streams is not UNSET:
        summary["agent_streams"] = agent_streams
    db.docs[f"tasks/{task}"]["result_summary"] = summary
    db.docs[f"tasks/{task}"]["completed_at"] = datetime.now(timezone.utc)
    return summary


CLAUDE_STREAMS = {
    "stdout": "claude-code.stdout.log",
    "stderr": "claude-code.stderr.log",
    "transcript": "claude-transcript.json",
    "transcript_skipped": None,
}

ALL_KINDS = {
    "synthesis.md": "# Synthesis\n\nthe answer\n",
    "data.json": '{"a": 1}',
    "events.jsonl": '{"a": 1}\n{"a": 2}\n',
    "claude-code.stdout.log": '{"type":"result","result":"hi"}\n',
    "claude-code.stderr.log": "",
    "claude-transcript.json": '{"type": "result"}',
    "shot.png": PNG,
    "diagram.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
    "bundle.zip": ZIP,
    "Makefile": "all:\n\ttrue\n",
    "notes/deep.txt": "nested\n",
}


def listing(client, task="task_a", user="alice"):
    return client.get(f"/v1/tasks/{task}/artifacts", headers=auth_header(user))


def raw(client, name, task="task_a", user="alice", **params):
    return client.get(
        f"/v1/tasks/{task}/artifacts/raw",
        params={"name": name, **params},
        headers=auth_header(user),
    )


# --------------------------------------------------------------------------
# 1. The listing
# --------------------------------------------------------------------------

def test_each_listed_artifact_says_its_attempt_kind_content_type_and_role(client, db, objects):
    a_finished_task(db, objects, files=ALL_KINDS, agent_streams=CLAUDE_STREAMS)

    response = listing(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["complete"] is True
    assert body["attempt_id"] == "att_1"
    rows = {row["name"]: row for row in body["artifacts"]}
    assert set(rows) == set(ALL_KINDS)

    expected = {
        "synthesis.md": ("markdown", "text/markdown", None),
        "data.json": ("json", "application/json", None),
        "events.jsonl": ("ndjson", "application/x-ndjson", None),
        "claude-code.stdout.log": ("log", "text/plain", "agent_stdout"),
        "claude-code.stderr.log": ("log", "text/plain", "agent_stderr"),
        "claude-transcript.json": ("json", "application/json", "agent_transcript"),
        "shot.png": ("image", "image/png", None),
        "diagram.svg": ("text", "text/plain", None),
        "bundle.zip": ("binary", None, None),
        "Makefile": ("text", "text/plain", None),
        "notes/deep.txt": ("text", "text/plain", None),
    }
    for name, (kind, content_type, role) in expected.items():
        row = rows[name]
        assert (row["kind"], row["content_type"], row["role"]) == (kind, content_type, role), name
        assert row["attempt_id"] == "att_1", name
        # The existing fields are unchanged.
        assert row["uri"] == f"gs://{BUCKET}/{artifact_key(name)}"
        assert row["bytes"] == len(ALL_KINDS[name] if isinstance(ALL_KINDS[name], bytes)
                                   else ALL_KINDS[name].encode())


def test_the_listing_reads_no_object(client, db, objects):
    """Kinds come from NAMES. A listing that opened every object to decide would
    turn one request into N downloads."""
    a_finished_task(db, objects, files=ALL_KINDS, agent_streams=CLAUDE_STREAMS,
                    store_objects=False)
    objects.fail_on("tenants/")

    response = listing(client)
    assert response.status_code == 200, response.text
    assert len(response.json()["artifacts"]) == len(ALL_KINDS)


def test_a_running_task_lists_nothing_and_says_it_is_not_complete(client, db, objects):
    """Artifacts are uploaded when the attempt ends. `complete: false` is what
    lets the UI say "uploaded when the attempt ends" instead of "none"."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING", runner_profile="claude-code")

    body = listing(client).json()
    assert body["complete"] is False
    assert body["artifacts"] == []
    assert body["attempt_id"] is None


def test_a_pre_change_attempt_gets_its_roles_from_the_runners_naming_convention(
    client, db, objects
):
    """task_73b5f4d9ca3641fbb914 and every attempt before #184: no
    `agent_streams` was written, and the runner's own files are named by its
    convention."""
    a_finished_task(db, objects, files={
        "claude-code.stdout.log": '{"type":"result"}\n',
        "claude-code.stderr.log": "",
        "claude-transcript.json": "{}",
        "report.md": "# r\n",
    })

    rows = {row["name"]: row["role"] for row in listing(client).json()["artifacts"]}
    assert rows == {
        "claude-code.stdout.log": "agent_stdout",
        "claude-code.stderr.log": "agent_stderr",
        "claude-transcript.json": "agent_transcript",
        "report.md": None,
    }


def test_a_runner_with_no_agent_cli_gives_no_roles(client, db, objects):
    """`agent_streams: null` says there is no agent CLI; a file that happens to
    carry a CLI runner's name is just a file."""
    a_finished_task(db, objects, runner_profile="mock", agent_streams=None, files={
        "output.txt": "done\n",
        "claude-code.stdout.log": "an agent's file, not a stream\n",
    })

    rows = {row["name"]: row["role"] for row in listing(client).json()["artifacts"]}
    assert rows == {"output.txt": None, "claude-code.stdout.log": None}


def test_a_declared_stream_that_was_not_uploaded_has_no_role(client, db, objects):
    a_finished_task(
        db, objects,
        files={"claude-code.stderr.log": "w\n"},
        agent_streams={"stdout": None, "stderr": "claude-code.stderr.log",
                       "transcript": None, "transcript_skipped": "too_large"},
    )
    rows = {row["name"]: row["role"] for row in listing(client).json()["artifacts"]}
    assert rows == {"claude-code.stderr.log": "agent_stderr"}


def test_another_tenants_listing_is_the_404_a_missing_task_gets(client, db, objects):
    a_finished_task(db, objects, files={"synthesis.md": "# s\n"})
    response = listing(client, user="bob")
    assert response.status_code == 404, response.text
    assert "synthesis" not in response.text


def test_the_content_route_says_the_kind_and_content_type(client, db, objects):
    a_finished_task(db, objects, files={"synthesis.md": "# s\n", "data.json": "{}"})

    body = client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": "synthesis.md"},
        headers=auth_header("alice"),
    ).json()
    assert body["status"] == "ok"
    assert (body["kind"], body["content_type"]) == ("markdown", "text/markdown")

    body = client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": "data.json"},
        headers=auth_header("alice"),
    ).json()
    assert (body["kind"], body["content_type"]) == ("json", "application/json")


# --------------------------------------------------------------------------
# 2. The raw bytes
# --------------------------------------------------------------------------

SAFETY_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "private, no-store",
    "content-security-policy": "default-src 'none'; img-src 'self'; sandbox",
    "referrer-policy": "no-referrer",
}


def assert_safety_headers(response) -> None:
    for name, value in SAFETY_HEADERS.items():
        assert response.headers.get(name) == value, (name, response.headers.get(name))
    # CHUNKED, never Content-Length: Cloud Run refuses an unchunked HTTP/1
    # response over 32 MiB, and uvicorn chunks exactly when none is declared.
    assert "content-length" not in response.headers


def test_a_png_is_served_inline_as_an_image_byte_for_byte(client, db, objects):
    a_finished_task(db, objects, files={"shot.png": PNG})

    response = raw(client, "shot.png", disposition="inline")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG, "an image passes through as stored"
    assert response.headers["content-disposition"] == 'inline; filename="shot.png"'
    assert response.headers["x-artifact-bytes"] == str(len(PNG))
    assert response.headers["x-artifact-kind"] == "image"
    assert response.headers["x-swarm-redaction"] == "not-applied"
    assert_safety_headers(response)


def test_an_image_is_an_attachment_unless_inline_is_asked_for(client, db, objects):
    a_finished_task(db, objects, files={"photo.jpg": JPEG})

    response = raw(client, "photo.jpg")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"] == 'attachment; filename="photo.jpg"'


def test_text_is_served_as_plain_text_and_redacted_whatever_its_name(client, db, objects):
    """An `.html` an agent wrote must not render in the API's origin, and a
    token in a text artifact must not leave it: text/plain always, redacted."""
    page = f"<html><script>alert(1)</script>token {GH_TOKEN}</html>\n"
    a_finished_task(db, objects, files={"page.html": page, "data.json": '{"k": 1}'})

    response = raw(client, "page.html", disposition="inline")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-artifact-kind"] == "text"
    assert response.headers["x-swarm-redaction"] == "applied"
    assert response.headers["content-disposition"] == 'inline; filename="page.html"'
    assert GH_TOKEN not in response.text
    assert "Aa1Bb2Cc3" not in response.text
    assert response.text == redact(page).text
    assert_safety_headers(response)

    json_response = raw(client, "data.json")
    assert json_response.headers["content-type"] == "text/plain; charset=utf-8"
    assert json_response.headers["content-disposition"] == 'attachment; filename="data.json"'


def test_an_image_name_over_text_bytes_is_text(client, db, objects):
    """The name is a hint; the magic bytes decide."""
    a_finished_task(db, objects, files={"fake.png": f"not an image {GH_TOKEN}\n"})

    response = raw(client, "fake.png", disposition="inline")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-artifact-kind"] == "text"
    assert GH_TOKEN not in response.text


def test_an_svg_is_never_served_as_an_image(client, db, objects):
    svg = "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    a_finished_task(db, objects, files={"diagram.svg": svg})

    response = raw(client, "diagram.svg", disposition="inline")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-artifact-kind"] == "text"


def test_other_binary_is_always_an_attachment_and_says_it_is_unredacted(client, db, objects):
    a_finished_task(db, objects, files={"bundle.zip": ZIP})

    response = raw(client, "bundle.zip", disposition="inline")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="bundle.zip"'
    assert response.headers["x-artifact-kind"] == "binary"
    assert response.headers["x-swarm-redaction"] == "not-applied"
    assert response.content == ZIP
    assert_safety_headers(response)


def test_a_nested_artifact_downloads_under_its_basename(client, db, objects):
    a_finished_task(db, objects, files={"reports/q3.md": "# q3\n"})

    response = raw(client, "reports/q3.md")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == 'attachment; filename="q3.md"'
    assert response.text == "# q3\n"


def test_an_empty_text_artifact_is_an_empty_200(client, db, objects):
    a_finished_task(db, objects, files={"empty.log": ""})

    response = raw(client, "empty.log")
    assert response.status_code == 200, response.text
    assert response.content == b""
    assert response.headers["x-artifact-bytes"] == "0"


def test_a_listed_artifact_whose_object_is_gone_is_a_410(client, db, objects):
    """Listed, and not in the bucket: retention reclaimed it, or its upload
    never completed. That is not "this task lists no such artifact"."""
    a_finished_task(db, objects, files={"synthesis.md": "# s\n"}, store_objects=False)

    response = raw(client, "synthesis.md")
    assert response.status_code == 410, response.text
    body = response.json()
    assert body["code"] == "artifact_gone"
    assert body["detail"] == {
        "task_id": "task_a",
        "name": "synthesis.md",
        "attempt_id": "att_1",
        "uri": f"gs://{BUCKET}/{artifact_key('synthesis.md')}",
    }


def test_a_name_the_manifest_does_not_list_is_a_404_naming_what_it_does(client, db, objects):
    a_finished_task(db, objects, files={"synthesis.md": "# s\n"})

    response = raw(client, "../logs/stdout.log")
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "not_found"
    assert body["detail"]["known_artifacts"] == ["synthesis.md"]
    assert body["detail"]["complete"] is True


def test_an_unreadable_store_is_a_503_before_any_byte(client, db, objects):
    a_finished_task(db, objects, files={"synthesis.md": "# s\n"})
    objects.fail_on(artifact_key("synthesis.md"))

    response = raw(client, "synthesis.md")
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "upstream_unavailable"
    assert "content-disposition" not in response.headers


def test_a_disposition_that_is_neither_inline_nor_attachment_is_a_422(client, db, objects):
    a_finished_task(db, objects, files={"synthesis.md": "# s\n"})

    response = raw(client, "synthesis.md", disposition="render")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_another_tenants_raw_artifact_is_a_404_and_no_byte_of_it(client, db, objects):
    a_finished_task(db, objects, files={"secret.md": "research's answer\n"})

    response = raw(client, "secret.md", user="bob")
    assert response.status_code == 404, response.text
    assert "research's answer" not in response.text
    assert "secret.md" not in response.text


# --------------------------------------------------------------------------
# Windowed redaction: every token whole, the concatenation exact
# --------------------------------------------------------------------------

@pytest.fixture
def small_windows(client):
    """Serve in 64-byte windows, through FastAPI's own injection point."""
    from swarm_api.agent_output import AgentOutputService
    from swarm_api.routes.tasks import agent_output_service

    context = client.app.state.ctx
    client.app.dependency_overrides[agent_output_service] = lambda: AgentOutputService(
        context.inspection, chunk_bytes=64
    )
    yield
    client.app.dependency_overrides.pop(agent_output_service, None)


def test_a_text_body_served_in_windows_is_the_redaction_of_the_whole(
    client, db, objects, small_windows
):
    """A token cut in half by a window boundary matches no rule in either half.
    Every window is cut at whitespace and the remainder carried, so the body is
    exactly what redacting the whole object gives -- tokens at every offset."""
    lines = [f"line {n:03d} key={GH_TOKEN} and more words\n" for n in range(40)]
    text = "".join(lines)
    a_finished_task(db, objects, files={"secrets.log": text})

    response = raw(client, "secrets.log")
    assert response.status_code == 200, response.text
    assert GH_TOKEN not in response.text
    assert "Aa1Bb2Cc3" not in response.text
    assert response.text == redact(text).text


def test_a_whitespace_free_run_is_carried_until_it_ends(client, db, objects, small_windows):
    blob = "A" * 300 + GH_TOKEN + "B" * 300 + "\ntail\n"
    a_finished_task(db, objects, files={"blob.txt": blob})

    response = raw(client, "blob.txt")
    assert response.text == redact(blob).text


def test_a_multibyte_character_across_a_window_boundary_survives(
    client, db, objects, small_windows
):
    text = "é" * 100 + "\n"  # 200 bytes, a two-byte character at every boundary
    a_finished_task(db, objects, files={"accents.txt": text})

    response = raw(client, "accents.txt")
    assert response.text == text


def test_a_failure_mid_download_ends_short_of_the_declared_size(
    client, db, objects, small_windows
):
    """After the status line is sent it cannot change; a body shorter than
    `X-Artifact-Bytes` is how a client sees it was cut."""
    body = bytes(range(256)) * 4
    a_finished_task(db, objects, files={"bundle.zip": ZIP + body})

    real_read = objects.read_range
    key = artifact_key("bundle.zip")

    def fail_after_first(k, *, offset, length):
        if k == key and offset > 0:
            from swarm_api.objects import ObjectUnreadable

            raise ObjectUnreadable(k, "injected failure after the first window")
        return real_read(k, offset=offset, length=length)

    objects.read_range = fail_after_first
    response = raw(client, "bundle.zip")

    total = len(ZIP + body)
    assert int(response.headers["x-artifact-bytes"]) == total
    assert len(response.content) < total
    assert response.content == (ZIP + body)[: len(response.content)]
