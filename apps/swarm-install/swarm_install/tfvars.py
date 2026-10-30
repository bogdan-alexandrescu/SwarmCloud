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
| `bootstrap-2` | bootstrap | the log view grant, the IAP programmatic client and the backends it applies to |
| `infra-3` | infra | the console's GitHub App ids, `enable_github_app`, `enable_forge_refresh` |
| `infra-4` | infra | `enable_github_hooks` |
| `infra-close` | infra | infra-4's inputs without the installer's control-plane CIDR (after stage 14's last kubectl) |

**Who gets in (decision 1).** IAP admits `domain:<org_domain>`, the installer's
`user:`, the QA identity's account, and, for programmatic sign-in, the
deployment's own Desktop client; swarm-api's `ALLOWED_DOMAINS` is
`[org_domain]`. Workspace membership still comes from the Google groups.
For one person (`sizing.mode == "individual"`, decision 7) IAP admits the
installer's `user:` and no `domain:` member; the QA identity and the Desktop
client stay (bootstrap refuses `enable_qa_identity` without the QA account),
and `ALLOWED_DOMAINS` is still `[org_domain]`.

**Sizing (decision 7).** Stage 0 records `outputs["sizing"]`; its
`pool_limits` and `service_max_instances` become infra's variables of the same
names on every infra pass, checked here against infra's own validations so a
proposal Terraform would refuse fails before any plan. An install state with
no `sizing` (older than the question) keeps Terraform's defaults.

**Add-ons.** Every pass merges `addons.inputs_for(outputs, root)` over the
core inputs, so an add-on's recorded phase survives a later install or
upgrade apply instead of being turned back off by core's defaults. The CI
add-on's recorded `sandbox_repository` becomes infra's
`acceptance_sandbox_repository_url`, the same clone URL `swarm addon enable ci`
sets as the repository variable, so the swarm-verify job's acceptance pushes to
the company's own sandbox; without one the input stays unset (null, no env).

**Cluster reach (decision 2).** Every infra pass before `infra-close` puts the
installer's public address (`installer_cidr`) in `gke_master_authorized_cidrs`
as `swarm-install`, so the cluster stages (10, 11) and stage 14's verify reads
(`verify.reconciler-jobs` runs kubectl from the laptop) can reach the control
plane, whose module admits no public Google range; `infra-close`, the last
pass, leaves it out.

A value a pass needs that the state does not hold yet is a `LookupError` that
names the step recording it, never a default.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import addons
from .api_client import ADMIN_ROLE, DELEGATION, deployer_email
from .state import ObjectStore, PreconditionFailed

PASSES = ("bootstrap-1", "registry", "infra-1", "infra-2", "bootstrap-2", "infra-3", "infra-4", "infra-close")
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
    # The graph service serves only with its ticket key and its image's digest.
    "swarm-graph-ticket-key": "graph_service.serve",
}
#: The IAP Desktop client's secret (stage 2, `edge.desktop-client`).
DESKTOP_SECRET = "swarm-iap-desktop-client-secret"
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

#: The display name of the installer's entry in the control plane's
#: authorised networks (decision 2); `infra-close` removes it.
CLUSTER_REACH_NAME = "swarm-install"
#: The IAP-protected backends terraform/modules/frontend/main.tf makes for
#: name_prefix "swarm" (`${name_prefix}-ui-backend`, `${name_prefix}-ui-ui-backend`);
#: they exist once infra pass 1 has applied, so only bootstrap pass 2 names them.
IAP_BACKENDS = ("swarm-ui-backend", "swarm-ui-ui-backend")
#: The QA identity terraform/bootstrap/qa_identity.tf makes; its plan refuses
#: unless the account is in frontend_iap_members.
QA_ACCOUNT = "qa-console"
#: The discovery document's name for the deployment.
DISCOVERY_NAME = "SwarmCloud"
#: The oldest plugin a new deployment accepts: the plugin version on main when
#: discovery (`swarm_mcp/discovery.py`) landed, the first that reads it.
PLUGIN_MIN_VERSION = "0.5.74"
#: The hourly doctor runs DAYONE-DOCTOR-1's code in the swarm-verify image.
#: On from infra pass 2: BGD turned it on (DAYONE-TFVARS-2, review finding 12).
SCHEDULED_DOCTOR_SHIPPED = True

#: `sizing.mode` (decision 7): one person, or a team of `members` people.
INDIVIDUAL = "individual"
TEAM = "team"
#: terraform/infra's `pool_limits` attributes (variables.tf, var.pool_limits).
POOL_LIMIT_NUMBERS = ("global", "default_tenant", "provider_tenant")
POOL_LIMIT_MAPS = ("resource_classes", "runner_profiles", "backends", "providers")
#: terraform/infra's `service_max_instances` default (variables.tf). Its
#: validation needs every control-plane service named; sizing names the four
#: it scales, so swarm-ui keeps its default.
SERVICE_MAX_DEFAULTS = {"swarm-api": 20, "swarm-scheduler": 3, "swarm-quota-broker": 3,
                        "swarm-reconciler": 2, "swarm-ui": 2}

#: Which step records each state key a pass needs, for the LookupError.
RECORDED_BY = {
    "region": "stage 0 (preflight.region)",
    "release": "stage 0 (preflight.release)",
    "hostname": "stage 0 (preflight)",
    "github_org": "stage 0 (preflight)",
    "org_domain": "stage 0 (preflight)",
    "installer_cidr": "stage 0 (preflight)",
    "desktop_client_id": "stage 2 (edge.desktop-client)",
}


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
        raise LookupError(f"the install state has no {name} yet; {RECORDED_BY.get(name, 'stage 0')} records it")
    return str(value)


def org_domain(state: Any) -> str:
    """The Workspace or Cloud Identity primary domain, lower-case, no `@`."""
    domain = _required(state, "org_domain").strip().lower().lstrip("@")
    if not domain or "." not in domain or "/" in domain or ":" in domain:
        raise ValueError(f"org_domain {domain!r} is not a domain name")
    return domain


def sizing(state: Any) -> dict[str, Any] | None:
    """Stage 0's `sizing`, or None for an install state recorded before it."""
    found = state.outputs.get("sizing")
    if not found:
        return None
    if not isinstance(found, Mapping):
        raise ValueError(f"the install state's sizing is {type(found).__name__}, not a mapping; rerun stage 0")
    mode = found.get("mode")
    if mode not in (INDIVIDUAL, TEAM):
        raise ValueError(f"the install state's sizing mode is {mode!r}; one of {INDIVIDUAL!r}, {TEAM!r}")
    return dict(found)


def individual(state: Any) -> bool:
    """Decision 7: the deployment is for one person."""
    found = sizing(state)
    return bool(found) and found["mode"] == INDIVIDUAL


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def pool_limits(found: Mapping[str, Any]) -> dict[str, Any]:
    """infra's `pool_limits` from the sizing, refused where infra would refuse it."""
    limits = found.get("pool_limits")
    if not isinstance(limits, Mapping):
        raise ValueError("the install state's sizing has no pool_limits mapping; rerun stage 0")
    unknown = sorted(set(limits) - set(POOL_LIMIT_NUMBERS) - set(POOL_LIMIT_MAPS))
    if unknown:
        raise ValueError(f"the sizing's pool_limits name {unknown}, which terraform/infra's pool_limits does not "
                         f"declare; it declares {[*POOL_LIMIT_NUMBERS, *POOL_LIMIT_MAPS]}")
    out: dict[str, Any] = {}
    for key in POOL_LIMIT_NUMBERS:
        if key in limits:
            if not _positive(limits[key]):
                raise ValueError(f"the sizing's pool_limits.{key} is {limits[key]!r}; it must be a positive number")
            out[key] = limits[key]
    for key in POOL_LIMIT_MAPS:
        if key in limits:
            values = limits[key]
            if not isinstance(values, Mapping) or not all(_positive(v) for v in values.values()):
                raise ValueError(f"the sizing's pool_limits.{key} is {values!r}; it must map names to positive "
                                 "numbers")
            out[key] = {str(k): v for k, v in values.items()}
    # terraform/infra's provider validation: a provider's platform-wide pool
    # (providers[p], else global) is at least (tenants holding it) x
    # provider_tenant. Installs declare no Terraform tenants, so the tenants are
    # the workspaces the sizing serves: one for one person, `members` for a team.
    # `global` is every provider's pool that `providers` does not name.
    tenants = 1 if found["mode"] == INDIVIDUAL else int(found.get("members") or 1)
    per_tenant = out.get("provider_tenant", 10)
    ceiling = out.get("global", 100)
    for name, value in {"global (every provider not named)": ceiling, **out.get("providers", {})}.items():
        if value < tenants * per_tenant:
            raise ValueError(f"the sizing breaks terraform/infra's pool_limits validation: provider_tenant "
                             f"{per_tenant} x {tenants} tenants = {tenants * per_tenant} is more than the "
                             f"provider-wide pool {name} = {value}; lower provider_tenant or raise that pool "
                             "in the sizing")
    return out


def service_max_instances(found: Mapping[str, Any]) -> dict[str, Any]:
    """infra's `service_max_instances`: the sizing's ceilings over infra's default."""
    chosen = found.get("service_max_instances") or {}
    if not isinstance(chosen, Mapping):
        raise ValueError("the install state's sizing has no service_max_instances mapping; rerun stage 0")
    out = dict(SERVICE_MAX_DEFAULTS)
    for name, value in chosen.items():
        if not _positive(value) or int(value) != value:
            raise ValueError(f"the sizing's service_max_instances[{name!r}] is {value!r}; every service needs a "
                             "positive whole max-instances")
        out[str(name)] = int(value)
    return out


def iap_members(state: Any, installer: str) -> list[str]:
    """Decision 1: the organisation's domain, the installer, the QA identity.

    Decision 7: for one person, no `domain:` member.
    """
    members = [] if individual(state) else [f"domain:{org_domain(state)}"]
    members += [f"user:{installer}", f"serviceAccount:{QA_ACCOUNT}@{state.project}.iam.gserviceaccount.com"]
    return list(dict.fromkeys(members))


def cluster_reach(state: Any) -> list[dict[str, str]]:
    """Decision 2: the installer's public address, while the install runs."""
    cidr = _required(state, "installer_cidr").strip()
    address, _, bits = cidr.partition("/")
    parts = address.split(".")
    if bits != "32" or len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ValueError(f"installer_cidr {cidr!r} is not one IPv4 address as a.b.c.d/32")
    return [{"cidr_block": cidr, "display_name": CLUSTER_REACH_NAME}]


def _set_switch(inputs: dict[str, Any], switch: str, on: bool) -> None:
    if "." in switch:
        variable, field = switch.split(".", 1)
        inputs.setdefault(variable, {})[field] = on
    else:
        inputs[switch] = on


def _with_addons(inputs: dict[str, Any], state: Any, root: str) -> dict[str, Any]:
    """Every add-on's recorded phase over the core inputs (addons.inputs_for)."""
    _overlay(inputs, addons.inputs_for(state.outputs, root))
    return inputs


def acceptance_sandbox(state: Any) -> str | None:
    """The CI add-on's sandbox clone URL for the swarm-verify job, or None when none is recorded."""
    choices = (addons.recorded(state.outputs).get("ci") or {}).get("choices") or {}
    if not isinstance(choices, Mapping) or not choices.get("sandbox_repository"):
        return None
    # One format for the repository variable and the job's env (addons.CI_VARIABLES["sandbox"]).
    return addons.Flow._ci_variable("sandbox", choices)


def _overlay(into: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), Mapping):
            merged = dict(into[key])
            _overlay(merged, value)
            into[key] = merged
        else:
            into[key] = dict(value) if isinstance(value, Mapping) else value


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
        "frontend_iap_members": iap_members(state, installer),
        # The live-browser QA identity; its account is in frontend_iap_members.
        "enable_qa_identity": True,
        # The receiver's dedup role exists before infra-4's enable_github_hooks grants it.
        "enable_github_hooks_dedup_role": True,
        # The backends exist only after infra pass 1 (a data lookup on a missing
        # one fails the plan), so bootstrap pass 1 names none.
        "frontend_iap_backends": list(IAP_BACKENDS) if second else [],
        "frontend_iap_programmatic_clients": [_required(state, "desktop_client_id")] if second else [],
    }
    digests = state.outputs.get("image_digests") or {}
    if "workspace-apply" in digests:
        inputs["workspace_apply_image"] = image_refs(project, region, {"workspace-apply": digests["workspace-apply"]})[
            "workspace-apply"
        ]
    return _with_addons(inputs, state, "bootstrap")


def infra_inputs(state: Any, *, pass_name: str, installer: str,
                 versions: Mapping[str, bool] | None = None) -> dict[str, Any]:
    """infra's inputs for `registry`, `infra-1`, `infra-2`, `infra-3`, `infra-4` and `infra-close`.

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
        # Decision 1: swarm-api admits the organisation's own domain.
        "allowed_domains": [org_domain(state)],
        "setup_github_org": _required(state, "github_org"),
        "discovery": {
            "name": DISCOVERY_NAME,
            "version": _required(state, "release"),
            "oauth_client_id": _required(state, "desktop_client_id"),
            "plugin_min_version": PLUGIN_MIN_VERSION,
        },
        # Decision 2: reach for the cluster stages, removed by infra-close.
        "gke_master_authorized_cidrs": [] if after(pass_name, "infra-close") else cluster_reach(state),
        "alert_emails": [str(outputs["alert_email"])] if outputs.get("alert_email") else [],
        "deployer_service_account": deployer_email(project),
        "repository_grants_enforced": True,
        "workspace_gate": True,
        "groups_auth": groups_auth(outputs),
        "image_refs": image_refs(project, region, outputs.get("image_digests") or {}),
    }
    found = sizing(state)
    if found:
        inputs["pool_limits"] = pool_limits(found)
        inputs["service_max_instances"] = service_max_instances(found)
    if outputs.get("admin_groups"):
        inputs["admin_groups"] = list(outputs["admin_groups"])
    if inputs["groups_auth"] == DELEGATION and outputs.get("groups_impersonate_user"):
        inputs["groups_impersonate_user"] = str(outputs["groups_impersonate_user"])
    second = after(pass_name, "infra-2")
    for secret, switch in KEY_SWITCHES.items():
        _set_switch(inputs, switch, second and bool(versions.get(secret)))
    # The ticket key's container is made in pass 2, once the cluster's workload
    # pool (its reader's principal) exists; the service answers only while it serves.
    inputs["enable_graph_service"] = second
    serving = inputs["graph_service"]["serve"] and "swarm-graph" in inputs["image_refs"]
    inputs["graph_service"] = {"serve": serving, "enabled": serving}
    # Design §9.3, Q1: the Desktop client's secret is served once it has a version.
    inputs["discovery_serves_client_secret"] = second and bool(versions.get(DESKTOP_SECRET))
    inputs["enable_scheduled_doctor"] = second and SCHEDULED_DOCTOR_SHIPPED
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
    sandbox = acceptance_sandbox(state)
    if sandbox:
        inputs["acceptance_sandbox_repository_url"] = sandbox
    return _with_addons(inputs, state, "infra")


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
