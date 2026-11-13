"""Every manifest entry names the step that satisfies it, and the doctor's checks
carry their probes (DAYONE-MANIFEST-STEPS; part of #2688; design §2.2, §3.1;
plan §1.5 DAYONE-DOCTOR-1).

`step:` is how `swarm install --repair <id>` finds what to run, and how
`swarm doctor` says which step repairs a finding. So:

* every entry in `install/manifest.yaml` names a `step:`, and so does every
  entry a fragment (`install/manifest.d/*.yaml`) adds or restates;
* every `step:` is the id of a step some module of `swarm_install.steps`
  defines (`STEPS`, and the steps those trigger);
* an entry a step's `satisfies` lists names that step, in the base file and in
  any fragment that restates it;
* the entries the design names as the doctor's live checks carry a `probe:`.

No fragment entry is excused: the waiting list the fragments that predate
this rule were held on is gone (DAYONE-MANIFEST-STEPS-2), and a fragment entry
with no `step:` fails. A fragment that restates a base entry names the base's
step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_install import manifest as m
from swarm_install import steps as engine_steps
from swarm_install.steps import triggered_of

#: The doctor's live checks the design names (plan §1.5 DAYONE-DOCTOR-1's red
#: tests and the lessons table), by entry, with the probe type each carries.
#: `api.policytroubleshooter` is the Troubleshooter the doctor uses to tell
#: propagation from a missing grant (lessons 19, 48).
DOCTOR_PROBES: dict[str, str] = {
    "quota.cloud-run-jobs": "cloud_run_job_count",  # lesson 34: test_job_count_and_ttl
    "cluster.autopilot": "gke_authorized_networks",  # lesson 37: test_authorised_networks_reported
    "rbac.dispatcher-role": "kubernetes_can_i",  # lesson 30: test_reconciler_can_list_jobs
    "iap-setting.frontend-programmatic-clients": "iap_programmatic_clients",  # lesson 18
    "oauth-client.desktop-sc": "iap_programmatic_clients",  # lesson 18: B12a
    "workspace-group.admin-surface-swarm-admins": "groups_probe_via_api",  # design §8
    "workspace-group.tenant-groups": "groups_probe_via_api",  # design §8
    "api.policytroubleshooter": "service_enabled",  # lessons 19, 48
}


def _all_steps() -> list[engine_steps.Step]:
    found: list[engine_steps.Step] = []
    pending = list(engine_steps.discover())
    while pending:
        step = pending.pop(0)
        found.append(step)
        pending.extend(triggered_of(step))
    return found


def _step_ids() -> set[str]:
    return {step.id for step in _all_steps()}


def _satisfied_by() -> dict[str, str]:
    """Entry id -> the id of the step whose `satisfies` lists it."""
    owner: dict[str, str] = {}
    for step in _all_steps():
        for entry_id in step.satisfies:
            assert entry_id not in owner, f"{entry_id} is satisfied by {owner[entry_id]} and {step.id}"
            owner[entry_id] = step.id
    return owner


def _fragments() -> dict[str, m.ManifestFile]:
    return {p.name: m.parse_file(p) for p in sorted(m.FRAGMENTS_DIR.glob("*.yaml"))}


def _base() -> m.ManifestFile:
    return m.parse_file(m.MANIFEST_PATH)


def test_every_base_entry_names_a_step() -> None:
    missing = sorted(e.id for e in _base().entries if not e.step)
    assert not missing, f"{len(missing)} entries in {m.MANIFEST_PATH.name} name no step: {missing}"


def _unstepped(fragments: dict[str, m.ManifestFile]) -> list[str]:
    """Fragment entries with no `step:`."""
    return sorted(f"{name}: {e.id}" for name, fragment in fragments.items() for e in fragment.entries if not e.step)


def _unknown_steps(files: dict[str, m.ManifestFile], known: set[str]) -> list[str]:
    """Entries whose `step:` names no step a module defines."""
    return sorted(
        f"{name}: {e.id} -> {e.step}"
        for name, file in files.items()
        for e in file.entries
        if e.step and e.step not in known
    )


def test_every_fragment_entry_names_a_step() -> None:
    missing = _unstepped(_fragments())
    assert not missing, (
        f"fragment entries name no step: {missing}; give each `step: <id>`, the step in "
        "swarm_install/steps/*.py that satisfies it"
    )


def test_every_step_names_a_defined_step() -> None:
    known = _step_ids()
    unknown = _unknown_steps({m.MANIFEST_PATH.name: _base(), **_fragments()}, known)
    assert not unknown, f"entries name a step no module defines: {unknown}; known: {sorted(known)}"


def test_a_fragment_entry_names_the_step_its_base_copy_names() -> None:
    """A fragment that restates a base entry names the step the base names."""
    base = {e.id: e for e in _base().entries}
    wrong = sorted(
        f"{name}: {e.id} names {e.step}, the base names {base[e.id].step}"
        for name, fragment in _fragments().items()
        for e in fragment.entries
        if e.id in base and base[e.id].step and e.step != base[e.id].step
    )
    assert not wrong, wrong


def test_an_entry_a_step_satisfies_names_that_step() -> None:
    owner = _satisfied_by()
    files = {m.MANIFEST_PATH.name: _base(), **_fragments()}
    wrong = sorted(
        f"{name}: {e.id} names {e.step}, but {owner[e.id]} satisfies it"
        for name, file in files.items()
        for e in file.entries
        if e.id in owner and e.step and e.step != owner[e.id]
    )
    assert not wrong, wrong


def test_every_satisfied_id_is_a_manifest_entry() -> None:
    ids = set(m.load().by_id())
    unknown = sorted(f"{step} -> {entry_id}" for entry_id, step in _satisfied_by().items() if entry_id not in ids)
    assert not unknown, f"steps satisfy ids no manifest entry has: {unknown}"


@pytest.mark.parametrize("entry_id", sorted(DOCTOR_PROBES))
def test_doctor_checks_carry_their_probe(entry_id: str) -> None:
    entry = m.load().by_id()[entry_id]
    assert entry.probe is not None, f"{entry_id}: the doctor checks it live, but it carries no probe"
    assert entry.probe.type == DOCTOR_PROBES[entry_id], (entry_id, entry.probe)


def test_fragment_rule_fails_on_a_new_unstepped_fragment_entry(tmp_path: Path) -> None:
    """The fragment rule bites: a fragment entry with no step is reported; one
    with a step that no module defines too."""
    fragment = tmp_path / "some-lane.yaml"
    fragment.write_text(
        "entries:\n"
        "- {id: other.unstepped, kind: other, scope: core, stage: 7, provider: engine}\n"
        "- {id: other.misstepped, kind: other, scope: core, stage: 7, provider: engine, step: infra.pass-9}\n",
        encoding="utf-8",
    )
    files = {fragment.name: m.parse_file(fragment)}
    assert _unstepped(files) == ["some-lane.yaml: other.unstepped"]
    assert _unknown_steps(files, _step_ids()) == ["some-lane.yaml: other.misstepped -> infra.pass-9"]


#: Entries whose stage is deliberately not their step's, and why. Every other
#: entry carries the stage of the step its `step:` names.
STAGE_EXCEPTIONS: dict[str, str] = {
    # install/manifest.d/edge-2.yaml's header: what it records is stage 7's
    # product (pass 2's front end behind IAP); its wait step runs first in
    # stage 8 only because within a stage the engine keeps registration order.
    "other.console-through-iap": "stage 7's product, checked by the first step of stage 8",
}

#: Registered steps no entry names, and why none is right: each re-applies
#: another step's pass or records what an entry's own step already makes.
NO_ENTRY_STEPS: dict[str, str] = {
    "infra.pass-identity": "infra pass 2 again in stage 8 with the admin group; its entries name infra.pass-2",
    "handoff.sign-in-bootstrap": "bootstrap pass 2 again with the Desktop client; its entries name bootstrap.pass-2",
    "handoff.sign-in-infra": "the latest infra pass again with the Desktop client; its entries name the pass",
    "handoff.sign-in-client": "records the Desktop client oauth-client.desktop-sc names edge.desktop-client for",
}


def _stage_mismatches(files: dict[str, m.ManifestFile], stages: dict[str, int]) -> list[str]:
    """Entries whose stage is not the stage of the step they name."""
    return sorted(
        f"{name}: {e.id} is stage {e.stage}, its step {e.step} is stage {stages[e.step]}"
        for name, file in files.items()
        for e in file.entries
        if e.step in stages and e.stage != stages[e.step] and e.id not in STAGE_EXCEPTIONS
    )


def test_every_entry_carries_its_steps_stage() -> None:
    stages = {step.id: step.stage for step in _all_steps()}
    wrong = _stage_mismatches({m.MANIFEST_PATH.name: _base(), **_fragments()}, stages)
    assert not wrong, f"{len(wrong)} entries carry a stage other than their step's: {wrong}"


def test_stage_exceptions_are_still_exceptions() -> None:
    """An excused entry exists and still differs from its step, so the
    exception list never outlives its reason."""
    stages = {step.id: step.stage for step in _all_steps()}
    by_id = m.load().by_id()
    stale = sorted(i for i in STAGE_EXCEPTIONS if i not in by_id or by_id[i].stage == stages.get(by_id[i].step or ""))
    assert not stale, stale


def test_every_step_is_named_by_an_entry_or_excused() -> None:
    named = {e.step for e in m.load().entries if e.step}
    unnamed = sorted(_step_ids() - named - set(NO_ENTRY_STEPS))
    assert not unnamed, f"steps no manifest entry names, so doctor and --repair cannot reach them: {unnamed}"
    stale = sorted(set(NO_ENTRY_STEPS) & named)
    assert not stale, f"excused steps an entry now names; drop them from NO_ENTRY_STEPS: {stale}"


def test_no_field_holds_the_string_null() -> None:
    """A field with no value is YAML null (or absent), never the string 'null'."""
    files = {m.MANIFEST_PATH.name: _base(), **_fragments()}
    quoted = sorted(
        f"{name}: {e.id}.{field}"
        for name, file in files.items()
        for e in file.entries
        for field, value in e.model_dump().items()
        if value == "null"
    )
    assert not quoted, f"{len(quoted)} fields hold the string 'null': {quoted[:20]}"


def test_stage_rule_fails_on_a_misstaged_entry(tmp_path: Path) -> None:
    fragment = tmp_path / "some-lane.yaml"
    fragment.write_text(
        "entries:\n- {id: other.misstaged, kind: other, scope: core, stage: 7, provider: engine, step: preflight.region}\n",
        encoding="utf-8",
    )
    files = {fragment.name: m.parse_file(fragment)}
    assert _stage_mismatches(files, {"preflight.region": 0}) == [
        "some-lane.yaml: other.misstaged is stage 7, its step preflight.region is stage 0"
    ]
