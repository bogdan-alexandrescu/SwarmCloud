"""What each Terraform pass makes, in the fake world, from the inputs it was
given: the deployer and its grants, the KMS ring, the platform service
accounts, the swarm Firestore database, the registry, the cluster.

A pass makes a thing only when its inputs ask for it, so a switch no pass
rendered leaves its resource missing and the manifest entry it backs unmet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from swarm_install.api_client import deployer_email
from swarm_install.steps.bootstrap import PLATFORM_ACCOUNTS
from swarm_install.terraform import IMPERSONATE_ENV

from .google import Cloud
from .world import World


def _account(cloud: Cloud, email: str) -> None:
    cloud.accounts.setdefault(email, {"email": email, "name": f"projects/-/serviceAccounts/{email}",
                                      "uniqueId": str(104000000000000000000 + len(cloud.accounts))})


def attach(world: World, cloud: Cloud) -> None:
    world.may_impersonate = lambda source, target: (  # type: ignore[method-assign]
        target in cloud.accounts and (source, target) in cloud.token_creators)
    world.passes = []  # type: ignore[attr-defined]  # (root, targets, inputs), as applied

    def remote_exists(resource_id: str) -> bool:
        """`terraform import`'s read of the remote object."""
        parts = resource_id.strip("/").split("/")
        if "addresses" in parts:
            return (parts[1], parts[-1]) in cloud.addresses
        if "keyRings" in parts:
            return resource_id in cloud.key_rings or resource_id in cloud.crypto_keys
        return False

    world.remote_exists = remote_exists  # type: ignore[attr-defined]

    terraform = world.terraform  # type: ignore[attr-defined]

    def guarded(words: list[str], env: dict[str, str], cwd: Path | None) -> tuple[int, str, str]:
        """Terraform as the deployer: Google refuses the token before any plan runs."""
        as_whom = env.get(IMPERSONATE_ENV)
        if as_whom and words[:1] in (["plan"], ["apply"], ["init"]) and not world.may_impersonate(
                world.installer, as_whom):
            return 1, "", (f"Error: impersonate: generateAccessToken: 403 PERMISSION_DENIED: {world.installer} "
                           f"lacks iam.serviceAccounts.getAccessToken on {as_whom}")
        return terraform(words, env, cwd)

    world.terraform = guarded  # type: ignore[attr-defined]

    def applied(root: str, inputs: dict[str, Any], env: dict[str, str], targets: list[str]) -> None:
        project = str(inputs.get("project_id") or "")
        world.passes.append((root, list(targets), dict(inputs)))  # type: ignore[attr-defined]
        if root == "bootstrap":
            bootstrap(world, cloud, project, inputs)
        else:
            infra(world, cloud, project, inputs, targets)

    world.applied = applied  # type: ignore[attr-defined]


def bootstrap(world: World, cloud: Cloud, project: str, inputs: dict[str, Any]) -> None:
    deployer = deployer_email(project)
    _account(cloud, deployer)
    region = str(inputs.get("region") or "")
    env = str(inputs.get("environment") or "prod")
    ring = f"projects/{project}/locations/{region}/keyRings/swarm-{env}-specs"
    cloud.key_rings[ring] = {"name": ring}
    cloud.crypto_keys[f"{ring}/cryptoKeys/step-spec"] = {"name": f"{ring}/cryptoKeys/step-spec"}
    cloud.token_creators.add((world.installer, deployer))
    for name in PLATFORM_ACCOUNTS:
        cloud.act_as.add((deployer, f"{name}@{project}.iam.gserviceaccount.com"))
    # platform_service_accounts.tf: one identity per component.
    for name in PLATFORM_ACCOUNTS:
        _account(cloud, f"{name}@{project}.iam.gserviceaccount.com")
    if inputs.get("enable_qa_identity"):
        qa_identity(cloud, project, inputs)
    if inputs.get("enable_workspace_deployer"):  # workspace_deployer.tf
        _account(cloud, f"swarm-workspace-deployer@{project}.iam.gserviceaccount.com")
    world.tf_outputs["bootstrap"].update({  # type: ignore[attr-defined]
        "state_bucket": f"swarm-tfstate-{project}",
        "project_id": project,
    })


#: The platform's Cloud Run services and the Scheduler job that ticks each one
#: (terraform/infra's scheduler module), with the account the tick presents.
SERVICES = ("swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-ui")
TICKS = {"swarm-scheduler-tick": "swarm-scheduler", "swarm-reconciler-tick": "swarm-reconciler",
         "swarm-quota-sweep": "swarm-quota-broker"}


def run_services(cloud: Cloud, project: str, region: str, inputs: dict[str, Any], env: dict[str, str],
                 subnet: str) -> None:
    """Each service's revision serves the image `image_refs` names, 100 % on
    the latest; each Scheduler tick's account holds run.invoker on its target."""
    refs = inputs.get("image_refs") or {}
    tick = f"swarm-tick@{project}.iam.gserviceaccount.com"
    _account(cloud, tick)
    for name in SERVICES:
        uri = f"https://{name}-{cloud.number(project)}.{region}.run.app"
        resource = f"projects/{project}/locations/{region}/services/{name}"
        image = str(refs.get(name) or "")
        revision = f"{name}-{len(cloud.revisions) + 1:05d}"
        cloud.revisions[revision] = {"metadata": {"name": revision},
                                     "status": {"imageDigest": image}}
        service: dict[str, Any] = {"name": resource, "uri": uri, "urls": [uri], "latestReadyRevision": revision,
                                   "template": {"containers": [{"image": image}]}}
        if name == "swarm-api":
            service["template"] = {"vpcAccess": {"networkInterfaces": [{"subnetwork": subnet}]},
                                   "containers": [{"image": image,
                                                   "env": [{"name": k, "value": v} for k, v in env.items()]}]}
        cloud.run_services[name] = service
        cloud.run_policies[resource] = {"bindings": [{"role": "roles/run.invoker",
                                                      "members": [f"serviceAccount:{tick}"]}]}
    for job, target in TICKS.items():
        uri = cloud.run_services[target]["uri"]
        cloud.scheduler_jobs[job] = {
            "name": f"projects/{project}/locations/{region}/jobs/{job}", "state": "ENABLED",
            "httpTarget": {"uri": f"{uri}/internal/tick", "httpMethod": "POST",
                           "oidcToken": {"serviceAccountEmail": tick, "audience": uri}}}


def qa_identity(cloud: Cloud, project: str, inputs: dict[str, Any]) -> None:
    """terraform/bootstrap/qa_identity.tf: the account (its precondition: its
    member is in frontend_iap_members), the signJwt-only role, and the
    account's authoritative policy for swarm-api alone."""
    email = f"qa-console@{project}.iam.gserviceaccount.com"
    if f"serviceAccount:{email}" not in (inputs.get("frontend_iap_members") or []):
        raise AssertionError("bootstrap: enable_qa_identity needs serviceAccount:qa-console in frontend_iap_members "
                             "(qa_identity.tf's precondition refuses the plan)")
    _account(cloud, email)
    role = f"projects/{project}/roles/swarmQaJwtSigner"
    cloud.roles[role] = {"name": role, "includedPermissions": ["iam.serviceAccounts.signJwt"], "stage": "GA"}
    cloud.account_policies[email] = [{"role": role,
                                      "members": [f"serviceAccount:swarm-api@{project}.iam.gserviceaccount.com"]}]


def infra(world: World, cloud: Cloud, project: str, inputs: dict[str, Any], targets: list[str]) -> None:
    region = str(inputs.get("region") or "")
    outputs = world.tf_outputs["infra"]  # type: ignore[attr-defined]
    outputs.update({"project_id": project, "region": region,
                    "image_base": f"{region}-docker.pkg.dev/{project}/swarm-images"})
    if targets:  # the registry pass: `-target` the registry only
        world.registries.add(f"projects/{project}/locations/{region}/repositories/swarm-images")  # type: ignore[attr-defined]
        return
    for name in PLATFORM_ACCOUNTS:
        _account(cloud, f"{name}@{project}.iam.gserviceaccount.com")
    cloud.firestore[f"{project}/swarm"] = {"name": f"projects/{project}/databases/swarm", "uid": "e2e-swarm-db",
                                           "locationId": region, "type": "FIRESTORE_NATIVE"}
    world.databases.add("swarm")
    # The platform keys' containers (child_tasks.tf, live.tf, settings_mac.tf,
    # workspace_grant_key.tf; graph_service.tf's only while the service is on).
    keys = ["swarm-child-key", "swarm-live-key", "swarm-settings-key", "swarm-workspace-grant-key"]
    if inputs.get("enable_graph_service"):
        keys.append("swarm-graph-ticket-key")
    for key in keys:
        cloud.secrets.setdefault((project, key), {"name": f"projects/{cloud.number(project)}/secrets/{key}",
                                                  "labels": {"managed-by": "swarm-terraform"}})
    if hasattr(world, "make_namespace"):  # infra's kubernetes provider makes swarm-system
        world.make_namespace("swarm-system")  # type: ignore[attr-defined]
    hostname = str(inputs.get("frontend_hostname") or inputs.get("hostname") or world.hostname)
    cloud.certificates.setdefault((project, "swarm-ui-cert"), {"name": "swarm-ui-cert", "project": project,
                                                               "type": "MANAGED", "domains": [hostname]})
    # Cloud Monitoring: a channel per alert email, the log metrics' descriptors
    # (which appear after the metrics exist), the policies once create_alerts is on.
    for email in inputs.get("alert_emails") or []:
        if not any(c["labels"]["email_address"] == email for c in cloud.channels):
            cloud.channels.append({"type": "email", "labels": {"email_address": email}})
    for metric in ("swarm_dispatch_errors", "swarm_stale_generation"):
        if f"logging.googleapis.com/user/{metric}" not in cloud.descriptors:
            cloud.descriptors.append(f"logging.googleapis.com/user/{metric}")
    if inputs.get("create_alerts") and not cloud.alert_policies:
        cloud.alert_policies.append({"displayName": "swarm dispatch errors"})
    # swarm-api's revision, its environment as terraform/infra/locals.tf renders it.
    subnet = f"projects/{project}/regions/{region}/subnetworks/swarm-subnet-{region}"
    cloud.subnetworks[subnet] = {"name": subnet.rsplit("/", 1)[-1],
                                 "ipCidrRange": str(inputs.get("subnet_cidr") or "10.40.0.0/20")}
    env = {"PROJECT_ID": project, "REGION": region,
           "ALLOWED_DOMAINS": ",".join(inputs.get("allowed_domains") or []),
           "GROUPS_AUTH": str(inputs.get("groups_auth") or "delegation")}
    run_services(cloud, project, region, inputs, env, subnet)
    hooks_on = bool(inputs.get("enable_github_hooks"))
    outputs["github_hooks"] = {
        "created": hooks_on, "serving": hooks_on and bool((inputs.get("image_refs") or {}).get("swarm-github-hooks")),
        "service": "swarm-github-hooks", "webhook_secret": "", "service_account": "", "webhook_url": ""}
    outputs.update({
        "firestore_database": "swarm",
        "gke_cluster_name": "swarm-agents",
        "service_urls": {s: f"https://{s}-e2e.a.run.app" for s in
                         ("swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-ui")},
        "quota_broker_url": "https://swarm-quota-broker-e2e.a.run.app",
        "frontend_iap_audiences": [f"/projects/{cloud.number(project)}/global/backendServices/{n}"
                                   for n in (7001, 7002)],
    })
