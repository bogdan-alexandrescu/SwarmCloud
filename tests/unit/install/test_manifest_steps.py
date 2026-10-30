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

Fragments that predate this rule are listed in `UNSTEPPED_FRAGMENT_ENTRIES`
with the step each should name; that list only shrinks. Their files belong to
other lanes, so this test does not edit them; a fragment entry not in the list
fails without a `step:`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_install import manifest as m
from swarm_install import steps as engine_steps
from swarm_install.steps import triggered_of

#: Fragment entries on main at 57b8530 with no `step:`, by (fragment, id), and
#: the step each should name: the stage and Terraform root say which pass makes
#: it (bootstrap stage <= 6 is bootstrap pass 1, stage 7 pass 2; infra stage 7
#: is infra pass 2). A lane that adds the `step:` may drop its line here; no
#: line is ever added.
UNSTEPPED_FRAGMENT_ENTRIES: dict[tuple[str, str], str] = {
    ("boot-2.yaml", "sa.swarm-api"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-quota-broker"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-reconciler"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-scheduler"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-tick"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-verify"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-schedule-tick"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-rollup-sweeper"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-discovery"): "bootstrap.pass-1",
    ("boot-2.yaml", "sa.swarm-doctor"): "bootstrap.pass-1",
    ("boot-2.yaml", "iam-binding.swarm-api-spec-signer"): "bootstrap.pass-2",
    ("boot-2.yaml", "iam-binding.verify-cluster-viewer"): "bootstrap.pass-2",
    ("boot-2.yaml", "iam-binding.broker-secret-lister"): "bootstrap.pass-2",
    ("boot-2.yaml", "switch.grant_broker_secret_lister"): "bootstrap.pass-1",
    ("boot-2.yaml", "switch.swarm_workers_log_view_exists"): "bootstrap.pass-2",
    ("identity-1.yaml", "declared-value.groups_auth"): "infra.pass-2",
    ("install-tools-auto.yaml", "local-tool.gcloud"): "preflight.tools",
    ("install-tools-auto.yaml", "local-tool.terraform"): "preflight.tools",
    ("install-tools-auto.yaml", "local-tool.kubectl"): "preflight.tools",
    ("install-tools-auto.yaml", "local-tool.jq"): "preflight.tools",
    ("install-tools-auto.yaml", "local-tool.uv"): "preflight.tools",
    ("install-tools-auto.yaml", "local-tool.python"): "preflight.tools",
    ("tf-2.yaml", "iam-binding.github-hooks-allusers"): "infra.pass-2",
    ("tf-2.yaml", "other.discovery-endpoint"): "infra.pass-2",
    ("tf-2.yaml", "cloud-run-service.swarm-discovery"): "infra.pass-2",
    ("tf-2.yaml", "iam-binding.discovery-client-secret"): "infra.pass-2",
    ("tf-2.yaml", "other.url-map"): "infra.pass-2",
    ("tf-2.yaml", "iam-binding.swarm-api-github-app-secret-writer"): "bootstrap.pass-1",
    ("tf-2.yaml", "iam-binding.deployer-iap-api-accessor"): "bootstrap.pass-1",
    ("tf-2.yaml", "switch.enable_console_app_setup"): "infra.pass-2",
    ("tf-2.yaml", "cloud-run-job.swarm-doctor"): "infra.pass-2",
    ("tf-2.yaml", "sched.doctor-hourly"): "infra.pass-2",
    ("tf-2.yaml", "iam-binding.doctor-read-only"): "bootstrap.pass-1",
    ("tf-2.yaml", "monitoring.doctor-findings-alert"): "infra.pass-2",
    ("tf-2.yaml", "switch.enable_scheduled_doctor"): "infra.pass-2",
    ("tf-2.yaml", "switch.enable_acceptance_fixture"): "infra.pass-2",
}

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
    """Fragment entries with no `step:` that the waiting list does not hold."""
    return sorted(
        f"{name}: {e.id}"
        for name, fragment in fragments.items()
        for e in fragment.entries
        if not e.step and (name, e.id) not in UNSTEPPED_FRAGMENT_ENTRIES
    )


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


def test_unstepped_fragment_list_names_defined_steps_and_agrees_with_the_base() -> None:
    """The waiting list says which step each fragment entry should name; that
    step exists and is the one the base file's copy of the entry names."""
    known = _step_ids()
    base = {e.id: e for e in _base().entries}
    wrong = sorted(
        f"{name}: {entry_id} -> {step}"
        for (name, entry_id), step in UNSTEPPED_FRAGMENT_ENTRIES.items()
        if step not in known or (entry_id in base and base[entry_id].step != step)
    )
    assert not wrong, f"UNSTEPPED_FRAGMENT_ENTRIES names an unknown step or disagrees with the base: {wrong}"


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
    """The fragment rule bites: a fragment entry with no step, not on the
    waiting list, is reported; one with a step that no module defines too."""
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
