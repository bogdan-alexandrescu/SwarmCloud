"""`swarm doctor`: the client tier, then the platform tier (design §2.1; plan
§1.5 DAYONE-DOCTOR-1).

    swarm doctor                         this machine's tier, then the deployment
    swarm doctor --project P [--env E]   the deployment of P, every manifest probe
    swarm doctor --project P --json      one JSON document

The **client tier** is the bridge's own doctor (`swarm_mcp.cli.cmd_doctor`):
which deployment this machine resolves and which auth tier it is on. It runs
first whenever the bridge is installed beside the installer.

The **platform tier** runs when the caller can read the install state of the
deployment (`--project`, else the gcloud configuration's project): every
manifest entry's probe (`swarm_install.doctor`), `met`, `unmet` or `unknown`
per entry, with the fix in words and the `swarm install --repair <id>` that
repairs it, and one line on the hourly doctor. It never writes.

Exit codes follow `sc`: 0 every probed entry is met; 3 something is unmet or
could not be checked (never 0 when a probe did not run); 1 the doctor could
not start (no install state readable, no manifest); 2 usage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import doctor
from .. import manifest as manifest_model
from ..exitcodes import ExitCode
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, local_path, state_bucket

NAME = "doctor"
HELP = "check this machine and the deployment against the manifest (never writes)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=None,
                        help="the deployment's Google Cloud project (default: gcloud's configured project)")
    parser.add_argument("--env", default="prod", help="the environment's name (default: prod)")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="the release's manifest.yaml (default: the one beside this package)")
    parser.add_argument("--json", action="store_true", help="the platform tier as one JSON document")
    parser.add_argument("--platform-only", action="store_true",
                        help="skip the client tier (this machine's auth)")


# -- seams the tests replace ---------------------------------------------------


def client_tier() -> int | None:
    """The bridge's doctor, or None when the bridge is not installed here."""
    try:
        from swarm_mcp import cli as bridge
    except ImportError:
        return None
    try:
        return int(bridge.cmd_doctor(None, argparse.Namespace()))
    except Exception as exc:  # noqa: BLE001 - the platform tier still runs
        print(f"client tier could not run ({type(exc).__name__}: {exc})")
        return int(ExitCode.TROUBLE)


def _gcloud(*words: str) -> str:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        return ""
    try:
        done = subprocess.run([gcloud, *words], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def default_project() -> str:
    return _gcloud("config", "get-value", "project")


def principal() -> str:
    account = _gcloud("config", "get-value", "account")
    if "@" not in account:
        return doctor.principal()
    kind = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    return f"{kind}:{account}"


def read_state(project: str) -> tuple[InstallState | None, str]:
    """The install state (the bucket's, else the local copy) and where it came from."""
    problem = ""
    try:
        found = GcsObjectStore(state_bucket(project)).read(STATE_OBJECT)
        if found is not None:
            return InstallState.from_bytes(found[0]), f"gs://{state_bucket(project)}/{STATE_OBJECT}"
    except Exception as exc:  # noqa: BLE001 - said, then the local copy is tried
        problem = f" (the bucket could not be read: {type(exc).__name__}: {exc})"
    local = local_path(project)
    if local.is_file():
        return InstallState.from_bytes(local.read_bytes()), str(local)
    return None, f"no install state in gs://{state_bucket(project)}/{STATE_OBJECT} or at {local}{problem}"


def load_manifest(path: Path | None) -> manifest_model.Manifest:
    base = path if path is not None else manifest_model.MANIFEST_PATH
    return manifest_model.load(base, base.parent / "manifest.d")


class _InstallApi:
    """swarm-api's groups probe through the install's client (the deployer's
    ID token for the API backend's IAP audience, `api_client`)."""

    def __init__(self, state: InstallState) -> None:
        self._state = state

    def groups_probe(self) -> list[dict[str, Any]]:
        from .. import api_client

        ctx = SimpleNamespace(project=self._state.project, state=self._state, outputs=self._state.outputs)
        client = api_client.for_install(ctx)
        # No pairs: swarm-api probes every registered group and answers
        # `{mode, results}`, the shape stage 8 reads too (finding N6).
        answer = client.groups_probe(())
        return [{"group": r.group, "state": r.state, "reason": r.reason}
                for r in answer.results]


def make_deps(state: InstallState) -> doctor.Deps:
    rest = doctor.RestGcp()
    return doctor.Deps(gcp=rest, api=_InstallApi(state), kube=doctor.kube_for(rest._bearer))


# -- the verb ------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    client_code = None if args.platform_only or args.json else client_tier()
    if client_code is not None:
        print()
    project = args.project or default_project()
    if not project:
        print("swarm doctor: no --project given and gcloud names no project; the platform tier did not run",
              file=sys.stderr)
        return ExitCode.TROUBLE if client_code is not None else ExitCode.USAGE
    state, source = read_state(project)
    if state is None:
        print(f"swarm doctor: {source}; the platform tier did not run. Start an install with: "
              f"swarm install --project {project}", file=sys.stderr)
        return ExitCode.FAIL
    if state.environment != args.env:
        print(f"swarm doctor: the install state of {project} is for environment {state.environment!r}, "
              f"not {args.env!r}", file=sys.stderr)
        return ExitCode.USAGE
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, manifest_model.ManifestError) as exc:
        print(f"swarm doctor: the manifest could not be loaded ({exc})", file=sys.stderr)
        return ExitCode.FAIL
    target = doctor.Target(
        project=project,
        environment=state.environment,
        region=state.region or str(state.outputs.get("region") or ""),
        outputs=dict(state.outputs),
        principal=principal(),
    )
    deps = make_deps(state)
    findings = doctor.run(manifest, target, deps)
    code = doctor.exit_code(findings)
    hourly = doctor.hourly_doctor(target, deps)
    skipped = len(manifest.entries) - len(findings)
    if args.json:
        print(json.dumps({
            "project": project,
            "environment": state.environment,
            "source": source,
            "hourly_doctor": hourly,
            "checks": [f.to_json() for f in findings],
            "unprobed": skipped,
            "exit_code": int(code),
        }, sort_keys=True))
        return code
    print(f"platform    {project} ({state.environment}), from {source}")
    doctor.render(findings, skipped)
    print(hourly)
    if client_code not in (None, 0) and code == ExitCode.OK:
        return ExitCode.TROUBLE
    return code
