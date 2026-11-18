"""The managed plugin catalogue's store (swarm_api.pluginstore): MAC'd records,
the intake report as untrusted data, the update diff and removal.

docs/design/managed-plugins.md sections 1, 2.3, 9 and 10. Offline: FakeFirestore.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import pluginstore as ps
from swarm_api.judgment import SettingsMac

from .test_settings_mac import new_key

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
SHA = hashlib.sha256(b"runnable").hexdigest()


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(db, clock) -> ps.PluginStore:
    return ps.PluginStore(db, now=clock, mac=SettingsMac(keys=(new_key(),)))


def _intake(store: ps.PluginStore, **over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"scope": ps.PLATFORM, "tenant_id": None, "install_id": None,
                              "source": {"kind": "github", "repository": "example-org/kit", "commit": "a" * 40},
                              "subdir": "", "overlay": None, "by": "root@example.com", "awaiting_upload": False}
    fields.update(over)
    return store.open_intake(**fields)


def _report(intake: dict[str, Any], **over: Any) -> dict[str, Any]:
    report = {
        "schema": ps.REPORT_SCHEMA, "intake_id": intake["intake_id"], "scope": intake["scope"],
        "tenant_id": intake["tenant_id"], "runnable_object": f"staging/{intake['intake_id']}/runnable.tar",
        "limits": dict(ps.LIMITS), "outcome": "pinned", "refusal": None,
        "source_sha256": SHA, "source_bytes": 10, "runnable_sha256": SHA, "runnable_bytes": 8,
        "runnable_format": "canonical-tar", "files": 2, "unpacked_bytes": 100,
        "manifest": {"name": "kit", "version": "1.0.0", "user_config": []}, "manifest_source": "plugin",
        "components": {"skills": [], "commands": [], "agents": [], "hooks": [], "mcp_servers": []},
        "overlay": None, "requires": {"suggested": {"hosts": []}, "declared": None},
    }
    report.update(over)
    return report


def _raw(report: dict[str, Any]) -> bytes:
    return json.dumps(report).encode()


# --- the report, as untrusted data ----------------------------------------------


def test_a_valid_report_parses(store):
    intake = _intake(store)
    assert ps.parse_report(_raw(_report(intake)), intake)["manifest"]["name"] == "kit"


@pytest.mark.parametrize(("raw", "code"), [
    (b"not json", "report_invalid"),
    (b"\xff\xfe", "report_invalid"),
    (b"[]", "report_invalid"),
    (b" " * (ps.MAX_REPORT_BYTES + 1), "report_invalid"),
])
def test_a_report_that_is_not_one_is_refused(store, raw, code):
    with pytest.raises(ps.PluginRefused) as refused:
        ps.parse_report(raw, _intake(store))
    assert refused.value.code == code


@pytest.mark.parametrize(("over", "code"), [
    ({"schema": "swarm.plugin-intake.report/2"}, "report_invalid"),
    ({"tenant_id": "eng"}, "report_mismatch"),
    ({"limits": {**ps.LIMITS, "max_packed_bytes": 1}}, "report_limits_differ"),
    ({"source_bytes": ps.MAX_PACKED_BYTES + 1}, "report_limits_differ"),
    ({"files": True}, "report_invalid"),
    ({"runnable_sha256": "A" * 64}, "report_invalid"),
    ({"manifest": {"name": "Not A Name"}}, "report_invalid"),
    ({"components": {"skills": []}}, "report_invalid"),
    ({"outcome": "refused", "refusal": {"code": "Bad Code"}}, "report_invalid"),
    ({"outcome": "maybe"}, "report_invalid"),
])
def test_a_report_outside_the_closed_schema_is_refused_by_code_alone(store, over, code):
    intake = _intake(store)
    with pytest.raises(ps.PluginRefused) as refused:
        ps.parse_report(_raw(_report(intake, **over)), intake)
    assert refused.value.code == code
    # Never a byte of the report in what is answered.
    assert str(refused.value) == ps.REFUSALS[code] and refused.value.detail == {}


# --- MAC'd records -------------------------------------------------------------


def test_with_no_settings_key_nothing_is_signed(db, clock):
    keyless = ps.PluginStore(db, now=clock, mac=SettingsMac(keys=()))
    with pytest.raises(ps.PluginRefused) as refused:
        _intake(keyless)
    assert refused.value.code == "settings_key_missing" and refused.value.status_code == 503


def test_a_pinned_version_is_bound_to_its_install_and_a_copy_elsewhere_is_absent(db, store):
    intake = _intake(store)
    version = store.pin(intake["intake_id"], _report(intake), f"platform/{SHA}.tar")
    install_id, version_id = version["install_id"], version["version_id"]
    assert store.version(install_id, version_id)["runnable_sha256"] == SHA
    # The same signed document copied under another install does not verify.
    other = ps.new_install_id(ps.PLATFORM)
    db.docs[f"plugin_installs/{other}/versions/{version_id}"] = dict(
        db.docs[f"plugin_installs/{install_id}/versions/{version_id}"])
    assert store.version(other, version_id) is None
    # Nor does an edited digest.
    db.docs[f"plugin_installs/{install_id}/versions/{version_id}"]["runnable_sha256"] = "0" * 64
    assert store.version(install_id, version_id) is None


def test_a_second_pin_or_refusal_of_an_ended_intake_changes_nothing(store):
    intake = _intake(store)
    assert store.pin(intake["intake_id"], _report(intake), f"platform/{SHA}.tar") is not None
    assert store.pin(intake["intake_id"], _report(intake), f"platform/{SHA}.tar") is None
    assert store.refuse(intake["intake_id"], "intake_timed_out") is None
    assert store.intake(intake["intake_id"])["state"] == ps.INTAKE_PINNED


def test_an_intake_times_out_at_its_deadline(store, clock):
    intake = _intake(store)
    assert not store.timed_out(intake)
    clock.at = NOW + ps.INTAKE_DEADLINE + timedelta(seconds=1)
    assert store.timed_out(intake)
    waiting = _intake(store, awaiting_upload=True)
    clock.at += ps.UPLOAD_DEADLINE - timedelta(seconds=1)
    assert not store.timed_out(waiting)


def test_a_refusal_code_must_be_a_known_one():
    with pytest.raises(KeyError):
        ps.PluginRefused("anything at all")


# --- the diff ---------------------------------------------------------------------


def _version(**over: Any) -> dict[str, Any]:
    version: dict[str, Any] = {"version_id": "v-000000000001", "manifest": {"name": "kit", "version": "1",
                                                                         "user_config": []},
                               "components": {"skills": [{"name": "a"}], "commands": [], "agents": [],
                                              "hooks": [], "mcp_servers": []},
                               "requires": {}, "source": {"repository": "example-org/kit", "commit": "a" * 40}}
    version.update(over)
    return version


def test_a_new_sensitive_field_is_marked():
    old = _version()
    new = _version(version_id="v-000000000002", manifest={"name": "kit", "version": "2", "user_config": [
        {"key": "api_key", "type": "string", "sensitive": True}]})
    diff = ps.version_diff(old, new)
    assert diff["settings"]["added"] == ["api_key"] and diff["marked"] == ["sensitive"]


def test_a_field_made_sensitive_and_a_server_change_are_marked():
    old = _version(manifest={"name": "kit", "user_config": [{"key": "url", "type": "string"}]},
                   components={"skills": [], "commands": [], "agents": [], "hooks": [],
                               "mcp_servers": [{"name": "db", "command": "./db"}]})
    new = _version(manifest={"name": "kit", "user_config": [{"key": "url", "type": "string", "sensitive": True}]},
                   components={"skills": [], "commands": [], "agents": [], "hooks": [],
                               "mcp_servers": [{"name": "db", "command": "./db2"}]})
    diff = ps.version_diff(old, new)
    assert diff["mcp_servers"]["changed"] == ["db"]
    assert diff["settings"]["newly_sensitive"] == ["url"]
    assert diff["marked"] == ["mcp_servers", "sensitive"]


def test_a_skill_change_alone_is_not_marked_and_the_digest_follows_the_diff():
    old = _version()
    new = _version(components={"skills": [{"name": "b"}], "commands": [], "agents": [], "hooks": [],
                               "mcp_servers": []})
    diff = ps.version_diff(old, new)
    assert diff["skills"] == {"added": ["b"], "removed": ["a"]} and diff["needs_confirmation"] is False
    assert ps.diff_digest(diff) == ps.diff_digest(json.loads(json.dumps(diff)))
    assert ps.diff_digest(diff) != ps.diff_digest(ps.version_diff(old, old))


# --- removal ------------------------------------------------------------------------


def test_refuse_removed_names_only_removed_installs_visible_to_the_workspace(store):
    intake = _intake(store, scope=ps.WORKSPACE, tenant_id="eng")
    version = store.pin(intake["intake_id"], _report(intake), f"tenants/eng/{SHA}.tar")
    install_id = version["install_id"]
    ps.refuse_removed(store, "eng", [install_id, "gstack", "w-ffffffffffff", 7])
    store.remove(install_id, by="root@example.com")
    with pytest.raises(ps.PluginRefused) as refused:
        ps.refuse_removed(store, "eng", ["gstack", install_id], field="capabilities.plugins")
    assert refused.value.code == "plugin_removed"
    assert refused.value.detail == {"install_id": install_id, "field": "capabilities.plugins"}
    ps.refuse_removed(store, "research", [install_id])
    with pytest.raises(ps.PluginRefused) as again:
        store.remove(install_id, by="root@example.com")
    assert again.value.code == "install_not_active"
