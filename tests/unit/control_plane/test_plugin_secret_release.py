"""The plugins' secrets in an attempt's release (docs/design/managed-plugins.md §6.2 and §8, lane 6).

`POST /v1/attempts/{attempt_id}/secrets:release` also releases, per managed
plugin the task's SIGNED plugin set (`metadata.dispatch.plugins`) names, the
secrets and file settings that entry references, read from Secret Manager
under the frozen contract's name `Tenant.plugin_secret_name(install_id,
field)` of the task's own tenant, and nothing else:

  * a plugin the set does not name is released nothing, though its secret
    is in the same workspace;
  * another workspace's secret for the same plugin and field is never read;
  * a binding to a workspace key is not a value: nothing is read for it;
  * a file whose content is not the signed digest is withheld;
  * a rewritten plugin set fails the spec signature and releases nothing;
  * one release row per secret, naming ids, never a value.

Drives the real route with the helpers of test_secret_release (the signed
task, the attempt key, the in-memory Secret Manager). Every value is built
at runtime.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from swarm_api.pluginconfig import plugin_consumer, secret_name
from swarm_api.routes.attempts_secrets import ReadUnavailable

from .conftest import PROJECT
from .test_child_tasks_api import worker_tokens  # noqa: F401  (a fixture)
from .test_secret_release import (  # noqa: F401  (client and sm are fixtures)
    add_secret,
    client,
    fake_value,
    marks,
    release,
    rows,
    running,
    sm,
)

LINT = "w-0123456789ab"
AUDIT = "p-ba9876543210"
IDLE = "w-aaaaaaaaaaaa"


def put_plugin_secret(sm, tenant_id: str, install_id: str, field: str, payload: bytes) -> str:
    """Store a plugin secret as the settings route does: under its contract name."""
    name = secret_name(tenant_id, install_id, field)
    sm.payloads[f"projects/{PROJECT}/secrets/{name}/versions/latest"] = payload
    return name


def plugin_set(**entries: dict[str, Any]) -> dict[str, Any]:
    return {"dispatch": {"plugins": entries}}


def entry(*, secrets_of: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": "kit", "scope": "workspace", "version_id": "v-0123456789ab", "runnable_sha256": "0" * 64,
            "object": "o", "env": {}, "files": files or {}, "secrets": secrets_of or {}, "skills": None,
            "disabled_skills": []}


def plugin_items(response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return response.json()["plugin_secrets"]


def test_an_attempt_receives_its_plugins_secrets_and_nothing_else(client, db, sm):
    lint_value, audit_value, idle_value = fake_value("a"), fake_value("b"), fake_value("c")
    lint_name = put_plugin_secret(sm, "eng", LINT, "api_key", lint_value.encode())
    audit_name = put_plugin_secret(sm, "eng", AUDIT, "token", audit_value.encode())
    # Bound to the plugin set's other field, and a plugin the set does not name: never read.
    idle_name = put_plugin_secret(sm, "eng", IDLE, "api_key", idle_value.encode())
    other_tenant = put_plugin_secret(sm, "research", LINT, "api_key", fake_value("d").encode())
    attempt = running(client, db, metadata=plugin_set(**{
        LINT: entry(secrets_of={
            "CLAUDE_PLUGIN_OPTION_API_KEY": {"field": "api_key", "target": "env", "secret": "plugin"},
            "LINT_BOUND": {"field": "bound", "target": "env", "binding": "provider_key:typesafe"}}),
        AUDIT: entry(secrets_of={"AUDIT_TOKEN": {"field": "token", "target": "file", "secret": "plugin"}}),
    }))
    response = release(client, attempt)
    items = plugin_items(response)
    # In install-id order: the platform plugin's, then the workspace's.
    assert items == [
        {"name": audit_name, "install_id": AUDIT, "field": "token", "variable": "AUDIT_TOKEN",
         "target": "file", "kind": "plugin_secret", "consumer": plugin_consumer(AUDIT), "value": audit_value},
        {"name": lint_name, "install_id": LINT, "field": "api_key", "variable": "CLAUDE_PLUGIN_OPTION_API_KEY",
         "target": "env", "kind": "plugin_secret", "consumer": plugin_consumer(LINT), "value": lint_value},
    ]
    assert idle_value not in response.text
    read = set(sm.reads)
    assert not any(idle_name in r or other_tenant in r for r in read)
    assert not any("/secrets/" + secret_name("eng", LINT, "bound") in r for r in read)
    # The workspace secrets the attempt has no grant to are not in it either.
    assert response.json()["secrets"] == []


def test_a_release_row_per_plugin_secret_names_ids_never_a_value(client, db, sm):
    value = fake_value()
    name = put_plugin_secret(sm, "eng", LINT, "api_key", value.encode())
    attempt = running(client, db, metadata=plugin_set(**{LINT: entry(secrets_of={
        "CLAUDE_PLUGIN_OPTION_API_KEY": {"field": "api_key", "target": "env", "secret": "plugin"}})}))
    assert len(plugin_items(release(client, attempt))) == 1
    [row] = rows(db)
    assert row["secret_id"] == name and row["tenant_id"] == "eng" and row["secret_tenant_id"] == "eng"
    assert row["attempt_id"] == attempt.tuple.attempt_id and row["generation"] == attempt.tuple.generation
    assert row["consumers"] == [plugin_consumer(LINT)] and row["slot"] == "CLAUDE_PLUGIN_OPTION_API_KEY"
    assert [m["released"] for m in marks(db)] == [1]
    assert not any(value in repr(d) for d in db.docs.values())
    # Once per attempt: the second call is refused and reads nothing.
    reads = len(sm.reads)
    assert release(client, attempt).status_code == 410
    assert len(sm.reads) == reads


def test_a_file_setting_is_released_only_with_its_signed_digest(client, db, sm):
    content = ("rules: " + secrets.token_hex(8)).encode()
    name = put_plugin_secret(sm, "eng", LINT, "rules", content)
    signed = hashlib.sha256(content).hexdigest()
    attempt = running(client, db, metadata=plugin_set(**{LINT: entry(files={
        "LINT_RULES": {"field": "rules", "sha256": signed}})}))
    [item] = plugin_items(release(client, attempt))
    assert (item["name"], item["kind"], item["target"], item["value"]) == (
        name, "plugin_file", "file", content.decode())


def test_a_file_changed_since_the_step_was_signed_is_withheld(client, db, sm):
    name = put_plugin_secret(sm, "eng", LINT, "rules", b"changed after submission")
    attempt = running(client, db, metadata=plugin_set(**{LINT: entry(files={
        "LINT_RULES": {"field": "rules", "sha256": hashlib.sha256(b"what was signed").hexdigest()}})}))
    response = release(client, attempt)
    assert plugin_items(response) == []
    assert {"name": name, "reason": "plugin_file_changed"} in response.json()["withheld"]
    assert "changed after submission" not in response.text


def test_a_plugin_set_rewritten_after_signing_releases_nothing(client, db, sm):
    put_plugin_secret(sm, "eng", IDLE, "api_key", fake_value().encode())
    attempt = running(client, db, metadata=plugin_set(**{LINT: entry()}))
    doc = db.docs[f"tasks/{attempt.tuple.task_id}"]
    doc["metadata"]["dispatch"]["plugins"][IDLE] = entry(secrets_of={
        "X": {"field": "api_key", "target": "env", "secret": "plugin"}})
    response = release(client, attempt)
    assert response.status_code != 200, response.text
    assert sm.reads == [] and rows(db) == []


def test_a_malformed_reference_is_skipped_never_guessed_at(client, db, sm):
    put_plugin_secret(sm, "eng", LINT, "api_key", fake_value().encode())
    attempt = running(client, db, metadata=plugin_set(**{
        LINT: entry(secrets_of={
            "lower_case": {"field": "api_key", "secret": "plugin"},
            "NO_FIELD": {"secret": "plugin"},
            "SLASHED": {"field": "../api_key", "secret": "plugin"},
            "NOT_PLUGIN": {"field": "api_key", "secret": "workspace"},
        }),
        "not-an-install": entry(secrets_of={"X": {"field": "api_key", "secret": "plugin"}}),
    }))
    assert plugin_items(release(client, attempt)) == [] and sm.reads == []


def test_a_plugin_secret_removed_after_submission_is_withheld_and_the_rest_released(client, db, sm):
    value = fake_value()
    put_plugin_secret(sm, "eng", AUDIT, "token", value.encode())
    missing = secret_name("eng", LINT, "api_key")

    def access(version: str) -> bytes:
        sm.reads.append(version)
        if missing in version:
            raise ReadUnavailable("version_unavailable")
        return sm.payloads[version]

    sm.access = access
    attempt = running(client, db, metadata=plugin_set(**{
        LINT: entry(secrets_of={"A": {"field": "api_key", "secret": "plugin"}}),
        AUDIT: entry(secrets_of={"B": {"field": "token", "secret": "plugin"}}),
    }))
    response = release(client, attempt)
    assert [i["value"] for i in plugin_items(response)] == [value]
    assert {"name": missing, "reason": "plugin_secret_unavailable"} in response.json()["withheld"]


def test_a_plugin_secret_swarm_api_may_not_read_yet_is_503_and_nothing_released(client, db, sm):
    put_plugin_secret(sm, "eng", LINT, "api_key", fake_value().encode())
    workspace_value = fake_value("w")
    add_secret(db, sm, "eng", "brave-key", workspace_value, grants=("profile:claude-code",))
    attempt = running(client, db, metadata=plugin_set(**{LINT: entry(secrets_of={
        "A": {"field": "api_key", "secret": "plugin"}})}))
    real = sm.access

    def access(version: str) -> bytes:
        if "-plg-" in version:
            raise ReadUnavailable("permission_denied")
        return real(version)

    sm.access = access
    response = release(client, attempt)
    assert response.status_code == 503, response.text
    assert workspace_value not in response.text and rows(db) == [] and marks(db) == []


def test_a_task_with_no_plugin_set_is_released_no_plugin_secret(client, db, sm):
    put_plugin_secret(sm, "eng", LINT, "api_key", fake_value().encode())
    attempt = running(client, db)
    assert plugin_items(release(client, attempt)) == [] and sm.reads == []

