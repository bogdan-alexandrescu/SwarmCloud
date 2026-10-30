"""The Day One edge (DAYONE-TF-2; docs/design/day-one-plan.md §1.2, design §9.2).

* the front door serves /.well-known/swarmcloud from swarm-discovery, through a
  backend with no IAP, and swarm-discovery holds no role unless it serves the
  Desktop client secret;
* no `allUsers` binding: the GitHub receiver disables the invoker check (Q3),
  and the acceptance fixture's public bucket is an `addon:ci` switch, off;
* swarm-api can store the GitHub App's three secrets, and only those, under
  `enable_console_app_setup`, whose containers exist without the App switches;
* the release deployer may pass IAP on the API backend only;
* the hourly doctor is off by default, is a platform account, and cannot write.

Read as text: unit tests run with no Terraform, no credentials and no cloud.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TF = ROOT / "terraform"
INFRA = TF / "infra"
BOOTSTRAP = TF / "bootstrap"

APP_SECRETS = {
    "swarm-github-app-client-secret",
    "swarm-github-app-private-key",
    "swarm-github-app-webhook-secret",
}
DOCTOR_ROLES_FILE = BOOTSTRAP / "doctor_roles.tf"
WRITE_VERBS = ("create", "update", "delete", "setIamPolicy", "add")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_tf() -> list[Path]:
    return sorted(TF.glob("bootstrap/*.tf")) + sorted(TF.glob("infra/*.tf")) + sorted(TF.glob("modules/*/*.tf"))


def _block(text: str, header: str) -> str:
    """The `header { ... }` block (header is a regex for its first line)."""
    match = re.search(rf"^{header}\s*\{{", text, re.M)
    assert match, f"no block {header!r}"
    depth = 0
    in_string = False
    for i in range(match.end() - 1, len(text)):
        # Braces inside a quoted string (an error_message's `{ ... }`) do not count.
        if text[i] == '"' and text[i - 1] != "\\":
            in_string = not in_string
        elif in_string:
            continue
        elif text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : i + 1]
    raise AssertionError(f"unbalanced block {header!r}")


def _resource(path: Path, rtype: str, name: str) -> str:
    return _block(_text(path), rf'resource\s+"{rtype}"\s+"{name}"')


def _code(text: str) -> str:
    """The text without `#` comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _variable_default(path: Path, name: str) -> str:
    block = _block(_text(path), rf'variable\s+"{name}"')
    match = re.search(r"^\s*default\s*=\s*(\S+)\s*$", block, re.M)
    assert match, f"variable {name} has no one-line default"
    return match.group(1)


def test_no_allusers_binding_anywhere() -> None:
    """No `allUsers` member, except the acceptance fixture's, which is gated off."""
    found = []
    for path in _all_tf():
        for n, line in enumerate(_code(_text(path)).splitlines(), 1):
            if re.search(r'=\s*"allUsers"\s*$', line):
                found.append(f"{path.relative_to(ROOT)}:{n}")
    assert all("acceptance_fixture.tf" in f for f in found), (
        f"an allUsers member outside the gated acceptance fixture: {found}"
    )
    hooks = _resource(INFRA / "github_hooks.tf", "google_cloud_run_v2_service", "github_hooks")
    assert re.search(r"^\s*invoker_iam_disabled\s*=\s*true\s*$", hooks, re.M), (
        "swarm-github-hooks must disable the invoker check instead of binding allUsers (Q3)"
    )
    assert "github_hooks_public" not in _code(_text(INFRA / "github_hooks.tf"))


def test_discovery_path_rule_targets_non_iap_backend() -> None:
    frontend = _text(TF / "modules" / "frontend" / "main.tf")
    backend = _resource(TF / "modules" / "frontend" / "main.tf", "google_compute_backend_service", "discovery")
    assert not re.search(r"enabled\s*=\s*true", backend), "the discovery backend must not turn IAP on"
    rule = re.search(
        r'(?:path_rule|content)\s*\{\s*paths\s*=\s*\["/\.well-known/swarmcloud"\]\s*service\s*=\s*([^\n]+)\n',
        frontend,
    )
    assert rule, "no path_rule for /.well-known/swarmcloud in the URL map"
    assert "google_compute_backend_service.discovery" in rule.group(1)
    neg = _resource(TF / "modules" / "frontend" / "main.tf", "google_compute_region_network_endpoint_group", "discovery")
    assert "var.discovery_service_name" in neg
    service = _resource(INFRA / "discovery.tf", "google_cloud_run_v2_service", "discovery")
    assert re.search(r"^\s*invoker_iam_disabled\s*=\s*true\s*$", service, re.M)
    assert re.search(r'^\s*ingress\s*=\s*"INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"\s*$', service, re.M)
    assert "swarm_api.discovery:create_app" in service
    main = _block(_text(INFRA / "main.tf"), r'module\s+"frontend"')
    assert "discovery_service_name" in main, "module.frontend is not given the discovery service"


def test_discovery_has_no_roles_by_default() -> None:
    assert _variable_default(INFRA / "variables.tf", "discovery_serves_client_secret") == "false"
    grants = []
    for path in _all_tf():
        code = _code(_text(path))
        for match in re.finditer(r'^resource\s+"(google_[a-z_]+_iam_(?:member|binding))"\s+"([a-z_]+)"', code, re.M):
            block = _block(code, rf'resource\s+"{match.group(1)}"\s+"{match.group(2)}"')
            if re.search(r"discovery_member|swarm-discovery|discovery_id", block):
                grants.append((path, match.group(2), block))
    assert len(grants) == 1, f"swarm-discovery must hold exactly one grant: {[g[1] for g in grants]}"
    _, name, block = grants[0]
    assert "roles/secretmanager.secretAccessor" in block
    assert "swarm-iap-desktop-client-secret" in block or "desktop_client_secret" in block
    assert re.search(r"count\s*=\s*var\.discovery_serves_client_secret\s*\?", block), name


def test_app_secret_writer_scoped_to_three_names() -> None:
    text = _text(BOOTSTRAP / "github_app_setup.tf")
    names = set(re.findall(r'"(swarm-github-app-[a-z-]+)"', text))
    assert names == APP_SECRETS, f"the writer's names are {sorted(names)}"
    for role in ("roles/secretmanager.secretVersionAdder", "roles/secretmanager.secretAccessor"):
        assert role in text, f"swarm-api lacks {role} on the App's secrets"
    for match in re.finditer(r'^resource\s+"google_project_iam_member"\s+"([a-z_]+)"', text, re.M):
        block = _block(text, rf'resource\s+"google_project_iam_member"\s+"{match.group(1)}"')
        assert "condition" in block and "resource.name" in block, (
            f"{match.group(1)}: a project grant on secrets must be conditioned on the three names"
        )
        assert "swarm-api" in block or "api_member" in block


def test_app_secret_containers_exist_without_app_switches() -> None:
    assert _variable_default(INFRA / "variables.tf", "enable_console_app_setup") == "false"
    main = _block(_text(INFRA / "main.tf"), r'module\s+"secret_manager"')
    assert re.search(
        r"github_app_secrets_enabled\s*=\s*var\.enable_github_app\s*\|\|\s*var\.enable_console_app_setup", main
    ), "the App's two secret containers must exist under enable_console_app_setup alone"
    webhook = _resource(INFRA / "github_hooks.tf", "google_secret_manager_secret", "github_webhook_secret")
    assert re.search(r"count\s*=\s*\(?local\.github_hooks_on\s*\|\|\s*var\.enable_console_app_setup", webhook), (
        "the webhook secret container must exist under enable_console_app_setup alone"
    )


def test_doctor_gated_off_by_default() -> None:
    assert _variable_default(INFRA / "variables.tf", "enable_scheduled_doctor") == "false"
    doctor = _text(INFRA / "doctor.tf")
    for rtype, name in (
        ("google_cloud_run_v2_job", "doctor"),
        ("google_cloud_scheduler_job", "doctor_hourly"),
        ("google_cloud_run_v2_job_iam_member", "doctor_scheduler_runs_job"),
        ("google_cloud_run_v2_service_iam_member", "doctor_invokes_api"),
        ("google_logging_metric", "doctor_findings"),
        ("google_monitoring_alert_policy", "doctor_findings"),
    ):
        block = _resource(INFRA / "doctor.tf", rtype, name)
        assert re.search(r"count\s*=\s*(local\.doctor_on|var\.enable_scheduled_doctor)", block), f"{rtype}.{name}"
    assert re.search(r'^\s*schedule\s*=\s*"0 \* \* \* \*"\s*$', doctor, re.M), "the doctor is not hourly"


def test_doctor_in_platform_service_accounts() -> None:
    locals_tf = _text(INFRA / "locals.tf")
    match = re.search(r"PLATFORM_SERVICE_ACCOUNTS\s*=\s*join\(\",\",\s*(.*?)\)\n", locals_tf, re.S)
    assert match, "PLATFORM_SERVICE_ACCOUNTS is not rendered"
    assert "doctor" in match.group(1), "swarm-doctor's email is not in PLATFORM_SERVICE_ACCOUNTS (lesson 38)"


def test_acceptance_fixture_off_by_default() -> None:
    assert _variable_default(INFRA / "variables.tf", "enable_acceptance_fixture") == "false"
    path = INFRA / "acceptance_fixture.tf"
    for rtype, name in (
        ("google_storage_bucket", "acceptance_fixture"),
        ("google_storage_bucket_object", "acceptance_fixture"),
        ("google_storage_bucket_iam_member", "acceptance_fixture_public_read"),
    ):
        assert re.search(r"count\s*=\s*var\.enable_acceptance_fixture\s*\?\s*1\s*:\s*0", _resource(path, rtype, name))
    text = _text(path)
    for name in ("acceptance_fixture", "acceptance_fixture_public_read"):
        assert re.search(rf"moved\s*\{{\s*from\s*=\s*\S+\.{name}\s*to\s*=\s*\S+\.{name}\[0\]", text), name
    for env in ("dev", "prod"):
        tfvars = _text(TF / "environments" / env / f"{env}.tfvars")
        assert re.search(r"^enable_acceptance_fixture\s*=\s*true\s*$", tfvars, re.M), (
            f"{env}.tfvars must keep the live fixture (no change in the live plan)"
        )


def test_deployer_iap_accessor_on_api_backend_only() -> None:
    text = _code(_text(BOOTSTRAP / "github_app_setup.tf"))
    block = _block(text, r'resource\s+"google_iap_web_backend_service_iam_member"\s+"deployer_api_accessor"')
    assert "roles/iap.httpsResourceAccessor" in block
    assert "google_service_account.deployer" in block
    assert re.search(r'"\$\{var\.name_prefix\}-ui-backend"', text), "the API backend is named exactly"
    assert "ui-ui-backend" not in text, "the deployer must never pass IAP on the UI backend"


def test_doctor_roles_are_read_only() -> None:
    text = _code(_text(DOCTOR_ROLES_FILE))
    roles = re.findall(r'"(roles/[A-Za-z.]+)"', text)
    assert roles, "doctor_roles.tf lists no role"
    data = json.loads(_text(BOOTSTRAP / "doctor-role-permissions.json"))
    exempt = set(re.findall(r'^\s*"([a-z]+\.[A-Za-z.]+)",?\s*$', _block(text, "locals"), re.M)) - set(roles)
    writes = []
    for role in roles:
        assert role in data["roles"], f"{role} has no checked-in permission list"
        writes += [
            f"{role}: {p}"
            for p in data["roles"][role]
            if p.rsplit(".", 1)[1].startswith(WRITE_VERBS) and p not in exempt
        ]
    assert not writes, "a doctor role can write:\n" + "\n".join(writes)
    for role in roles:
        assert not role.endswith((".admin", ".editor", "/owner", "/editor"))
