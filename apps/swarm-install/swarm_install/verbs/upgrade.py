"""`swarm upgrade --to V`: move a deployment to release V (docs/design/day-one-plan.md
§9.3; lane DAYONE-UPGRADE-1, part of #2688; `swarm_install.upgrade` holds the
order and says why each step exists).

    swarm upgrade [--to V] --project P [--env prod] [--allow-downgrade] [--json-events]

With no `--to`, the release is the one the deployment's channel names now
(`channels/<channel>`: rc for a deployment running a release candidate, else
stable); its RELEASE.md is shown with the plan, and the plan waits for the
typed project id.

**V's own engine runs the upgrade** (audit 4e F3). V's Firestore migrations
and its Terraform inputs live in V's wheel, not in the one installed here: when
this CLI is not release V, the upgrade asks once (the project id typed), fetches
V's four wheels, each checked against V's SHA256SUMS, which cosign verifies with
the pinned release key, installs them as the `swarm` uv tool exactly as
install.sh does, and runs `swarm upgrade --to V` again from V's engine.

**The GitHub App** is read as the App (audit 4e F4): a private App answers
`GET /apps/{slug}` only with a token, so the engine signs an App JWT with
`swarm-github-app-private-key`, which it reads as the owner, held in memory
only, and reads `GET /app` and the installation.

The real `UpgradeWorld` is built here from the install's own steps, so an
upgrade applies exactly what the install applies: the release's images through
`images.copy`, the Terraform passes through `apply_pass` (the deployer, the
plan review, the typed destroy approval), the policies through
`cluster.policies`' render and the install cluster's context only, the verify
keys through `team.ensure_verify_keys`, and the checks through stage 14's
quick checks. Dispatch is paused and resumed as the person (a platform admin
signed in with `swarm login`) through `POST /v1/admin/dispatch/{pause,resume}`.

Exit: 0 upgraded (or nothing to do); 1 a step failed (the output names it and
`swarm upgrade --to V` resumes there); 4 the drain passed its bound; 5 refused
(a downgrade without `--allow-downgrade`, unapproved IAM changes, a plan that
cannot be applied safely, V's engine not approved).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from ..engine import Context, Engine
from ..events import Events
from ..exitcodes import MET, UNKNOWN, ExitCode
from ..lease import Lease, LeaseHeld, LeaseRefused
from ..migrations import FirestoreMigrationDocuments, MigrationFailed
from ..record import DATABASE
from ..state import GcsObjectStore, StateStore, local_path, state_bucket
from ..tfvars import APP_SECRETS
from ..upgrade import AppInstallation, GithubApp, Upgrade, UpgradeFailed, UpgradeRefused, version_key
from ..waits import SystemClock, WaitGaveUp

NAME = "upgrade"
HELP = "upgrade the deployment to release V: plan, approve IAM changes, apply in order, check, roll back on failure"

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_VERSION = re.compile(r"^v?\d+(\.\d+)*(-[0-9A-Za-z.-]+)?$")
_ENV = re.compile(r"^[a-z][a-z0-9-]{0,29}$")

#: Stage 14's quick checks: what `swarm doctor` runs after a release.
CHECKS = ("verify.scheduler-targets", "verify.digests", "verify.reconciler-jobs", "verify.firestore-migrations")
#: Bootstrap's latest install pass: an upgrade applies the widest inputs. Infra's
#: is the latest pass the install reached (`steps.infra.latest_pass`; N12).
LATEST_BOOTSTRAP_PASS = "bootstrap-2"
#: Task states that hold infrastructure (invariant 1; `swarm_common.states.CONCURRENCY_STATES`).
DEMAND_STATES = ("LEASED", "DISPATCHED", "STARTING", "RUNNING")
#: What a release may ship to name the GitHub App permissions it needs.
APP_PERMISSIONS_FILE = "github-app-permissions.json"
LOGGING = "https://logging.googleapis.com/v2"
GITHUB_API = "https://api.github.com"
#: The secret holding the App's private key (the first of `tfvars.APP_SECRETS`,
#: which Admin › Setup writes); the engine reads it as the owner.
APP_KEY_SECRET = APP_SECRETS[0]
#: Set on the re-exec into V's engine: the release it was installed as, so a
#: CLI that still is not V refuses rather than installing again.
ENGINE_ENV = "SWARM_UPGRADE_ENGINE"
#: Set on the re-exec when the person did not name the release: V's engine
#: waits for the typed project id on the plan even with no IAM change.
CONFIRM_PLAN_ENV = "SWARM_UPGRADE_CONFIRM_PLAN"
#: The wheels install.sh installs, in its order: swarm-mcp is the tool, the others ride with it.
WHEELS = ("swarm_mcp", "swarm_install", "swarm_common", "swarm_redaction")
INSTALL_LINE = "curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARMCLOUD_VERSION={version} sh"


def _check(pattern: re.Pattern[str], what: str) -> Any:
    def parse(value: str) -> str:
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"{value!r} is not {what}")
        return value

    return parse


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", default=None, type=_check(_VERSION, "a release version"),
                        help="the release to upgrade to, e.g. 2026.11.1 (default: the release the deployment's "
                             "channel names now, shown with its notes before the typed approval)")
    parser.add_argument("--project", required=True, type=_check(_PROJECT_ID, "a Google Cloud project id"),
                        help="the install's Google Cloud project")
    parser.add_argument("--env", default="prod", type=_check(_ENV, "an environment name"),
                        help="the install's environment (default prod)")
    parser.add_argument("--allow-downgrade", action="store_true", help="allow a release older than the running one")
    parser.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    parser.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")


def exit_code_for(exc: BaseException) -> ExitCode:
    if isinstance(exc, UpgradeRefused):
        return ExitCode.REFUSED
    if isinstance(exc, WaitGaveUp):
        return ExitCode.PAUSED
    if isinstance(exc, (UpgradeFailed, MigrationFailed)):
        return ExitCode.FAIL
    return ExitCode.TROUBLE


# -- the GitHub App as itself ---------------------------------------------------------


def app_jwt(app_id: str, private_key_pem: str, now: float) -> str:
    """A GitHub App JWT: RS256, `iss` the App id, `iat` a minute back (clock
    skew), `exp` nine minutes on, signed with google-auth's RSA signer (already
    this wheel's), so no JWT library is added. A key that cannot sign is said
    in constant words: a parser error may quote the key."""
    from google.auth import crypt, jwt

    if not app_id:
        raise UpgradeFailed("the install records no GitHub App id to read the App as itself")
    try:
        signer = crypt.RSASigner.from_string(private_key_pem)
        token = jwt.encode(signer, {"iat": int(now) - 60, "exp": int(now) + 9 * 60, "iss": app_id})
    except Exception:  # noqa: BLE001 -- constant text, never the key or the parser's view of it
        raise UpgradeFailed(f"{APP_KEY_SECRET} is not an RSA private key the App JWT can be signed with") from None
    return token.decode("ascii") if isinstance(token, bytes) else str(token)


# -- which release, and whose engine ------------------------------------------------


def channel_release(read_public: Any, running: str | None) -> str:
    """The release the deployment's channel names now: rc for a deployment on
    a release candidate, else stable (`install.channel_of`)."""
    from ..steps.preflight import RELEASE_HOME
    from .install import channel_of

    channel = channel_of(running)
    found = read_public(f"{RELEASE_HOME}/channels/{channel}").decode().strip()
    if not _VERSION.match(found):
        raise UpgradeRefused(f"channels/{channel} names {found[:60]!r}, which is not a release")
    return found


def wheel_version(release: str) -> str:
    """install.sh's mapping of a release name to its wheels' PEP 440 version:
    2026.01.2 is 2026.1.2, 2026.11.3-rc.2 is 2026.11.3rc2."""
    main, _, candidate = release.lstrip("v").partition("-rc.")
    year, month, number = main.split(".")
    version = f"{year}.{int(month)}.{number}"
    return f"{version}rc{candidate}" if candidate else version


def fetch_wheels(release: Any, version: str, where: Path) -> list[Path]:
    """V's four wheels in install.sh's order, each checked against V's
    SHA256SUMS, which `release.sums` verifies with the pinned key first."""
    from ..steps.preflight import RELEASE_HOME

    sums = release.sums(version, where)
    paths = []
    for dist in WHEELS:
        name = f"{dist}-{wheel_version(version)}-py3-none-any.whl"
        if name not in sums:
            raise UpgradeRefused(f"release {version}'s SHA256SUMS names no {name}; nothing was installed")
        data = release.read_public(f"{RELEASE_HOME}/releases/{version}/{name}")
        if hashlib.sha256(data).hexdigest() != sums[name]:
            raise UpgradeRefused(f"release {version}: {name} does not match its signed SHA256SUMS; nothing was "
                                 "installed")
        path = where / name
        path.write_bytes(data)
        paths.append(path)
    return paths


def install_engine(uv: str, wheels: Sequence[Path], run_: Any = subprocess.run) -> Path:
    """The wheels as the `swarm` uv tool, as install.sh installs them; the new `swarm`'s path."""
    mcp, install, common, redaction = wheels
    done = run_([uv, "tool", "install", "--force", "--python", ">=3.11", f"swarm-mcp @ file://{mcp}",
                 "--with", f"swarm-install @ file://{install}", "--with", f"swarm-common @ file://{common}",
                 "--with", f"swarm-redaction @ file://{redaction}"], capture_output=True, text=True)
    if done.returncode != 0:
        raise UpgradeFailed(f"uv tool install failed: {str(done.stderr).strip()[-500:]}")
    found = run_([uv, "tool", "dir", "--bin"], capture_output=True, text=True)
    swarm = Path(str(found.stdout).strip() or str(Path.home() / ".local" / "bin")) / "swarm"
    if found.returncode != 0 or not swarm.exists():
        raise UpgradeFailed(f"uv installed the release's CLI, yet {swarm} is not there")
    return swarm


def reexec_argv(swarm: Path, target: str, args: argparse.Namespace) -> list[str]:
    argv = [str(swarm), "upgrade", "--to", target, "--project", args.project, "--env", args.env]
    if args.allow_downgrade:
        argv.append("--allow-downgrade")
    if args.context:
        argv += ["--context", args.context]
    if args.json_events:
        argv.append("--json-events")
    return argv


def ensure_engine(target: str, args: argparse.Namespace, *, installed: str | None, release: Any,
                  confirm: Any, confirm_plan: bool, environ: Mapping[str, str] = os.environ,
                  which: Any = shutil.which, run_: Any = subprocess.run, execve: Any = os.execve,
                  say: Any = print) -> None:
    """Return when this engine is release `target`'s; else, after the typed
    project id, install `target`'s CLI and run `swarm upgrade` from it (this
    process is replaced). Refused (exit 5) without the approval."""
    from ..steps.bootstrap import workdir

    if installed == target:
        return
    if installed is None:
        say(f"swarm upgrade: this engine is a source checkout, not a release; its own migrations and Terraform "
            f"inputs are used for {target}")
        return
    if environ.get(ENGINE_ENV) == target:
        raise UpgradeRefused(f"release {target}'s CLI was installed for this upgrade, yet the engine that ran is "
                             f"{installed}; nothing was applied")
    install_line = INSTALL_LINE.format(version=target)
    prompt = (f"This swarm CLI is release {installed}; release {target}'s Firestore migrations and Terraform inputs "
              f"are in {target}'s own engine. Type the project id ({args.project}) to install {target}'s swarm CLI "
              f"(its four wheels, each checked against {target}'s signed SHA256SUMS) and continue the upgrade with "
              "it, anything else refuses: ")
    if confirm(prompt).strip() != args.project:
        raise UpgradeRefused(f"the upgrade to {target} runs {target}'s engine, and this CLI is {installed}; nothing "
                             f"was applied. Approve installing it, or install it with `{install_line}`, then run "
                             f"swarm upgrade --to {target}")
    uv = which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        raise UpgradeRefused(f"uv is not on PATH to install {target}'s CLI; nothing was applied. Install it with "
                             f"`{install_line}`, then run swarm upgrade --to {target}")
    where = workdir(args.project, target) / "engine"
    where.mkdir(parents=True, exist_ok=True)
    swarm = install_engine(uv, fetch_wheels(release, target, where), run_)
    say(f"swarm upgrade: installed release {target}'s swarm CLI; the upgrade goes on with its engine")
    env = dict(environ)
    env[ENGINE_ENV] = target
    if confirm_plan:
        env[CONFIRM_PLAN_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        execve(str(swarm), reexec_argv(swarm, target, args), env)
    except OSError as exc:
        raise UpgradeFailed(f"cannot run {swarm}: {exc}") from None


def release_notes(fetch: Any, target: str) -> str:
    """Release `target`'s RELEASE.md (checked against its signed SHA256SUMS), or a line saying it could not be read."""
    try:
        return bytes(fetch(target, "RELEASE.md")).decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001 -- the notes are informative; the plan below is what is approved
        return f"(release {target}'s RELEASE.md could not be read: {type(exc).__name__})"


def nothing_to_do(state: Any, target: str) -> bool:
    """The deployment runs `target` and no upgrade to it is part-way."""
    progress = dict(state.outputs.get("upgrade") or {})
    resuming = progress.get("to") == target and progress.get("finished") != target
    return not resuming and str(state.release or "") == target


class LiveWorld:
    """`UpgradeWorld` on the real project, through the install's own steps."""

    def __init__(self, project: str, environment: str, store: StateStore, engine: Engine, events: Events,
                 clock: SystemClock, holder: str, context: str | None) -> None:
        from ..steps import discover as discover_steps
        from ..steps.bootstrap import DEPS

        self.project = project
        self.environment = environment
        self.store = store
        self.engine = engine
        self.events = events
        self.clock = clock
        self.holder = holder
        self.deps = DEPS
        self.steps = {step.id: step for step in discover_steps()}
        self._documents = FirestoreMigrationDocuments(project, database=DATABASE)
        self._context = context
        self._api: Any = None
        self._key: str | None = None
        self._installation_id: int | None = None

    def ctx(self, release: str | None = None) -> Context:
        state = self.store.load()
        if release is not None:
            state.release = release
        ctx = Context(project=self.project, environment=self.environment, holder=self.holder, state=state,
                      events=self.events, clock=self.clock, dry_run=False, engine=self.engine)
        self.engine.ctx = ctx
        return ctx

    def _release(self) -> Any:
        from ..steps.bootstrap import Release

        if self.deps.release is None:
            self.deps.release = Release(read_public=self.deps.gcp().read_public, exec_=self.deps.exec_)
        return self.deps.release

    # -- release files and Terraform ---------------------------------------------

    def fetch(self, version: str, name: str) -> bytes:
        from ..steps.bootstrap import workdir

        return self._release().fetch(version, name, workdir(self.project, version))

    def read_public(self, url: str) -> bytes:
        return bytes(self._release().read_public(url))

    def _terraform(self, ctx: Context, root: str) -> Any:
        from ..terraform import Terraform

        return Terraform(root=root, root_dir=self.deps.module_root(ctx, root), state_bucket=state_bucket(self.project),
                         environment=self.environment, exec_=self.deps.exec_)

    def plan_root(self, root: str, version: str) -> dict[str, Any]:
        from .. import tfvars
        from ..api_client import deployer_email
        from ..steps.bootstrap import generate

        ctx = self.ctx(version)
        inputs = generate(ctx, self._pass_for(ctx, root), self.deps.gcp())
        tf = self._terraform(ctx, root)
        tfvars.write_local(tf.root_dir, inputs)
        deployer = deployer_email(self.project)
        tf.init(impersonate=deployer)
        return tf.plan(impersonate=deployer)

    @staticmethod
    def _pass_for(ctx: Context, root: str, *, reopening: bool = False) -> str:
        """The pass an upgrade applies to `root`: never infra-2 once infra-3 or
        later has applied, whose inputs switch the GitHub App off (N12)."""
        from ..steps.infra import latest_pass, reach_closed

        if root == "bootstrap":
            return LATEST_BOOTSTRAP_PASS
        return latest_pass(ctx.outputs, reopening=reopening or reach_closed(ctx.outputs))

    def apply_root(self, root: str, version: str) -> None:
        from ..steps.bootstrap import apply_pass
        from ..steps.infra import REACH, REOPENED, reach_closed
        from ..steps.preflight import remember

        ctx = self.ctx(version)
        # After the closing pass, infra's re-apply is infra-4's inputs, which carry
        # the installer's address (decision 2): refresh it to this machine's /32 (it
        # may not be the network recorded at install) and record it so the
        # upgrade's close removes it again (finding 16).
        reopens = root == "infra" and reach_closed(ctx.outputs)
        if reopens:
            REACH.address(ctx)
        apply_pass(ctx, self.deps, self._pass_for(ctx, root, reopening=reopens))
        if reopens:
            remember(ctx, outputs={REOPENED: str(ctx.outputs.get("installer_cidr") or "")})

    def close_reach(self) -> str | None:
        """The closing pass again when this upgrade reopened the control plane; the line to print if it failed."""
        from ..steps.infra import REACH

        return REACH.close_after(self.ctx(), "swarm upgrade")

    def patch_log_metric(self, address: str, after: Mapping[str, Any]) -> None:
        name = str(after.get("name") or address.rsplit(".", 1)[-1])
        descriptor = dict((after.get("metric_descriptor") or [{}])[0] or {})
        body = {
            "name": name,
            "filter": after.get("filter"),
            "description": after.get("description") or "",
            "labelExtractors": dict(after.get("label_extractors") or {}),
            "metricDescriptor": {
                "metricKind": descriptor.get("metric_kind") or "DELTA",
                "valueType": descriptor.get("value_type") or "INT64",
                "unit": descriptor.get("unit") or "1",
                "labels": [{"key": label.get("key"), "valueType": label.get("value_type") or "STRING",
                            "description": label.get("description") or ""}
                           for label in descriptor.get("labels") or ()],
            },
        }
        self.deps.gcp()._call("PUT", f"{LOGGING}/projects/{self.project}/metrics/{name}", body=body)

    # -- images and the cluster --------------------------------------------------

    def copy_images(self, version: str, images: Mapping[str, str]) -> None:
        step = self.steps["images.copy"]
        ctx = self.ctx(version)
        step.apply(ctx, step.plan(ctx))

    def policies(self, version: str) -> str:
        return str(self.steps["cluster.policies"].rendered(self.ctx(version)))

    def apply_policies(self, rendered: str) -> None:
        self.steps["cluster.policies"].apply_render(self.ctx(), rendered)

    def delete_policies(self, documents: Sequence[tuple[str, str]]) -> None:
        step = self.steps["cluster.policies"]
        kube = step.kube(self.ctx())
        for kind, name in documents:
            code, _, err = kube("delete", kind.lower(), name, "--ignore-not-found")
            if code != 0:
                raise RuntimeError(f"kubectl delete {kind} {name} failed: {err.strip()[:300]}")

    def tenants(self) -> list[str]:
        return sorted(path.split("/", 1)[1] for path, _, _ in self._documents.list("tenants"))

    def apply_namespace(self, tenant: str) -> None:
        from ..steps.cluster import CLUSTER_GCP, kube_for
        from ..team import KubeCluster, ensure_verify_keys, scheduler_spec_settings

        ctx = self.ctx()
        settings = scheduler_spec_settings(CLUSTER_GCP(), self.project, str(ctx.state.region))
        ensure_verify_keys(KubeCluster(kube_for(ctx)), tenant=tenant, settings=settings)

    # -- the GitHub App ----------------------------------------------------------

    def _app_record(self) -> dict[str, Any]:
        state = self.store.load()
        app = dict(state.outputs.get("github_app") or {})
        app.update({k: v for k, v in dict(state.console.get("github_app") or {}).items() if v})
        return app

    def _app_key(self) -> str:
        """The App's private key, read once per run and held in memory only."""
        from ..steps.preflight import SECRET_MANAGER_API

        if self._key is None:
            try:
                found = self.deps.gcp()._call(
                    "GET", f"{SECRET_MANAGER_API}/projects/{self.project}/secrets/{APP_KEY_SECRET}/versions/latest:access")
                self._key = base64.b64decode(found["payload"]["data"]).decode("utf-8")
            except Exception as exc:  # noqa: BLE001 -- the error names the secret, never its value
                raise UpgradeFailed(f"cannot read {APP_KEY_SECRET} in {self.project} to read the GitHub App as "
                                    f"itself ({type(exc).__name__}: {str(exc)[:200]})") from None
        return self._key

    def _github(self, path: str) -> Any:
        """`GET path` on GitHub's API as the App (an App JWT); an error is an `UpgradeFailed`."""
        import requests

        app_id = str(self._app_record().get("id") or "")
        token = app_jwt(app_id, self._app_key(), time.time())
        try:
            found = requests.get(f"{GITHUB_API}{path}", timeout=30, headers={
                "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28"})
        except requests.RequestException as exc:
            raise UpgradeFailed(f"GitHub did not answer GET {path} ({type(exc).__name__})") from None
        if found.status_code >= 400:
            try:
                message = str(found.json().get("message") or "")
            except ValueError:
                message = ""
            raise UpgradeFailed(f"GitHub answered {found.status_code} to GET {path} as App {app_id}"
                                + (f": {message[:200]}" if message else ""))
        return found.json()

    def app(self) -> GithubApp | None:
        if not self._app_record().get("id"):
            return None
        found = dict(self._github("/app") or {})
        owner = dict(found.get("owner") or {})
        return GithubApp(slug=str(found.get("slug") or self._app_record().get("slug") or ""),
                         owner=str(owner.get("login") or ""), owner_is_org=owner.get("type") == "Organization",
                         permissions={str(k): str(v) for k, v in dict(found.get("permissions") or {}).items()})

    def installation(self) -> AppInstallation | None:
        """The installation on the account Admin › Setup installed the App on
        (`installed_on`, else the deployment's `github_org`), else the App's only one."""
        if not self._app_record().get("id"):
            return None
        if self._installation_id is None:
            wanted = str(self._app_record().get("installed_on") or self.store.load().outputs.get("github_org")
                         or "").lower()
            found = [dict(i) for i in self._github("/app/installations?per_page=100") or ()]
            matching = [i for i in found if str((i.get("account") or {}).get("login") or "").lower() == wanted]
            chosen = matching or (found if len(found) == 1 else [])
            if not chosen:
                return None
            self._installation_id = int(chosen[0]["id"])
        found = dict(self._github(f"/app/installations/{self._installation_id}") or {})
        account = dict(found.get("account") or {})
        return AppInstallation(id=int(found.get("id") or self._installation_id), account=str(account.get("login") or ""),
                               account_is_org=account.get("type") == "Organization",
                               permissions={str(k): str(v) for k, v in dict(found.get("permissions") or {}).items()})

    def release_app_permissions(self, version: str) -> dict[str, str] | None:
        from ..steps.bootstrap import workdir

        if APP_PERMISSIONS_FILE not in self._release().sums(version, workdir(self.project, version)):
            return None
        found = json.loads(self.fetch(version, APP_PERMISSIONS_FILE))
        return {str(k): str(v) for k, v in dict(found).items()}

    # -- dispatch and the checks -------------------------------------------------

    def _swarm_api(self) -> Any:
        if self._api is None:
            from swarm_mcp.client import SwarmClient  # the swarm_mcp wheel install.sh installs beside this one

            self._api = SwarmClient(context=self._context)
        return self._api

    def pause_dispatch(self, reason: str) -> None:
        self._swarm_api().request("POST", "/v1/admin/dispatch/pause", payload={"reason": reason})

    def resume_dispatch(self, reason: str) -> None:
        self._swarm_api().request("POST", "/v1/admin/dispatch/resume", payload={"reason": reason})

    def running(self) -> int:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._documents.client.collection("tasks").where(filter=FieldFilter("state", "in",
                                                                                     list(DEMAND_STATES)))
        return sum(1 for _ in query.stream())

    def checks(self) -> list[tuple[str, str, str]]:
        from .. import record as install_record

        if self.engine.record is None:
            self.engine.record = install_record.open_record(self.project, self.environment)
        ctx = self.ctx()
        results = []
        for step_id in CHECKS:
            try:
                seen = self.steps[step_id].check(ctx)
                results.append((step_id, seen.status, seen.detail))
            except Exception as exc:  # noqa: BLE001 -- a check that cannot run is unknown, never green
                results.append((step_id, UNKNOWN, str(exc)[:300]))
        return results

    def documents(self) -> FirestoreMigrationDocuments:
        return self._documents


def run(args: argparse.Namespace) -> int:
    from ..steps import discover as discover_steps
    from ..steps import preflight
    from .install import _typed, holder_identity

    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(args.json_events))
    bucket = GcsObjectStore(state_bucket(args.project))
    store = StateStore(project=args.project, environment=args.env, local=local_path(args.project), bucket=bucket)
    if bucket.read("swarm-install/state.json") is None:
        print(f"swarm upgrade: gs://{state_bucket(args.project)} holds no install state: run swarm install first",
              file=sys.stderr)
        return int(ExitCode.FAIL)
    holder = holder_identity()
    lease = Lease(holder=holder, store=bucket, clock=clock.now)
    engine = Engine(discover_steps(), store=store, lease=lease, events=events, clock=clock, holder=holder,
                    confirm=_typed)
    world = LiveWorld(args.project, args.env, store, engine, events, clock, holder, args.context)
    confirm_plan = os.environ.get(CONFIRM_PLAN_ENV) == "1"
    target = args.to
    try:
        state = store.load()
        if target is None:
            target = channel_release(world.read_public, state.release)
            if nothing_to_do(state, target):
                print(f"swarm upgrade: {args.project} runs {target}, the release its channel names; nothing to do")
                return int(ExitCode.OK)
            print(f"swarm upgrade: release {target} is available ({args.project} runs {state.release}). Its notes:")
            print(release_notes(world.fetch, target))
            confirm_plan = True
        running = str(state.release or "")
        if running and version_key(target) < version_key(running) and not args.allow_downgrade:
            # Refused here too, before an older release's CLI is installed for it.
            raise UpgradeRefused(f"{target} is older than the running {running}; a downgrade needs "
                                 "--allow-downgrade")
        if not nothing_to_do(state, target):
            ensure_engine(target, args, installed=preflight.installed_release(), release=world._release(),
                          confirm=_typed, confirm_plan=confirm_plan)
    except Exception as exc:  # noqa: BLE001 -- said in one line with its exit code, never a traceback
        return _failed(events, exc)
    flow = Upgrade(project=args.project, world=world, store=store, events=events, clock=clock, confirm=_typed,
                   confirm_plan=confirm_plan, heartbeat=lambda: lease.renew() if lease.heartbeat_due() else None)
    try:
        lease.acquire(_typed)
    except (LeaseHeld, LeaseRefused) as exc:
        print(f"swarm upgrade: {exc}", file=sys.stderr)
        return int(ExitCode.PAUSED)
    try:
        try:
            code = flow.run(target, allow_downgrade=args.allow_downgrade)
        finally:
            left = world.close_reach()
            if left:
                print(left, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- every step failure, a GitHub or Google API error with them
        return _failed(events, exc)
    finally:
        lease.release()
    events.emit("run_end", outcome="ok", exit_code=int(code), message=f"{args.project} runs {target}")
    return int(code)


def _failed(events: Events, exc: BaseException) -> int:
    """One line naming the failure and its exit code, never a traceback (audit 4e F4)."""
    code = exit_code_for(exc)
    known = isinstance(exc, (UpgradeRefused, UpgradeFailed, MigrationFailed, WaitGaveUp))
    message = str(exc) if known else f"{type(exc).__name__}: {str(exc)[:500]}"
    print(f"swarm upgrade: {message}", file=sys.stderr)
    events.emit("run_end", outcome="refused" if code == ExitCode.REFUSED else "failed", exit_code=int(code),
                message=message[:500])
    return int(code)


__all__ = ["CHECKS", "HELP", "LiveWorld", "NAME", "MET", "add_arguments", "app_jwt", "channel_release",
           "ensure_engine", "exit_code_for", "fetch_wheels", "install_engine", "run", "wheel_version"]
