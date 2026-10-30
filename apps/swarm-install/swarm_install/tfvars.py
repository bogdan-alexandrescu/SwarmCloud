"""The Terraform inputs an install generates (design §5.1, §5.4; plan R7, R8).

The engine writes `swarm-install.auto.tfvars.json` into each root it applies,
built from the install state alone: the project, region, environment and
hostname; the person who installs; the image digests of the release, in the
project's own registry; the switches computed from secret versions. It reads
**no committed tfvars** and nothing under `terraform/environments/` (lesson 5:
those name the live deployment), and it never passes a `-var` (lesson 11: a
value given only on the command line is one the next apply does not have).
Every generated file is also kept in the state bucket, beside the install
state, so the next apply (a resume, an upgrade, another laptop) has every
value the last one had.

The four applies (design §5) and the two after the hand-off (plan §5.5):

| Pass | Root | What it adds |
|---|---|---|
| `bootstrap-1` | bootstrap | roles, the deployer, KMS; no adoption, no broker lister, no log view |
| `registry` | infra | `-target` the registry only, so stage 4 can copy images into it |
| `infra-1` | infra | everything, every key-gated switch off (lesson 39) |
| `infra-2` | infra | each key switch on exactly when its key has an ENABLED version; `enable_console_app_setup`, `require_alert_channels`, `tenant_job_vpc_egress` |
| `bootstrap-2` | bootstrap | the log view grant and the IAP programmatic clients |
| `infra-3` | infra | the console's GitHub App ids, `enable_github_app`, `enable_forge_refresh` |
| `infra-4` | infra | `enable_github_hooks` |
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .api_client import ADMIN_ROLE, DELEGATION, deployer_email
from .state import ObjectStore, PreconditionFailed

PASSES = ("bootstrap-1", "registry", "infra-1", "infra-2", "bootstrap-2", "infra-3", "infra-4")
ROOT_OF = {p: ("bootstrap" if p.startswith("bootstrap") else "infra") for p in PASSES}

#: The generated file's name in a root: Terraform loads `*.auto.tfvars.json`
#: on its own, so no `-var-file` (and no `-var`) is ever on a command line.
INPUTS_FILE = "swarm-install.auto.tfvars.json"
#: Where the generated inputs are kept in the state bucket, per root.
INPUTS_PREFIX = "swarm-install/inputs/"

#: The release-home registry images are copied from (D21, RELHOME-1).
RELEASE_REGISTRY = "us-docker.pkg.dev/swarmcloud-prod/swarmcloud"
#: The project's own repository (`var.artifact_registry_repository`'s default).
REPOSITORY = "swarm"

#: Platform keys and the switch each one gates (design §5.4, lesson 39).
KEY_SWITCHES: dict[str, str] = {
    "swarm-child-key": "enable_child_tasks",
    "swarm-live-key": "enable_live_browser",
    "swarm-settings-key": "settings_mac_key_ready",
    "swarm-workspace-grant-key": "workspace_grant_attestation.serve",
}
#: The three secrets swarm-api writes from the console's App step (plan §5.5).
APP_SECRETS = (
    "swarm-github-app-private-key",
    "swarm-github-app-webhook-secret",
    "swarm-github-app-client-secret",
)

#: Words no generated input name may contain: a value that pins traffic to a
#: revision makes a later deploy serve 0 % (lesson 28); every service routes
#: 100 % to its latest ready revision, as the modules declare by default.
PINNING_WORDS = ("traffic", "revision")


def _order(pass_name: str) -> int:
    if pass_name not in PASSES:
        raise ValueError(f"unknown Terraform pass {pass_name!r}; one of {PASSES}")
    return PASSES.index(pass_name)


def after(pass_name: str, other: str) -> bool:
    """`pass_name` comes at or after `other` in the install's sequence."""
    return _order(pass_name) >= _order(other)


def image_refs(project: str, region: str, digests: Mapping[str, str]) -> dict[str, str]:
    """Every release image, by digest, in the project's own registry (lesson 13)."""
    refs = {}
    for name, digest in sorted(digests.items()):
        if not str(digest).startswith("sha256:") or len(str(digest)) != 71:
            raise ValueError(f"image {name}: {digest!r} is not a sha256 digest; the release pins every image")
        refs[name] = f"{region}-docker.pkg.dev/{project}/{REPOSITORY}/{name}@{digest}"
    return refs


def groups_auth(outputs: Mapping[str, Any]) -> str:
    """`admin_role` for every install; `delegation` only when the person chose
    the fallback at stage 8 (Q2; review finding 8)."""
    return DELEGATION if outputs.get("groups_auth") == DELEGATION else ADMIN_ROLE


def _required(state: Any, name: str) -> str:
    value = getattr(state, name, None) or state.outputs.get(name)
    if not value:
        raise LookupError(f"the install state has no {name} yet; stage 0 records it")
    return str(value)


def _set_switch(inputs: dict[str, Any], switch: str, on: bool) -> None:
    if "." in switch:
        variable, field = switch.split(".", 1)
        inputs.setdefault(variable, {})[field] = on
    else:
        inputs[switch] = on


def bootstrap_inputs(state: Any, *, pass_name: str, installer: str) -> dict[str, Any]:
    """bootstrap's inputs for `bootstrap-1` or `bootstrap-2`."""
    if ROOT_OF[pass_name] != "bootstrap":
        raise ValueError(f"{pass_name} is not a bootstrap pass")
    project = state.project
    region = _required(state, "region")
    second = after(pass_name, "bootstrap-2")
    inputs: dict[str, Any] = {
        "project_id": project,
        "region": region,
        # Lesson 8: the first bootstrap adopts nothing and grants nothing infra creates.
        "adopt_from_infra_states": [],
        "infra_tenants_tfvars": "",
        # One step-spec key ring, for this environment only.
        "spec_signing_environments": [state.environment],
        "grant_broker_secret_lister": second,
        "swarm_workers_log_view_exists": second,
        "enable_github_wif": False,
        "ci_fix_service_account": "",
        "frontend_iap_members": [f"user:{installer}"],
    }
    digests = state.outputs.get("image_digests") or {}
    if "workspace-apply" in digests:
        inputs["workspace_apply_image"] = image_refs(project, region, {"workspace-apply": digests["workspace-apply"]})[
            "workspace-apply"
        ]
    return inputs


def infra_inputs(state: Any, *, pass_name: str, installer: str,
                 versions: Mapping[str, bool] | None = None) -> dict[str, Any]:
    """infra's inputs for `registry`, `infra-1`..`infra-4`.

    `versions`: secret name -> it has an ENABLED version now (lesson 39).
    Before `infra-2` every key switch is off whatever `versions` says.
    """
    if ROOT_OF[pass_name] != "infra":
        raise ValueError(f"{pass_name} is not an infra pass")
    versions = versions or {}
    project = state.project
    region = _required(state, "region")
    outputs = state.outputs
    inputs: dict[str, Any] = {
        "project_id": project,
        "region": region,
        "environment": state.environment,
        # Design §5.6: installs declare no Terraform tenants.
        "tenants": {},
        "enable_frontend": True,
        "frontend_hostname": _required(state, "hostname"),
        "admin_users": [installer],
        "alert_emails": [str(outputs["alert_email"])] if outputs.get("alert_email") else [],
        "deployer_service_account": deployer_email(project),
        "repository_grants_enforced": True,
        "workspace_gate": True,
        "groups_auth": groups_auth(outputs),
        "image_refs": image_refs(project, region, outputs.get("image_digests") or {}),
    }
    if outputs.get("admin_groups"):
        inputs["admin_groups"] = list(outputs["admin_groups"])
    if inputs["groups_auth"] == DELEGATION and outputs.get("groups_impersonate_user"):
        inputs["groups_impersonate_user"] = str(outputs["groups_impersonate_user"])
    second = after(pass_name, "infra-2")
    for secret, switch in KEY_SWITCHES.items():
        _set_switch(inputs, switch, second and bool(versions.get(secret)))
    # Plan R7: the channel and the flag go in pass 2; stage 13 checks them.
    inputs["require_alert_channels"] = second and bool(inputs["alert_emails"])
    # Lesson 23: an alert on a log metric fails ("Cannot find metric") until
    # the metric's descriptor exists; stage 13 turns the policies on after it.
    inputs["create_alerts"] = bool(outputs.get("metric_descriptors_ready"))
    # Review finding 2: the App secrets' containers and swarm-api's grants
    # exist before the console's App step writes them (key before switch).
    inputs["enable_console_app_setup"] = second
    # Lesson 35: scheduler-created Jobs reach the internal API.
    inputs["tenant_job_vpc_egress"] = second
    app = outputs.get("github_app") or {}
    third = after(pass_name, "infra-3") and bool(app.get("id"))
    app_keys = all(versions.get(s) for s in APP_SECRETS)
    inputs["enable_github_app"] = third and app_keys
    inputs["enable_forge_refresh"] = third and app_keys
    if third:
        inputs["github_app_id"] = str(app["id"])
        for key, variable in (("client_id", "github_app_client_id"), ("slug", "github_app_slug"),
                              ("name", "github_app_name")):
            if app.get(key):
                inputs[variable] = str(app[key])
    inputs["enable_github_hooks"] = (after(pass_name, "infra-4") and inputs["enable_github_app"]
                                     and bool(versions.get("swarm-github-app-webhook-secret")))
    return inputs


def refuse_unsafe(inputs: Mapping[str, Any]) -> None:
    for name in inputs:
        if any(word in name for word in PINNING_WORDS):
            raise ValueError(f"generated inputs may not carry {name}: traffic goes to the latest revision")


def write_local(root_dir: Path, inputs: Mapping[str, Any]) -> Path:
    """Write the generated inputs into a root, where Terraform loads them."""
    refuse_unsafe(inputs)
    path = root_dir / INPUTS_FILE
    path.write_text(json.dumps(dict(inputs), indent=2, sort_keys=True) + "\n")
    return path


def object_name(root: str) -> str:
    return f"{INPUTS_PREFIX}{root}.auto.tfvars.json"


def store(bucket: ObjectStore, root: str, inputs: Mapping[str, Any]) -> str:
    """Keep the inputs in the state bucket (the next apply's source of truth)."""
    refuse_unsafe(inputs)
    name = object_name(root)
    data = (json.dumps(dict(inputs), indent=2, sort_keys=True) + "\n").encode()
    for _ in range(3):
        found = bucket.read(name)
        try:
            bucket.write(name, data, if_generation_match=found[1] if found else 0)
            return name
        except PreconditionFailed:
            continue
    raise PreconditionFailed(f"gs://.../{name} kept changing under the engine")


def stored(bucket: ObjectStore, root: str) -> dict[str, Any] | None:
    found = bucket.read(object_name(root))
    return json.loads(found[0]) if found else None
