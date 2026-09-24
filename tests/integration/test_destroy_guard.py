"""Independent rehearsal of the destroy guard, driving the real jq filter.

`scripts/destroy.sh --self-test` already covers deny-listed neighbours, unlabelled
resources, unlabelable types and foreign projects. This file exists to cover what that
fixture does NOT: a near-miss label value, and failing closed on unusable input.

Why bother: the deploy runs unattended against a SHARED project holding a live 4-node
cluster `agents-staging`, VPC `agents-staging-vpc`, and service accounts for promptlab,
crawler, aipipeline, external-secrets and tournament-digest. The guard is the only thing
between a bad plan and another team's production.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD_JQ = REPO / "scripts" / "lib" / "destroy-guard.jq"
DESTROY = REPO / "scripts" / "destroy.sh"
PROJECT = "saga-agents-staging"

COMMON_SH = REPO / "scripts" / "lib" / "common.sh"
TYPES_JSON = REPO / "scripts" / "lib" / "unlabelable-types.json"


def _shared_deny_list() -> list[str]:
    """Every entry of `SHARED_DENY_LIST` in scripts/lib/common.sh.

    DERIVED, NOT RESTATED, and that is a defect fixed rather than a tidy-up.

    This file used to carry its own fourteen-entry list of "the real neighbours
    in the shared project", written by hand. `SHARED_DENY_LIST` has twenty-one
    entries, so the suite proving `make destroy` refuses to touch another team's
    resources was proving it for fourteen of them. Missing entirely: the three
    shared buckets (`saga-agents-crawled-media-staging`,
    `saga-agents-files-staging`, `saga-agents-terraform-state-staging`) and the
    other team's Compute Engine default service account. A plan deleting any of
    those was never shown to be refused.

    The SHAPE was wrong too, which is the more interesting half. The hand-written
    list held service accounts by short name (`promptlab-runner`); `common.sh`
    holds them as full emails, and `scripts/destroy.sh` and
    `scripts/lib/plan-guard.sh` pass those full emails to the guard. So the one
    matching behaviour production actually depends on was the one the test did
    not exercise -- and `209012342332-compute@developer.gserviceaccount.com` is
    not even on `PROJECT`'s domain, so rebuilding an email from a short name, as
    this file did, cannot produce it at all.

    CLAUDE.md: "The deny-list lives once, in scripts/lib/common.sh."
    """
    text = COMMON_SH.read_text()
    assert "SHARED_DENY_LIST=(" in text, (
        f"{COMMON_SH} no longer declares SHARED_DENY_LIST=( ; the deny-list moved "
        f"and this suite would otherwise judge by nothing"
    )
    block = text.split("SHARED_DENY_LIST=(", 1)[1].split("\n)", 1)[0]
    return [
        line.strip().strip('"')
        for line in block.splitlines()
        if line.strip().startswith('"')
    ]


#: The deny-list in the exact form the guard is given it. The two
#: transformations are `guard_deny_json` in common.sh, which is what
#: scripts/destroy.sh and scripts/lib/plan-guard.sh both call: the bare
#: `default` comes OUT (too common a string to compare against every token --
#: destroy-guard.jq matches the shared default network on the network field
#: instead) and Firestore's `(default)` goes IN. Asserted structurally below
#: rather than taken on trust.
SHARED = _shared_deny_list()
DENY = [entry for entry in SHARED if entry != "default"] + ["(default)"]

#: Read from the file that three other things read, for the reason that file
#: states in its own header: it was restated in awk in the workflows, the copies
#: drifted, and the awk version exempted only `google_project_iam*` -- so a plan
#: deleting a `google_storage_bucket_iam_member` was blocked from production by a
#: rule nobody had decided. This file restated it too, as seven of the forty
#: types, so the exemption logic was proved over a sixth of its input.
UNLABELABLE = json.loads(TYPES_JSON.read_text())["types"]

pytestmark = pytest.mark.skipif(
    not GUARD_JQ.exists(), reason="scripts/lib/destroy-guard.jq not built yet"
)


def run_guard(resource_changes: list[dict], tmp_path: Path) -> dict:
    fixture = tmp_path / "plan.json"
    fixture.write_text(json.dumps({"resource_changes": resource_changes}))
    proc = subprocess.run(
        ["jq", "-f", str(GUARD_JQ),
         "--argjson", "deny", json.dumps(DENY),
         "--argjson", "allow_types", json.dumps(UNLABELABLE),
         "--arg", "project", PROJECT,
         str(fixture)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"guard jq failed: {proc.stderr}"
    return json.loads(proc.stdout)


def deletion(address: str, type_: str, **before) -> dict:
    before.setdefault("project", PROJECT)
    return {"address": address, "type": type_,
            "change": {"actions": ["delete"], "before": before, "after": None}}


def addresses(verdict: dict, key: str) -> set[str]:
    return {e["address"] for e in verdict.get(key, [])}


def test_near_miss_label_value_is_an_offender(tmp_path):
    """`managed-by=other-terraform` must NOT pass.

    A substring or truthiness check on the label would let this through and delete
    another team's resource. The self-test fixture never exercises a wrong VALUE.
    """
    verdict = run_guard([
        deletion("ours", "google_storage_bucket",
                 name="saga-agents-staging-swarm-artifacts",
                 labels={"managed-by": "swarm-terraform"}),
        deletion("near_miss", "google_storage_bucket",
                 name="someone-elses-bucket",
                 labels={"managed-by": "other-terraform"}),
    ], tmp_path)
    assert "near_miss" in addresses(verdict, "offenders"), (
        "GUARD FAILED OPEN on managed-by=other-terraform — a wrong label value was "
        f"treated as ours. Verdict: {json.dumps(verdict)[:400]}"
    )
    assert "ours" not in addresses(verdict, "offenders")


def test_label_key_present_but_empty_is_an_offender(tmp_path):
    verdict = run_guard([
        deletion("empty_label", "google_storage_bucket", name="x", labels={"managed-by": ""}),
    ], tmp_path)
    assert "empty_label" in addresses(verdict, "offenders")


def test_no_labels_key_at_all_is_an_offender(tmp_path):
    verdict = run_guard([
        deletion("no_labels", "google_storage_bucket", name="x"),
    ], tmp_path)
    assert "no_labels" in addresses(verdict, "offenders")


def test_the_derived_deny_list_is_the_one_production_passes_the_guard():
    """The derivation, asserted, so a silent parse failure cannot read as a pass.

    A `split()` that stopped finding the array would yield `[]`, every
    parametrised case below would collapse to nothing, and the suite would go
    green having checked no resource at all -- the failure mode CLAUDE.md calls
    out by name ("Empty output is not success").

    MUTATION: rename `SHARED_DENY_LIST` in common.sh, or delete an entry, and
    this fails on the count. Drop the `grep -vx default` from `guard_deny_json`
    and the third assertion fails.
    """
    assert len(SHARED) >= 20, (
        f"parsed only {len(SHARED)} deny-list entries out of scripts/lib/common.sh; "
        f"the shared project holds a cluster, a VPC, two subnets, three buckets and "
        f"twelve service accounts, so a short list means the parse broke"
    )
    assert len(UNLABELABLE) >= 35, (
        f"parsed only {len(UNLABELABLE)} unlabelable types out of {TYPES_JSON.name}"
    )
    # The two transformations `guard_deny_json` applies, checked by their effect
    # rather than by a second copy of the pipeline.
    assert "default" not in DENY, (
        "the bare `default` must not reach the token comparison -- it appears inside "
        "too many unrelated resource ids; destroy-guard.jq matches the shared "
        "default network on the network/subnetwork field instead"
    )
    assert "(default)" in DENY, (
        "Firestore's `(default)` database must be on the list the guard judges by; "
        "CONTRACT.md reserves it for the other teams in this shared project"
    )
    # And both callers build it with the shared function rather than their own
    # pipeline, which is how the two copies of it drifted apart in the first place.
    for script in ("scripts/destroy.sh", "scripts/lib/plan-guard.sh"):
        source = (REPO / script).read_text()
        assert "guard_deny_json" in source, (
            f"{script} no longer calls guard_deny_json; a second implementation of "
            f"the list a destroy guard judges by is how `make destroy` and the CI "
            f"plan guard come to disagree about what belongs to another team"
        )


@pytest.mark.parametrize("protected", DENY)
def test_every_real_neighbour_is_caught_even_if_mislabelled(protected, tmp_path):
    """A neighbour must be caught by the deny-list even when it carries OUR label.

    Defence in depth: if a bad import ever pulled a foreign resource into our state
    and it inherited our label, the deny-list is the remaining backstop.

    EVERY ENTRY, not a chosen four. `destroy-guard.jq`'s `deny_hits` is
    type-agnostic -- it compares `$deny` against `tokens_of(before)`, which reads
    `.name`, `.id`, `.email`, `.bucket`, `.cluster`, `.network` and friends -- so
    one case per protected resource costs nothing and is the only way to say that
    all of them are covered. The four that were here proved four.

    The entry is fed AS IT IS WRITTEN, never rebuilt: a service account goes in as
    its own full email, because one of the twelve
    (`209012342332-compute@developer.gserviceaccount.com`) is not on this
    project's service-account domain and no `f"{name}@{PROJECT}..."` could
    produce it.
    """
    before = {"name": protected}
    if "@" in protected:
        before["email"] = protected
    verdict = run_guard([
        deletion("intruder", "google_service_account",
                 labels={"managed-by": "swarm-terraform"}, **before),
    ], tmp_path)
    caught = addresses(verdict, "denylist_hits")
    assert "intruder" in caught, (
        f"GUARD FAILED OPEN: {protected} carried our label and slipped past the "
        f"deny-list. Verdict: {json.dumps(verdict)[:400]}"
    )


def test_guard_fails_closed_on_unparseable_plan(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    proc = subprocess.run(
        ["jq", "-f", str(GUARD_JQ), "--argjson", "deny", json.dumps(DENY),
         "--argjson", "allow_types", json.dumps(UNLABELABLE),
         "--arg", "project", PROJECT, str(bad)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "guard parsed invalid JSON as a safe plan"


@pytest.mark.skipif(not DESTROY.exists(), reason="destroy.sh not built yet")
def test_shipped_self_test_passes(tmp_path):
    """The guard's own self-test must pass, or we do not deploy at all."""
    proc = subprocess.run(
        ["bash", str(DESTROY), "--self-test"],
        capture_output=True, text=True, cwd=REPO, timeout=180,
        env={**os.environ, "PROJECT_ID": PROJECT, "REGION": "us-central1",
             "ENVIRONMENT": "dev"},
    )
    assert proc.returncode == 0, f"destroy.sh --self-test FAILED:\n{proc.stdout}\n{proc.stderr}"
