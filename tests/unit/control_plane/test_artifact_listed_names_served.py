"""The read routes serve every artifact name the listing serves.

`GET /v1/tasks/{id}/artifacts` lists any printable name the worker uploaded
(`attemptreads._ARTIFACT_NAME`). `/artifacts/content` and `/artifacts/raw`
rebuild the object key from that name, and used to pass each of its segments
through `objects.safe_segment` -- an id check that refuses a leading
underscore, a space or a non-ASCII letter -- so a listed `_export.yaml`
answered 422 on every read. A name segment is now refused only for what can
leave the attempt's artifacts prefix, and the tenant, task and attempt ids
that build the prefix are still held to `safe_segment`.
"""

from __future__ import annotations

import pytest

from swarm_api.objects import UnsafeKeySegment, safe_artifact_name, safe_segment

from .conftest import auth_header
from .test_artifact_outputs import a_finished_task, artifact_key, listing, raw

SERVED = ["_export.yaml", "dir/_x.json", "a b.txt", "résumé.md", "notes/日本.txt", "a+b.txt"]

#: Names a manifest may record that must still never become a key.
REFUSED = ["..", "a/../b", ".", "a/./b", "a//b", "/abs.txt", "dir/", "a\x01b.txt",
           "a\x7fb", "a\\b.txt"]


def content(client, name, task="task_a"):
    return client.get(
        f"/v1/tasks/{task}/artifacts/content",
        params={"name": name},
        headers=auth_header("alice"),
    )


@pytest.mark.parametrize("name", SERVED)
def test_a_listed_name_is_served_by_raw_and_content(client, db, objects, name):
    a_finished_task(db, objects, files={name: "the body\n"})
    assert [a["name"] for a in listing(client).json()["artifacts"]] == [name]

    response = raw(client, name)
    assert response.status_code == 200, response.text
    assert response.text == "the body\n"
    # The download filename stays one quoted ASCII token whatever the name holds.
    disposition = response.headers["content-disposition"]
    assert disposition.isascii() and disposition.count('"') == 2

    response = content(client, name)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("name", REFUSED)
def test_a_name_that_could_leave_the_prefix_is_refused_and_nothing_is_read(
    client, db, objects, name
):
    a_finished_task(db, objects, files={name: "x"}, store_objects=False)
    # Something IS at the key the name would escape to; it must not be served.
    objects.put(artifact_key("b"), b"other")
    objects.put("tenants/eng/tasks/task_a/attempts/att_1/b", b"other")

    assert raw(client, name).status_code == 422
    assert content(client, name).status_code == 422


def test_an_attempt_id_with_a_leading_underscore_is_still_refused(client, db, objects):
    a_finished_task(db, objects, files={"_export.yaml": "x"}, attempt="_att")

    assert raw(client, "_export.yaml").status_code == 422
    assert content(client, "_export.yaml").status_code == 422


@pytest.mark.parametrize("name", SERVED)
def test_safe_artifact_name_accepts_every_listed_shape(name):
    assert safe_artifact_name(name) == name


@pytest.mark.parametrize("name", [*REFUSED, "", None])
def test_safe_artifact_name_refuses_what_can_escape(name):
    with pytest.raises(UnsafeKeySegment):
        safe_artifact_name(name)


@pytest.mark.parametrize("value", ["_tenant", "_task", "_att", "a b", "é", "a/b", "..", ""])
def test_safe_segment_stays_as_strict_for_ids(value):
    with pytest.raises(UnsafeKeySegment):
        safe_segment(value, what="tenant id")
