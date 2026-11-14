"""Contract requests 140-144: the managed plugins design's five requests.

docs/design/managed-plugins.md section 14, requests P1-P5, accepted by the
owner (the owner) on 6 November 2026 ("accept all five"):

  * 139 (P1) `SETTINGS_MAC_FIELDS["plugins"]` gains `defaults` and
    `configs_digest` (pinned in test_settings_block.py);
  * 140 (P2) `canonical_plugin_record(kind, fields)`, the bytes swarm-api MACs
    for a plugin install, version and intake under `swarm-settings-key`;
  * 141 (P3) `Tenant.plugin_secret_name(install_id, field)` and the secret
    consumer kind `plugin`;
  * 142 (P4) the five claude-code profiles' `runtime_capabilities` (pinned in
    test_runtime_capabilities_contract.py);
  * 143 (P5) `canonical_platform_settings(fields)`, the bytes swarm-api MACs
    for the platform switches.

And contract request 153 (P6), accepted by the owner on 9 November 2026: the
plugin record kind `bundle`, a platform-scope `plugin_bundles/{bundle_id}`
record MAC'd in P2's canonical form (design sections 1.4.3 and 14).

The bytes are pinned literally: a MAC is over bytes, so a change to them is a
new purpose, never an edit to this one.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone

import pytest

from swarm_api import pluginstore
from swarm_common import models, specsign
from swarm_common.models import Tenant, consumer_name, parse_consumer
from swarm_common.specsign import (
    PLATFORM_SETTINGS_MAC_FIELDS,
    PLATFORM_SETTINGS_PURPOSE,
    PLUGIN_RECORD_IDS,
    PLUGIN_RECORD_KINDS,
    PLUGIN_RECORD_PURPOSE,
    PLUGIN_RECORD_UNCOVERED,
    PLUGIN_SCOPES,
    SETTINGS_PURPOSE,
    SPEC_PURPOSE,
    SpecNotCanonical,
    canonical_platform_settings,
    canonical_plugin_record,
)

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
SHA = "a" * 64

INSTALL = {
    "install_id": "w-0123456789ab",
    "scope": "workspace",
    "tenant_id": "team-a",
    "name": "jev-enforce",
    "display_name": "Jev enforce",
    "source": {"kind": "github", "repository": "acme/jev-enforce", "ref": "v1.0.0"},
    "current_version": "v-1",
    "built_in": False,
    "state": "active",
    "created_by": "alice@example.com",
    "created_at": NOW,
    "updated_by": "alice@example.com",
    "updated_at": NOW,
    "mac": {"v": 1, "key": "k", "sig": "s"},
}

VERSION = {
    "install_id": "w-0123456789ab",
    "version_id": "v-1",
    "scope": "workspace",
    "tenant_id": "team-a",
    "source_sha256": SHA,
    "runnable_sha256": SHA,
    "object": "tenants/team-a/" + SHA + ".tar",
    "bytes": 1024,
    "created_at": NOW,
}

INTAKE = {"intake_id": "i-1", "scope": "platform", "tenant_id": None, "state": "fetching"}

#: A `plugin_bundles/{bundle_id}` record as design section 1.4.3 describes it:
#: the bundle key (each bundled install's version, source and recipe), the
#: base it was built FROM, the output digest per runner image, the built
#: tree per plugin and the state. Platform scope, no tenant.
BUNDLE = {
    "bundle_id": "b-0123456789ab",
    "scope": "platform",
    "tenant_id": None,
    "key": {
        "builtin:gstack": {"version_id": "v-1", "source_sha256": SHA, "recipe_sha256": SHA},
        "builtin:security-audit": {"version_id": "v-1", "source_sha256": SHA, "recipe_sha256": SHA},
    },
    "base": {"agent-runtime-base": "sha256:" + SHA},
    "outputs": {"agent-runtime-base": "sha256:" + SHA},
    "trees": {"builtin:gstack": SHA, "builtin:security-audit": SHA},
    "state": "building",
    "created_by": "admin@example.com",
    "created_at": NOW,
}


# --------------------------------------------------------------------------
# Request 141 (P2): canonical_plugin_record
# --------------------------------------------------------------------------


def test_the_record_constants_are_the_request():
    assert PLUGIN_RECORD_PURPOSE == "swarm.plugin-record/v1"
    assert PLUGIN_RECORD_PURPOSE not in (SPEC_PURPOSE, SETTINGS_PURPOSE, PLATFORM_SETTINGS_PURPOSE)
    assert PLUGIN_RECORD_KINDS == ("install", "version", "intake", "bundle")
    assert PLUGIN_SCOPES == ("platform", "workspace")
    assert dict(PLUGIN_RECORD_IDS) == {
        "install": ("install_id",),
        "version": ("install_id", "version_id"),
        "intake": ("intake_id",),
        "bundle": ("bundle_id",),
    }
    assert PLUGIN_RECORD_UNCOVERED == ("mac", "created_at", "created_by", "updated_at", "updated_by")
    with pytest.raises(TypeError):
        PLUGIN_RECORD_IDS["install"] = ()  # type: ignore[index]


def test_an_install_records_bytes_are_stable():
    assert canonical_plugin_record("install", INSTALL) == (
        b'{"fields":{"built_in":false,"current_version":"v-1","display_name":"Jev enforce",'
        b'"install_id":"w-0123456789ab","name":"jev-enforce",'
        b'"source":{"kind":"github","ref":"v1.0.0","repository":"acme/jev-enforce"},'
        b'"state":"active"},"kind":"install","purpose":"swarm.plugin-record/v1",'
        b'"scope":"workspace","tenant_id":"team-a"}'
    )


def test_a_version_and_an_intake_have_their_own_bytes():
    assert canonical_plugin_record("version", VERSION) == (
        b'{"fields":{"bytes":1024,"install_id":"w-0123456789ab",'
        b'"object":"tenants/team-a/' + SHA.encode() + b'.tar",'
        b'"runnable_sha256":"' + SHA.encode() + b'","source_sha256":"' + SHA.encode() + b'",'
        b'"version_id":"v-1"},"kind":"version","purpose":"swarm.plugin-record/v1",'
        b'"scope":"workspace","tenant_id":"team-a"}'
    )
    assert canonical_plugin_record("intake", INTAKE) == (
        b'{"fields":{"intake_id":"i-1","state":"fetching"},"kind":"intake",'
        b'"purpose":"swarm.plugin-record/v1","scope":"platform","tenant_id":null}'
    )


def test_scope_and_tenant_are_inside_the_bytes():
    out = json.loads(canonical_plugin_record("install", INSTALL))
    assert (out["scope"], out["tenant_id"]) == ("workspace", "team-a")
    # A record copied onto another workspace, or lifted to platform scope, does not verify.
    other = canonical_plugin_record("install", {**INSTALL, "tenant_id": "team-b"})
    platform = canonical_plugin_record("install", {**INSTALL, "scope": "platform", "tenant_id": None})
    assert len({canonical_plugin_record("install", INSTALL), other, platform}) == 3


def test_the_kind_is_inside_the_bytes():
    """An intake's MAC never verifies an install, nor an install's a version, nor any a bundle."""
    every = {**VERSION, "intake_id": "i-1", "bundle_id": "b-1", "scope": "platform", "tenant_id": None}
    assert len({canonical_plugin_record(k, every) for k in PLUGIN_RECORD_KINDS}) == 4


def test_every_stored_key_is_covered_but_the_mac_and_the_writer_record():
    plain = canonical_plugin_record("install", INSTALL)
    # Who and when changed nothing a reader acts on, and the MAC cannot cover itself.
    touched = {**INSTALL, "updated_by": "mallory@example.com", "mac": {"v": 2}}
    touched.pop("created_at")
    assert canonical_plugin_record("install", touched) == plain
    # Any other key is covered, including one no writer wrote: a planted key does not verify.
    for key, value in (("state", "removed"), ("current_version", "v-2"), ("planted", True)):
        assert canonical_plugin_record("install", {**INSTALL, key: value}) != plain
    # A version's digests and object: the swap a forger wants.
    for key in ("runnable_sha256", "object"):
        assert canonical_plugin_record("version", {**VERSION, key: "b" * 64}) != \
            canonical_plugin_record("version", VERSION)


def test_key_order_is_irrelevant():
    reordered = dict(reversed(list(INSTALL.items())))
    assert canonical_plugin_record("install", reordered) == canonical_plugin_record("install", INSTALL)


@pytest.mark.parametrize("kind", ["", "Install", "settings", "platform", None])
def test_an_unknown_kind_has_no_canonical_form(kind):
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record(kind, INSTALL)  # type: ignore[arg-type]


@pytest.mark.parametrize("change", [
    {"scope": None},
    {"scope": "tenant"},
    {"scope": "workspace", "tenant_id": None},
    {"scope": "workspace", "tenant_id": ""},
    {"scope": "workspace", "tenant_id": 7},
    {"scope": "platform", "tenant_id": "team-a"},
    {"install_id": None},
    {"install_id": ""},
    {"install_id": 7},
])
def test_a_record_without_a_coherent_scope_or_id_has_no_canonical_form(change):
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("install", {**INSTALL, **change})


def test_each_kind_needs_its_own_ids():
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("version", {k: v for k, v in VERSION.items() if k != "version_id"})
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("intake", {k: v for k, v in INTAKE.items() if k != "intake_id"})
    # A new install's intake names no install yet.
    assert b'"install_id"' not in canonical_plugin_record("intake", INTAKE)


def test_a_platform_record_may_omit_its_tenant():
    no_tenant = {k: v for k, v in INTAKE.items() if k != "tenant_id"}
    assert canonical_plugin_record("intake", no_tenant) == canonical_plugin_record("intake", INTAKE)


def test_fields_that_are_not_a_map_or_hold_a_timestamp_have_no_canonical_form():
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("install", [INSTALL])  # type: ignore[arg-type]
    # A covered timestamp has no JSON form: only the writer-record keys may hold one.
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("install", {**INSTALL, "removed_at": NOW})


# --------------------------------------------------------------------------
# Request 153 (P6): the plugin record kind `bundle`
# --------------------------------------------------------------------------


def test_a_bundle_records_bytes_are_stable():
    digest = b'"sha256:' + SHA.encode() + b'"'
    pins = b'{"recipe_sha256":"' + SHA.encode() + b'","source_sha256":"' + SHA.encode() + b'","version_id":"v-1"}'
    assert canonical_plugin_record("bundle", BUNDLE) == (
        b'{"fields":{"base":{"agent-runtime-base":' + digest + b'},"bundle_id":"b-0123456789ab",'
        b'"key":{"builtin:gstack":' + pins + b',"builtin:security-audit":' + pins + b'},'
        b'"outputs":{"agent-runtime-base":' + digest + b'},"state":"building",'
        b'"trees":{"builtin:gstack":"' + SHA.encode() + b'","builtin:security-audit":"' + SHA.encode() + b'"}},'
        b'"kind":"bundle","purpose":"swarm.plugin-record/v1","scope":"platform","tenant_id":null}'
    )


def test_a_signed_bundle_record_verifies_and_a_tampered_one_does_not():
    """swarm-api's MAC over the contract's bytes: a tenant worker that rewrites a
    bundle (a forged `ready`, another output digest) holds no key to re-sign it."""
    keys = ("settings-" + secrets.token_hex(24),)
    signed = {**BUNDLE, "mac": pluginstore.record_mac("bundle", BUNDLE, keys)}
    assert pluginstore.record_verifies("bundle", signed, keys)
    # The writer record is not covered: a later writer's name changes nothing.
    assert pluginstore.record_verifies("bundle", {**signed, "updated_by": "other@example.com"}, keys)
    for key, value in (
        ("state", "ready"),
        ("outputs", {"agent-runtime-base": "sha256:" + "b" * 64}),
        ("key", {**BUNDLE["key"], "builtin:gstack": {"version_id": "v-2", "source_sha256": SHA,
                                                      "recipe_sha256": SHA}}),
        ("base", {"agent-runtime-base": "sha256:" + "c" * 64}),
        ("trees", {**BUNDLE["trees"], "builtin:gstack": "d" * 64}),
        ("bundle_id", "b-other"),
        ("planted", True),
    ):
        assert not pluginstore.record_verifies("bundle", {**signed, key: value}, keys), key
    # Another key, or the record read as another kind, does not verify.
    assert not pluginstore.record_verifies("bundle", signed, ("settings-" + secrets.token_hex(24),))
    assert not pluginstore.record_verifies("intake", {**signed, "intake_id": "b-0123456789ab"}, keys)


@pytest.mark.parametrize("change", [
    {"scope": "workspace", "tenant_id": "team-a"},
    {"scope": "platform", "tenant_id": "team-a"},
    {"scope": None},
    {"bundle_id": None},
    {"bundle_id": ""},
])
def test_a_bundle_is_platform_scope_only_and_names_its_id(change):
    """Design section 1.4.2: a bundle image is built once per deployment, so a
    bundle record is never a workspace's, and a workspace-scope one has no bytes
    any MAC could cover."""
    with pytest.raises(SpecNotCanonical):
        canonical_plugin_record("bundle", {**BUNDLE, **change})


# --------------------------------------------------------------------------
# Request 144 (P5): canonical_platform_settings
# --------------------------------------------------------------------------


def test_the_platform_settings_bytes_are_stable():
    assert PLATFORM_SETTINGS_PURPOSE == "swarm.platform-settings/v1"
    assert PLATFORM_SETTINGS_MAC_FIELDS == ("work_contracts", "contract_review")
    out = canonical_platform_settings({
        "work_contracts": "on", "contract_review": "report",
        "set_by": "root@example.com", "set_at": NOW, "mac": {"v": 1},
    })
    assert out == (
        b'{"fields":{"contract_review":"report","work_contracts":"on"},'
        b'"purpose":"swarm.platform-settings/v1"}'
    )


def test_an_unset_switch_projects_as_null_and_a_change_changes_the_bytes():
    unset = canonical_platform_settings({})
    assert json.loads(unset)["fields"] == {"work_contracts": None, "contract_review": None}
    on = canonical_platform_settings({"work_contracts": "on", "contract_review": "enforce"})
    off = canonical_platform_settings({"work_contracts": "off", "contract_review": "enforce"})
    assert len({unset, on, off}) == 3
    # A key outside the list is not covered and must not be acted on.
    assert canonical_platform_settings(
        {"work_contracts": "on", "contract_review": "enforce", "jev": "on"}) == on


def test_platform_settings_never_read_as_a_workspace_block():
    platform = canonical_platform_settings({"contract_review": "report"})
    block = specsign.canonical_settings_block(
        tenant_id="team-a", block="contract_review", fields={"enabled": True})
    assert platform != block
    assert b"tenant_id" not in platform


def test_platform_settings_that_are_not_a_map_have_no_canonical_form():
    with pytest.raises(SpecNotCanonical):
        canonical_platform_settings(["work_contracts"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Request 142 (P3): the plugin secret name and the consumer kind
# --------------------------------------------------------------------------


def _tenant(tenant_id: str = "team-a") -> Tenant:
    return Tenant(tenant_id=tenant_id, kind="group", principal="team-a@example.com", created_at=NOW)


def test_the_plugin_secret_names_shape():
    name = _tenant().plugin_secret_name("w-0123456789ab", "TYPESAFE_API_KEY")
    want = hashlib.sha256(b"w-0123456789ab/TYPESAFE_API_KEY").hexdigest()[:16]
    assert name == "swarm-tenant-team-a-plg-" + want
    assert name.startswith(_tenant().secret_name("plg-"))


def test_the_name_is_derived_from_the_workspace_the_plugin_and_the_field():
    names = {
        _tenant().plugin_secret_name("w-0123456789ab", "A"),
        _tenant().plugin_secret_name("w-0123456789ab", "B"),
        _tenant().plugin_secret_name("w-ba9876543210", "A"),
        _tenant("team-b").plugin_secret_name("w-0123456789ab", "A"),
    }
    assert len(names) == 4
    assert _tenant().plugin_secret_name("w-0123456789ab", "A") == \
        _tenant().plugin_secret_name("w-0123456789ab", "A")


@pytest.mark.parametrize("install_id,field", [
    ("", "A"), ("w-0123456789ab", ""), (None, "A"), ("w-0123456789ab", 7),
    # The separator inside an install id would let ("a/b", "c") name ("a", "b/c")'s secret.
    ("w-0123/x", "A"),
])
def test_a_malformed_install_or_field_has_no_secret_name(install_id, field):
    with pytest.raises(ValueError):
        _tenant().plugin_secret_name(install_id, field)


def test_plugin_is_a_consumer_kind_and_round_trips():
    assert models.SECRET_CONSUMER_KINDS[-1] == "plugin"
    spelt = consumer_name("plugin", "w-0123456789ab")
    assert spelt == "plugin:w-0123456789ab"
    assert parse_consumer(spelt) == ("plugin", "w-0123456789ab")
    with pytest.raises(ValueError):
        consumer_name("plugin", "W_bad")


# --------------------------------------------------------------------------
# Nothing else in swarm_common changes
# --------------------------------------------------------------------------

#: specsign's public names before requests 140-144, and the ones they add.
SPECSIGN_BEFORE = {
    "MAX_DEPTH", "MAX_SAFE_INTEGER", "SETTINGS_MAC_FIELDS", "SETTINGS_PURPOSE",
    "SIGNED_METADATA_KEYS", "SPEC_FORMAT", "SPEC_FORMATS", "SPEC_PURPOSE",
    "SpecNotCanonical", "canonical_settings_block", "canonical_step_spec", "jcs",
    "signing_format", "spec_digest",
}
SPECSIGN_ADDED = {
    "PLUGIN_RECORD_PURPOSE", "PLUGIN_RECORD_KINDS", "PLUGIN_SCOPES", "PLUGIN_RECORD_IDS",
    "PLUGIN_RECORD_UNCOVERED", "canonical_plugin_record",
    "PLATFORM_SETTINGS_PURPOSE", "PLATFORM_SETTINGS_MAC_FIELDS", "canonical_platform_settings",
}
#: Contract request 155 (per-person signed submissions, accepted 2026-11-13).
SPECSIGN_ADDED_155 = {
    "PERSON_SUBMISSION_PURPOSE", "STANDING_GRANT_PURPOSE", "STANDING_GRANT_REVOKE_PURPOSE",
    "TENANT_VIA", "STANDING_GRANT_ORIGIN_KINDS", "canonical_submission_statement",
    "canonical_standing_grant", "canonical_standing_grant_revoke",
}


def test_specsign_gains_only_the_requests_names():
    public = {n for n, v in vars(specsign).items()
              if not n.startswith("_") and getattr(v, "__module__", specsign.__name__) == specsign.__name__
              and not isinstance(v, type(hashlib))}
    assert public == SPECSIGN_BEFORE | SPECSIGN_ADDED | SPECSIGN_ADDED_155


def test_the_step_spec_format_is_unchanged():
    assert specsign.SPEC_FORMAT == 8
    assert specsign.SPEC_FORMATS == (1, 2, 3, 4, 5, 6, 7, 8)
    assert specsign.SIGNED_METADATA_KEYS == ("dispatch", "input_from", "expected_outputs", "work_contract")


def test_tenant_gains_only_plugin_secret_name():
    public = {n for n in vars(Tenant) if not n.startswith("_")}
    assert public == {
        "capacity_units", "display_name", "enabled", "gcs_prefix", "max_active",
        "monthly_budget_usd", "namespace", "secret_name", "service_account",
        "plugin_secret_name",
    }
    assert _tenant().secret_name("anthropic") == "swarm-tenant-team-a-anthropic"
