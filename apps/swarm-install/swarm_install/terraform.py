"""Terraform, driven by the engine (design §5; manifest `other.engine-terraform-rules`).

The engine runs the release's `terraform/bootstrap` and `terraform/infra`
roots itself, and every run goes through `run_pass`, which holds the rules the
history taught:

* **The pinned version, then `init`, then anything** (lesson 12: Homebrew
  terraform 1.3.6 refused the state; "Module not installed" without `init`).
* **No `-var`, ever** (lesson 11). Inputs are the generated
  `swarm-install.auto.tfvars.json` (`tfvars.py`), also kept in the state bucket.
* **An empty bootstrap state is refused while the deployer exists** (lesson 7:
  a fresh worktree planned "9 to import, 91 to add").
* **Every apply after bootstrap pass 1 runs as `swarm-tf-deployer`** (lessons
  22, 23): an owner's apply hides a missing `actAs`. Before such an apply the
  person's right to impersonate the deployer and the deployer's `actAs` on
  every runtime account that exists are checked.
* **Every plan is read before it is applied** (lessons 10, 11, 28): an IAM
  resource replaced destroy-first is refused outright (the gap in which CI had
  no `projectIamAdmin`); every IAM condition is linted against the limits that
  bit (`hasOnly()` at most 10 entries, no `matches()` on Cloud Storage, at most
  12 logical operators) and by `iamPolicies:lintPolicy`; a Cloud Run service
  whose traffic is not 100 % to the latest revision is refused; any other
  destroy needs the project id typed (never `SWARM_ASSUME_YES`).
* **Freshness is read from Terraform state, never from the registry** (lesson 9).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tfvars
from .api_client import deployer_email
from .state import ObjectStore
from .steps.preflight import TERRAFORM_VERSION

#: The env var the google provider and the gcs backend both read to
#: impersonate a service account.
IMPERSONATE_ENV = "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"
#: The passes that run as the person. Pass 1 creates the deployer. Pass 2
#: applies IAP membership (the accessor bindings and google_iap_settings),
#: which the deployer by design cannot: it holds no iap.admin, and IAP
#: membership is applied from bootstrap by the owner
#: (terraform/bootstrap/deployer_conditions.tf; N4, #2688).
AS_PERSON = ("bootstrap-1", "bootstrap-2")

HAS_ONLY_MAX = 10
LOGICAL_OPERATORS_MAX = 12
_IAM_TYPE = re.compile(r"^google_.*_iam_(member|binding|policy)$")
_HAS_ONLY = re.compile(r"hasOnly\(\s*\[([^\]]*)\]\s*\)")
_LATEST = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"


class TerraformRefused(RuntimeError):
    """A rule refused the run; nothing was applied."""


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: `(argv, env, cwd) -> Completed`: the one way a command runs (a fake in tests).
Exec = Callable[[Sequence[str], Mapping[str, str], Path], Completed]


def subprocess_exec(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Completed:
    done = subprocess.run(list(argv), env={**os.environ, **env}, cwd=cwd, capture_output=True, text=True,
                          check=False)
    return Completed(done.returncode, done.stdout, done.stderr)


def refuse_var(argv: Sequence[str]) -> None:
    for arg in argv:
        if arg == "-var" or arg.startswith("-var=") or arg.startswith("--var"):
            raise TerraformRefused(
                f"refusing {arg!r}: a -var value never reaches the next apply (lesson 11); "
                "every input goes into the generated tfvars"
            )


@dataclass
class Terraform:
    """One root (`bootstrap` or `infra`) of the release's module, in `root_dir`."""

    root: str
    root_dir: Path
    state_bucket: str
    environment: str = "dev"
    exec_: Exec = subprocess_exec
    binary: str = "terraform"
    version: str = TERRAFORM_VERSION
    initialized: bool = field(default=False, init=False)
    version_checked: bool = field(default=False, init=False)

    def run(self, *args: str, impersonate: str | None = None, check: bool = True) -> Completed:
        argv = [self.binary, *args]
        refuse_var(argv)
        env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        if impersonate:
            env[IMPERSONATE_ENV] = impersonate
        done = self.exec_(argv, env, self.root_dir)
        if check and done.returncode != 0:
            raise RuntimeError(f"terraform {args[0]} in {self.root} failed ({done.returncode}): "
                               f"{done.stderr.strip()[-2000:]}")
        return done

    def check_version(self) -> None:
        found = json.loads(self.run("version", "-json").stdout or "{}").get("terraform_version")
        if found != self.version:
            raise TerraformRefused(f"terraform {found} is not the pinned {self.version} (lesson 12: an older "
                                   f"terraform refuses the state); install {self.version}")
        self.version_checked = True

    def init(self, *, impersonate: str | None = None) -> None:
        if not self.version_checked:
            self.check_version()
        args = ["init", "-input=false", "-reconfigure", f"-backend-config=bucket={self.state_bucket}"]
        if self.root == "infra":
            args.append(f"-backend-config=prefix=infra/{self.environment}")
        self.run(*args, impersonate=impersonate)
        self.initialized = True

    def _initialized(self) -> None:
        if not self.initialized:
            raise TerraformRefused(f"{self.root}: init first (lesson 12: 'Module not installed')")

    def state_list(self, *, impersonate: str | None = None) -> list[str]:
        self._initialized()
        done = self.run("state", "list", impersonate=impersonate, check=False)
        if done.returncode != 0 and "No state file was found" not in done.stderr:
            raise RuntimeError(f"terraform state list in {self.root} failed: {done.stderr.strip()[-1000:]}")
        return [line for line in done.stdout.splitlines() if line.strip()]

    def plan(self, *, targets: Sequence[str] = (), impersonate: str | None = None) -> dict[str, Any]:
        self._initialized()
        args = ["plan", "-input=false", "-out=swarm-install.tfplan"]
        args.extend(f"-target={t}" for t in targets)
        self.run(*args, impersonate=impersonate)
        return json.loads(self.run("show", "-json", "swarm-install.tfplan", impersonate=impersonate).stdout)

    def apply(self, *, impersonate: str | None = None) -> None:
        self._initialized()
        self.run("apply", "-input=false", "swarm-install.tfplan", impersonate=impersonate)

    def outputs(self, *, impersonate: str | None = None) -> dict[str, Any]:
        found = json.loads(self.run("output", "-json", impersonate=impersonate).stdout or "{}")
        return {name: item.get("value") for name, item in found.items()}


def read_outputs(tf: Terraform, names: Sequence[str] = (), *, impersonate: str | None = None) -> dict[str, Any]:
    """A root's outputs outside a pass (stage 10 reads infra's manifests),
    `init` first. Terraform keeps no state for an output that is null, so a
    name in `names` it does not list is read as None."""
    if not tf.initialized:
        tf.init(impersonate=impersonate)
    found = tf.outputs(impersonate=impersonate)
    return {name: found.get(name) for name in names} if names else found


def is_fresh(tf: Terraform, *, impersonate: str | None = None) -> bool:
    """A root nothing was applied to yet: its state is empty (lesson 9)."""
    return not tf.state_list(impersonate=impersonate)


# -- reading a plan -------------------------------------------------------------


def lint_condition(expression: str, resource_type: str = "") -> list[str]:
    """The IAM-condition limits that bit (lesson 10)."""
    problems = []
    for found in _HAS_ONLY.finditer(expression):
        entries = [e for e in found.group(1).split(",") if e.strip()]
        if len(entries) > HAS_ONLY_MAX:
            problems.append(f"hasOnly() holds {len(entries)} entries; at most {HAS_ONLY_MAX}")
    operators = len(re.findall(r"&&|\|\|", expression)) + len(re.findall(r"(?<![=!])!(?!=)", expression))
    if operators > LOGICAL_OPERATORS_MAX:
        problems.append(f"{operators} logical operators; at most {LOGICAL_OPERATORS_MAX}")
    if "storage" in resource_type and "matches(" in expression:
        problems.append("Cloud Storage conditions cannot use matches()")
    return problems


def _conditions(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "condition":
                for cond in item if isinstance(item, list) else [item]:
                    if isinstance(cond, Mapping) and cond.get("expression"):
                        found.append(str(cond["expression"]))
            else:
                found.extend(_conditions(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_conditions(item))
    return found


def traffic_problems(change: Mapping[str, Any]) -> list[str]:
    """A Cloud Run service must route 100 % to its latest ready revision (lesson 28)."""
    if not str(change.get("type", "")).startswith("google_cloud_run_v2_service"):
        return []
    after = (change.get("change") or {}).get("after") or {}
    traffic = after.get("traffic")
    if not traffic:
        return []
    if len(traffic) == 1 and traffic[0].get("type") == _LATEST and int(traffic[0].get("percent") or 0) == 100:
        return []
    return [f"{change.get('address')}: traffic {traffic} is not 100 % to the latest revision"]


@dataclass
class Review:
    destroys: list[str] = field(default_factory=list)
    iam_destroy_first: list[str] = field(default_factory=list)
    conditions: list[tuple[str, str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def review_plan(plan: Mapping[str, Any]) -> Review:
    review = Review()
    for change in plan.get("resource_changes") or ():
        address = str(change.get("address"))
        rtype = str(change.get("type", ""))
        actions = list((change.get("change") or {}).get("actions") or ())
        if "delete" in actions:
            if _IAM_TYPE.match(rtype) and actions == ["delete", "create"]:
                review.iam_destroy_first.append(address)
            else:
                review.destroys.append(address)
        after = (change.get("change") or {}).get("after")
        if after:
            for expression in _conditions(after):
                review.conditions.append((address, rtype, expression))
                review.problems.extend(f"{address}: {p}" for p in lint_condition(expression, rtype))
        review.problems.extend(traffic_problems(change))
    return review


# -- one pass ---------------------------------------------------------------------


@dataclass
class Guards:
    """What a pass asks Google before it runs (each a fake in tests).

    * `deployer_exists()`: the deployer service account exists;
    * `can_impersonate()`: the person may mint the deployer's tokens;
    * `act_as_missing()`: runtime accounts that exist and the deployer may not act as;
    * `lint(expression)`: problems `iamPolicies:lintPolicy` reports.
    """

    deployer_exists: Callable[[], bool]
    can_impersonate: Callable[[], bool]
    act_as_missing: Callable[[], list[str]]
    lint: Callable[[str], list[str]] = lambda expression: []


def run_pass(
    tf: Terraform,
    pass_name: str,
    inputs: Mapping[str, Any],
    *,
    project: str,
    bucket: ObjectStore,
    guards: Guards,
    confirm: Callable[[str], str],
    targets: Sequence[str] = (),
    announce: Callable[[str], None] = lambda line: None,
) -> dict[str, Any]:
    """Run one of the install's passes, with every rule above; its outputs."""
    if tfvars.ROOT_OF[pass_name] != tf.root:
        raise ValueError(f"{pass_name} is not a pass of {tf.root}")
    deployer = None if pass_name in AS_PERSON else deployer_email(project)
    tf.check_version()
    tfvars.store(bucket, tf.root, inputs)
    tfvars.write_local(tf.root_dir, inputs)
    if deployer is not None:
        if not guards.can_impersonate():
            raise TerraformRefused(
                f"{pass_name} runs as {deployer} (lessons 22, 23) and you may not impersonate it: grant "
                "roles/iam.serviceAccountTokenCreator on it (bootstrap pass 1 does), then run swarm install again"
            )
        missing = guards.act_as_missing()
        if missing:
            raise TerraformRefused(f"{deployer} may not act as {', '.join(missing)} (lesson 23: the first release "
                                   "past IAP failed on it); bootstrap grants it, so run bootstrap first")
    tf.init(impersonate=deployer)
    if pass_name == "bootstrap-1" and not tf.state_list() and guards.deployer_exists():
        raise TerraformRefused(
            f"the bootstrap state in gs://{tf.state_bucket}/bootstrap is empty, but {deployer_email(project)} "
            "exists: this deployment was bootstrapped from state that is not here (lesson 7). Restore the state "
            "object's earlier version; an empty state would plan to recreate everything"
        )
    plan = tf.plan(targets=targets, impersonate=deployer)
    review = review_plan(plan)
    if review.iam_destroy_first:
        raise TerraformRefused(
            f"{pass_name} would replace {', '.join(review.iam_destroy_first)} destroy-first; an IAM grant is "
            "created before the old one is destroyed (lesson 10: create_before_destroy)"
        )
    problems = list(review.problems)
    for address, _, expression in review.conditions:
        problems.extend(f"{address}: {p}" for p in guards.lint(expression))
    if problems:
        raise TerraformRefused(f"{pass_name}: " + "; ".join(problems))
    if review.destroys:
        prompt = (f"{pass_name} would DESTROY {len(review.destroys)}: {', '.join(review.destroys)}. "
                  f"Type the project id ({project}) to approve, anything else refuses: ")
        if confirm(prompt).strip() != project:
            raise TerraformRefused(f"{pass_name}: the destroys were not approved; nothing was applied")
    announce(f"terraform apply: {pass_name} ({tf.root})" + (f" as {deployer}" if deployer else ""))
    tf.apply(impersonate=deployer)
    return tf.outputs(impersonate=deployer)
