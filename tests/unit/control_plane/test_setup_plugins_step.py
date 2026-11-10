"""Admin › Setup's optional "add a plugin" step (docs/design/managed-plugins.md
§15.1, lane 22 of §17.1; the owner's Q15, 6 November 2026).

`GET /v1/admin/setup` serves `steps.plugins`, derived on every read: `done`
once the deployment holds an active install that is not bundled (a platform
install or any workspace's own), `skipped` when a platform admin skipped it and
none exists, else `todo`. `POST /v1/admin/setup/plugins/skip` remembers the
skip in `console/plugins_step` and `DELETE` of the same route forgets it, each
audited in the same transaction. The step is optional: `finish` never waits
on it.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import install_record as ir
from swarm_api import pluginstore as ps
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.judgment import settings_keys

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    ROOT,
    SETTINGS_KEY,
    db,
    directory,
    fake_secrets,
    finished_install,
    github,
    group_map,
    pool,
    runs,
    seed_engine_document,
    setup_app,
    setup_client,
    tokens,
)

PAGE = "/v1/admin/setup"
SKIP = "/v1/admin/setup/plugins/skip"
DOC = f"swarm-install/{ENV}/console/plugins_step"
KEYS = settings_keys(SETTINGS_KEY)


def seed_install(db, install_id: str, *, name: str, scope: str = ps.PLATFORM,
                 tenant_id: str | None = None, built_in: bool = False,
                 state: str = ps.STATE_ACTIVE, signed: bool = True, **extra: Any) -> None:
    """One `plugin_installs/<id>` document, MAC'd as `PluginStore` writes it."""
    fields = {"install_id": install_id, "scope": scope, "tenant_id": tenant_id, "name": name,
              "display_name": name, "source": None, "current_version": None,
              "built_in": built_in, "state": state, "removed_at": None,
              "created_at": datetime(2026, 11, 9, tzinfo=timezone.utc), "created_by": ROOT, **extra}
    db.docs[f"{ps.INSTALLS}/{install_id}"] = ps._signed("install", fields, KEYS) if signed else fields


def seed_bundled(db) -> None:
    """The two bundled installs of §1.4.1: reserved ids, `built_in`, `build: "image"`."""
    for name in ("gstack", "security-audit"):
        seed_install(db, f"builtin:{name}", name=name, built_in=True, build="image")


def new_install_id(scope: str = ps.PLATFORM) -> str:
    return ("p-" if scope == ps.PLATFORM else "w-") + secrets.token_hex(6)


def step(client) -> dict[str, Any]:
    answer = client.get(PAGE, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    return answer.json()["steps"]["plugins"]


def audit_rows(db, action: str) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items()
            if path.startswith(f"{AUDIT_COLLECTION}/") and doc.get("action") == action]


def test_the_bundled_installs_alone_leave_the_step_to_do(setup_client, db) -> None:
    seed_engine_document(db)
    seed_bundled(db)
    plugins = step(setup_client)
    assert plugins["state"] == "todo"
    assert plugins["needed"] is False and plugins["optional"] is True
    assert plugins["count"] == 0 and plugins["added"] == []
    assert plugins["skipped"] is None


@pytest.mark.parametrize("scope", [ps.PLATFORM, ps.WORKSPACE])
def test_an_added_install_makes_the_step_done(setup_client, db, scope) -> None:
    seed_engine_document(db)
    seed_bundled(db)
    install_id = new_install_id(scope)
    tenant = "team-a" if scope == ps.WORKSPACE else None
    seed_install(db, install_id, name="jev-belay", scope=scope, tenant_id=tenant)
    plugins = step(setup_client)
    assert plugins["state"] == "done"
    assert plugins["count"] == 1
    assert plugins["added"] == [{"install_id": install_id, "name": "jev-belay",
                                 "display_name": "jev-belay", "scope": scope, "tenant_id": tenant}]


def test_a_removed_or_unsigned_install_does_not_count(setup_client, db) -> None:
    seed_engine_document(db)
    seed_install(db, new_install_id(), name="gone", state=ps.STATE_REMOVED)
    seed_install(db, new_install_id(), name="forged", signed=False)
    seed_install(db, new_install_id(), name="also-built-in", built_in=True)
    assert step(setup_client)["state"] == "todo"


def test_the_step_serves_the_active_bundled_installs_names(setup_client, db) -> None:
    seed_engine_document(db)
    seed_bundled(db)
    seed_install(db, "builtin:retired", name="retired", built_in=True, build="image",
                 state=ps.STATE_REMOVED)
    seed_install(db, "builtin:forged", name="forged", built_in=True, signed=False)
    seed_install(db, new_install_id(), name="jev-belay")
    plugins = step(setup_client)
    assert plugins["bundled"] == ["gstack", "security-audit"]
    assert [p["name"] for p in plugins["added"]] == ["jev-belay"]


def test_the_step_serves_no_bundled_names_when_none_is_installed(setup_client, db) -> None:
    seed_engine_document(db)
    seed_install(db, new_install_id(), name="jev-belay")
    assert step(setup_client)["bundled"] == []


def test_skip_is_remembered_and_audited_once(setup_client, db) -> None:
    seed_engine_document(db)
    answer = setup_client.post(SKIP, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert set(db.docs[DOC]) == {"skipped_at", "skipped_by"}
    assert db.docs[DOC]["skipped_by"] == ROOT
    plugins = step(setup_client)
    assert plugins["state"] == "skipped"
    assert plugins["skipped"]["skipped_by"] == ROOT
    rows = audit_rows(db, "setup_step_skipped")
    assert len(rows) == 1
    assert rows[0]["by"] == ROOT and rows[0]["target"] == "plugins"
    assert rows[0]["detail"] == {"step": "plugins", "env": ENV}
    # A second skip changes nothing and audits nothing.
    first = dict(db.docs[DOC])
    assert setup_client.post(SKIP, headers=auth_header("root")).status_code == 200
    assert db.docs[DOC] == first
    assert len(audit_rows(db, "setup_step_skipped")) == 1


def test_an_added_install_wins_over_a_skip(setup_client, db) -> None:
    seed_engine_document(db)
    assert setup_client.post(SKIP, headers=auth_header("root")).status_code == 200
    seed_install(db, new_install_id(), name="jev-enforce")
    assert step(setup_client)["state"] == "done"


def test_undo_forgets_the_skip_and_is_audited(setup_client, db) -> None:
    seed_engine_document(db)
    # Nothing skipped: the undo writes and audits nothing.
    assert setup_client.delete(SKIP, headers=auth_header("root")).status_code == 200
    assert DOC not in db.docs and audit_rows(db, "setup_step_unskipped") == []
    assert setup_client.post(SKIP, headers=auth_header("root")).status_code == 200
    answer = setup_client.delete(SKIP, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert DOC not in db.docs
    assert step(setup_client)["state"] == "todo"
    rows = audit_rows(db, "setup_step_unskipped")
    assert len(rows) == 1
    assert rows[0]["by"] == ROOT and rows[0]["detail"] == {"step": "plugins", "env": ENV}


def test_finish_never_waits_on_the_plugin_step(setup_client, db, github, pool, runs) -> None:
    """Ready is the eight required lines; the optional ninth is never one."""
    finished_install(setup_client, db, github, pool, runs)
    assert step(setup_client)["state"] == "todo"
    done = setup_client.post("/v1/admin/setup/finish", headers=auth_header("root"))
    assert done.status_code == 200, done.text
    assert "plugins" not in str(done.json())
    assert step(setup_client)["state"] == "todo"


def test_the_skip_document_takes_only_its_rows_fields(db) -> None:
    record = ir.InstallRecord(db, ENV)
    with pytest.raises(ir.RecordFieldRefused):
        record.write(record.console_path(ir.PLUGINS_STEP), {"plugins": ["x"]})
    record.write(record.console_path(ir.PLUGINS_STEP), {"skipped_at": "t", "skipped_by": ROOT})
    assert db.docs[DOC] == {"skipped_at": "t", "skipped_by": ROOT}
