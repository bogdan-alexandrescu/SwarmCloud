"""Voice it's own speech engine: the speech machine exists, stopped, on swarm-vpc.

docs/design/voice-it-own-speech.md §3.2 (address, firewall, identity), §5.1
(the machine's shape), §6 (the IAM rows) and §12.3 (the samples' TTL), lane L1.
Owner's answers of 2026-11-05: one zone, one machine; the machine's account
may write its own logs (logging.logWriter) beside its metrics
(monitoring.metricWriter).

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
FRAGMENT = ROOT / "install" / "manifest.d" / "voice-speech-machine.yaml"
COMMON_SH = ROOT / "scripts" / "lib" / "common.sh"

OPERATOR_PERMISSIONS = {
    "compute.instances.get",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.setMachineType",
    "compute.instances.setMetadata",
    "compute.instances.getSerialPortOutput",
}

# What the release deployer may do to the speech machine and its boot disk:
# exactly the verbs voice_speech.tf's google_compute_instance and
# google_compute_instance_iam_member use, and nothing on any other type.
DEPLOYER_PERMISSIONS = {
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
def instance(speech: str) -> str:
    return _resource(speech, "google_compute_instance", "speech")


def _resource_types(text: str) -> list[tuple[str, str]]:
    return re.findall(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"', text, re.M)


# --- the switch --------------------------------------------------------------


def test_variable_is_off_by_default() -> None:
    body = _block(VARIABLES_TF.read_text(), r'^variable\s+"voice_speech"')
    assert re.search(r"enabled\s*=\s*optional\(bool,\s*false\)", body), body
    assert re.search(r'zone\s*=\s*optional\(string,\s*"us-central1-a"\)', body), body
    assert re.search(r'machine_type\s*=\s*optional\(string,\s*"g2-standard-4"\)', body), body
    assert re.search(r"disk_gb\s*=\s*optional\(number,\s*150\)", body), body
    assert re.search(r"idle_minutes\s*=\s*optional\(number,\s*5\)", body), body
    # The pinned image is required once it is on, and only a Deep Learning VM
    # image by name (never a family, which moves under a stopped machine).
    assert "deeplearning-platform-release" in body
    assert "/family/" in body


@pytest.mark.parametrize("tfvars", sorted((TF / "environments").glob("*/*.tfvars")), ids=lambda p: p.parent.name)
def test_every_environment_leaves_it_off(tfvars: Path) -> None:
    text = _code(tfvars.read_text())
    body = _block(text, r"^voice_speech\s*=")
    assert re.search(r"^\s*enabled\s*=\s*false\s*$", body, re.M), f"{tfvars}: voice_speech is not off"


def test_every_resource_is_behind_the_switch(speech: str) -> None:
    assert re.search(
        r"voice_speech_on\s*=\s*var\.voice_speech\.enabled\s*&&\s*var\.voice_it\s*==\s*\"on\"", speech
    ), "voice_speech_on is not voice_speech.enabled && voice_it == \"on\""
    for rtype, name in _resource_types(speech):
        body = _resource(speech, rtype, name)
        assert re.search(r"^\s*count\s*=\s*local\.voice_speech_on\s*\?\s*1\s*:\s*0\s*$", body, re.M), (
            f"{rtype}.{name} is not behind local.voice_speech_on"
        )
    for name in re.findall(r'^data\s+"[a-z0-9_]+"\s+"([a-z0-9_]+)"', speech, re.M):
        body = _block(speech, rf'^data\s+"[a-z0-9_]+"\s+"{name}"')
        assert re.search(r"^\s*count\s*=\s*local\.voice_speech_on\s*\?\s*1\s*:\s*0\s*$", body, re.M), name


# --- the machine (§5.1) ------------------------------------------------------


def test_machine_is_on_demand(instance: str) -> None:
    sched = _block(instance, r"^\s*scheduling")
    assert re.search(r'provisioning_model\s*=\s*"STANDARD"', sched), "invariant 6: Spot is off platform-wide"
    assert re.search(r"preemptible\s*=\s*false", sched)
    assert re.search(r'on_host_maintenance\s*=\s*"TERMINATE"', sched)
    assert re.search(r"automatic_restart\s*=\s*false", sched), "a host event must leave it stopped"
    assert "SPOT" not in instance.upper()


def test_machine_is_created_stopped_and_swarm_api_owns_its_state(instance: str) -> None:
    assert re.search(r'^\s*desired_status\s*=\s*"TERMINATED"', instance, re.M)
    life = _block(instance, r"^\s*lifecycle")
    ignored = re.search(r"ignore_changes\s*=\s*\[(.*?)\]\s*$", life, re.S | re.M)
    assert ignored, "lifecycle has no ignore_changes"
    ignored = ignored.group(1)
    for field in (
        "desired_status",
        "machine_type",
        'metadata["swarm-speech-image"]',
        'metadata["swarm-speech-models"]',
    ):
        assert field in ignored, f"lifecycle.ignore_changes misses {field}"


def test_machine_has_no_external_ip_and_sits_on_swarm_vpc(instance: str) -> None:
    nic = _block(instance, r"^\s*network_interface")
    assert "access_config" not in nic, "the speech machine has no external IP"
    assert "ipv6_access_config" not in nic
    assert re.search(r"subnetwork\s*=\s*module\.network\.subnetwork_id", nic)
    assert re.search(r"network_ip\s*=\s*google_compute_address\.speech\[0\]\.address", nic)


def test_machine_carries_its_tag_and_labels(speech: str, instance: str) -> None:
    assert re.search(r"speech_tag\s*=\s*\"\$\{var\.name_prefix\}-speech\"", speech)
    assert re.search(r"tags\s*=\s*\[local\.speech_tag\]", instance)
    for rtype, name in [("google_compute_instance", "speech"), ("google_compute_address", "speech")]:
        body = _resource(speech, rtype, name)
        assert re.search(r"labels\s*=\s*local\.speech_labels", body), f"{rtype}.{name} has no labels"
    disk = _block(instance, r"^\s*initialize_params")
    assert re.search(r"labels\s*=\s*local\.speech_labels", disk), "the boot disk has no labels"
    assert re.search(r'speech_labels\s*=\s*merge\(local\.labels,\s*\{\s*"component"\s*=\s*"voice-speech"', speech)


def test_machine_shape(speech: str, instance: str) -> None:
    assert re.search(r"machine_type\s*=\s*var\.voice_speech\.machine_type", instance)
    assert re.search(r"zone\s*=\s*var\.voice_speech\.zone", instance)
    assert re.search(r'name\s*=\s*"\$\{local\.speech_machine_prefix\}\$\{local\.speech_zone_suffix\}"', instance)
    assert re.search(r'^\s*speech_machine_prefix\s*=\s*"\$\{var\.name_prefix\}-speech-"', speech, re.M)
    disk = _block(instance, r"^\s*initialize_params")
    assert re.search(r'type\s*=\s*"pd-balanced"', disk)
    assert re.search(r"size\s*=\s*var\.voice_speech\.disk_gb", disk)
    assert re.search(r"image\s*=\s*var\.voice_speech\.image", disk)
    shielded = _block(instance, r"^\s*shielded_instance_config")
    assert re.search(r"enable_secure_boot\s*=\s*false", shielded)
    assert re.search(r"enable_vtpm\s*=\s*true", shielded)
    assert re.search(r"enable_integrity_monitoring\s*=\s*true", shielded)
    sa = _block(instance, r"^\s*service_account")
    assert re.search(r"email\s*=\s*local\.speech_email", sa)


def test_machine_metadata_names_its_caller_and_no_secret(instance: str) -> None:
    meta = _block(instance, r"^\s*metadata\s*=")
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


def test_the_zone_suffix_is_the_zone_letter(speech: str) -> None:
    assert re.search(
        r'speech_zone_suffix\s*=\s*element\(split\("-",\s*var\.voice_speech\.zone\),\s*2\)', speech
    )


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


def test_swarm_api_operates_each_instance_only(speech: str) -> None:
    body = _resource(speech, "google_compute_instance_iam_member", "api_speech_operator")
    assert re.search(r"instance_name\s*=\s*google_compute_instance\.speech\[0\]\.name", body)
    assert re.search(r"role\s*=\s*local\.speech_operator_role", body)
    assert re.search(r'member\s*=\s*module\.iam\.service_account_members\["swarm-api"\]', body)
    assert re.search(r'speech_operator_role\s*=\s*module\.speech_role_ids\.names\["speech_operator"\]', speech)
    ids = _block(speech, r'^module\s+"speech_role_ids"')
    assert re.search(r'source\s*=\s*"\.\./modules/custom_role_ids"', ids)


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
    assert perms == OPERATOR_PERMISSIONS, perms

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


@pytest.mark.parametrize("path", [SPEECH_TF, BOOTSTRAP_TF, DEPLOYER_TF], ids=lambda p: p.name)
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
    its instance with, and the boot disk takes the instance's name. A rename on
    either side alone would leave CI unable to make the machine, or able to
    make something else."""
    machine = re.search(r'^\s*speech_machine_prefix\s*=\s*"([^"]+)"', speech, re.M)
    assert machine, "voice_speech.tf names its instance from no local.speech_machine_prefix"
    instance = _resource(speech, "google_compute_instance", "speech")
    assert re.search(r'^\s*name\s*=\s*"\$\{local\.speech_machine_prefix\}\$\{local\.speech_zone_suffix\}"', instance, re.M)
    # A boot disk made from initialize_params with no source of its own is
    # named after the instance.
    boot = _block(instance, r"^\s*boot_disk")
    assert "initialize_params" in boot
    assert not re.search(r"^\s*source\s*=", boot, re.M)

    granted = re.search(r'^\s*deployer_speech_machine_prefix\s*=\s*"([^"]+)"', deployer, re.M)
    assert granted, "deployer_conditions.tf has no deployer_speech_machine_prefix"
    assert granted.group(1) == machine.group(1)
    # Ends at the dash, so swarm-speechx is not admitted.
    assert granted.group(1) == "${var.name_prefix}-speech-"

    kinds = re.search(r"deployer_speech_machine_kinds\s*=\s*\[([^\]]*)\]", deployer)
    assert kinds and set(re.findall(r'"(\w+)"', kinds.group(1))) == {"instances", "disks"}


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


@pytest.mark.parametrize("path", [SPEECH_TF, BOOTSTRAP_TF, FRAGMENT], ids=lambda p: p.name)
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
    for path in (BOOTSTRAP_TF, DEPLOYER_TF):
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
