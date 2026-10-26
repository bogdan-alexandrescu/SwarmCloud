"""S9b's Remove: a workspace admin dismisses a found route, so no later run of
the QA target offers it for Promote, and restores it (lane QA-RUN-REMOVE, BGD
2026-10-25, part of #2157; docs/web-ui/mockups/config-e.html).

What these tests hold, over the real app and the in-memory Firestore:

  * `:dismiss-route` takes the route out of every leg's `new_since_last_run`,
    the leg that found it and a leg whose LATER run finds it, and lists it in
    the target's `dismissed_routes`; `revision` and `screens` are not written;
  * `:restore-route` puts it back where a leg's last run found it;
  * a member, and another workspace's admin, are refused with nothing written
    and no audit row; the other workspace's target answers as a missing one;
  * each dismissal and restore commits one `admin_audit` row naming the route's
    id and who, never its path.
"""

from __future__ import annotations

from typing import Any

from .conftest import auth_header
from .test_qa_target_discovery import (  # noqa: F401 -- pytest fixtures, used by name
    api_context,
    group_map,
    resolver,
    seeded,
)
from .test_qa_target_environments import entry_of
from .test_qa_target_settle import _later, finish_capture, found, row, run_leg, shown
from .test_qa_run_groups import approved_two
from .test_qa_targets import TARGETS, audit_rows, register


def dismiss(client, target_id: str, route_id: str, user: str = "carol"):
    return client.post(f"{TARGETS}/{target_id}:dismiss-route", json={"route_id": route_id},
                       headers=auth_header(user))


def restore(client, target_id: str, route_id: str, user: str = "carol"):
    return client.post(f"{TARGETS}/{target_id}:restore-route", json={"route_id": route_id},
                       headers=auth_header(user))


def new_paths(answer: dict[str, Any], leg: str) -> list[str]:
    return [r["path"] for r in entry_of(answer, leg)["new_since_last_run"]]


def staging_found(client, db, objects) -> tuple[dict[str, Any], str]:
    """carol's two-leg target, whose staging run found /billing and /team."""
    target = approved_two(client)
    workflow_id = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, workflow_id, "g1",
                   found(new=[row("billing", "/billing"), row("team", "/team")]))
    answer = shown(client, target["id"])
    # CONTROL: before any dismissal, both are offered.
    assert new_paths(answer, "staging") == ["/billing", "/team"]
    [billing] = [r for r in entry_of(answer, "staging")["new_since_last_run"] if r["path"] == "/billing"]
    return target, billing["id"]


def test_remove_dismisses_a_route_and_a_later_run_leaves_it_out(client, db, objects, seeded, api_context) -> None:
    target, billing = staging_found(client, db, objects)
    before = db.docs[f"qa_targets/{target['id']}"]
    screens, revision = list(before["screens"]), before["revision"]
    response = dismiss(client, target["id"], billing)
    assert response.status_code == 200, response.text
    answer = response.json()["qa_target"]
    # MUTATION: skip the dismissed keys in `discovery_status` and /billing is still offered.
    assert new_paths(answer, "staging") == ["/team"]
    # MUTATION: leave `dismissed_routes` out of the answer and the "N dismissed" line has nothing to show.
    assert [(d["id"], d["path"], d["by"]) for d in answer["dismissed_routes"]] == \
        [(billing, "/billing", "carol@example.com")]
    after = db.docs[f"qa_targets/{target['id']}"]
    # MUTATION: bump `revision` on a dismissal and a running leg's mark is refused.
    assert after["screens"] == screens and after["revision"] == revision
    api_context.now = _later(api_context.now, 1)
    workflow_id = run_leg(client, target["id"], "production")
    finish_capture(client, db, objects, workflow_id, "g1",
                   found(new=[row("billing", "/billing"), row("careers", "/careers")]))
    later = shown(client, target["id"])
    # MUTATION: keep the dismissal on the staging leg alone and production's later run offers /billing.
    assert new_paths(later, "production") == ["/careers"]
    assert new_paths(later, "staging") == ["/team"]


def test_restore_puts_a_dismissed_route_back(client, db, objects, seeded) -> None:
    target, billing = staging_found(client, db, objects)
    assert dismiss(client, target["id"], billing).status_code == 200
    response = restore(client, target["id"], billing)
    assert response.status_code == 200, response.text
    answer = response.json()["qa_target"]
    # MUTATION: make restore a no-op and /billing stays hidden.
    assert new_paths(answer, "staging") == ["/billing", "/team"]
    assert answer["dismissed_routes"] == []
    # A route the target never dismissed is not restored.
    again = restore(client, target["id"], billing)
    assert again.status_code == 404, again.text


def test_a_route_no_leg_found_is_not_dismissed(client, db, objects, seeded) -> None:
    target, _ = staging_found(client, db, objects)
    response = dismiss(client, target["id"], "nowhere")
    # MUTATION: store whatever id is sent and a typo is an invisible dismissal.
    assert response.status_code == 404, response.text
    assert not db.docs[f"qa_targets/{target['id']}"].get("dismissed_routes")


def eng_target_with_a_found_route(client, db) -> dict[str, Any]:
    """The eng workspace's target (root registered it), whose one leg found /billing."""
    created = register(client, "root", repository="example-org/widgets")
    assert created.status_code == 201, created.text
    target = created.json()["qa_target"]
    doc = db.docs[f"qa_targets/{target['id']}"]
    doc["discovered"] = [{"id": "billing", "path": "/billing"}]
    doc["discovered_new"] = ["billing"]
    return target


def test_a_member_is_refused_and_nothing_is_written(client, db, seeded) -> None:
    target = eng_target_with_a_found_route(client, db)
    before = dict(db.docs[f"qa_targets/{target['id']}"])
    rows = len(audit_rows(db))
    # MUTATION: drop `require_workspace_admin` from `dismiss_route` and alice (eng's member) gets 200.
    response = dismiss(client, target["id"], "billing", user="alice")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert restore(client, target["id"], "billing", user="alice").status_code == 403
    assert db.docs[f"qa_targets/{target['id']}"] == before
    assert len(audit_rows(db)) == rows
    # CONTROL: the member reads the route still offered, and the workspace's admin may dismiss it.
    answer = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("alice")).json()["qa_target"]
    assert [r["path"] for r in answer["new_since_last_run"]] == ["/billing"]
    assert dismiss(client, target["id"], "billing", user="root").status_code == 200


def test_another_workspaces_admin_is_refused_and_nothing_is_written(client, db, seeded) -> None:
    target = eng_target_with_a_found_route(client, db)
    before = dict(db.docs[f"qa_targets/{target['id']}"])
    rows = len(audit_rows(db))
    # carol administers her own workspace, not eng: eng's target is not hers to see.
    theirs = dismiss(client, target["id"], "billing", user="carol")
    missing = dismiss(client, f"qat_{'0' * 16}", "billing", user="carol")
    # MUTATION: drop the tenant check in `_edit_dismissed` and carol's call is 200.
    assert theirs.status_code == missing.status_code == 404, theirs.text
    assert theirs.json() == missing.json()
    assert restore(client, target["id"], "billing", user="carol").status_code == 404
    assert db.docs[f"qa_targets/{target['id']}"] == before
    assert len(audit_rows(db)) == rows


def test_a_dismissal_and_a_restore_are_audited(client, db, objects, seeded) -> None:
    target, billing = staging_found(client, db, objects)
    rows = len(audit_rows(db))
    assert dismiss(client, target["id"], billing).status_code == 200
    assert restore(client, target["id"], billing).status_code == 200
    written = audit_rows(db)[rows:]
    # MUTATION: drop the `_audit` call from `_edit_dismissed` and this is [].
    assert [(r["action"], r["by"], r["target_qa_target_id"], r["detail"]["route_id"]) for r in written] == [
        ("qa_target_route_dismissed", "carol@example.com", target["id"], billing),
        ("qa_target_route_restored", "carol@example.com", target["id"], billing),
    ]
    # The route's path is the app's text: never in an audit row.
    assert all("/billing" not in str(r["detail"]) for r in written)


def test_a_change_of_the_target_keeps_its_dismissed_routes(client, db, objects, seeded) -> None:
    target, billing = staging_found(client, db, objects)
    assert dismiss(client, target["id"], billing).status_code == 200
    response = client.patch(f"{TARGETS}/{target['id']}", json={"name": "Shop renamed"}, headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    # MUTATION: write the PATCH's document without the stored list and the dismissal is lost.
    assert [d["id"] for d in shown(client, target["id"])["dismissed_routes"]] == [billing]
    assert new_paths(shown(client, target["id"]), "staging") == ["/team"]
