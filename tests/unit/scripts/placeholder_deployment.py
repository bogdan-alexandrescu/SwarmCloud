"""The placeholder deployment a test runs a script against: one helper for every test directory.

scripts/lib/common.sh has no default project, region or zone
(docs/design/generic-repository.md, section 2.2), and once a project is named
it reads that project's never-touch list from config/<env>/deny-list.json and
stops when it cannot (section 2.4). A test that runs a script, or sources
common.sh, therefore needs a deployment. This module gives it the placeholder
one: REGION and ZONE from scripts/guards/placeholders.json (through
tests/placeholders.py), and a never-touch list served the way config_fetch
reads one offline -- a SWARM_DEPLOY_CONFIG_DIR laid out as
<dir>/<env>/deny-list.json, owned by this user and not group- or
world-writable, with a cache of its own. <env> is dev, prod and whatever
environment the test sets (ENVIRONMENT in this process, in a child's env or in
its SWARM_ENV_FILE; CI's integration job sets `ci`), each with the same list.

The list is scripts/lib/deny-list.example.json, written for the project the
child process names: its entries, floors and reviewer are placeholders, so no
test reads, or falls back to, a real deployment's list. A test's fake project
(PROJECT_ID set with monkeypatch or in a subprocess env) gets the same list
under a directory of its own, because one SWARM_DEPLOY_CONFIG_DIR serves one
project: `serve` rewrites the child's SWARM_DEPLOY_CONFIG_DIR to that
project's directory, only while it still points at this deployment's, so a
test that names a directory of its own, or clears the variable, is left alone.

A test that proves a missing, foreign or truncated list refuses opts out with
`pytest.mark.no_placeholder_deployment`: it sees none of these variables and
no list is served for it.

Loaded by path, once per process, under one module name (`MODULE`), through
tests/placeholders.py's `deployment()`, so tests/unit/scripts/conftest.py,
tests/integration/conftest.py and a test file elsewhere share one deployment's
state; `tests/` is not a package. A test file outside those two directories
activates it itself, with an autouse fixture whose body is `activate`, and a test
that builds its child's environment from scratch adds `child_env(project)`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_DENY_LIST = REPO / "scripts" / "lib" / "deny-list.example.json"
MODULE = "swarm_tests_placeholder_deployment"
MARKER = "no_placeholder_deployment"
MARKER_HELP = (
    f"{MARKER}: the test sees no placeholder PROJECT_ID, REGION, ZONE or never-touch list "
    "(it proves what common.sh does without them)"
)
#: The variables this deployment sets; an opted-out test sees none of them.
NAMES = ("PROJECT_ID", "REGION", "ZONE", "SWARM_ORG_DOMAIN", "SWARM_DEPLOY_CONFIG_DIR", "XDG_CACHE_HOME",
         "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL")
#: The environments the repository's tfvars examples name; a child's own gets its list too.
ENVIRONMENTS = ("dev", "prod")


def _placeholders():
    spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_P = _placeholders()
PROJECT: str = _P.PROJECT
SHARED_PROJECT: str = _P.SHARED_PROJECT
REGION: str = _P.REGION
ZONE: str = _P.ZONE
#: The placeholder organisation's domain: kubernetes/label_prefix.py's default
#: prefix, swarm.<domain>, which every render of a key needs (the owner's
#: "configurable", 8 November 2026).
ORG_DOMAIN: str = _P.ORG_DOMAIN
#: The placeholder list's entries by kind (cluster, network, subnet, bucket, service_account):
#: what a test that proves a never-touch refusal names, never a real resource.
PEER: dict[str, str] = dict(_P.PEER)
#: The deployment's private acceptance sandbox: configuration with no default in
#: scripts/acceptance/config.sh (Terraform renders it on the swarm-verify job), so
#: a child gets the placeholder repository's clone URL, never a real sandbox.
SANDBOX_REPOSITORY_URL: str = f"https://github.com/{_P.GITHUB_REPOSITORY}.git"


def environments(*named: str | None) -> list[str]:
    """ENVIRONMENTS and each NAMED environment that can be a directory name: a test's
    own (ENVIRONMENT, as CI's integration job sets `ci`) gets the placeholder list too,
    and a name that cannot be a directory is left to refuse."""
    found = set(ENVIRONMENTS)
    for env in named:
        if env and "/" not in env and not env.startswith("."):
            found.add(env)
    return sorted(found)


def write_deny_list(config_dir: Path, project: str, envs: tuple[str, ...] | list[str] = ENVIRONMENTS) -> None:
    """The example list, written for PROJECT, under CONFIG_DIR/<env>/ for each env."""
    body = json.loads(EXAMPLE_DENY_LIST.read_text())
    body["project"] = project
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    for env in envs:
        target = config_dir / env
        target.mkdir(exist_ok=True)
        target.chmod(0o700)
        path = target / "deny-list.json"
        if path.exists():
            continue
        path.write_text(json.dumps(body, indent=2) + "\n")
        path.chmod(0o600)


#: The placeholder settings a script reads from a deployment's <env>.tfvars
#: (deployment_setting and deployment_list in scripts/lib/common.sh): its allowed
#: domain, which gives the label prefix swarm.<domain>, and its platform owner.
TFVARS: dict[str, str | list[str]] = {"allowed_domains": [_P.ORG_DOMAIN], "platform_owner": _P.OWNER}


def write_tfvars(config_dir: Path, envs: tuple[str, ...] | list[str] = ENVIRONMENTS,
                 settings: Mapping[str, str | list[str]] | None = None) -> None:
    """The placeholder <env>.tfvars (TFVARS, or SETTINGS), under CONFIG_DIR/<env>/ for each
    env, the way config_fetch reads one offline. Only for a test whose script reads one:
    the package deployment serves none, so a script that needs one and finds none is proved
    to stop."""
    values = TFVARS if settings is None else settings
    lines = [f"{name} = {json.dumps(value)}" for name, value in values.items()]
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    for env in envs:
        target = config_dir / env
        target.mkdir(exist_ok=True)
        target.chmod(0o700)
        path = target / f"{env}.tfvars"
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)


class PlaceholderDeployment:
    """The placeholder deployment rooted at ROOT (a private temporary directory)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.chmod(0o700)
        (root / "cache").mkdir(mode=0o700, exist_ok=True)
        self.config_dir = self.config_dir_for(PROJECT)
        # The environment this process names, too: a child that inherits it before
        # any child is served (a test file's import-time probe) finds its list.
        write_deny_list(self.config_dir, PROJECT, environments(os.environ.get("ENVIRONMENT")))

    def config_dir_for(self, project: str) -> Path:
        return self.root / "projects" / project / "config"

    def env(self) -> dict[str, str]:
        """The variables a child needs: the placeholder project, region, zone, organisation domain, sandbox and list."""
        return {
            "PROJECT_ID": PROJECT,
            "REGION": REGION,
            "ZONE": ZONE,
            "SWARM_ORG_DOMAIN": ORG_DOMAIN,
            "SWARM_DEPLOY_CONFIG_DIR": str(self.config_dir),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL": SANDBOX_REPOSITORY_URL,
        }

    def child_env(self, project: str = PROJECT, environment: str = "dev") -> dict[str, str]:
        """For a test that builds its child's environment from scratch: the placeholder
        region and zone, the placeholder sandbox, and the placeholder list served for
        PROJECT in ENVIRONMENT."""
        config_dir = self.config_dir_for(project)
        write_deny_list(config_dir, project, environments(environment, os.environ.get("ENVIRONMENT")))
        return {
            "REGION": REGION,
            "ZONE": ZONE,
            "SWARM_ORG_DOMAIN": ORG_DOMAIN,
            "SWARM_DEPLOY_CONFIG_DIR": str(config_dir),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL": SANDBOX_REPOSITORY_URL,
        }

    def serve(self, env: Mapping[str, str]) -> dict[str, str] | None:
        """ENV with a list served for the project it names, or None when it needs none.

        The project and environment are the ones common.sh will see: its env file's
        (SWARM_ENV_FILE, which load_env sources over the environment) when that names
        them, else ENV's. Only a child whose SWARM_DEPLOY_CONFIG_DIR is still this
        deployment's is served; a project id that cannot be a directory name is left
        to refuse.
        """
        if env.get("SWARM_DEPLOY_CONFIG_DIR") != str(self.config_dir):
            return None
        from_file = _env_file_settings(env.get("SWARM_ENV_FILE"))
        project = from_file.get("PROJECT_ID") or env.get("PROJECT_ID") or ""
        environment = from_file.get("ENVIRONMENT") or env.get("ENVIRONMENT") or ""
        if not project or project == PROJECT or "/" in project or project.startswith("."):
            if project == PROJECT:
                write_deny_list(self.config_dir, PROJECT, environments(environment))
            return None
        config_dir = self.config_dir_for(project)
        write_deny_list(config_dir, project, environments(environment))
        served = dict(env)
        served["SWARM_DEPLOY_CONFIG_DIR"] = str(config_dir)
        return served

    @contextmanager
    def active(self, mp: pytest.MonkeyPatch) -> Iterator[None]:
        """Set this deployment's variables and serve every child process started meanwhile."""
        for name, value in self.env().items():
            mp.setenv(name, value)
        _install()
        _SERVING.append(self)
        try:
            yield
        finally:
            _SERVING.remove(self)


def _env_file_settings(path: str | None) -> dict[str, str]:
    """PROJECT_ID and ENVIRONMENT as plain NAME=value lines of the env file at PATH."""
    found: dict[str, str] = {}
    if not path:
        return found
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return found
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if sep and name in ("PROJECT_ID", "ENVIRONMENT"):
            found[name] = value.strip().strip("'\"")
    return found


#: The deployment whose children are served now (the last activated); None while none is,
#: or while a test opts out.
_SERVING: list[PlaceholderDeployment | None] = [None]
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__


def _serving_popen_init(popen, args, *rest, **kwargs):
    deployment = _SERVING[-1]
    # env is Popen's eleventh positional parameter; every caller here passes it by name.
    if deployment is not None and len(rest) < 10:
        env = kwargs.get("env")
        served = deployment.serve(os.environ if env is None else env)
        if served is not None:
            kwargs["env"] = served
    _ORIGINAL_POPEN_INIT(popen, args, *rest, **kwargs)


def _install() -> None:
    """Route Popen through the deployment, once per process; it does nothing while none serves."""
    if subprocess.Popen.__init__ is not _serving_popen_init:
        subprocess.Popen.__init__ = _serving_popen_init


def current() -> PlaceholderDeployment:
    """The deployment serving this test; a test outside the wired directories activates
    one first (`activate`)."""
    deployment = _SERVING[-1]
    if deployment is None:
        raise RuntimeError("no placeholder deployment is active for this test")
    return deployment


def child_env(project: str = PROJECT, environment: str = "dev") -> dict[str, str]:
    """`PlaceholderDeployment.child_env` of the deployment serving this test."""
    return current().child_env(project, environment)


def activate(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """The body of an autouse fixture: a placeholder deployment for its scope.

        @pytest.fixture(scope="module", autouse=True)
        def _placeholder_deployment(tmp_path_factory):
            yield from placeholder_deployment.activate(tmp_path_factory)
    """
    deployment = PlaceholderDeployment(tmp_path_factory.mktemp("placeholder-deployment"))
    with pytest.MonkeyPatch.context() as mp, deployment.active(mp):
        yield


@contextmanager
def opted_out(mp: pytest.MonkeyPatch) -> Iterator[None]:
    """No placeholder variables and no served list, for one test."""
    for name in NAMES:
        mp.delenv(name, raising=False)
    _SERVING.append(None)
    try:
        yield
    finally:
        _SERVING.pop()
