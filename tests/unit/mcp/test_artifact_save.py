"""`swarm_artifact` saves a task's image artifacts to a local file (#2182).

A Claude Code session could list a lane's screenshots and not look at one:
`/artifacts/content` answers `binary` with no bytes for a PNG, so on
2026-10-23 a mock-up review was re-rendered locally instead. With `save_to`,
an image artifact is read through `/artifacts/raw` and written to
`<save_to>/<task_id>/<name>`, which the session reads with its own Read tool.

AGAINST THE REAL swarm-api, over an in-memory Firestore and object store, as
`test_swarm_says_what_happened.py` builds it, so the tenant boundary and the
image classification are the route's and not a fake's. Offline: no
credentials, no emulator, no network.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import urllib.error
from pathlib import Path

import pytest

from swarm_mcp import checkout, server
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmError

from control_plane.conftest import PROJECT, seed_task
from test_swarm_says_what_happened import AUTH, TENANT, World, swarm, world  # noqa: F401

BUCKET = f"swarm-artifacts-{PROJECT}"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8 + b"\x00tail"


class _Headed(io.BytesIO):
    """An opener's answer with the response headers a real one carries."""

    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = headers

    def __enter__(self) -> "_Headed":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture()
def requests(world, monkeypatch) -> list[str]:
    """Route the client to the real application, keeping headers, and record each path."""
    seen: list[str] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        seen.append(path)
        headers = {**dict(req.headers), **AUTH}
        response = world.api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers,
                io.BytesIO(response.content),
            )
        return _Headed(response.content, response.status_code, dict(response.headers))

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return seen


@pytest.fixture()
def here(tmp_path, monkeypatch) -> Path:
    """The bridge's working directory, where `save_to` may point."""
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setenv(checkout.CHECKOUT_DIR_ENV, str(root))
    return root


def _finished(world: World, task_id: str, files: dict[str, bytes | str], *, tenant: str = TENANT) -> None:
    """A terminal task whose manifest and bucket agree, as a real run leaves them."""
    seed_task(world.db, task_id=task_id, tenant_id=tenant, state="SUCCEEDED")
    entries = []
    for name, body in files.items():
        data = body.encode("utf-8") if isinstance(body, str) else body
        key = f"tenants/{tenant}/tasks/{task_id}/attempts/att_1/artifacts/{name}"
        world.objects.put(key, data)
        entries.append({"name": name, "bytes": len(data), "uri": f"gs://{BUCKET}/{key}"})
    world.set(
        task_id, result_summary={"artifacts": entries, "artifact_bytes": sum(e["bytes"] for e in entries)}
    )


def _call(swarm, args: dict) -> dict:
    return json.loads(server._call(swarm, "swarm_artifact", args))


def test_a_png_artifact_saves_to_a_file_whose_bytes_are_the_served_bytes(swarm, world, requests, here):
    task_id = "task_0000000000savepng0"
    _finished(world, task_id, {"shots/light-00.png": PNG})

    reply = _call(swarm, {"task_id": task_id, "name": "shots/light-00.png", "save_to": str(here / "shots")})

    target = (here / "shots" / task_id / "shots" / "light-00.png").resolve()
    assert reply == {
        "status": "saved",
        "path": str(target),
        "bytes": len(PNG),
        "content_type": "image/png",
        "task_id": task_id,
        "name": "shots/light-00.png",
    }, reply
    assert target.read_bytes() == PNG
    assert any(path.startswith(f"/v1/tasks/{task_id}/artifacts/raw?") for path in requests), requests


def test_a_relative_save_to_is_under_the_working_directory(swarm, world, requests, here):
    task_id = "task_0000000000saverel0"
    _finished(world, task_id, {"a.webp": b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x01" * 64})

    reply = _call(swarm, {"task_id": task_id, "name": "a.webp", "save_to": "scratch"})

    assert reply["status"] == "saved", reply
    assert reply["content_type"] == "image/webp", reply
    assert Path(reply["path"]) == (here / "scratch" / task_id / "a.webp").resolve()


def test_a_text_artifact_is_still_redacted_and_windowed_with_save_to(swarm, world, requests, here):
    task_id = "task_0000000000savetxt0"
    leaked = "ghp_" + secrets.token_hex(18)
    body = "line one\n" + f"token={leaked}\n" + "x" * 9000 + "\n"
    _finished(world, task_id, {"notes.txt": body})

    reply = _call(
        swarm,
        {"task_id": task_id, "name": "notes.txt", "save_to": str(here), "limit_bytes": 1024},
    )

    assert reply["status"] == "ok", reply
    assert leaked not in reply["content"], reply["content"][:200]
    assert reply["redacted"] is True, reply
    assert reply["truncated"] is True and reply["next_offset"], reply
    assert "path" not in reply, reply
    assert not (here / task_id).exists()
    assert not any("/artifacts/raw" in path for path in requests), requests


def test_an_image_named_artifact_that_is_text_keeps_the_redacted_read(swarm, world, requests, here):
    """The route classifies by magic bytes too: a `.png` holding text is text."""
    task_id = "task_0000000000savefake"
    leaked = "ghp_" + secrets.token_hex(18)
    _finished(world, task_id, {"fake.png": f"not an image, token={leaked}\n"})

    reply = _call(swarm, {"task_id": task_id, "name": "fake.png", "save_to": str(here)})

    assert reply["status"] == "ok", reply
    assert leaked not in reply["content"], reply
    assert not (here / task_id).exists()


def test_a_binary_that_is_not_an_image_is_not_saved(swarm, world, requests, here):
    task_id = "task_0000000000savebin0"
    _finished(world, task_id, {"blob.bin": b"ab\x00cd" * 40})

    reply = _call(swarm, {"task_id": task_id, "name": "blob.bin", "save_to": str(here)})

    assert reply["status"] == "binary", reply
    assert "content" not in reply or not reply["content"], reply
    assert "path" not in reply, reply
    assert not (here / task_id).exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.png", "shots/../../escape.png", "/etc/escape.png", "shots\\..\\..\\escape.png", "C:/x.png"],
)
def test_a_name_that_could_write_outside_save_to_is_refused_before_any_request(swarm, requests, here, name):
    with pytest.raises(SwarmError) as caught:
        _call(swarm, {"task_id": "task_0000000000savename", "name": name, "save_to": str(here)})

    assert "Nothing was read" in str(caught.value), str(caught.value)
    assert requests == []
    assert list(here.iterdir()) == []


def test_a_save_to_outside_the_working_directory_is_refused_before_any_request(swarm, requests, here, tmp_path):
    outside = tmp_path / "elsewhere"

    for save_to in (str(outside), "../elsewhere", str(here / ".." / "elsewhere")):
        with pytest.raises(SwarmError) as caught:
            _call(swarm, {"task_id": "task_0000000000saveout0", "name": "a.png", "save_to": save_to})
        assert "outside" in str(caught.value), str(caught.value)

    assert requests == []
    assert not outside.exists()


def test_a_save_to_that_symlinks_out_of_the_working_directory_is_refused(swarm, requests, here, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (here / "link").symlink_to(outside)

    with pytest.raises(SwarmError) as caught:
        _call(swarm, {"task_id": "task_0000000000savelink", "name": "a.png", "save_to": str(here / "link")})
    assert "outside" in str(caught.value), str(caught.value)

    assert requests == []
    assert list(outside.iterdir()) == []


def test_a_task_id_that_is_not_one_path_segment_is_refused_before_any_request(swarm, requests, here):
    with pytest.raises(SwarmError) as caught:
        _call(swarm, {"task_id": "../task_x", "name": "a.png", "save_to": str(here)})
    assert "Nothing was read" in str(caught.value), str(caught.value)

    assert requests == []


def test_another_tenants_task_errors_exactly_as_the_text_read_does(swarm, world, requests, here):
    task_id = "task_0000000000saveothr"
    _finished(world, task_id, {"shots/light-00.png": PNG}, tenant="other")

    with pytest.raises(SwarmError) as text_read:
        _call(swarm, {"task_id": task_id, "name": "shots/light-00.png"})
    with pytest.raises(SwarmError) as saved:
        _call(swarm, {"task_id": task_id, "name": "shots/light-00.png", "save_to": str(here)})

    assert text_read.value.status == 404, str(text_read.value)
    assert saved.value.status == text_read.value.status, str(saved.value)
    assert saved.value.code == text_read.value.code, (saved.value.code, text_read.value.code)
    assert not (here / task_id).exists()


def test_the_tool_offers_save_to_and_says_the_saved_file_is_read_with_read():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_artifact")
    assert "save_to" in tool["inputSchema"]["properties"]
    assert set(tool["inputSchema"]["required"]) == {"task_id", "name"}
    assert "Read tool" in tool["description"], tool["description"]


def test_a_save_to_in_the_claude_scratchpad_is_accepted(swarm, world, requests, here, tmp_path, monkeypatch):
    """The session's scratchpad sits under `<tmp>/claude-<uid>`, outside the project."""
    from swarm_mcp import artifacts

    temp = tmp_path / "tmp"
    monkeypatch.setattr(artifacts.tempfile, "gettempdir", lambda: str(temp))
    scratchpad = temp / f"claude-{os.getuid()}" / "project" / "session" / "scratchpad"
    task_id = "task_0000000000savepad0"
    _finished(world, task_id, {"shot.gif": b"GIF89a" + b"\x01" * 64})

    reply = _call(swarm, {"task_id": task_id, "name": "shot.gif", "save_to": str(scratchpad)})

    assert reply["status"] == "saved", reply
    assert Path(reply["path"]).read_bytes() == b"GIF89a" + b"\x01" * 64
    # The control: another user's directory beside it is not the scratchpad.
    with pytest.raises(SwarmError) as caught:
        _call(swarm, {"task_id": task_id, "name": "shot.gif", "save_to": str(temp / "claude-0x" / "s")})
    assert "outside" in str(caught.value), str(caught.value)
