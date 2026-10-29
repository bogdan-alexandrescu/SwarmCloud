"""A new deployment's Terraform inputs are inputs: no live literal, no
hand-copied value, one Terraform version (lane DAYONE-TF-1, part of #2688;
docs/design/day-one-install.md §5.2 and §14 Q8, docs/design/day-one-plan.md R9).

  * module.frontend depends on the project's services only, not on
    module.cloud_run, so swarm-api's IAP_AUDIENCES is read from the frontend
    module's own output instead of being copied out of `terraform output` and
    back into tfvars;
  * the quota broker's and swarm-api's URLs are built from the project number,
    Cloud Run's deterministic `https://<service>-<number>.<region>.run.app`,
    instead of being pasted from a first apply;
  * both roots require the Terraform scripts/prerequisites.sh pins;
  * no `default` in either root (nor the monitoring module the infra root
    calls) names the live project, its number, its domain, its tailnet or its
    tenants: a default is what a fresh install gets without asking;
  * ALLOWED_USERS' extra principal is a variable, not a literal in locals.tf.

Text-level and offline: nothing here runs terraform. `terraform validate`
and `terraform test` (the terraform workflow) hold the same files to a plan.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "terraform" / "infra"
BOOTSTRAP = ROOT / "terraform" / "bootstrap"
MONITORING_VARS = ROOT / "terraform" / "modules" / "monitoring" / "variables.tf"
PREREQUISITES = ROOT / "scripts" / "prerequisites.sh"

# What only the live deployment is: its project, the project's number, its
# hosted domain, its tailnet and its two project-derived service tenants. A
# default holding any of them configures a new install as the live one.
LIVE_MARKERS = (
    "saga-agents-staging",
    "209012342332",
    "saga.xyz",
    "taile0bda9",
    "u-sw-c90291",
    "u-or-9a0792",
)


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"\s#.*$|^\s*#.*$", "", line) for line in text.splitlines())


def _block(text: str, header: str) -> str:
    """The body of the top-level block opened by `header` (e.g. `module "frontend"`)."""
    match = re.search(rf"(?m)^{re.escape(header)} \{{\n(.*?)^\}}", text, re.S)
    assert match, f"no top-level `{header}` block"
    return match.group(1)


def _variables(path: Path) -> dict[str, str]:
    """Each top-level `variable` block in `path`, by name, comments stripped."""
    text = path.read_text()
    return {
        m.group(1): _strip_comments(m.group(2))
        for m in re.finditer(r'(?m)^variable "([^"]+)" \{\n(.*?)^\}', text, re.S)
    }


def _default(body: str) -> str | None:
    """A variable body's `default` expression, up to the next attribute or block."""
    match = re.search(r"(?ms)^  default\s*=\s*(.*?)(?=^  [a-z_]+\s*(?:=|\{)|\Z)", body)
    return match.group(1).strip() if match else None


def test_frontend_depends_only_on_services():
    body = _block((INFRA / "main.tf").read_text(), 'module "frontend"')
    (depends_on,) = re.findall(r"(?m)^  depends_on\s*=\s*(\[[^\]]*\])", body)
    assert re.sub(r"\s", "", depends_on) == "[module.project_services]", (
        "module.frontend must not wait for module.cloud_run: swarm-api's environment reads the "
        "frontend's IAP audiences, so that edge is the cycle that forced a hand copy"
    )


def test_iap_audiences_computed():
    locals_tf = _strip_comments((INFRA / "locals.tf").read_text())
    (iap,) = re.findall(r"(?m)^\s*IAP_AUDIENCES\s*=\s*(.+)$", locals_tf)
    assert "module.frontend[0].iap_audiences" in iap
    assert "var.enable_frontend" in iap, "module.frontend has count 0 when the frontend is off"
    for path in INFRA.glob("*.tf"):
        assert "var.frontend_iap_audiences" not in path.read_text(), path.name
    assert "frontend_iap_audiences" not in _variables(INFRA / "variables.tf")


@pytest.mark.parametrize(
    ("env_name", "local_name", "service"),
    [
        ("QUOTA_BROKER_URL", "quota_broker_url", "swarm-quota-broker"),
        ("SWARM_API_URL", "swarm_api_url", "swarm-api"),
    ],
)
def test_broker_url_computed(env_name: str, local_name: str, service: str):
    texts = {p.name: _strip_comments(p.read_text()) for p in INFRA.glob("*.tf")}
    assignments = re.findall(rf"(?m)^\s*{env_name}\s*=\s*(\S+)\s*$", texts["locals.tf"])
    assert assignments, f"{env_name} is not rendered in locals.tf"
    # The services' own environments take the computed URL; worker jobs, which
    # live outside module.cloud_run, may read its output. Never a variable.
    assert f"local.{local_name}" in assignments
    assert set(assignments) <= {
        f"local.{local_name}",
        f'module.cloud_run.service_urls["{service}"]',
    }, assignments
    (definition,) = [
        m.group(1)
        for text in texts.values()
        for m in re.finditer(rf"(?m)^\s*{local_name}\s*=\s*(.+)$", text)
    ]
    assert definition == (
        f'"https://{service}-${{data.google_project.this.number}}.${{var.region}}.run.app"'
    )
    declared = {name for p in INFRA.glob("*.tf") for name in _variables(p)}
    assert local_name not in declared, f"var.{local_name} is a hand copy of a computable URL"
    for name, text in texts.items():
        assert f"var.{local_name}" not in text, name


@pytest.mark.parametrize("root", [INFRA, BOOTSTRAP], ids=["infra", "bootstrap"])
def test_required_version_pinned(root: Path):
    (pin,) = re.findall(r'check_tool terraform "([0-9.]+)"', PREREQUISITES.read_text())
    versions = _strip_comments((root / "versions.tf").read_text())
    assert re.findall(r'(?m)^\s*required_version\s*=\s*"([^"]+)"', versions) == [f"~> {pin}"]


def _roots_variables() -> list[tuple[str, str, str]]:
    paths = [*INFRA.glob("*.tf"), *BOOTSTRAP.glob("*.tf"), MONITORING_VARS]
    return [
        (str(p.relative_to(ROOT)), name, body)
        for p in sorted(paths)
        for name, body in _variables(p).items()
    ]


def test_no_live_default():
    assert len(_roots_variables()) > 100, "the variable scan found too few blocks to be reading them"
    found = []
    for path, name, body in _roots_variables():
        default = _default(body)
        if default is None:
            continue
        hits = [m for m in LIVE_MARKERS if m in default]
        if hits:
            found.append(f"{path} variable {name}: {hits}")
    assert not found, "defaults that configure a new install as the live one:\n" + "\n".join(found)


def test_allowed_domains_has_no_default_domain():
    body = _variables(INFRA / "variables.tf")["allowed_domains"]
    assert _default(body) == "null"


def test_tailscale_target_has_no_default():
    body = _variables(INFRA / "tailscale.tf")["tailscale_egress_target"]
    assert _default(body) == "null"


def test_monitoring_excluded_tenants_default_names_no_tenant():
    body = _variables(MONITORING_VARS)["fencing_alert_excluded_tenants"]
    assert _default(body) == "[]"
    monitoring = _block((INFRA / "main.tf").read_text(), 'module "monitoring"')
    assert re.search(r"(?m)^\s*fencing_alert_excluded_tenants\s*=", monitoring)


def test_allowed_users_from_variable():
    locals_tf = _strip_comments((INFRA / "locals.tf").read_text())
    match = re.search(r"ALLOWED_USERS\s*=\s*join\(\",\",\s*(.*?)\)\n", locals_tf, re.S)
    assert match, "ALLOWED_USERS is not rendered in locals.tf"
    assert "var.allowed_users" in match.group(1)
    assert "@" not in match.group(1), "an address written into ALLOWED_USERS is a live literal"
    assert "allowed_users" in _variables(INFRA / "variables.tf")
