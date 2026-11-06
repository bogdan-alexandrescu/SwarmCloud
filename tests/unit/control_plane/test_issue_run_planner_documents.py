"""The planner's design doc and diagrams, served on the run read (BGD, 6 November 2026: "Serve them on the run").

The proposal page (/work/runs?proposal=<run>) said "not known here" for the
design doc and the diagrams, because a run served only its plan.
`GET /v1/runs/{id}` now serves them as `planner_documents`: each by NAME,
size and a read link through the planner task's own raw artifact route --
never the bytes, never a signed URL, so the link carries the route's tenant
check and read-time redaction and another tenant's member cannot open it.
Read-only: serving them moves nothing and writes nothing to the run document.
"""

from __future__ import annotations

from swarm_api import issueruns

from .conftest import auth_header
from .test_issue_run_tick import (  # noqa: F401 -- fixtures are used by name
    _stored,
    api_context,
    client,
    github,
)
from .test_issue_runs import BUCKET, PLAN, _create, _finish_planner, _run

DOC = "# Timer\n\nThe hold timer sits in the order summary.\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _wrote(db, objects, run: dict, name: str, data: bytes | str) -> None:
    """The planner task's attempt left `name` among its artifacts, as a worker uploads it."""
    task_id = run["planner_task_id"]
    doc = db.docs[f"tasks/{task_id}"]
    key = f"tenants/{doc['tenant_id']}/tasks/{task_id}/attempts/att_1/artifacts/{name}"
    objects.put(key, data)
    size = len(data.encode() if isinstance(data, str) else data)
    doc["result_summary"]["artifacts"].append({"name": name, "bytes": size, "uri": f"gs://{BUCKET}/{key}"})


def _planned(client, db, objects) -> dict:
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, PLAN)
    return run


def _link(task_id: str, name: str) -> str:
    return f"/v1/tasks/{task_id}/artifacts/raw?name={name}&disposition=inline"


def test_a_run_serves_its_planners_design_doc_and_diagrams_by_name_size_and_link(client, db, objects):
    run = _planned(client, db, objects)
    _wrote(db, objects, run, "design.md", DOC)
    _wrote(db, objects, run, "diagrams/flow.png", PNG)
    _wrote(db, objects, run, "notes.md", "not the design")
    _wrote(db, objects, run, "diagram.svg", "<svg/>")

    served = _run(client, run["id"]).json()["run"]
    task_id = run["planner_task_id"]

    assert served["planner_documents"] == {
        "task_id": task_id,
        "design_docs": [{"name": "design.md", "bytes": len(DOC.encode()), "content_type": "text/markdown",
                         "read_url": _link(task_id, "design.md")}],
        "diagrams": [{"name": "diagrams/flow.png", "bytes": len(PNG), "content_type": "image/png",
                      "read_url": _link(task_id, "diagrams%2Fflow.png")}],
    }
    # By reference only: no byte of either is inline in the run.
    assert "hold timer" not in str(served["planner_documents"])
    # Read-only: never stored on the run.
    assert "planner_documents" not in _stored(db, run["id"])

    # The link is the existing artifact route, and it opens for the run's tenant.
    doc = client.get(served["planner_documents"]["design_docs"][0]["read_url"], headers=auth_header("alice"))
    assert doc.status_code == 200
    assert doc.headers["content-type"].startswith("text/plain")
    assert "hold timer" in doc.text
    image = client.get(served["planner_documents"]["diagrams"][0]["read_url"], headers=auth_header("alice"))
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


def test_another_tenants_member_cannot_read_the_run_or_follow_its_links(client, db, objects):
    run = _planned(client, db, objects)
    _wrote(db, objects, run, "design.md", DOC)
    _wrote(db, objects, run, "flow.png", PNG)
    served = _run(client, run["id"]).json()["run"]["planner_documents"]

    # bob is in another tenant: the run is a 404, as a missing one is.
    assert _run(client, run["id"], user="bob").status_code == 404
    for entry in (*served["design_docs"], *served["diagrams"]):
        theirs = client.get(entry["read_url"], headers=auth_header("bob"))
        assert theirs.status_code == 404
        assert "hold timer" not in theirs.text
        # Naming the run's tenant on the link is put through bob's membership, and refused.
        named = client.get(f"{entry['read_url']}&tenant=eng", headers=auth_header("bob"))
        assert named.status_code in (403, 404)
        assert "hold timer" not in named.text


def test_a_run_whose_planner_wrote_none_serves_empty_lists(client, db, objects):
    run = _planned(client, db, objects)

    served = _run(client, run["id"]).json()["run"]["planner_documents"]

    assert served == {"task_id": run["planner_task_id"], "design_docs": [], "diagrams": []}


def test_a_run_whose_planner_has_not_finished_serves_none(client, db, objects):
    run = _create(client).json()["run"]

    assert _run(client, run["id"]).json()["run"]["planner_documents"] is None


def test_the_list_does_not_serve_them(client, db, objects):
    """One artifact listing per row would be the list's cost; only a run's own read serves them."""
    run = _planned(client, db, objects)
    _wrote(db, objects, run, "design.md", DOC)

    rows = client.get("/v1/runs", headers=auth_header("alice")).json()["runs"]

    assert [r["id"] for r in rows] == [run["id"]]
    assert "planner_documents" not in rows[0]


def test_only_design_markdown_and_raster_images_are_chosen_and_bounded():
    rows = [
        {"name": "plan.json", "bytes": 10, "kind": "json", "content_type": "application/json"},
        {"name": "office-hours-design-20261105.md", "bytes": 5, "kind": "markdown", "content_type": "text/markdown"},
        {"name": "DESIGN.md", "bytes": 6, "kind": "markdown", "content_type": "text/markdown"},
        {"name": "agent-stdout.log", "bytes": 7, "kind": "log", "content_type": "text/plain"},
        {"name": "verification.md", "bytes": 8, "kind": "markdown", "content_type": "text/markdown"},
        {"name": "shot.webp", "bytes": "9", "kind": "image", "content_type": "image/webp"},
        {"name": "", "bytes": 1, "kind": "image", "content_type": "image/png"},
        {"bytes": 1, "kind": "image"},
        *({"name": f"d{i:02}.png", "bytes": i, "kind": "image", "content_type": "image/png"}
          for i in range(issueruns.MAX_PLANNER_DIAGRAMS + 3)),
    ]

    served = issueruns.planner_documents("t 1", rows)

    assert [d["name"] for d in served["design_docs"]] == ["DESIGN.md", "office-hours-design-20261105.md"]
    assert served["design_docs"][0]["read_url"] == "/v1/tasks/t%201/artifacts/raw?name=DESIGN.md&disposition=inline"
    names = [d["name"] for d in served["diagrams"]]
    # In name order, at most MAX_PLANNER_DIAGRAMS of them.
    assert names == [f"d{i:02}.png" for i in range(issueruns.MAX_PLANNER_DIAGRAMS)]
    # A size the manifest did not record as a number is not invented.
    rows_one = [{"name": "shot.webp", "bytes": "9", "kind": "image", "content_type": "image/webp"}]
    assert issueruns.planner_documents("t", rows_one)["diagrams"][0]["bytes"] is None
