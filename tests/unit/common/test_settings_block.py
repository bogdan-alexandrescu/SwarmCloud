"""Contract request 86: `canonical_settings_block`, the bytes a `workspace_settings` MAC covers.

Accepted by the owner 2026-10-17 on PR 1464 (docs/design/tenant-settings-integrity.md
§5 request B, owner question Q2's interim, part of #1457). swarm-api MACs the
`workspace_settings` fields the control plane acts on, and the scheduler and
swarm-api verify them, so both must compute identical bytes from one
definition. The tenant is inside the bytes: a block copied from tenant A's
document onto tenant B's must not verify.
"""

from __future__ import annotations

import json

import pytest

from swarm_common.specsign import (
    SETTINGS_MAC_FIELDS,
    SETTINGS_PURPOSE,
    SPEC_PURPOSE,
    SpecNotCanonical,
    canonical_settings_block,
)

MERGE = {"mode": "train", "train_size": 4, "max_train_age_minutes": 30}


def test_two_tenants_with_the_same_fields_have_different_bytes():
    a = canonical_settings_block(tenant_id="team-a", block="merge", fields=MERGE)
    b = canonical_settings_block(tenant_id="team-b", block="merge", fields=MERGE)
    assert a != b


def test_a_key_outside_the_blocks_list_is_not_covered():
    plain = canonical_settings_block(tenant_id="team-a", block="merge", fields=MERGE)
    extra = canonical_settings_block(
        tenant_id="team-a", block="merge", fields={**MERGE, "unsigned_knob": "anything"}
    )
    assert extra == plain
    assert b"unsigned_knob" not in extra


def test_a_changed_covered_value_changes_the_bytes():
    plain = canonical_settings_block(tenant_id="team-a", block="merge", fields=MERGE)
    forged = canonical_settings_block(
        tenant_id="team-a", block="merge", fields={**MERGE, "train_size": 40}
    )
    assert forged != plain


def test_the_block_name_is_inside_the_bytes():
    """`judgment_backend` is covered at the top level and in
    `code_intelligence`; one block's MAC must not verify the other."""
    fields = {"judgment_backend": "typesafe"}
    top = canonical_settings_block(tenant_id="team-a", block="top", fields=fields)
    nested = canonical_settings_block(
        tenant_id="team-a", block="code_intelligence", fields=fields
    )
    assert top != nested


def test_the_form_is_jcs_with_its_own_purpose_and_every_covered_key():
    out = canonical_settings_block(tenant_id="team-a", block="top", fields={})
    assert json.loads(out) == {
        "purpose": SETTINGS_PURPOSE,
        "tenant_id": "team-a",
        "block": "top",
        "fields": {"judgment_backend": None},
    }
    assert SETTINGS_PURPOSE == "swarm.settings/v1"
    assert SETTINGS_PURPOSE != SPEC_PURPOSE
    # JCS: no whitespace, members sorted, so key order in `fields` is irrelevant.
    assert out == (
        b'{"block":"top","fields":{"judgment_backend":null},'
        b'"purpose":"swarm.settings/v1","tenant_id":"team-a"}'
    )
    reordered = dict(reversed(list(MERGE.items())))
    assert canonical_settings_block(
        tenant_id="t", block="merge", fields=reordered
    ) == canonical_settings_block(tenant_id="t", block="merge", fields=MERGE)


def test_the_covered_fields_are_the_requests():
    assert dict(SETTINGS_MAC_FIELDS) == {
        "top": ("judgment_backend",),
        "merge": ("mode", "train_size", "max_train_age_minutes"),
        "code_intelligence": (
            "jev",
            "context",
            "context_ab",
            "graph_delivery",
            "judgment_backend",
        ),
        # Contract request 119 (#2459): the workspace's per-agent budget.
        "budget": ("unit", "amount"),
        # Contract request 128 (JEV-LANES, PR 2905): the lane quality gates.
        "quality_gates": ("jev",),
        # Contract request 136 (AQ-WORK-CONTRACT, part of #2688): the catalogue
        # plugins a workspace's steps may name.
        "plugins": ("allowed",),
        # Contract request 138 (JEV-SUBMISSION-REVIEW, part of #2688): the
        # workspace's opt-in to Jev's review of its work contracts.
        "contract_review": ("enabled",),
    }
    with pytest.raises(TypeError):
        SETTINGS_MAC_FIELDS["top"] = ()  # type: ignore[index]


#: Every block that existed before contract request 136, with the bytes its
#: MAC covered then. Adding the "plugins" block must not change one of them,
#: so every MAC already stored still verifies.
BEFORE_PLUGINS = {
    "top": ({"judgment_backend": "typesafe"},
            b'{"block":"top","fields":{"judgment_backend":"typesafe"},'
            b'"purpose":"swarm.settings/v1","tenant_id":"team-a"}'),
    "merge": (MERGE,
              b'{"block":"merge","fields":{"max_train_age_minutes":30,"mode":"train",'
              b'"train_size":4},"purpose":"swarm.settings/v1","tenant_id":"team-a"}'),
    "code_intelligence": ({"jev": "on"},
                          b'{"block":"code_intelligence","fields":{"context":null,'
                          b'"context_ab":null,"graph_delivery":null,"jev":"on",'
                          b'"judgment_backend":null},"purpose":"swarm.settings/v1",'
                          b'"tenant_id":"team-a"}'),
    "budget": ({"unit": "usd", "amount": 5},
               b'{"block":"budget","fields":{"amount":5,"unit":"usd"},'
               b'"purpose":"swarm.settings/v1","tenant_id":"team-a"}'),
    "quality_gates": ({"jev": "on"},
                      b'{"block":"quality_gates","fields":{"jev":"on"},'
                      b'"purpose":"swarm.settings/v1","tenant_id":"team-a"}'),
}


@pytest.mark.parametrize("block", sorted(BEFORE_PLUGINS))
def test_a_block_without_plugins_signs_the_same_bytes_as_before(block):
    fields, pinned = BEFORE_PLUGINS[block]
    assert canonical_settings_block(tenant_id="team-a", block=block, fields=fields) == pinned
    # A workspace document that carries a plugins list changes no other block's bytes.
    with_plugins = {**fields, "plugins": {"allowed": ["gstack"]}, "allowed": ["gstack"]}
    if block != "top":
        assert canonical_settings_block(
            tenant_id="team-a", block=block, fields=with_plugins
        ) == pinned


def test_the_plugins_block_signs_the_allowed_list_and_nothing_else():
    out = canonical_settings_block(
        tenant_id="team-a", block="plugins",
        fields={"allowed": ["gstack"], "mode": "unattended"},
    )
    assert out == (
        b'{"block":"plugins","fields":{"allowed":["gstack"]},'
        b'"purpose":"swarm.settings/v1","tenant_id":"team-a"}'
    )
    unset = canonical_settings_block(tenant_id="team-a", block="plugins", fields={})
    assert json.loads(unset)["fields"] == {"allowed": None}
    assert canonical_settings_block(
        tenant_id="team-b", block="plugins", fields={"allowed": ["gstack"]}
    ) != out


def test_the_contract_review_block_signs_enabled_and_nothing_else():
    """Contract request 138: the bytes swarm-api signed for the block before the
    frozen list named it (`judgment._canonical_block`), so no MAC changes."""
    out = canonical_settings_block(
        tenant_id="eng", block="contract_review",
        fields={"enabled": True, "set_at": "2026-11-04T00:00:00Z",
                "set_by": "alice@example.com", "mac": {"v": 1}},
    )
    assert out == (
        b'{"block":"contract_review","fields":{"enabled":true},'
        b'"purpose":"swarm.settings/v1","tenant_id":"eng"}'
    )
    unset = canonical_settings_block(tenant_id="eng", block="contract_review", fields={})
    assert json.loads(unset)["fields"] == {"enabled": None}


@pytest.mark.parametrize("block", ["", "Merge", "tenant", "metadata"])
def test_an_unknown_block_has_no_canonical_form(block):
    with pytest.raises(SpecNotCanonical):
        canonical_settings_block(tenant_id="team-a", block=block, fields={})


@pytest.mark.parametrize("tenant_id", ["", None, 7])
def test_a_missing_tenant_has_no_canonical_form(tenant_id):
    with pytest.raises(SpecNotCanonical):
        canonical_settings_block(tenant_id=tenant_id, block="top", fields={})  # type: ignore[arg-type]


def test_fields_that_are_not_a_map_have_no_canonical_form():
    with pytest.raises(SpecNotCanonical):
        canonical_settings_block(tenant_id="team-a", block="top", fields=["judgment_backend"])  # type: ignore[arg-type]
