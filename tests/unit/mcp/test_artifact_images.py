"""`swarm_artifact` shows a task's image artifact to the session as MCP image content (#2136).

BGD, 2026-10-26: a Claude Code session can look at a lane's `shots/*.png`
through the sc plugin, not only in the console. `/artifacts/content` answers a
PNG as `binary` with no bytes; for a name with an image extension the bridge
now reads `/artifacts/raw?disposition=inline` and answers the bytes as an
`image` content block beside the usual metadata, `redaction: not-applied`.

WHAT MUST NOT MOVE. A text artifact is the redacted window it always was, a
binary that is not an image is still `binary` with no bytes, and nothing that
is not an image BY ITS MAGIC BYTES leaves through the image path -- the
route's classification is checked again here, so a route that said `image`
for a text body would still not put that body in front of the session.

AGAINST THE REAL swarm-api, over an in-memory Firestore and object store, as
`test_artifact_save.py` builds it. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import secrets
import urllib.error
import urllib.request

import pytest

from swarm_mcp import artifacts, server
from swarm_mcp import client as mcp_client
from swarm_mcp.client import RawBody, SwarmClient

from test_artifact_save import PNG, _finished, _Headed
from test_swarm_says_what_happened import AUTH, TENANT, World, world  # noqa: F401

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + bytes(range(256)) * 4 + b"\xff\xd9"


@pytest.fixture()
def seen(world, monkeypatch) -> list[tuple[str, dict[str, str]]]:
    """Route the client to the real application and record each (path, headers)."""
    calls: list[tuple[str, dict[str, str]]] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        sent = {key.lower(): value for key, value in req.header_items()}
        calls.append((path, sent))
        headers = {**dict(req.headers), **AUTH}
        response = world.api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers,
                io.BytesIO(response.content),
            )
        return _Headed(response.content, response.status_code, dict(response.headers))

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return calls


@pytest.fixture()
def swarm(seen) -> SwarmClient:
    """A real client that names the tenant, as the bridge's does."""
    return SwarmClient(base_url="http://api.invalid", tenant=TENANT)


def _image(answer) -> tuple[dict, dict]:
    """The metadata and the image block of an image answer, as the host receives them."""
    assert isinstance(answer, artifacts.ImageAnswer), answer
    blocks = answer.content()
    assert [block["type"] for block in blocks] == ["text", "image"], blocks
    return json.loads(blocks[0]["text"]), blocks[1]


def test_a_png_artifact_comes_back_as_image_content_with_its_bytes(swarm, world, seen):
    task_id = "task_00000000000imgpng0"
    _finished(world, task_id, {"shots/light-00.png": PNG})

    meta, block = _image(server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "shots/light-00.png"}))

    assert block["mimeType"] == "image/png", block["mimeType"]
    assert base64.b64decode(block["data"]) == PNG
    assert meta["status"] == "image", meta
    assert meta["name"] == "shots/light-00.png"
    assert meta["bytes"] == len(PNG)
    assert meta["content_type"] == "image/png"
    assert meta["redaction"] == "not-applied"
    raw = [path for path, _ in seen if "/artifacts/raw?" in path]
    assert len(raw) == 1 and "disposition=inline" in raw[0], seen


def test_a_jpeg_artifact_comes_back_as_image_content_with_its_bytes(swarm, world, seen):
    task_id = "task_00000000000imgjpg0"
    _finished(world, task_id, {"shots/dark.jpg": JPEG})

    meta, block = _image(server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "shots/dark.jpg"}))

    assert block["mimeType"] == "image/jpeg"
    assert base64.b64decode(block["data"]) == JPEG
    assert meta["content_type"] == "image/jpeg" and meta["bytes"] == len(JPEG), meta


def test_a_text_artifact_is_unchanged_and_redacted(swarm, world, seen):
    task_id = "task_00000000000imgtxt0"
    leaked = "ghp_" + secrets.token_hex(18)
    _finished(world, task_id, {"notes.txt": f"line one\ntoken={leaked}\n"})

    answer = server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "notes.txt"})

    assert isinstance(answer, str), answer
    reply = json.loads(answer)
    assert reply["status"] == "ok" and reply["redacted"] is True, reply
    assert leaked not in answer
    assert not any("/artifacts/raw" in path for path, _ in seen), seen


def test_an_image_named_text_artifact_is_the_redacted_text_read(swarm, world, seen):
    """A `.png` holding text is text, by its magic bytes: never an image block."""
    task_id = "task_00000000000imgfake"
    leaked = "ghp_" + secrets.token_hex(18)
    _finished(world, task_id, {"fake.png": f"not an image, token={leaked}\n"})

    answer = server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "fake.png"})

    assert isinstance(answer, str), answer
    assert json.loads(answer)["status"] == "ok"
    assert leaked not in answer


def test_a_non_image_binary_is_still_binary_with_no_bytes(swarm, world, seen):
    task_id = "task_00000000000imgbin0"
    _finished(world, task_id, {"blob.bin": b"ab\x00cd" * 40, "fake.gif": b"ab\x00cd" * 40})

    for name in ("blob.bin", "fake.gif"):
        answer = server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": name})
        assert isinstance(answer, str), (name, answer)
        reply = json.loads(answer)
        assert reply["status"] == "binary", reply
        assert not reply.get("content"), reply
    # `blob.bin` has no image extension: no raw read at all.
    assert not any("/artifacts/raw" in path and "blob.bin" in path for path, _ in seen), seen


def test_a_body_the_route_calls_an_image_but_whose_magic_bytes_are_not_is_never_shown(swarm, monkeypatch):
    """The review's surface: the bridge checks the magic bytes itself."""
    leaked = "ghp_" + secrets.token_hex(18)
    body = f"token={leaked}\n".encode()
    monkeypatch.setattr(
        swarm, "artifact_content",
        lambda *a, **k: {"status": "binary", "total_bytes": len(body), "name": "x.png"},
    )
    monkeypatch.setattr(
        swarm, "artifact_raw",
        lambda *a, **k: RawBody(body, {
            "content-type": "image/png", "x-artifact-kind": "image",
            "x-artifact-bytes": str(len(body)), "x-swarm-redaction": "not-applied",
        }),
    )

    answer = server._call(swarm, "swarm_artifact", {"task_id": "task_00000000000imglie0", "name": "x.png"})

    assert isinstance(answer, str), answer
    assert json.loads(answer)["status"] == "binary"
    assert leaked not in answer


def test_an_image_over_the_cap_answers_its_size_and_the_console_link_not_bytes(swarm, world, seen, monkeypatch):
    task_id = "task_00000000000imgbig0"
    _finished(world, task_id, {"shots/huge.png": PNG})
    monkeypatch.setattr(artifacts, "MAX_INLINE_IMAGE_BYTES", len(PNG) - 1)
    # This world's API has no console origin; a deployment's serves one on the task.
    link = f"https://console.invalid/agents/{task_id}"
    read_task = swarm.task
    monkeypatch.setattr(swarm, "task", lambda tid: {**read_task(tid), "links": {"console": link}})

    answer = server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "shots/huge.png"})

    assert isinstance(answer, str), answer
    reply = json.loads(answer)
    assert reply["status"] == "image_too_large", reply
    assert reply["bytes"] == len(PNG)
    assert reply["max_bytes"] == len(PNG) - 1
    assert reply["redaction"] == "not-applied"
    assert reply["console"] == link, reply
    assert "data" not in reply and "content" not in reply, reply
    assert not any("/artifacts/raw" in path for path, _ in seen), seen


def test_every_read_of_an_image_carries_the_tenant(swarm, world, seen):
    task_id = "task_00000000000imgtnt0"
    _finished(world, task_id, {"shot.png": PNG})

    server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "shot.png"})

    reads = [(path, headers) for path, headers in seen if "/artifacts/" in path]
    assert any("/artifacts/raw?" in path for path, _ in reads), reads
    assert all(headers.get("x-swarm-tenant") == TENANT for _, headers in reads), reads


def test_the_host_receives_an_image_block_over_the_wire(swarm, world, seen, monkeypatch):
    task_id = "task_00000000000imgwire"
    _finished(world, task_id, {"shot.png": PNG})
    monkeypatch.setattr(server, "SwarmClient", lambda tenant=None: swarm)
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "swarm_artifact", "arguments": {"task_id": task_id, "name": "shot.png"}}}
    wire = io.StringIO()

    with contextlib.redirect_stdout(wire):
        server.serve(stdin=iter([json.dumps(call) + "\n"]))

    result = json.loads(wire.getvalue().splitlines()[-1])["result"]
    assert "isError" not in result, result
    assert [block["type"] for block in result["content"]] == ["text", "image"], result
    assert base64.b64decode(result["content"][1]["data"]) == PNG


def test_the_tool_says_image_bytes_are_not_redacted():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_artifact")
    text = " ".join(tool["description"].split())
    assert "NOT redacted" in text, text
    assert "no scanner reads pixels" in text, text


def test_the_plugin_moved_to_a_bridge_that_shows_images():
    """Installed plugins fetch the bridge at the tag their version pins."""
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert manifest["version"] == "0.5.70", manifest["version"]
    assert any("@sc-v0.5.70#" in arg for arg in manifest["mcpServers"]["swarmcloud"]["args"])


def test_as_image_reads_the_image_without_the_text_read_first(swarm, world, seen):
    task_id = "task_00000000000imgasis"
    _finished(world, task_id, {"shot.png": PNG})

    meta, block = _image(
        server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "shot.png", "as_image": True})
    )

    assert base64.b64decode(block["data"]) == PNG and meta["status"] == "image"
    assert not any("/artifacts/content" in path for path, _ in seen), seen


def test_as_image_on_text_is_still_the_redacted_text_read(swarm, world, seen):
    task_id = "task_00000000000imgastx"
    leaked = "ghp_" + secrets.token_hex(18)
    _finished(world, task_id, {"fake.png": f"token={leaked}\n"})

    answer = server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "fake.png", "as_image": True})

    assert isinstance(answer, str), answer
    assert json.loads(answer)["status"] == "ok"
    assert leaked not in answer


def test_save_to_also_refuses_a_body_whose_magic_bytes_are_not_an_image(swarm, monkeypatch, tmp_path):
    from swarm_mcp import checkout

    monkeypatch.setenv(checkout.CHECKOUT_DIR_ENV, str(tmp_path))
    leaked = "ghp_" + secrets.token_hex(18)
    body = f"token={leaked}\n".encode()
    monkeypatch.setattr(swarm, "artifact_content", lambda *a, **k: {"status": "binary", "name": "x.png"})
    monkeypatch.setattr(
        swarm, "artifact_raw",
        lambda *a, **k: RawBody(body, {"content-type": "image/png", "x-artifact-kind": "image"}),
    )

    answer = server._call(
        swarm, "swarm_artifact", {"task_id": "task_00000000000imgsvlie", "name": "x.png", "save_to": str(tmp_path)}
    )

    assert json.loads(answer)["status"] == "binary"
    assert not (tmp_path / "task_00000000000imgsvlie").exists()
