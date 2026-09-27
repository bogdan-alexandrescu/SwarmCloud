"""`GET /v1/tasks/{id}/logs` -- the other seam that did not exist.

The worker writes each stream twice: a complete `logs/<stream>.log` at exit,
and a bounded `logs/live/<stream>.tail.log` republished every few seconds while
the agent runs. Nothing served either, so watching a run meant a gcloud session
and reading one meant knowing the key layout.

The redaction half of this route is pinned in `test_log_redaction.py`. This
file pins the rest: which object is served, how paging works, and the three
answers a stream can give -- present, absent, and could-not-be-read -- which
must never collapse into each other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _attempt(db, attempt_id, tenant, task_id, *, generation=1, minutes_ago=0, exit_code=None):
    created = NOW - timedelta(minutes=minutes_ago)
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": f"swarm-job-{tenant}-mock-{attempt_id}",
        "created_at": created,
        "started_at": created + timedelta(seconds=10),
        "completed_at": created + timedelta(minutes=2) if exit_code is not None else None,
        "exit_code": exit_code,
        "error": None,
        "peak_rss_bytes": 1234,
        "oom_near_miss": False,
        "checkpoints": [],
    })


def base_prefix(tenant="eng", task="task_a", attempt="att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}"


def final_key(stream="stdout", **kw) -> str:
    return f"{base_prefix(**kw)}/logs/{stream}.log"


def live_key(stream="stdout", **kw) -> str:
    return f"{base_prefix(**kw)}/logs/live/{stream}.tail.log"


def a_finished_attempt(db, objects, *, out="hello from the agent\n", err="a warning\n"):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    _attempt(db, "att_1", "eng", "task_a", minutes_ago=20, exit_code=0)
    if out is not None:
        objects.put(final_key("stdout"), out)
    if err is not None:
        objects.put(final_key("stderr"), err)


def get(client, task="task_a", user="alice", query=""):
    return client.get(f"/v1/tasks/{task}/logs{query}", headers=auth_header(user))


def stream_of(body, name="stdout") -> dict:
    return next(s for s in body["streams"] if s["stream"] == name)


# --------------------------------------------------------------------------
# What it serves
# --------------------------------------------------------------------------

def test_the_completed_streams_are_what_the_route_returns(client, db, objects):
    a_finished_attempt(db, objects)

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["task_id"] == "task_a"
    assert body["tenant_id"] == "eng"
    assert body["attempt_id"] == "att_1"
    assert body["attempt"]["status"] == "latest"
    assert body["attempt"]["known"] is True
    assert body["attempt"]["exit_code"] == 0
    assert [s["stream"] for s in body["streams"]] == ["stdout", "stderr"]

    out = stream_of(body)
    assert out["status"] == "ok"
    assert out["source"] == "final"
    assert out["content"] == "hello from the agent\n"
    assert out["key"] == final_key("stdout")
    assert out["uri"].startswith("gs://")
    assert out["truncated"] is False
    assert out["next_offset"] is None
    assert stream_of(body, "stderr")["content"] == "a warning\n"


def test_one_stream_can_be_asked_for_on_its_own(client, db, objects):
    a_finished_attempt(db, objects)
    body = get(client, query="?stream=stderr").json()
    assert [s["stream"] for s in body["streams"]] == ["stderr"]


def test_an_unknown_stream_name_is_refused(client, db, objects):
    a_finished_attempt(db, objects)
    response = get(client, query="?stream=syslog")
    assert response.status_code == 422, response.text


def test_the_live_tail_is_served_while_the_agent_is_still_running(client, db, objects):
    """Mid-run there IS no completed log -- `_upload_outputs` has not happened.
    The live tail is the only thing that exists, and it is what makes watching
    a run possible at all."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_1", "eng", "task_a", minutes_ago=2)
    objects.put(live_key("stdout"), "#swarm-tail offset=4096 size=9000\nstill working\n")

    out = stream_of(get(client).json())
    assert out["status"] == "ok"
    assert out["source"] == "live"
    assert out["content"] == "still working\n", "the tail header is metadata, not a log line"
    # `published_at` since #184: the header's `at=`, null for a header written
    # before the worker stamped one -- as this one is.
    assert out["tail_window"] == {
        "object_offset": 4096,
        "stream_size": 9000,
        "published_at": None,
    }


def test_the_completed_log_wins_over_a_stale_live_tail(client, db, objects):
    """Both objects survive the run -- nothing deletes the tail. The complete
    record is the one to serve, or a finished task shows the last 256 KiB
    window for ever."""
    a_finished_attempt(db, objects, out="the whole run\n")
    objects.put(live_key("stdout"), "#swarm-tail offset=0 size=5\nstale\n")

    out = stream_of(get(client).json())
    assert out["source"] == "final"
    assert out["content"] == "the whole run\n"


def test_the_live_tail_can_be_asked_for_explicitly(client, db, objects):
    a_finished_attempt(db, objects, out="the whole run\n")
    objects.put(live_key("stdout"), "#swarm-tail offset=0 size=5\nlast window\n")

    out = stream_of(get(client, query="?source=live").json())
    assert out["source"] == "live"
    assert out["content"] == "last window\n"


def test_an_unrecognised_source_is_refused(client, db, objects):
    a_finished_attempt(db, objects)
    assert get(client, query="?source=guess").status_code == 422


# --------------------------------------------------------------------------
# Present, absent, unreadable
# --------------------------------------------------------------------------

def test_a_stream_that_was_never_written_is_absent_not_empty(client, db, objects):
    """`content: null`, never `""`. A client that renders content without
    reading status then shows nothing, rather than an empty log that reads as
    a successful capture of a silent agent."""
    a_finished_attempt(db, objects, err=None)

    err = stream_of(get(client).json(), "stderr")
    assert err["status"] == "absent"
    assert err["content"] is None
    assert err["total_bytes"] is None, "a size may not be claimed for an object that is not there"
    assert err["detail"]


def test_a_zero_byte_stream_is_present_and_empty(client, db, objects):
    """A third answer again: the object EXISTS and the agent printed nothing.
    That is a fact about the run, and it is not an absence."""
    a_finished_attempt(db, objects, err="")

    err = stream_of(get(client).json(), "stderr")
    assert err["status"] == "ok"
    assert err["content"] == ""
    assert err["total_bytes"] == 0


def test_a_failed_read_is_reported_as_a_failed_read(client, db, objects):
    """THE DISTINCTION THIS ROUTE EXISTS TO KEEP.

    A 403 on the log object and a task that printed nothing produce the same
    empty panel. One means "there is nothing to see"; the other means "I could
    not look, and you should not conclude anything". This repository has
    shipped that collapse in a deployment gate, a staleness guard and a destroy
    check within the last week.
    """
    a_finished_attempt(db, objects)
    objects.fail_on(final_key("stdout"))

    body = get(client).json()
    out = stream_of(body)
    assert out["status"] == "unreadable"
    assert out["content"] is None
    assert out["total_bytes"] is None
    assert out["detail"], "a failed read says why"
    # The other stream is unaffected: one object failing is not the whole
    # answer failing.
    assert stream_of(body, "stderr")["status"] == "ok"


def test_an_unreadable_completed_log_does_not_silently_fall_back_to_the_tail(
    client, db, objects
):
    """Falling back on a FAILURE would serve a 256 KiB window while reporting
    success, which is the same lie as an empty array -- the caller believes
    they are reading the whole run. Fallback happens on ABSENCE only."""
    a_finished_attempt(db, objects)
    objects.fail_on(final_key("stdout"))
    objects.put(live_key("stdout"), "#swarm-tail offset=0 size=3\nwindow\n")

    out = stream_of(get(client).json())
    assert out["status"] == "unreadable"
    assert out["source"] == "final"
    assert out["content"] is None


def test_neither_object_existing_says_which_question_was_asked(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="STARTING")
    _attempt(db, "att_1", "eng", "task_a", minutes_ago=1)

    out = stream_of(get(client).json())
    assert out["status"] == "absent"
    assert "neither the completed log nor a live tail" in out["detail"]


def test_a_task_with_no_attempt_yet_says_so_rather_than_failing(client, db, objects):
    """QUEUED, PARKED or READY: there is no attempt, so there is no object that
    could exist. That is a statement about the task, not a failed read."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="QUEUED")

    body = get(client).json()
    assert body["attempt_id"] is None
    assert body["attempt"]["status"] == "no_attempt_yet"
    out = stream_of(body)
    assert out["status"] == "absent"
    assert "no attempt yet" in out["detail"]


def test_an_attempt_with_no_document_is_still_readable_and_flagged(client, db, objects):
    """`purge-data.sh` removes documents and objects in separate steps. An
    attempt whose document is gone but whose logs are still in the bucket must
    stay reachable -- and the caller must know the control plane has no record
    of it."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    objects.put(final_key("stdout", attempt="att_ghost"), "output from a purged attempt\n")

    body = get(client, query="?attempt_id=att_ghost").json()
    assert body["attempt"]["status"] == "unknown_attempt"
    assert body["attempt"]["known"] is False
    assert stream_of(body)["content"] == "output from a purged attempt\n"


def test_an_unconfigured_artifact_store_says_so(db, tokens, group_map):
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        objects=None,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")

    response = client.get("/v1/tasks/task_a/logs", headers=auth_header("alice"))
    assert response.status_code == 503, response.text
    assert "ARTIFACT_BUCKET" in response.json()["message"]


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------

def test_a_large_log_is_paged_and_never_cut_mid_line(client, db, objects):
    """Two properties at once.

    Paging, because a captured stream is capped at 32 MiB and a route that
    served one in a single response would put 32 MiB through JSON encoding into
    a browser.

    Line boundaries, because a credential split across a chunk boundary matches
    no pattern in either half. The window is cut back to the last newline and
    `next_offset` points at that boundary, so the redactor always sees whole
    lines and concatenating the windows reproduces the object exactly.

    `limit_bytes` is clamped UP to `min_log_bytes`, so the window here is 4 KiB
    even though a smaller one was asked for: a window too small to contain a
    line has no boundary to cut at and could only be withheld.
    """
    lines = [f"line {n:04d} of output\n" for n in range(1000)]
    a_finished_attempt(db, objects, out="".join(lines))

    seen, offset, pages = "", 0, 0
    while True:
        body = get(client, query=f"?stream=stdout&offset={offset}&limit_bytes=137").json()
        out = stream_of(body)
        assert out["status"] == "ok"
        assert out["content"].endswith("\n"), "a window never ends mid-line"
        seen += out["content"]
        pages += 1
        if out["next_offset"] is None:
            assert out["truncated"] is False
            break
        assert out["truncated"] is True
        offset = out["next_offset"]
        assert pages < 200, "the offset is not advancing"

    assert seen == "".join(lines)
    assert pages > 1, "the point of the test is that it took several windows"


def test_the_byte_accounting_is_over_the_raw_object(client, db, objects):
    """Redaction shortens the text. Paging on the length of the REDACTED string
    would drift, and the next window would start in the middle of a line that
    was already served."""
    a_finished_attempt(db, objects, out="Authorization: Bearer abcdefghijklmnop\nplain\n")

    out = stream_of(get(client, query="?stream=stdout").json())
    assert out["total_bytes"] == len("Authorization: Bearer abcdefghijklmnop\nplain\n")
    assert out["returned_bytes"] == out["total_bytes"]
    assert len(out["content"]) != out["returned_bytes"], "the served text really is shorter"


def test_paging_past_the_end_is_an_empty_window_not_an_error(client, db, objects):
    a_finished_attempt(db, objects, out="short\n")
    out = stream_of(get(client, query="?stream=stdout&offset=9999").json())
    assert out["status"] == "ok"
    assert out["content"] == ""
    assert out["next_offset"] is None


def test_a_window_larger_than_the_ceiling_is_clamped_not_refused(client, db, objects):
    a_finished_attempt(db, objects, out="x" * 4096)
    out = stream_of(get(client, query="?stream=stdout&limit_bytes=99999999").json())
    assert out["status"] == "ok"
    assert out["returned_bytes"] == 4096


def test_a_negative_offset_is_refused(client, db, objects):
    a_finished_attempt(db, objects)
    assert get(client, query="?offset=-1").status_code == 422


# --------------------------------------------------------------------------
# Tenant isolation -- invariant 9
# --------------------------------------------------------------------------

def test_another_tenants_logs_are_a_404_and_leak_nothing(client, db, objects):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    a_finished_attempt(db, objects, out="eng's private output\n")

    response = client.get("/v1/tasks/task_a/logs", headers=auth_header("bob"))
    assert response.status_code == 404, response.text
    assert "private output" not in response.text
    assert "tenants/eng" not in response.text


def test_no_attempt_id_reaches_another_tenants_objects(client, db, objects):
    """The attempt segment is appended to a prefix that already pins tenant and
    task. It is validated as a single traversal-free segment anyway: GCS
    happens not to normalise `..`, and "the current backend happens not to" is
    not an access-control argument."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_task(db, task_id="task_a", tenant_id="eng")
    seed_task(db, task_id="task_b", tenant_id="research")
    objects.put(final_key("stdout", tenant="research", task="task_b", attempt="att_r"),
                "research's private output\n")

    hostile = [
        "../../../../research/tasks/task_b/attempts/att_r",
        "..",
        ".",
        "att_r/../../../..",
        "",
        "att_r%2F..",
    ]
    for value in hostile:
        response = get(client, query=f"?attempt_id={value}")
        assert response.status_code in (200, 404, 422), (value, response.status_code)
        assert "private output" not in response.text, value


def test_a_task_id_belonging_to_another_tenant_cannot_be_read_by_any_route(client, db, objects):
    """Both routes, one assertion: the scope check is a dependency on the route
    and a tenant filter in the store, so there is no parameter that moves the
    prefix. Pinned together because "the other one must be fine" is how a
    boundary ends up half-applied."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    a_finished_attempt(db, objects, out="eng's private output\n")

    for path in ("/v1/tasks/task_a/logs", "/v1/tasks/task_a/checkpoints"):
        response = client.get(path, headers=auth_header("bob"))
        assert response.status_code == 404, (path, response.text)
        assert "private output" not in response.text


def test_the_route_is_read_only(client, db, objects):
    a_finished_attempt(db, objects)
    before_objects = dict(objects.objects)
    before_task = dict(db.docs["tasks/task_a"])

    assert get(client).status_code == 200
    assert objects.objects == before_objects
    assert db.docs["tasks/task_a"] == before_task


def test_no_request_header_is_ever_echoed(client, db, objects):
    """The `Authorization` header is consumed by `deps.current_auth` and read
    nowhere else in the process. A log route that echoed it would hand a caller
    their own bearer token back inside a response body that gets pasted into
    issues."""
    a_finished_attempt(db, objects)
    response = client.get(
        "/v1/tasks/task_a/logs",
        headers={**auth_header("alice"), "X-Custom-Trace": "trace-me-if-you-can"},
    )
    assert response.status_code == 200
    assert "token-alice" not in response.text
    assert "trace-me-if-you-can" not in response.text
    assert "Authorization" not in response.text
