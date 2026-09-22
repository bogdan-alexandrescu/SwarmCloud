"""`GET /v1/tasks/{id}/artifacts/content` -- the route that made outputs visible.

THE HOLE THIS CLOSES. `GET /v1/tasks/{id}/artifacts` has listed artifacts by
reference since the manifest was pointed at the right field, and nothing has
ever returned one. "A way to visualize the resulting work" and "a way to
inspect the outputs" were the headline asks of the redesign brief and there was
no server seam under either: a run that produced `synthesis.md`, five step
markdown files and a transcript offered a reader a `gs://` uri and a gcloud
session.

WHAT THESE TESTS ARE FOR, in order of how much they matter:

  1. THE TENANT BOUNDARY. Artifacts are tenant-scoped GCS objects, and a route
     that serves bytes out of one is invariant 9 with a network interface on
     it. `test_another_tenants_artifact_is_not_served` is the test to mutate.
  2. THE CALLER NAMES AN ARTIFACT, NEVER A PATH. Every path-shaped input --
     absolute, traversing, another task's, another tenant's -- resolves to
     nothing, because resolution is exact-equality against the manifest rather
     than any kind of path handling.
  3. TRUNCATION IS REPORTED. A capped window that did not say it was capped
     reads as the whole output, which is the same class of lie as `[]` with a
     200.
  4. REDACTION RUNS AT READ TIME, whatever happened at write time.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"


def prefix(tenant="eng", task="task_a", attempt="att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}"


def artifact_key(name, **kw) -> str:
    return f"{prefix(**kw)}/artifacts/{name}"


def manifest_entry(name: str, body: bytes | str, **kw) -> dict:
    """One row exactly as `AgentLifecycle._upload_outputs` writes it.

    Spelled out here rather than imported: the worker is not installed in the
    API image, and a change on that side must surface as a failure in this file
    rather than be silently followed.
    """
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return {
        "name": name,
        "bytes": len(raw),
        "uri": f"gs://{BUCKET}/{artifact_key(name, **kw)}",
    }


def a_finished_task(
    db,
    objects,
    *,
    tenant="eng",
    task="task_a",
    attempt="att_1",
    files: dict[str, bytes | str] | None = None,
    skipped: list[str] | None = None,
) -> dict:
    """A terminal task whose manifest and bucket agree, as a real run leaves them."""
    files = {"synthesis.md": "# Synthesis\n\nthe agent's answer\n"} if files is None else files
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED")
    entries = []
    for name, body in files.items():
        raw = body.encode("utf-8") if isinstance(body, str) else body
        objects.put(artifact_key(name, tenant=tenant, task=task, attempt=attempt), raw)
        entries.append(manifest_entry(name, body, tenant=tenant, task=task, attempt=attempt))
    summary: dict = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
    }
    if skipped:
        summary["artifacts_skipped"] = skipped
    db.docs[f"tasks/{task}"]["result_summary"] = summary
    db.docs[f"tasks/{task}"]["completed_at"] = datetime.now(timezone.utc)
    return summary


def get(client, task, name, user="alice", **params):
    return client.get(
        f"/v1/tasks/{task}/artifacts/content",
        params={"name": name, **params},
        headers=auth_header(user),
    )


# --------------------------------------------------------------------------
# 1. The tenant boundary. MUTATE THESE FIRST.
# --------------------------------------------------------------------------

def test_another_tenants_artifact_is_not_served(client, db, objects) -> None:
    """THE ONE THAT MATTERS.

    `bob` is in research; the task, the manifest and the object are all eng's.
    The answer must be the same 404 a missing task gets -- a different status
    would confirm that the id exists somewhere else, and any 200 at all would
    be one tenant reading another's agent output.
    """
    a_finished_task(db, objects, tenant="eng", task="task_a")

    response = get(client, "task_a", "synthesis.md", user="bob")

    assert response.status_code == 404, response.text
    body = response.json()
    assert "task_a" in body["message"]
    # Nothing about the artifact leaks through the refusal either: no name, no
    # size, no uri, no bucket.
    assert "synthesis" not in response.text
    assert BUCKET not in response.text


def test_the_owning_tenant_is_served_the_same_artifact(client, db, objects) -> None:
    """The control for the test above: same task, same name, eng's own caller."""
    a_finished_task(db, objects, tenant="eng", task="task_a")

    response = get(client, "task_a", "synthesis.md", user="alice")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["tenant_id"] == "eng"
    assert body["attempt_id"] == "att_1"
    assert body["content"] == "# Synthesis\n\nthe agent's answer\n"
    assert body["truncated"] is False


# --------------------------------------------------------------------------
# 2. A name, never a path
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "supplied",
    [
        # A traversal out of this task's prefix.
        "../../../research/tasks/task_b/attempts/att_9/artifacts/secret.md",
        "../artifacts/synthesis.md",
        # A fully-qualified key, which is what a route that proxied paths would
        # have accepted.
        f"tenants/eng/tasks/task_a/attempts/att_1/artifacts/synthesis.md",
        f"gs://{BUCKET}/tenants/eng/tasks/task_a/attempts/att_1/artifacts/synthesis.md",
        "/etc/passwd",
        # The log objects, which this route must not become a second reader of.
        "../logs/stdout.log",
    ],
)
def test_a_caller_supplied_path_is_refused(client, db, objects, supplied) -> None:
    """None of these is a name in the manifest, so none of them resolves.

    The point is not that each string is individually rejected by a filter. It
    is that resolution NEVER consults the string as a path: the manifest is a
    list of names the server wrote, and `find()` compares for equality. A
    string that is not in that list addresses nothing, whatever it looks like.
    """
    a_finished_task(db, objects, tenant="eng", task="task_a")

    response = get(client, "task_a", supplied)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_a_path_that_would_reach_another_tenants_object_is_refused(
    client, db, objects
) -> None:
    """The traversal with a real object at the other end.

    Without this the parametrised test above could pass because the target did
    not exist rather than because the route refused to look. Here research's
    artifact IS in the bucket and eng's caller still cannot name it.
    """
    a_finished_task(db, objects, tenant="eng", task="task_a")
    seed_tenant(db, "research")
    objects.put(
        artifact_key("private.md", tenant="research", task="task_b", attempt="att_9"),
        b"research's answer",
    )

    response = get(
        client,
        "task_a",
        "../../../../research/tasks/task_b/attempts/att_9/artifacts/private.md",
    )

    assert response.status_code == 404, response.text
    # The refusal reflects the caller's own string back, which is fine and
    # useful. What must never appear is the OBJECT it was aiming at.
    assert "research's answer" not in response.text


def test_an_artifact_the_task_did_not_write_is_a_404_that_says_what_it_has(
    client, db, objects
) -> None:
    """An honest 404: what this task DOES list, and whether the list is final.

    `complete` is carried because "not yet" and "not ever" are different
    answers and the listing route already makes that distinction; losing it
    here would send a caller polling for a file that will never arrive.
    """
    a_finished_task(db, objects, files={"a.md": "one", "b.md": "two"})

    body = get(client, "task_a", "c.md").json()

    assert sorted(body["detail"]["known_artifacts"]) == ["a.md", "b.md"]
    assert body["detail"]["complete"] is True


def test_an_unfinished_task_says_its_manifest_is_not_final(client, db) -> None:
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_running", tenant_id="eng", state="RUNNING")

    body = get(client, "task_running", "synthesis.md").json()

    assert body["detail"]["complete"] is False
    assert body["detail"]["known_artifacts"] == []


def test_a_nested_artifact_name_is_served(client, db, objects) -> None:
    """`_upload_outputs` walks `artifacts/` recursively, so a name has slashes.

    This is why `name` is validated segment by segment rather than as a single
    path segment -- and why the traversal cases above have to be refused by
    manifest membership rather than by "it contains a slash".
    """
    a_finished_task(db, objects, files={"steps/research.md": "findings\n"})

    body = get(client, "task_a", "steps/research.md").json()

    assert body["status"] == "ok"
    assert body["content"] == "findings\n"
    assert body["key"].endswith("/artifacts/steps/research.md")


# --------------------------------------------------------------------------
# 3. The stored uri is evidence, not an address
# --------------------------------------------------------------------------

def test_a_manifest_uri_pointing_outside_this_task_is_refused(
    client, db, objects
) -> None:
    """A tampered or corrupted manifest cannot redirect the read.

    The manifest lists a name this task owns and a uri in ANOTHER tenant's
    prefix, with a real object behind it. The route takes the attempt segment
    from the uri only after checking the uri sits under this task's own
    attempts prefix, so this one never yields an attempt id and never becomes a
    key.
    """
    a_finished_task(db, objects, tenant="eng", task="task_a")
    seed_tenant(db, "research")
    objects.put(
        artifact_key("synthesis.md", tenant="research", task="task_b", attempt="att_9"),
        b"research's answer",
    )
    db.docs["tasks/task_a"]["result_summary"]["artifacts"][0]["uri"] = (
        f"gs://{BUCKET}/"
        + artifact_key("synthesis.md", tenant="research", task="task_b", attempt="att_9")
    )

    response = get(client, "task_a", "synthesis.md")

    assert response.status_code == 503, response.text
    assert "research's answer" not in response.text


def test_a_manifest_uri_that_disagrees_with_the_layout_is_refused(
    client, db, objects
) -> None:
    """The two records of one location must agree or nothing is read.

    `_upload_outputs` builds the key and the uri from the same expression, so
    they cannot differ in a run this platform produced. A difference is
    therefore evidence that something else wrote the manifest, and it is
    refused rather than resolved in either direction.
    """
    a_finished_task(db, objects, tenant="eng", task="task_a")
    db.docs["tasks/task_a"]["result_summary"]["artifacts"][0]["uri"] = (
        f"gs://other-bucket/{artifact_key('synthesis.md')}"
    )

    response = get(client, "task_a", "synthesis.md")

    assert response.status_code == 503, response.text
    assert "disagree" in response.json()["message"]


# --------------------------------------------------------------------------
# 4. Bounded, and it says so
# --------------------------------------------------------------------------

def test_a_capped_window_reports_that_it_was_capped(client, db, objects) -> None:
    """THE SILENT-TRUNCATION TEST.

    A transcript is routinely larger than any sane response. The failure being
    closed is not the cap -- it is a capped response that looks complete, which
    a reader takes for the agent's whole output.
    """
    body_text = "\n".join(f"line {i}" for i in range(4000)) + "\n"
    a_finished_task(db, objects, files={"transcript.txt": body_text})

    body = get(client, "task_a", "transcript.txt", limit_bytes=4096).json()

    assert body["status"] == "ok"
    assert body["truncated"] is True
    assert body["next_offset"] is not None
    assert body["next_offset"] < body["total_bytes"]
    assert body["returned_bytes"] < body["total_bytes"]
    # In words, not only as a flag: the flag is for a client, the sentence is
    # for whoever is reading the payload when the client got it wrong.
    assert "not the whole artifact" in body["detail"]


def test_a_complete_read_does_not_claim_truncation(client, db, objects) -> None:
    """The control: `truncated` has to be able to be false, or it says nothing."""
    a_finished_task(db, objects, files={"small.md": "short\n"})

    body = get(client, "task_a", "small.md", limit_bytes=4096).json()

    assert body["truncated"] is False
    assert body["next_offset"] is None
    assert body["detail"] is None


def test_paging_from_next_offset_reassembles_the_artifact(client, db, objects) -> None:
    """The windows concatenate to the object exactly, so paging is lossless."""
    body_text = "\n".join(f"line {i}" for i in range(4000)) + "\n"
    a_finished_task(db, objects, files={"transcript.txt": body_text})

    seen = ""
    offset = 0
    for _ in range(200):
        body = get(client, "task_a", "transcript.txt", limit_bytes=4096, offset=offset).json()
        assert body["status"] == "ok"
        seen += body["content"]
        if body["next_offset"] is None:
            break
        offset = body["next_offset"]

    assert seen == body_text


def test_limit_bytes_cannot_be_raised_past_the_service_ceiling(
    client, db, objects
) -> None:
    """A caller cannot turn one request into an unbounded download."""
    body_text = "x" * 64 + "\n"
    a_finished_task(db, objects, files={"small.md": body_text})

    body = get(client, "task_a", "small.md", limit_bytes=999_999_999).json()

    assert body["status"] == "ok"
    assert body["returned_bytes"] <= 4 * 1024 * 1024


# --------------------------------------------------------------------------
# 5. Redaction at read time
# --------------------------------------------------------------------------

def test_a_credential_in_an_artifact_is_masked_on_the_way_out(
    client, db, objects
) -> None:
    """The worker's pass is over REGISTERED LITERAL VALUES and nothing else.

    `_redact_before_upload` short-circuits entirely on `if not
    self.log.has_secrets`, and `scrub_file` skips binary and oversized files,
    so an `Authorization:` header a tool echoed into a report reaches the
    bucket in the clear. This route scrubs it again on the way out and says
    that it did.
    """
    leaked = "Authorization: Bearer ya29.A0ARrdaM-notarealtokenbutlongenough\n"
    a_finished_task(db, objects, files={"report.md": f"# Report\n\n{leaked}done\n"})

    body = get(client, "task_a", "report.md").json()

    assert body["status"] == "ok"
    assert "ya29.A0ARrdaM-notarealtokenbutlongenough" not in body["content"]
    assert "********" in body["content"]
    assert body["redacted"] is True
    assert body["redaction_count"] >= 1
    assert body["redaction"]["applied_at_read_time"] is True


def test_a_clean_artifact_reports_that_nothing_was_masked(client, db, objects) -> None:
    """`redacted: false` must be reachable, or `true` carries no information."""
    a_finished_task(db, objects, files={"report.md": "# Report\n\nall clear\n"})

    body = get(client, "task_a", "report.md").json()

    assert body["redacted"] is False
    assert body["redaction_count"] == 0
    assert body["redaction"]["applied_at_read_time"] is True


# --------------------------------------------------------------------------
# 6. Three answers, never two
# --------------------------------------------------------------------------

def test_a_manifest_entry_whose_object_is_gone_is_absent_not_empty(
    client, db, objects
) -> None:
    """`absent` is a fact about the world; `""` would be a claim about the run."""
    a_finished_task(db, objects, files={"synthesis.md": "kept\n"})
    objects.objects.pop(artifact_key("synthesis.md"))

    body = get(client, "task_a", "synthesis.md").json()

    assert body["status"] == "absent"
    assert body["content"] is None
    assert "not in the bucket" in body["detail"]


def test_a_failed_read_is_unreadable_not_absent(client, db, objects) -> None:
    """A bucket that 403s must never render as an artifact that is empty."""
    a_finished_task(db, objects, files={"synthesis.md": "kept\n"})
    objects.fail_on(prefix())

    body = get(client, "task_a", "synthesis.md").json()

    assert body["status"] == "unreadable"
    assert body["content"] is None
    assert "nothing may be concluded" in body["detail"]


def test_a_zero_byte_artifact_is_ok_and_empty(client, db, objects) -> None:
    """An agent that wrote an empty file is a third answer again."""
    a_finished_task(db, objects, files={"empty.md": ""})

    body = get(client, "task_a", "empty.md").json()

    assert body["status"] == "ok"
    assert body["content"] == ""
    assert body["total_bytes"] == 0
    assert body["truncated"] is False


def test_a_binary_artifact_is_named_rather_than_served(client, db, objects) -> None:
    """Bytes nothing can scan are bytes this route does not hand out.

    Base64 would be a download, and it would also be an unredactable one --
    every rule in `swarm_api.redaction` is a pattern over text.
    """
    a_finished_task(db, objects, files={"heap.bin": b"MZ\x00\x00\x90\x00ya29.secret"})

    body = get(client, "task_a", "heap.bin").json()

    assert body["status"] == "binary"
    assert body["content"] is None
    assert body["total_bytes"] == 17
    assert body["uri"].startswith(f"gs://{BUCKET}/tenants/eng/")
    assert "not text" in body["detail"]


def test_the_binary_decision_does_not_change_with_the_window(
    client, db, objects
) -> None:
    """Classified over the HEAD, once, so it is a property of the artifact.

    Deciding per window would make the same file text on page one and binary on
    page four, which is not a thing a file can be.
    """
    body_bytes = b"# Report\n" + b"a" * 8192 + b"\x00\x00tail\n"
    a_finished_task(db, objects, files={"mixed.md": body_bytes})

    head = get(client, "task_a", "mixed.md", limit_bytes=4096).json()
    later = get(client, "task_a", "mixed.md", limit_bytes=4096, offset=8000).json()

    assert head["status"] == "ok"
    assert later["status"] == "ok"


# --------------------------------------------------------------------------
# 7. The route is not configured away
# --------------------------------------------------------------------------

def test_no_artifact_store_configured_is_not_an_empty_artifact(
    db, tokens, group_map
) -> None:
    """A deployment with no bucket has a named fix, and it is not IAM."""
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    db.docs["tasks/task_a"]["result_summary"] = {
        "artifacts": [manifest_entry("synthesis.md", "x")],
    }
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = get(client, "task_a", "synthesis.md")

    assert response.status_code == 503, response.text
    assert "ARTIFACT_BUCKET" in response.json()["message"]
