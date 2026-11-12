"""Voice it's own speech engine: the speech machine exists, stopped, on swarm-vpc.

docs/design/voice-it-own-speech.md §3.2 (address, firewall, identity), §5.1
(the machine's shape), §6 (the IAM rows) and §12.3 (the samples' TTL), lane L1.
Owner's answers of 2026-11-05: the machine's account may write its own logs
(logging.logWriter) beside its metrics (monitoring.metricWriter). Owner's
revision of 2026-11-09: "deploys in a if capacity available, if not checks B
and then C" -- Terraform makes the machine's regional template, swarm-api
creates and starts the machine zone by zone under a project grant
conditioned to it, and a release never creates the machine.

These read the Terraform as text: no provider, no plan, no credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TF = ROOT / "terraform"
SPEECH_TF = TF / "infra" / "voice_speech.tf"
VARIABLES_TF = TF / "infra" / "variables.tf"
BOOTSTRAP_TF = TF / "bootstrap" / "platform_service_accounts.tf"
DEPLOYER_TF = TF / "bootstrap" / "deployer_conditions.tf"
GRANT_TF = TF / "bootstrap" / "speech_operator_grant.tf"
NETWORK_TF = TF / "modules" / "network" / "main.tf"
FRAGMENT = ROOT / "install" / "manifest.d" / "voice-speech-machine.yaml"
COMMON_SH = ROOT / "scripts" / "lib" / "common.sh"

# What swarm-api may do to the speech machine (speech_machine.py's
# RestCompute): operate it, and since 2026-11-09 create it from its template
# in a zone with a free L4 and delete a stopped one it moves out of a zone with
# none, with the verbs a create is checked for.
OPERATOR_PERMISSIONS = {
    "compute.instances.get",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.setMachineType",
    "compute.instances.setMetadata",
    "compute.instances.getSerialPortOutput",
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.setLabels",
    "compute.instances.setServiceAccount",
    "compute.instances.setTags",
    "compute.disks.create",
    "compute.disks.delete",
    "compute.disks.setLabels",
    "compute.instanceTemplates.get",
    "compute.instanceTemplates.useReadOnly",
    "compute.subnetworks.use",
    "compute.addresses.useInternal",
}

# What the release deployer may do to the speech machine's template, and to
# the instance and boot disk an earlier release made: exactly the verbs
# voice_speech.tf's template and the instance and instance-level grant it
# made before 2026-11-09 use, and nothing on any other type.
DEPLOYER_PERMISSIONS = {
    "compute.instanceTemplates.create",
    "compute.instanceTemplates.delete",
    "compute.instanceTemplates.get",
    "compute.instanceTemplates.useReadOnly",
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.get",
    "compute.instances.getIamPolicy",
    "compute.instances.setDeletionProtection",
    "compute.instances.setIamPolicy",
    "compute.instances.setLabels",
    "compute.instances.setMachineType",
    "compute.instances.setMetadata",
    "compute.instances.setScheduling",
    "compute.instances.setServiceAccount",
    "compute.instances.setTags",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.update",
    "compute.instances.updateShieldedVmConfig",
    "compute.disks.create",
    "compute.disks.delete",
    "compute.disks.get",
    "compute.disks.setLabels",
}


def _block(text: str, header: str) -> str:
    """The body of the block whose opening line matches `header` (a regex),
    braces balanced; fails the test if there is none."""
    m = re.search(header + r"\s*\{", text, re.M)
    assert m, f"no block matching {header!r}"
    depth, i = 0, m.end() - 1
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    raise AssertionError(f"unbalanced block {header!r}")


def _resource(text: str, rtype: str, name: str) -> str:
    return _block(text, rf'^resource\s+"{rtype}"\s+"{name}"')


def _code(text: str) -> str:
    """The text with every `#` comment removed (no `#` sits inside a string
    in these files)."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


@pytest.fixture(scope="module")
def speech() -> str:
    assert SPEECH_TF.is_file(), "terraform/infra/voice_speech.tf does not exist"
    return _code(SPEECH_TF.read_text())


@pytest.fixture(scope="module")
def template(speech: str) -> str:
    return _resource(speech, "google_compute_region_instance_template", "speech")


def _resource_types(text: str) -> list[tuple[str, str]]:
    return re.findall(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"', text, re.M)


# --- the switch --------------------------------------------------------------


def test_variable_is_off_by_default() -> None:
    body = _block(VARIABLES_TF.read_text(), r'^variable\s+"voice_speech"')
    assert re.search(r"enabled\s*=\s*optional\(bool,\s*false\)", body), body
    # The ordered zones, empty for the region's a, b and c (voice_speech.tf);
    # the one-zone form stays, optional, for one release.
    assert re.search(r"zones\s*=\s*optional\(list\(string\),\s*\[\]\)", body), body
    assert re.search(r"\bzone\s*=\s*optional\(string\)", body), body
    assert re.search(r'machine_type\s*=\s*optional\(string,\s*"g2-standard-4"\)', body), body
    assert re.search(r"disk_gb\s*=\s*optional\(number,\s*150\)", body), body
    assert re.search(r"idle_minutes\s*=\s*optional\(number,\s*5\)", body), body
    # The template switch: on unless a deployment's bootstrap does not yet let
    # the release identity make the template (the field's description says why).
    assert re.search(r"\btemplate\s*=\s*optional\(bool,\s*true\)", body), body
    assert re.search(r"^\s*template\s+true:[^\n]*\n(?:[^\n]*\n)*?\s*false: the release does not make\s+the machine's template",
                     body, re.M), (
        "the voice_speech description does not document `template` beside the other fields"
    )
    # The pinned image is required once it is on, and only a Deep Learning VM
    # image by name (never a family, which moves under a stopped machine).
    assert "deeplearning-platform-release" in body
    assert "/family/" in body


#: The environments that may turn own speech on. Dev does since 2026-11-08, the owner's plan for finishing
#: Voice it; every other environment keeps it off until the owner turns it on there.
SPEECH_ON_ENVIRONMENTS = {"dev"}

#: A Deep Learning VM image BY NAME on Ubuntu 22.04 with an NVIDIA driver of 580 or later, the kind the
#: installer pins: never a /family/ path, which moves under a stopped machine and would replace it.
PINNED_IMAGE = re.compile(
    r'^\s*image\s*=\s*"projects/deeplearning-platform-release/global/images/'
    r'common-cu[0-9]+-ubuntu-2204-nvidia-(?P<driver>[0-9]+)-v[0-9]{8}"\s*$',
    re.M,
)


@pytest.mark.parametrize("tfvars", sorted((TF / "environments").glob("*/*.tfvars.example")), ids=lambda p: p.parent.name)
def test_own_speech_is_off_or_on_with_a_pinned_image(tfvars: Path) -> None:
    text = _code(tfvars.read_text())
    body = _block(text, r"^voice_speech\s*=")
    if tfvars.parent.name not in SPEECH_ON_ENVIRONMENTS:
        assert re.search(r"^\s*enabled\s*=\s*false\s*$", body, re.M), f"{tfvars}: voice_speech is not off"
        return
    if re.search(r"^\s*enabled\s*=\s*false\s*$", body, re.M):
        return
    assert re.search(r"^\s*enabled\s*=\s*true\s*$", body, re.M), f"{tfvars}: voice_speech is neither on nor off"
    pinned = PINNED_IMAGE.search(body)
    assert pinned, f"{tfvars}: own speech is on without an image pinned by name"
    assert int(pinned.group("driver")) >= 580, f"{tfvars}: the pinned image's NVIDIA driver is older than 580"
    assert "/family/" not in body, f"{tfvars}: the image is a family path"


def test_every_resource_is_behind_the_switch(speech: str) -> None:
    assert re.search(
        r"voice_speech_on\s*=\s*var\.voice_speech\.enabled\s*&&\s*var\.voice_it\s*==\s*\"on\"", speech
    ), "voice_speech_on is not voice_speech.enabled && voice_it == \"on\""
    for rtype, name in _resource_types(speech):
        body = _resource(speech, rtype, name)
        if (rtype, name) == ("google_compute_region_instance_template", "speech"):
            continue  # behind the switch and `template` too: the next test
        assert re.search(r"^\s*count\s*=\s*local\.voice_speech_on\s*\?\s*1\s*:\s*0\s*$", body, re.M), (
            f"{rtype}.{name} is not behind local.voice_speech_on"
        )
    for name in re.findall(r'^data\s+"[a-z0-9_]+"\s+"([a-z0-9_]+)"', speech, re.M):
        body = _block(speech, rf'^data\s+"[a-z0-9_]+"\s+"{name}"')
        assert re.search(r"^\s*count\s*=\s*local\.voice_speech_on\s*\?\s*1\s*:\s*0\s*$", body, re.M), name


# --- the machine's template (§5.1) ------------------------------------------


TEMPLATE_GUARD = r"local\.voice_speech_on\s*&&\s*var\.voice_speech\.template\s*\?"


def test_the_template_is_made_only_while_its_field_is_true(template: str) -> None:
    # A deployment whose bootstrap does not yet grant the release identity
    # compute.instanceTemplates.create sets template = false, and its apply
    # never asks for the template.
    assert re.search(r"^\s*count\s*=\s*" + TEMPLATE_GUARD + r"\s*1\s*:\s*0\s*$", template, re.M), (
        "the template's count is not local.voice_speech_on && var.voice_speech.template ? 1 : 0"
    )


def test_every_reference_to_the_template_handles_its_absence() -> None:
    refs = []
    for path in sorted((TF / "infra").glob("*.tf")):
        for n, line in enumerate(_code(path.read_text()).splitlines(), 1):
            if "google_compute_region_instance_template.speech[0]" in line:
                refs.append((path.name, n, line))
    assert {name for name, _, _ in refs} >= {"locals.tf", "voice_speech.tf"}, refs
    for name, n, line in refs:
        assert re.search(TEMPLATE_GUARD + r"\s*google_compute_region_instance_template\.speech\[0\]\.id\s*:", line), (
            f"{name}:{n} reads the template without `local.voice_speech_on && var.voice_speech.template ?`: {line.strip()}"
        )
    locals_tf = _code((TF / "infra" / "locals.tf").read_text())
    assert re.search(r'SWARM_SPEECH_TEMPLATE\s*=\s*' + TEMPLATE_GUARD + r'.*:\s*""\s*$', locals_tf, re.M)
    output = _block(_code(SPEECH_TF.read_text()), r'^output\s+"voice_speech"')
    assert re.search(r"^\s*template\s*=\s*" + TEMPLATE_GUARD + r".*:\s*null\s*$", output, re.M), output


@pytest.mark.parametrize("tfvars", sorted((TF / "environments").glob("*/*.tfvars.example")), ids=lambda p: p.parent.name)
def test_a_deployment_holding_the_template_back_says_why(tfvars: Path) -> None:
    # Dev held it back until its bootstrap granted the template permissions
    # (applied 2026-11-09); a deployment that holds it back again says why.
    raw = tfvars.read_text()
    body = _block(_code(raw), r"^voice_speech\s*=")
    if not re.search(r"^\s*template\s*=\s*false\s*$", body, re.M):
        return
    lines = raw.splitlines()
    i = next(i for i, line in enumerate(lines) if re.match(r"^\s*template\s*=\s*false\b", line))
    why = " ".join(lines[i - 2 : i])
    assert lines[i - 2].lstrip().startswith("#") and lines[i - 1].lstrip().startswith("#"), why
    assert "bootstrap" in why and "true" in why, why


def test_machine_is_on_demand(template: str) -> None:
    sched = _block(template, r"^\s*scheduling")
    assert re.search(r'provisioning_model\s*=\s*"STANDARD"', sched), "invariant 6: Spot is off platform-wide"
    assert re.search(r"preemptible\s*=\s*false", sched)
    assert re.search(r'on_host_maintenance\s*=\s*"TERMINATE"', sched)
    assert re.search(r"automatic_restart\s*=\s*false", sched), "a host event must leave it stopped"
    assert "SPOT" not in template.upper()


def test_a_release_never_creates_the_machine(speech: str, template: str) -> None:
    """Terraform owns the template, never the instance: a release cannot fail
    on a zone with no free L4. The instance an earlier release made leaves the
    state through a `removed` block with destroy = false, so no plan deletes
    it."""
    types = {t for t, _ in _resource_types(speech)}
    assert "google_compute_instance" not in types
    assert "google_compute_instance_from_template" not in types
    assert "google_compute_instance_iam_member" not in types
    removed = _block(speech, r"^removed")
    assert re.search(r"^\s*from\s*=\s*google_compute_instance\.speech\s*$", removed, re.M)
    life = _block(removed, r"^\s*lifecycle")
    assert re.search(r"^\s*destroy\s*=\s*false\s*$", life, re.M)
    # Immutable: a change makes the new template before the old goes.
    assert re.search(r"create_before_destroy\s*=\s*true", _block(template, r"^\s*lifecycle"))
    assert re.search(r"region\s*=\s*var\.region", template)


def test_machine_has_no_external_ip_and_sits_on_swarm_vpc(template: str) -> None:
    nic = _block(template, r"^\s*network_interface")
    assert "access_config" not in nic, "the speech machine has no external IP"
    assert "ipv6_access_config" not in nic
    assert re.search(r"subnetwork\s*=\s*module\.network\.subnetwork_id", nic)
    assert re.search(r"network_ip\s*=\s*google_compute_address\.speech\[0\]\.address", nic)


def test_machine_carries_its_tag_and_labels(speech: str, template: str) -> None:
    assert re.search(r"speech_tag\s*=\s*\"\$\{var\.name_prefix\}-speech\"", speech)
    assert re.search(r"tags\s*=\s*\[local\.speech_tag\]", template)
    for rtype, name in [("google_compute_region_instance_template", "speech"), ("google_compute_address", "speech")]:
        body = _resource(speech, rtype, name)
        assert re.search(r"labels\s*=\s*local\.speech_labels", body), f"{rtype}.{name} has no labels"
    disk = _block(template, r"^\s*disk")
    assert re.search(r"labels\s*=\s*local\.speech_labels", disk), "the boot disk has no labels"
    assert re.search(r'speech_labels\s*=\s*merge\(local\.labels,\s*\{\s*"component"\s*=\s*"voice-speech"', speech)


def test_machine_shape(speech: str, template: str) -> None:
    assert re.search(r"machine_type\s*=\s*var\.voice_speech\.machine_type", template)
    assert re.search(r"^\s*name_prefix\s*=\s*local\.speech_machine_prefix\s*$", template, re.M)
    assert re.search(r'^\s*speech_machine_prefix\s*=\s*"\$\{var\.name_prefix\}-speech-"', speech, re.M)
    disk = _block(template, r"^\s*disk")
    assert re.search(r'disk_type\s*=\s*"pd-balanced"', disk)
    assert re.search(r"disk_size_gb\s*=\s*var\.voice_speech\.disk_gb", disk)
    assert re.search(r"source_image\s*=\s*var\.voice_speech\.image", disk)
    assert re.search(r"boot\s*=\s*true", disk) and re.search(r"auto_delete\s*=\s*true", disk)
    shielded = _block(template, r"^\s*shielded_instance_config")
    assert re.search(r"enable_secure_boot\s*=\s*false", shielded)
    assert re.search(r"enable_vtpm\s*=\s*true", shielded)
    assert re.search(r"enable_integrity_monitoring\s*=\s*true", shielded)
    sa = _block(template, r"^\s*service_account")
    assert re.search(r"email\s*=\s*local\.speech_email", sa)


def test_machine_metadata_names_its_caller_and_no_secret(template: str) -> None:
    meta = _block(template, r"^\s*metadata\s*=")
    for key in (
        "swarm-speech-image",
        "swarm-speech-models",
        "swarm-speech-caller",
        "swarm-speech-idle-minutes",
        "block-project-ssh-keys",
    ):
        assert f'"{key}"' in meta, f"metadata misses {key}"
    assert re.search(r'"swarm-speech-caller"\s*=\s*module\.iam\.service_account_emails\["swarm-api"\]', meta)
    assert "startup-script" not in meta
    assert not re.search(r"(?i)secret|token|password|key\s*=", meta.replace("block-project-ssh-keys", ""))


def test_the_zones_are_listed_in_order_and_zone_is_a_one_zone_list(speech: str) -> None:
    zones = re.search(r"^\s*speech_zones\s*=\s*\((.*?)\n\s*\)", speech, re.S | re.M)
    assert zones, "voice_speech.tf has no local.speech_zones"
    body = zones.group(1)
    assert "var.voice_speech.zones" in body
    assert "[var.voice_speech.zone]" in body
    assert '["${var.region}-a", "${var.region}-b", "${var.region}-c"]' in body
    # Every listed zone in the region, still a check block.
    check = _block(speech, r'^check\s+"voice_speech_zone_in_region"')
    assert re.search(r"for\s+z\s+in\s+local\.speech_zones\s*:\s*startswith\(z,\s*\"\$\{var\.region\}-\"\)", check)
    assert "voice_speech.zones" in _block(speech, r'^check\s+"voice_speech_zone_is_replaced_by_zones"')
    # The one-zone form's validation points to zones.
    variables = _block(VARIABLES_TF.read_text(), r'^variable\s+"voice_speech"')
    messages = re.findall(r'error_message\s*=\s*"([^"\n]*(?:\\"[^"\n]*)*)"', variables)
    assert any("voice_speech.zone is replaced by voice_speech.zones" in m for m in messages), messages


def test_address_is_internal_on_the_subnet(speech: str) -> None:
    body = _resource(speech, "google_compute_address", "speech")
    assert re.search(r'address_type\s*=\s*"INTERNAL"', body)
    assert re.search(r"subnetwork\s*=\s*module\.network\.subnetwork_id", body)
    assert re.search(r'name\s*=\s*"\$\{var\.name_prefix\}-speech"', body)


# --- the firewall pair (§3.2) -------------------------------------------------


def test_firewall_pair_admits_the_subnet_on_8080_and_denies_the_rest(speech: str) -> None:
    allow = _resource(speech, "google_compute_firewall", "allow_speech_from_subnet")
    deny = _resource(speech, "google_compute_firewall", "deny_speech_other")
    for body in (allow, deny):
        assert re.search(r'direction\s*=\s*"INGRESS"', body)
        assert re.search(r"target_tags\s*=\s*\[local\.speech_tag\]", body)
        assert re.search(r"network\s*=\s*module\.network\.network_name", body)
        assert re.search(r'name\s*=\s*"\$\{var\.name_prefix\}-fw-', body), "outside the deployer's firewall prefix"
    assert re.search(r"priority\s*=\s*940\b", allow)
    assert re.search(r"source_ranges\s*=\s*\[var\.subnet_cidr\]", allow)
    a = _block(allow, r"^\s*allow")
    assert re.search(r'protocol\s*=\s*"tcp"', a) and re.search(r'ports\s*=\s*\["8080"\]', a)
    assert "deny {" not in allow
    # Below allow_internal's 1000, so the pod ranges it admits are shut out.
    assert re.search(r"priority\s*=\s*950\b", deny)
    d = _block(deny, r"^\s*deny")
    assert re.search(r'protocol\s*=\s*"all"', d)
    assert re.search(r'source_ranges\s*=\s*\["0\.0\.0\.0/0"\]', deny)
    assert "allow {" not in deny


# --- IAM (§6 and the owner's answer 2) ---------------------------------------


def test_infra_grants_swarm_api_no_compute_role(speech: str) -> None:
    """Row I1 is terraform/bootstrap's (speech_operator_grant.tf): from this
    root, CI applies it and the role would have to sit on the deployer's
    grantable list, which limits which roles, never to whom."""
    assert "speech_operator" not in speech
    assert "swarm-api" not in "".join(
        _resource(speech, t, n) for t, n in _resource_types(speech) if t.endswith("_iam_member")
    )


@pytest.fixture(scope="module")
def grant() -> str:
    assert GRANT_TF.is_file(), "terraform/bootstrap/speech_operator_grant.tf does not exist"
    return _code(GRANT_TF.read_text())


def test_swarm_api_operates_the_speech_machine_only(grant: str, speech: str) -> None:
    body = _resource(grant, "google_project_iam_member", "api_speech_operator")
    assert re.search(r"role\s*=\s*google_project_iam_custom_role\.speech_operator\.name", body)
    assert re.search(r'member\s*=\s*"serviceAccount:\$\{local\.platform_sa_email\["swarm-api"\]\}"', body)
    condition = _block(body, r"^\s*condition")
    assert re.search(r"expression\s*=\s*local\.speech_operator_condition", condition)
    # The deployer's linted extract() form, over the deployer's own paths.
    assert re.search(r"for\s+p\s+in\s+local\.deployer_speech_machine_paths\s*:", grant)
    assert '"resource.name.extract(\\"${p.zone_prefix}{zone}${p.name_prefix}\\") != \\"\\""' in grant
    assert '"resource.name.startsWith(\\"${local.deployer_speech_template_prefix}\\")"' in grant
    # The address and the subnet it names are infra's, spelled the same.
    address = re.search(r'regions/\$\{var\.region\}/addresses/([^\\"]+)\\"', grant)
    assert address and address.group(1) == "${var.name_prefix}-speech"
    assert re.search(r'name\s*=\s*"\$\{var\.name_prefix\}-speech"', _resource(speech, "google_compute_address", "speech"))
    subnet = re.search(r'regions/\$\{var\.region\}/subnetworks/([^\\"]+)\\"', grant)
    network = _code(NETWORK_TF.read_text())
    sub = _resource(network, "google_compute_subnetwork", "this")
    assert subnet and re.search(r'name\s*=\s*"' + re.escape(subnet.group(1)) + '"', sub), subnet
    # No other matcher: no matches(), no hasOnly(), no type guard.
    assert "matches(" not in grant and "hasOnly(" not in grant and "resource.type" not in grant


def test_no_project_wide_compute_grant(speech: str) -> None:
    types = {t for t, _ in _resource_types(speech)}
    assert "google_project_iam_binding" not in types
    assert "google_project_iam_policy" not in types
    roles = re.findall(r'role\s*=\s*"([^"]+)"', speech)
    assert not [r for r in roles if r.startswith("roles/compute")], roles


def test_the_machine_account_holds_exactly_its_three_rows(speech: str) -> None:
    grants = []
    for rtype, name in _resource_types(speech):
        body = _resource(speech, rtype, name)
        if re.search(r"member\s*=\s*local\.speech_member", body):
            role = re.search(r'role\s*=\s*"([^"]+)"', body)
            grants.append((rtype, role.group(1) if role else None))
    assert sorted(grants) == sorted(
        [
            ("google_artifact_registry_repository_iam_member", "roles/artifactregistry.reader"),
            ("google_project_iam_member", "roles/logging.logWriter"),
            ("google_project_iam_member", "roles/monitoring.metricWriter"),
        ]
    ), grants
    reader = _resource(speech, "google_artifact_registry_repository_iam_member", "speech_reader")
    assert re.search(r"repository\s*=\s*module\.artifact_registry\.repository_id", reader)


def test_bootstrap_makes_the_account_and_the_operator_role() -> None:
    text = _code(BOOTSTRAP_TF.read_text())
    sa = _resource(text, "google_service_account", "speech")
    assert re.search(r"account_id\s*=\s*module\.service_account_ids\.speech_id", sa)
    assert re.search(r'description\s*=\s*"managed-by=swarm-terraform;', sa)
    desc = re.search(r'description\s*=\s*"([^"]*)"', sa).group(1)
    assert len(desc) <= 256
    assert "create_ignore_already_exists" not in sa

    role = _resource(text, "google_project_iam_custom_role", "speech_operator")
    assert re.search(r'role_id\s*=\s*module\.custom_role_ids\.ids\["speech_operator"\]', role)
    perms = set(re.findall(r'"([a-z]+\.[a-zA-Z.]+)"', role.split("permissions", 1)[1]))
    assert perms == OPERATOR_PERMISSIONS, sorted(perms ^ OPERATOR_PERMISSIONS)

    # The two grants ON the account (actAs): the release deployer, to attach
    # it to the machine, and swarm-api (row I2), for setMetadata on a machine
    # that runs as it. Nothing grants it a project role here.
    acts = {}
    for name in ("deployer_acts_as_speech", "api_acts_as_speech"):
        body = _resource(text, "google_service_account_iam_member", name)
        assert re.search(r'role\s*=\s*"roles/iam\.serviceAccountUser"', body), name
        assert re.search(r"service_account_id\s*=\s*google_service_account\.speech\.name", body), name
        acts[name] = body
    assert re.search(r"google_service_account\.deployer\[0\]\.email", acts["deployer_acts_as_speech"])
    assert re.search(r'local\.platform_sa_email\["swarm-api"\]', acts["api_acts_as_speech"])
    assert not re.search(r'resource\s+"google_project_iam_member"', text)


def test_infra_reads_the_account_and_never_makes_it(speech: str) -> None:
    assert '"google_service_account"' not in "".join(f'"{t}"' for t, _ in _resource_types(speech))
    assert re.search(r'^data\s+"google_service_account"\s+"speech"', speech, re.M)
    assert re.search(
        r'speech_email\s*=\s*"\$\{module\.service_account_ids\.speech_id\}@\$\{var\.project_id\}'
        r'\.iam\.gserviceaccount\.com"',
        speech,
    )


# --- the ids, spelled once (owner's pick, 2026-11-05) -------------------------


def test_the_ids_are_spelled_in_the_id_modules() -> None:
    accounts = _code((TF / "modules" / "service_account_ids" / "main.tf").read_text())
    assert re.search(r'^\s*speech_id\s*=\s*"swarm-speech"', accounts, re.M)
    outputs = _code((TF / "modules" / "service_account_ids" / "outputs.tf").read_text())
    assert re.search(r'^output\s+"speech_id"', outputs, re.M)
    # Off bootstrap_platform, so the deployer holds no serviceAccountAdmin on it.
    platform = re.search(r"bootstrap_platform\s*=\s*sort\(concat\((.*?)\)\)", accounts, re.S)
    assert platform and "voice_id" in platform.group(1)
    assert "speech_id" not in platform.group(1)

    roles = _code((TF / "modules" / "custom_role_ids" / "main.tf").read_text())
    assert re.search(r'^\s*speech_operator\s*=\s*"swarmSpeechOperator\$\{local\.role_suffix\}"', roles, re.M)


@pytest.mark.parametrize("path", [SPEECH_TF, BOOTSTRAP_TF, DEPLOYER_TF, GRANT_TF], ids=lambda p: p.name)
def test_no_lane_file_spells_the_ids(path: Path) -> None:
    code = _code(path.read_text())
    assert "swarmSpeechOperator" not in code
    # The audience string "swarm-speech" and the metadata keys
    # "swarm-speech-*" are not the account id; an account id is followed by @
    # or stands as a whole quoted string after account_id.
    assert "swarm-speech@" not in code
    assert not re.search(r'account_id\s*=\s*"swarm-speech"', code)


# --- the release deployer's grant on the machine (owner, 2026-11-05) -------------


@pytest.fixture(scope="module")
def deployer() -> str:
    return _code(DEPLOYER_TF.read_text())


def test_the_deployer_condition_names_the_prefix_the_machine_is_named_with(speech: str, deployer: str) -> None:
    """The prefix in the deployer's condition IS the one voice_speech.tf names
    the template with, and swarm-api names the machine
    `<prefix>machine` (SWARM_SPEECH_INSTANCE), whose boot disk takes the
    instance's name. A rename on either side alone would leave CI unable to
    make the template, or swarm-api the machine, or able to make something
    else."""
    machine = re.search(r'^\s*speech_machine_prefix\s*=\s*"([^"]+)"', speech, re.M)
    assert machine, "voice_speech.tf names its template from no local.speech_machine_prefix"
    template = _resource(speech, "google_compute_region_instance_template", "speech")
    assert re.search(r"^\s*name_prefix\s*=\s*local\.speech_machine_prefix\s*$", template, re.M)
    # A boot disk from the template's source_image, with no name of its own,
    # is named after the instance.
    disk = _block(template, r"^\s*disk")
    assert "source_image" in disk
    assert not re.search(r"^\s*(disk_name|source)\s*=", disk, re.M)
    locals_tf = _code((TF / "infra" / "locals.tf").read_text())
    assert re.search(r"SWARM_SPEECH_INSTANCE\s*=\s*local\.voice_speech_on\s*\?\s*local\.speech_machine_prefix", locals_tf)

    granted = re.search(r'^\s*deployer_speech_machine_prefix\s*=\s*"([^"]+)"', deployer, re.M)
    assert granted, "deployer_conditions.tf has no deployer_speech_machine_prefix"
    assert granted.group(1) == machine.group(1)
    # Ends at the dash, so swarm-speechx is not admitted.
    assert granted.group(1) == "${var.name_prefix}-speech-"

    kinds = re.search(r"deployer_speech_machine_kinds\s*=\s*\[([^\]]*)\]", deployer)
    assert kinds and set(re.findall(r'"(\w+)"', kinds.group(1))) == {"instances", "disks"}
    assert re.search(
        r'deployer_speech_template_prefix\s*=\s*"projects/\$\{var\.project_id\}/regions/\$\{var\.region\}'
        r'/instanceTemplates/\$\{local\.deployer_speech_machine_prefix\}"', deployer)


def test_the_deployer_holds_the_machine_verbs_and_only_conditioned(deployer: str) -> None:
    role = _resource(deployer, "google_project_iam_custom_role", "deployer_speech_machine")
    perms = set(re.findall(r'"([a-z]+\.[a-zA-Z.]+)"', role.split("permissions", 1)[1]))
    assert perms == DEPLOYER_PERMISSIONS, sorted(perms ^ DEPLOYER_PERMISSIONS)
    assert re.search(r'description\s*=\s*"managed-by=swarm-terraform;', role)
    desc = re.search(r'description\s*=\s*"([^"]*)"', role).group(1)
    assert len(desc) <= 300

    binding = _resource(deployer, "google_project_iam_member", "deployer_speech_machine")
    assert re.search(r"google_project_iam_custom_role\.deployer_speech_machine\[0\]\.role_id", binding)
    assert re.search(r"google_service_account\.deployer\[0\]\.email", binding)
    condition = _block(binding, r"^\s*condition")
    assert re.search(r"expression\s*=\s*local\.deployer_speech_machine_condition", condition)
    # The role is held through this one binding, never unconditioned.
    held = re.findall(r"google_project_iam_custom_role\.deployer_speech_machine\b", deployer)
    assert len(held) == 1


# --- the samples' TTL (§12.3) ---------------------------------------------------


def test_samples_expire_through_a_ttl_policy(speech: str) -> None:
    body = _resource(speech, "google_firestore_field", "voice_speech_samples_ttl")
    assert re.search(r'collection\s*=\s*"samples"', body)
    assert re.search(r'field\s*=\s*"expire_at"', body)
    assert re.search(r"ttl_config\s*\{\s*\}", body)
    assert re.search(r"database\s*=\s*module\.firestore\.database_name", body)


# --- the shared project ---------------------------------------------------------


def _deny_list() -> list[str]:
    """The never-touch list, read the way every script reads it: through
    deny_list_load in scripts/lib/common.sh, from a config object. The
    repository holds no deployment's list (docs/design/generic-repository.md,
    section 2.4), so the object is the placeholder one,
    scripts/lib/deny-list.example.json, laid out by deny_list_use_example in a
    directory of this test's own; the plan guard judges every real plan against
    the deployment's own object."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROJECT_ID", "ENVIRONMENT", "SWARM_DEPLOY_CONFIG_DIR", "XDG_CACHE_HOME")}
        env.update(SWARM_NO_PROJECT="1", SWARM_ENV_FILE=str(Path(tmp) / "absent.env"), NO_COLOR="1")
        proc = subprocess.run(
            ["bash", "-c", 'set -euo pipefail; source "$1"; deny_list_use_example "$2"; '
             'printf "%s\\n" "${SHARED_DENY_LIST[@]}"', "_", str(COMMON_SH), tmp],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.splitlines()
    assert "peer-vpc" in names, names
    return names


@pytest.mark.parametrize("path", [SPEECH_TF, BOOTSTRAP_TF, GRANT_TF, FRAGMENT], ids=lambda p: p.name)
def test_nothing_names_a_deny_listed_resource(path: Path) -> None:
    text = path.read_text()
    literals = set(re.findall(r'"([^"\n]*)"', text))
    for name in _deny_list():
        # Every quoted value, whole; and the multi-word names anywhere at all
        # ("default" alone is an ordinary English word in a comment).
        assert name not in literals, f"{path.name} quotes deny-listed {name!r}"
        if "-" in name or "@" in name or "_" in name:
            assert name not in text, f"{path.name} names deny-listed {name!r}"
    code = _code(text)
    assert not re.search(r'network\s*=\s*"', code), "a network named by a literal, not module.network"


# --- the installer's manifest fragment -------------------------------------------


def test_manifest_fragment_lists_every_resource() -> None:
    from swarm_install import manifest as m

    data = yaml.safe_load(FRAGMENT.read_text())
    entries = [m.Entry.model_validate(e) for e in data["entries"]]
    assert all(e.step for e in entries), "every entry names the step that satisfies it"
    refs = {r for e in entries for r in e.refs.terraform}
    speech = _code(SPEECH_TF.read_text())
    want = {f"infra/{t}.{n}" for t, n in _resource_types(speech)}
    want |= {f"infra/data.google_service_account.{n}" for n in re.findall(
        r'^data\s+"google_service_account"\s+"([a-z_]+)"', speech, re.M)}
    for path in (BOOTSTRAP_TF, DEPLOYER_TF, GRANT_TF):
        boot = _code(path.read_text())
        want |= {
            f"bootstrap/{t}.{n}"
            for t, n in _resource_types(boot)
            if "speech" in n
        }
    assert want <= refs, sorted(want - refs)
    steps = {e.id: e.step for e in entries}
    assert steps["sa.swarm-speech"] == "bootstrap.pass-1"
    assert steps["compute.voice-speech-machine"] == "infra.pass-1"
    assert steps["iam-binding.deployer-speech-machine"] == "bootstrap.pass-1"
