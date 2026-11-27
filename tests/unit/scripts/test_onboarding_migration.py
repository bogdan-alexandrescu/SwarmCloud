# source-text: contract - swarm-api settings.py must default REPOSITORY_GRANTS_ENFORCED to off and terraform must render it from the repository_grants_enforced variable
"""Onboarding's migration lane, OB10 (the per-person-slots migration): the switch is deployed.

WHY THIS EXISTS. `REPOSITORY_GRANTS_ENFORCED` (swarm_api.settings
`repository_grants_enforced`) decides whether a PERSON's task on a repository
they never chose is refused (403 REPOSITORY_NOT_GRANTED) or quietly runs with
the tenant token. Until this lane it reached swarm-api only by hand: nothing in
terraform set it, so the next apply would have put it back to off, and the
tenant token would have gone on standing in for every unconnected person
without anyone seeing it. Here terraform renders it from a variable, and dev's
tfvars turns it on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LOCALS = REPO / "terraform" / "infra" / "locals.tf"
VARIABLES = REPO / "terraform" / "infra" / "variables.tf"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars.example"
PROD_TFVARS = REPO / "terraform" / "environments" / "prod" / "prod.tfvars.example"
SETTINGS = REPO / "apps" / "swarm-api" / "swarm_api" / "settings.py"

ENV_NAME = "REPOSITORY_GRANTS_ENFORCED"
VAR_NAME = "repository_grants_enforced"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _uncommented(path: Path) -> str:
    """The HCL with `#` comments removed, so a commented-out line never counts."""
    return "\n".join(line.split("#", 1)[0] for line in _text(path).splitlines())


def _tfvar(path: Path, name: str) -> str | None:
    match = re.search(rf"^\s*{name}\s*=\s*(\S+)\s*$", _uncommented(path), flags=re.M)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# (1) the switch goes through terraform
# --------------------------------------------------------------------------


def test_swarm_api_reads_the_name_terraform_sets():
    assert f'_bool("{ENV_NAME}", False)' in _text(SETTINGS), \
        "swarm_api.settings reads REPOSITORY_GRANTS_ENFORCED; the name below must be the same one"


def test_the_variable_is_a_bool_that_defaults_off():
    body = re.search(rf'^variable "{VAR_NAME}" \{{(.*?)^\}}', _text(VARIABLES), flags=re.M | re.S)
    assert body, f"terraform/infra/variables.tf declares var.{VAR_NAME}"
    assert re.search(r"^\s*type\s*=\s*bool\s*$", body.group(1), flags=re.M)
    assert re.search(r"^\s*default\s*=\s*false\s*$", body.group(1), flags=re.M), \
        "off unless an environment's tfvars turns it on, as settings.py's own default"
    assert "description" in body.group(1)


def test_locals_renders_the_switch_into_swarm_apis_environment_only():
    text = _uncommented(LOCALS)
    rendered = re.findall(rf"^\s*{ENV_NAME}\s*=\s*(.+?)\s*$", text, flags=re.M)
    assert rendered == [f'var.{VAR_NAME} ? "true" : "false"'], \
        f"one rendering of {ENV_NAME}, from var.{VAR_NAME}, as the strings settings._bool reads"
    start = text.index('"swarm-api" = merge(')
    end = text.index('"swarm-scheduler" = merge(')
    assert start < text.index(f"{ENV_NAME} =") < end, "it is in swarm-api's block of local.service_env"


def test_dev_turns_grant_enforcement_on_and_says_why():
    assert _tfvar(DEV_TFVARS, VAR_NAME) == "true"
    lines = _text(DEV_TFVARS).splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith(f"{VAR_NAME} "))
    comment = " ".join(lines[max(0, at - 15):at])
    assert "#780" in comment and "service submission" in comment, \
        "the comment says why: people refused without a grant, service submissions keep the tenant token"


def test_prod_does_not_turn_it_on_before_the_owner_says_so():
    # prod.tfvars registers no GitHub App, so no person there can connect and
    # choose a repository: on, it would refuse every person's task. It turns
    # on only by the owner's answer (questions.json of the OB10 lane).
    assert _tfvar(PROD_TFVARS, VAR_NAME) in (None, "false")


def test_the_settings_comment_points_at_the_tfvars():
    flat = _flat(_text(SETTINGS))
    assert f"var.{VAR_NAME}" in flat, "settings.py says where the deployed value comes from"
