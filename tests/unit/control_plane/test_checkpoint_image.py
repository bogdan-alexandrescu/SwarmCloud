"""A checkpoint's IMAGE member, served as its own bytes (BGD, 2026-11-06).

The Changes view draws an image a step CHANGED from the step's checkpoint:
the patch holds a changed image as a delta against bytes it does not carry.
`GET /v1/tasks/{id}/checkpoints/{n}/files/{path}?format=image` serves a
png, jpeg, webp or gif member byte for byte, and only when the member's
first bytes are that type's signature -- the name alone never decides. The
type comes from the signature, the response is an attachment with
`nosniff`, and the tenant and access checks are the text read's own.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from swarm_api.checkpoint_content import MAX_IMAGE_BYTES, CheckpointContent
from swarm_api.routes.checkpoints import checkpoint_content

from .conftest import auth_header, seed_tenant
from .test_checkpoint_content import (  # noqa: F401 - `objects` overrides conftest's
    objects,
    put_checkpoint,
    seed,
    tar_gz,
)


def png_bytes() -> bytes:
    """A real 2x1 PNG, built here so its signature is the one PNG writes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"\x00\xff\x00\x00\x00\xff\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def image(client, path, user="alice", **params):
    return client.get(
        f"/v1/tasks/task_a/checkpoints/ckpt-00001/files/{path}",
        params={"format": "image", **params},
        headers=auth_header(user),
    )


def test_a_png_whose_bytes_match_is_served_as_image_png(client, db, objects):
    seed(db)
    data = png_bytes()
    put_checkpoint(objects, archive=tar_gz([("repo/mock/hero.png", "file", data)]))

    response = image(client, "repo/mock/hero.png")

    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="hero.png"'
    assert response.headers["content-length"] == str(len(data))
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-swarm-redaction"] == "not-applied"


@pytest.mark.parametrize(
    ("name", "head", "media"),
    [
        ("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32, "image/jpeg"),
        ("a.jpeg", b"\xff\xd8\xff\xdb" + b"\x00" * 32, "image/jpeg"),
        ("a.gif", b"GIF89a" + b"\x00" * 32, "image/gif"),
        ("a.webp", b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32, "image/webp"),
    ],
)
def test_each_image_type_takes_its_type_from_its_signature(client, db, objects, name, head, media):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([(name, "file", head)]))
    response = image(client, name)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == media
    assert response.content == head


def test_a_png_holding_html_is_refused(client, db, objects):
    seed(db)
    page = b"<html><script>alert(document.cookie)</script></html>"
    put_checkpoint(objects, archive=tar_gz([("evil.png", "file", page)]))

    response = image(client, "evil.png")

    assert response.status_code == 415, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert b"<script>" not in response.content
    assert response.json()["code"] == "checkpoint_member_not_an_image"


def test_a_jpeg_signature_under_a_png_name_is_refused(client, db, objects):
    """The signature must be the NAMED type's: a `.png` holding a JPEG is not
    served under either type."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("a.png", "file", b"\xff\xd8\xff\xe0" + b"\x00" * 32)]))
    assert image(client, "a.png").status_code == 415


@pytest.mark.parametrize("name", ["logo.svg", "page.html", "notes.txt", "Makefile"])
def test_an_svg_or_any_other_name_stays_text(client, db, objects, name):
    """An SVG can carry script; it is never an image here, even when it starts
    like one."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([(name, "file", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)]))
    response = image(client, name)
    assert response.status_code == 415, response.text
    assert b"PNG" not in response.content


def test_another_tenants_checkpoint_is_refused_and_no_object_is_touched(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("hero.png", "file", png_bytes())]))
    seed_tenant(db, "research")
    objects.forget()

    response = image(client, "hero.png", user="bob")

    assert response.status_code == 404, response.text
    assert b"PNG" not in response.content
    assert objects.reads == []
    assert objects.lists == []


def test_an_image_over_the_cap_is_refused_before_its_bytes_are_read(client, db, objects, api_context):
    seed(db)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    put_checkpoint(objects, archive=tar_gz([("big.png", "file", big)]))
    client.app.dependency_overrides[checkpoint_content] = lambda: CheckpointContent(
        api_context.inspection, max_image_bytes=32
    )
    try:
        response = image(client, "big.png")
    finally:
        client.app.dependency_overrides.pop(checkpoint_content, None)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["max_image_bytes"] == 32
    assert MAX_IMAGE_BYTES >= 5 * 1024 * 1024


def test_a_missing_image_is_a_404(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("hero.png", "file", png_bytes())]))
    assert image(client, "other.png").status_code == 404


def test_an_unknown_format_is_a_422(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("hero.png", "file", png_bytes())]))
    assert image(client, "hero.png", format="raw").status_code == 422


def test_without_format_the_png_is_still_the_json_binary_answer(client, db, objects):
    """The text read is unchanged: the checkpoint browser's JSON reads keep
    their shape, and a png there is `binary` with no bytes."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("hero.png", "file", png_bytes())]))
    response = client.get(
        "/v1/tasks/task_a/checkpoints/ckpt-00001/files/hero.png", headers=auth_header("alice")
    )
    assert response.status_code == 200
    assert response.json()["status"] == "binary"
    assert response.json()["content"] is None

