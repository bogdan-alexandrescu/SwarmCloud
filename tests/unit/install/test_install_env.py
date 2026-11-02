"""`swarm install --env` takes only the names Terraform accepts (audit 4f S3).

terraform/infra's `environment` variable is validated against a fixed list,
and bootstrap's `spec_signing_environments` limits a name to 20 characters.
A name either refuses would pass stage 0 and fail at the registry pass, after
bootstrap pass 1 had made billable resources; the verb refuses it at once.
The allowed set is read from terraform/infra/variables.tf, so the two cannot
drift apart.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from swarm_install import cli
from swarm_install.exitcodes import ExitCode
from swarm_install.verbs import install

TERRAFORM = Path(__file__).resolve().parents[3] / "terraform"


def _infra_environments() -> set[str]:
    text = (TERRAFORM / "infra" / "variables.tf").read_text()
    block = re.search(r'variable\s+"environment"\s*\{(.*?)\n\}', text, re.S)
    assert block is not None, "terraform/infra/variables.tf declares no environment variable"
    allowed = re.search(r"contains\(\[([^\]]*)\],\s*var\.environment\)", block.group(1))
    assert allowed is not None, "the environment variable's validation is no longer a contains([...]) list"
    return set(re.findall(r'"([^"]+)"', allowed.group(1)))


def _spec_signing_rule() -> re.Pattern[str]:
    text = (TERRAFORM / "bootstrap" / "spec_signing.tf").read_text()
    rule = re.search(r'regex\("(\^[^"]+\$)", e\)', text)
    assert rule is not None, "terraform/bootstrap/spec_signing.tf no longer validates names with a regex"
    return re.compile(rule.group(1).replace("\\\\", "\\"))


def test_env_accepts_exactly_what_terraform_infra_accepts() -> None:
    assert set(install.ENVIRONMENTS) == _infra_environments()
    assert install.DEFAULT_ENV in install.ENVIRONMENTS
    rule = _spec_signing_rule()
    for name in install.ENVIRONMENTS:
        assert rule.match(name), f"{name} breaks bootstrap's spec_signing_environments rule"
        assert install._env(name) == name


@pytest.mark.parametrize("name", ["acme", "production", "a" * 21, "staging-2", "Prod"])
def test_env_refuses_a_name_terraform_refuses(name: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="dev, staging, prod"):
        install._env(name)


def test_the_command_line_refuses_it_before_anything_runs(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["install", "--project", "acme-swarm", "--env", "acme", "--dry-run"])
    assert raised.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "'acme' is not an environment this release installs: choose dev, staging, prod" in err


def test_help_names_the_environments(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["install", "--help"])
    assert "dev, staging, prod" in capsys.readouterr().out
