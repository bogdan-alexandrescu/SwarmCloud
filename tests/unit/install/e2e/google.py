"""Google's REST APIs, faked with state: Resource Manager, billing, budgets,
Service Usage, IAM and IAM Credentials, Cloud Storage, Compute (regions,
addresses, certificates, operations), IAP, Cloud DNS, Secret Manager, KMS,
Firestore admin, Cloud Run, GKE, Cloud Identity, the Admin SDK and the
release home.

Each answers the way the real API does for what the installer sends, and
refuses what the real one refuses: a project id taken, a budget without a
quota project under user credentials, a secret version on a secret that
does not exist, a token minted without Token Creator.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from functools import cache
from subprocess import run as _run  # bound before the walk patches subprocess.run
from dataclasses import dataclass, field
from typing import Any

from .world import REPO, Call, Response, World, error

CRM = "https://cloudresourcemanager.googleapis.com"
BILLING = "https://cloudbilling.googleapis.com/v1"
BUDGETS = "https://billingbudgets.googleapis.com/v1"
USAGE = "https://serviceusage.googleapis.com"
IAM = "https://iam.googleapis.com/v1"
IAMCRED = "https://iamcredentials.googleapis.com/v1"
STORAGE = "https://storage.googleapis.com/storage/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
IAP = "https://iap.googleapis.com/v1"
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1"
RELEASE_HOME = "https://storage.googleapis.com/swarmcloud"

ORG_ID = "123456789012"
ORG = f"organizations/{ORG_ID}"
DOMAIN = "acme.example"
BILLING_ACCOUNT = "01AB23-45CD67-89EF01"
INSTALLER_IP = "203.0.113.7"


#: The images a release names (images.json), each pinned by digest.
#: What a release's images.json holds: every image terraform/infra/images.json
#: pins (assemble.sh refuses a build record without one), and the images
#: built beside them.
_PINNED = json.loads((REPO / "terraform" / "infra" / "images.json").read_text())
RELEASE_IMAGES = (*_PINNED["platform"], *_PINNED["gated"], "agent-worker", "workspace-apply")


def signature(blob: bytes) -> bytes:
    """The fake release key's signature over `blob`: what `cosign verify-blob` accepts."""
    return b"e2e-cosign-signature:" + hashlib.sha256(blob).hexdigest().encode()


@dataclass
class Cloud:
    """What the fake Google holds."""

    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    numbers: dict[str, str] = field(default_factory=dict)
    billing: dict[str, str] = field(default_factory=dict)
    services: dict[str, set[str]] = field(default_factory=dict)
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    budgets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    secrets: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    versions: dict[tuple[str, str], list[bytes]] = field(default_factory=dict)
    addresses: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    certificates: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    brands: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    adc_scopes: tuple[str, ...] = ()
    releases: dict[str, bytes] = field(default_factory=dict)
    key_rings: dict[str, dict[str, Any]] = field(default_factory=dict)
    crypto_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: account email -> its whole policy's bindings, where Terraform sets it authoritatively.
    account_policies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    firestore: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: (member, service account email) pairs holding roles/iam.serviceAccountTokenCreator.
    token_creators: set[tuple[str, str]] = field(default_factory=set)
    #: (member, service account email) pairs holding roles/iam.serviceAccountUser.
    act_as: set[tuple[str, str]] = field(default_factory=set)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)  # email -> group
    members: dict[str, set[str]] = field(default_factory=dict)  # group email -> member emails
    admin_roles: list[dict[str, Any]] = field(default_factory=lambda: [
        {"roleId": "9100", "roleName": "_GROUPS_READER_ROLE", "isSystemRole": True},
        {"roleId": "9101", "roleName": "_GROUPS_ADMIN_ROLE", "isSystemRole": True},
        {"roleId": "9102", "roleName": "_USER_MANAGEMENT_ADMIN_ROLE", "isSystemRole": True},
        {"roleId": "9103", "roleName": "_SEED_ADMIN_ROLE", "isSystemRole": True}])
    role_assignments: list[dict[str, Any]] = field(default_factory=list)
    run_services: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> service
    subnetworks: dict[str, dict[str, Any]] = field(default_factory=dict)
    revisions: dict[str, dict[str, Any]] = field(default_factory=dict)  # revision name -> knative revision
    run_policies: dict[str, dict[str, Any]] = field(default_factory=dict)  # service resource -> its policy
    scheduler_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Cloud Monitoring: notification channels, swarm_* log metric descriptor types, alert policies.
    channels: list[dict[str, Any]] = field(default_factory=list)
    descriptors: list[str] = field(default_factory=list)
    alert_policies: list[dict[str, Any]] = field(default_factory=list)  # projects/P/regions/R/subnetworks/N
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def publish(self, release: str, acceptances: bytes = b"[]") -> None:
        """The release home's channel and release objects, as `install.sh` and
        stage 4 read them: SHA256SUMS over every file, signed (`signature`)."""
        self.releases["channels/stable"] = release.encode() + b"\n"
        files = {"cve-acceptances.json": acceptances, "kubernetes.tgz": kubernetes_bundle(),
                 "images.json": json.dumps({name: "sha256:" + hashlib.sha256(name.encode()).hexdigest()
                                            for name in RELEASE_IMAGES}).encode()}
        for name, data in files.items():
            self.releases[f"releases/{release}/{name}"] = data
        sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
        self.releases[f"releases/{release}/SHA256SUMS"] = sums.encode()
        self.releases[f"releases/{release}/SHA256SUMS.sig"] = signature(sums.encode())

    def number(self, project: str) -> str:
        return self.numbers.setdefault(project, str(400000000000 + len(self.numbers) + 1))


@cache
def kubernetes_bundle() -> bytes:
    """`kubernetes.tgz` as scripts/release/assemble.sh builds it: git's tracked
    kubernetes/ files at the root, beside the Python packages render.py imports."""
    homes = (("kubernetes/", ""), ("apps/common/swarm_common/", "swarm_common/"),
             ("apps/quota-broker/quota_broker/", "quota_broker/"), ("apps/scheduler/scheduler/", "scheduler/"))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for home, prefix in homes:
            tracked = _run(["git", "ls-files", home], cwd=REPO, capture_output=True, text=True,
                                     check=True).stdout.split()
            for f in tracked:
                tar.add(REPO / f, arcname=prefix + f[len(home):], recursive=False)
    return out.getvalue()


def _operation(cloud: Cloud, name: str, response: dict[str, Any]) -> dict[str, Any]:
    op = {"name": name, "done": True, "response": response}
    cloud.operations[name] = op
    return op


def _member(email: str) -> str:
    return ("serviceAccount:" if email.endswith(".gserviceaccount.com") else "user:") + email


def _members(policy: dict[str, Any], role: str) -> list[str]:
    for binding in policy.get("bindings", []):
        if binding.get("role") == role:
            return list(binding.get("members", []))
    return []


def register(world: World, cloud: Cloud) -> None:
    route = world.route

    # -- sign-in --------------------------------------------------------------
    @route("POST", r"https://oauth2\.googleapis\.com/tokeninfo")
    def tokeninfo(call: Call) -> Any:
        return {"scope": " ".join(cloud.adc_scopes), "email": world.installer}

    # -- Resource Manager -----------------------------------------------------
    @route("GET", rf"{CRM}/v3/projects:search")
    def search(call: Call) -> Any:
        wanted = str(call.params.get("query", "")).removeprefix("id:")
        found = cloud.projects.get(wanted)
        return {"projects": [found]} if found else {}

    @route("GET", rf"{CRM}/v3/organizations:search")
    def orgs(call: Call) -> Any:
        return {"organizations": [{"name": ORG, "displayName": DOMAIN,
                                   "directoryCustomerId": "C0abc123", "state": "ACTIVE"}]}

    @route("GET", rf"{CRM}/v3/folders:search")
    def folders(call: Call) -> Any:
        return {}

    @route("GET", rf"{CRM}/v3/(organizations/\d+)")
    def org(call: Call) -> Any:
        if call.group(1) != ORG:
            return error(404, "NOT_FOUND", call.group(1))
        return {"name": ORG, "displayName": DOMAIN, "directoryCustomerId": "C0abc123", "state": "ACTIVE"}

    @route("POST", rf"{CRM}/v3/projects")
    def create_project(call: Call) -> Any:
        body = call.body or {}
        project = body["projectId"]
        if project in cloud.projects:
            return error(409, "ALREADY_EXISTS", f"project {project} already exists")
        cloud.projects[project] = {"name": f"projects/{cloud.number(project)}", "projectId": project,
                                   "parent": body.get("parent", ORG), "state": "ACTIVE",
                                   "displayName": body.get("displayName", project),
                                   "labels": body.get("labels", {})}
        cloud.services[project] = set()
        cloud.policies[f"projects/{project}"] = {
            "version": 1, "etag": "BwX1", "bindings": [{"role": "roles/owner", "members": [f"user:{world.installer}"]}]}
        return _operation(cloud, f"operations/cp.{cloud.number(project)}", cloud.projects[project])

    @route("GET", rf"{CRM}/v3/(operations/.+)")
    def operation(call: Call) -> Any:
        return cloud.operations.get(call.group(1)) or error(404, "NOT_FOUND", call.group(1))

    @route("GET", rf"{CRM}/v3/projects/([^/:]+)")
    def project(call: Call) -> Any:
        wanted = call.group(1)
        for found in cloud.projects.values():
            if wanted in (found["projectId"], found["name"].split("/")[1]):
                return found
        return error(403, "PERMISSION_DENIED", f"project {wanted} not found or denied")

    @route("POST", rf"{CRM}/v1/projects/([^/:]+):getAncestry")
    def ancestry(call: Call) -> Any:
        if call.group(1) not in cloud.projects:
            return error(403, "PERMISSION_DENIED", call.group(1))
        return {"ancestor": [{"resourceId": {"type": "project", "id": call.group(1)}},
                             {"resourceId": {"type": "organization", "id": ORG_ID}}]}

    @route("POST", rf"(?:{CRM}/v3|{BILLING})/(.+):testIamPermissions")
    def test_permissions(call: Call) -> Any:
        return {"permissions": list((call.body or {}).get("permissions", []))}

    @route("POST", rf"{CRM}/v1/projects/([^/:]+):getIamPolicy")
    def get_policy(call: Call) -> Any:
        return json.loads(json.dumps(cloud.policies.setdefault(f"projects/{call.group(1)}",
                                                               {"version": 1, "etag": "BwX0", "bindings": []})))

    @route("POST", rf"{CRM}/v1/projects/([^/:]+):setIamPolicy")
    def set_policy(call: Call) -> Any:
        policy = dict((call.body or {})["policy"])
        policy["etag"] = "BwX" + hashlib.sha1(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:6]
        cloud.policies[f"projects/{call.group(1)}"] = policy
        return policy

    @route("POST", rf"{CRM}/v1/(.+):getEffectiveOrgPolicy")
    def effective(call: Call) -> Any:
        return {"constraint": (call.body or {}).get("constraint"), "listPolicy": {}}

    # -- billing and budgets ----------------------------------------------------
    @route("GET", rf"{BILLING}/billingAccounts")
    def accounts(call: Call) -> Any:
        return {"billingAccounts": [{"name": f"billingAccounts/{BILLING_ACCOUNT}", "open": True,
                                     "displayName": "Acme billing"}]}

    @route("GET", rf"{BILLING}/projects/([^/]+)/billingInfo")
    def billing_info(call: Call) -> Any:
        account = cloud.billing.get(call.group(1))
        return {"name": f"projects/{call.group(1)}/billingInfo", "projectId": call.group(1),
                "billingAccountName": account or "", "billingEnabled": bool(account)}

    @route("PUT", rf"{BILLING}/projects/([^/]+)/billingInfo")
    def link_billing(call: Call) -> Any:
        cloud.billing[call.group(1)] = (call.body or {}).get("billingAccountName", "")
        return billing_info(call)

    @route("GET", rf"{BUDGETS}/billingAccounts/([^/]+)/budgets")
    def budgets(call: Call) -> Any:
        if not call.headers.get("x-goog-user-project"):
            return error(403, "PERMISSION_DENIED", "Your application is authenticating by using local Application "
                         "Default Credentials. The billingbudgets.googleapis.com API requires a quota project")
        return {"budgets": cloud.budgets.get(call.group(1), [])}

    @route("POST", rf"{BUDGETS}/billingAccounts/([^/]+)/budgets")
    def create_budget(call: Call) -> Any:
        if not call.headers.get("x-goog-user-project"):
            return error(403, "PERMISSION_DENIED", "billingbudgets.googleapis.com requires a quota project")
        made = dict(call.body or {})
        made["name"] = f"billingAccounts/{call.group(1)}/budgets/b{len(cloud.budgets.get(call.group(1), [])) + 1}"
        cloud.budgets.setdefault(call.group(1), []).append(made)
        return made

    # -- Service Usage ----------------------------------------------------------
    @route("GET", rf"{USAGE}/v1/projects/([^/]+)/services")
    def services(call: Call) -> Any:
        return {"services": [{"config": {"name": s}, "state": "ENABLED"}
                             for s in sorted(cloud.services.get(call.group(1), set()))]}

    @route("POST", rf"{USAGE}/v1/projects/([^/]+)/services:batchEnable")
    def enable(call: Call) -> Any:
        ids = list((call.body or {}).get("serviceIds", []))
        if len(ids) > 20:
            return error(400, "INVALID_ARGUMENT", "batchEnable takes at most 20 services")
        cloud.services.setdefault(call.group(1), set()).update(ids)
        return {"name": "operations/enable", "done": True}

    @route("POST", rf"{USAGE}/v1/projects/([^/]+)/services/([^/:]+):disable")
    def disable(call: Call) -> Any:
        cloud.services.setdefault(call.group(1), set()).discard(call.group(2))
        return {"name": "operations/disable", "done": True}

    @route("POST", rf"{USAGE}/v1beta1/projects/([^/]+)/services/([^/:]+):generateServiceIdentity")
    def identity(call: Call) -> Any:
        return {"name": "operations/gsi", "done": True,
                "response": {"email": f"service-{cloud.number(call.group(1))}@gcp-sa-{call.group(2).split('.')[0]}"
                                      ".iam.gserviceaccount.com"}}

    @route("GET", rf"{USAGE}/v1beta1/projects/([^/]+)/services/([^/]+)/consumerQuotaMetrics")
    def quotas(call: Call) -> Any:
        return {"metrics": []}

    # -- Compute ------------------------------------------------------------------
    @route("GET", rf"{COMPUTE}/projects/([^/]+)/regions/([^/]+)")
    def region(call: Call) -> Any:
        return {"name": call.group(2), "quotas": [{"metric": "CPUS", "limit": 200.0, "usage": 0.0},
                                                   {"metric": "SSD_TOTAL_GB", "limit": 4096.0, "usage": 0.0},
                                                   {"metric": "IN_USE_ADDRESSES", "limit": 64.0, "usage": 0.0}]}

    @route("GET", rf"{COMPUTE}/projects/([^/]+)/global/addresses/([^/]+)")
    def address(call: Call) -> Any:
        return cloud.addresses.get((call.group(1), call.group(2))) or error(404, "NOT_FOUND", call.group(2))

    @route("POST", rf"{COMPUTE}/projects/([^/]+)/global/addresses")
    def create_address(call: Call) -> Any:
        name = (call.body or {})["name"]
        n = len(cloud.addresses) + 10
        cloud.addresses[(call.group(1), name)] = {"name": name, "address": f"34.120.0.{n}", "status": "RESERVED"}
        return {"name": f"operation-address-{name}", "status": "DONE", "kind": "compute#operation"}

    @route("GET", rf"{COMPUTE}/projects/([^/]+)/global/sslCertificates/([^/]+)")
    def certificate(call: Call) -> Any:
        found = cloud.certificates.get((call.group(1), call.group(2)))
        if found is None:
            return error(404, "NOT_FOUND", call.group(2))
        return {**found, "managed": {"domains": found["domains"], "status": certificate_status(found)}}

    def certificate_status(found: dict[str, Any]) -> str:
        """Google provisions a managed certificate once it is attached and every
        domain resolves to the load balancer's address; ten minutes later it is ACTIVE."""
        address = cloud.addresses.get((found["project"], "swarm-ui-ip"), {}).get("address")
        if not all(world.resolve(d) == {address} for d in found["domains"]):
            found.pop("resolving_since", None)
            return "PROVISIONING"
        since = found.setdefault("resolving_since", world.clock.now())
        return "ACTIVE" if world.clock.now() - since >= 600 else "PROVISIONING"

    @route("GET", r"https://(" + world.hostname.replace(".", r"\.") + r")/?")
    def console(call: Call) -> Any:
        """The console's hostname: TLS only with an ACTIVE certificate, then IAP's sign-in redirect."""
        for (project, name), found in cloud.certificates.items():
            if call.group(1) in found["domains"] and certificate_status(found) == "ACTIVE":
                return Response(302, b"", "Found")
        import requests

        raise requests.exceptions.SSLError(f"no certificate is served for {call.group(1)} yet")

    @route("GET", rf"{COMPUTE}/projects/([^/]+)/global/operations/([^/]+)")
    def compute_op(call: Call) -> Any:
        return {"name": call.group(2), "status": "DONE"}

    # -- KMS, custom roles, Firestore admin -------------------------------------------
    KMS = "https://cloudkms.googleapis.com/v1"

    @route("GET", rf"{KMS}/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+)")
    def key_ring(call: Call) -> Any:
        return cloud.key_rings.get(call.group(1)) or error(404, "NOT_FOUND", call.group(1))

    @route("GET", rf"{KMS}/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+)")
    def crypto_key(call: Call) -> Any:
        return cloud.crypto_keys.get(call.group(1)) or error(404, "NOT_FOUND", call.group(1))

    @route("GET", rf"{IAM}/projects/([^/]+)/roles")
    def roles(call: Call) -> Any:
        return {"roles": [r for name, r in sorted(cloud.roles.items()) if name.startswith(f"projects/{call.group(1)}/")]}

    @route("GET", r"https://firestore\.googleapis\.com/v1/projects/([^/]+)/databases/([^/]+)")
    def database(call: Call) -> Any:
        return cloud.firestore.get(f"{call.group(1)}/{call.group(2)}") or error(404, "NOT_FOUND", call.group(2))

    @route("POST", rf"{IAM}/iamPolicies:lintPolicy")
    def lint(call: Call) -> Any:
        return {"lintResults": []}

    # -- IAM ------------------------------------------------------------------------
    @route("POST", rf"{IAM}/projects/-/serviceAccounts/([^/:]+):testIamPermissions")
    def sa_permissions(call: Call) -> Any:
        email = call.group(1)
        if email not in cloud.accounts:
            return error(404, "NOT_FOUND", email)
        held = []
        for permission in (call.body or {}).get("permissions", []):
            if permission == "iam.serviceAccounts.getAccessToken" and world.may_impersonate(world.principal, email):
                held.append(permission)
            elif permission == "iam.serviceAccounts.actAs" and (world.principal, email) in cloud.act_as:
                held.append(permission)
        return {"permissions": held}

    @route("GET", rf"{IAM}/projects/-/serviceAccounts/([^/:]+)")
    def account(call: Call) -> Any:
        return cloud.accounts.get(call.group(1)) or error(404, "NOT_FOUND", call.group(1))

    # -- Storage ----------------------------------------------------------------------
    @route("GET", rf"{STORAGE}/b/([^/]+)")
    def bucket(call: Call) -> Any:
        found = world.buckets.get(call.group(1))
        return found if found is not None else error(404, "NOT_FOUND", call.group(1))

    @route("POST", rf"{STORAGE}/b")
    def create_bucket(call: Call) -> Any:
        body = dict(call.body or {})
        if body["name"] in world.buckets:
            return error(409, "CONFLICT", "You already own this bucket")
        body["projectNumber"] = cloud.number(call.params["project"])
        world.buckets[body["name"]] = body
        return body

    # -- IAP ----------------------------------------------------------------------------
    @route("GET", rf"{IAP}/projects/(\d+)/brands")
    def brands(call: Call) -> Any:
        return {"brands": cloud.brands.get(call.group(1), [])}

    # -- Secret Manager -------------------------------------------------------------------
    @route("GET", rf"{SECRET_MANAGER}/projects/([^/]+)/secrets/([^/:]+)")
    def secret(call: Call) -> Any:
        return cloud.secrets.get((call.group(1), call.group(2))) or error(404, "NOT_FOUND", call.group(2))

    @route("POST", rf"{SECRET_MANAGER}/projects/([^/]+)/secrets")
    def create_secret(call: Call) -> Any:
        key = (call.group(1), call.params["secretId"])
        if key in cloud.secrets:
            return error(409, "ALREADY_EXISTS", key[1])
        cloud.secrets[key] = {"name": f"projects/{cloud.number(key[0])}/secrets/{key[1]}",
                              "labels": (call.body or {}).get("labels", {})}
        return cloud.secrets[key]

    @route("GET", rf"{SECRET_MANAGER}/projects/([^/]+)/secrets/([^/:]+)/versions")
    def versions(call: Call) -> Any:
        found = cloud.versions.get((call.group(1), call.group(2)), [])
        return {"versions": [{"name": f"projects/{cloud.number(call.group(1))}/secrets/{call.group(2)}/versions/{n}",
                              "state": "ENABLED"} for n in range(1, len(found) + 1)]}

    @route("POST", rf"{SECRET_MANAGER}/projects/([^/]+)/secrets/([^/:]+):addVersion")
    def add_version(call: Call) -> Any:
        key = (call.group(1), call.group(2))
        if key not in cloud.secrets:
            return error(404, "NOT_FOUND", f"Secret [{key[1]}] not found")
        data = base64.b64decode((call.body or {})["payload"]["data"])
        cloud.versions.setdefault(key, []).append(data)
        return {"name": f"projects/{cloud.number(key[0])}/secrets/{key[1]}/versions/{len(cloud.versions[key])}"}

    @route("GET", rf"{SECRET_MANAGER}/projects/([^/]+)/secrets/([^/:]+)/versions/(latest|\d+):access")
    def access(call: Call) -> Any:
        found = cloud.versions.get((call.group(1), call.group(2)), [])
        if not found:
            return error(404, "NOT_FOUND", call.group(2))
        n = len(found) if call.group(3) == "latest" else int(call.group(3))
        return {"payload": {"data": base64.b64encode(found[n - 1]).decode()}}

    # -- Artifact Registry ---------------------------------------------------------------------
    @route("GET", r"https://artifactregistry\.googleapis\.com/v1/projects/([^/]+)/locations/([^/]+)/repositories/"
                  r"([^/]+)/dockerImages/(.+)")
    def docker_image(call: Call) -> Any:
        from urllib.parse import unquote

        repo = f"projects/{call.group(1)}/locations/{call.group(2)}/repositories/{call.group(3)}"
        image = f"{call.group(2)}-docker.pkg.dev/{call.group(1)}/{call.group(3)}/{unquote(call.group(4))}"
        if repo not in world.registries or image not in world.images:
            return error(404, "NOT_FOUND", image)
        return {"name": f"{repo}/dockerImages/{call.group(4)}", "uri": image}

    # -- DNS: no Cloud DNS zone covers the hostname (an outside registrar) --------------------
    @route("GET", r"https://dns\.googleapis\.com/dns/v1/projects/([^/]+)/managedZones")
    def zones(call: Call) -> Any:
        return {"managedZones": []}

    @route("GET", r"https://dns\.google/resolve")
    def resolve(call: Call) -> Any:
        found = sorted(world.resolve(str(call.params.get("name", "")).rstrip(".")))
        if not found:
            return {"Status": 3}
        return {"Status": 0, "Answer": [{"name": call.params["name"], "type": 1, "TTL": 300, "data": ip}
                                        for ip in found]}

    # -- Cloud Identity Groups (billed to a quota project with user credentials) ------------------
    CI = "https://cloudidentity.googleapis.com/v1"

    def billed(call: Call) -> Response | None:
        if not call.headers.get("x-goog-user-project"):
            return error(403, "PERMISSION_DENIED", "cloudidentity.googleapis.com needs a quota project with user "
                         "credentials")
        return None

    @route("GET", rf"{CI}/groups:lookup")
    def group_lookup(call: Call) -> Any:
        refused = billed(call)
        if refused:
            return refused
        found = cloud.groups.get(str(call.params.get("groupKey.id")))
        return {"name": found["name"]} if found else error(404, "NOT_FOUND", "group not found")

    @route("POST", rf"{CI}/groups")
    def group_create(call: Call) -> Any:
        refused = billed(call)
        if refused:
            return refused
        body = dict(call.body or {})
        email = body["groupKey"]["id"]
        if email in cloud.groups:
            return error(409, "ALREADY_EXISTS", email)
        cloud.groups[email] = {"name": f"groups/g{len(cloud.groups) + 1}", **body}
        cloud.members[email] = {world.installer}
        return {"done": True, "response": cloud.groups[email]}

    def group_email(name: str) -> str | None:
        return next((e for e, g in cloud.groups.items() if g["name"] == name), None)

    @route("GET", rf"{CI}/(groups/[^/]+)/memberships")
    def memberships(call: Call) -> Any:
        email = group_email(call.group(1))
        if email is None:
            return error(404, "NOT_FOUND", call.group(1))
        return {"memberships": [{"preferredMemberKey": {"id": m}, "roles": [{"name": "MEMBER"}]}
                                for m in sorted(cloud.members.get(email, set()))]}

    @route("GET", rf"{CI}/(groups/[^/]+)/memberships:checkTransitiveMembership")
    def check_membership(call: Call) -> Any:
        email = group_email(call.group(1))
        member = str(call.params.get("query", "")).split("'")[1] if "'" in str(call.params.get("query", "")) else ""
        return {"hasMembership": bool(email and member in cloud.members.get(email, set()))}

    @route("GET", rf"{CI}/(groups/[^/]+)/memberships:lookup")
    def membership_lookup(call: Call) -> Any:
        email = group_email(call.group(1))
        member = str(call.params.get("memberKey.id", ""))
        if email and member in cloud.members.get(email, set()):
            return {"name": f"{call.group(1)}/memberships/{member}"}
        return error(404, "NOT_FOUND", member)

    # -- the Directory API (Admin SDK): the Directory scopes, else 403 --------------------------
    ADMIN = "https://admin.googleapis.com/admin/directory/v1"

    def scoped(call: Call) -> Response | None:
        if world.principal == world.installer and not any("admin.directory" in sc for sc in cloud.adc_scopes):
            return error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.")
        return None

    @route("GET", rf"{ADMIN}/customers/my_customer")
    def customer(call: Call) -> Any:
        return scoped(call) or {"id": "C0abc123", "customerDomain": DOMAIN}

    @route("GET", rf"{ADMIN}/customer/([^/]+)/roles")
    def admin_roles(call: Call) -> Any:
        return scoped(call) or {"items": cloud.admin_roles}

    @route("GET", rf"{ADMIN}/customer/([^/]+)/roleassignments")
    def role_assignments(call: Call) -> Any:
        return scoped(call) or {"items": [a for a in cloud.role_assignments
                                          if a["roleId"] == call.params.get("roleId", a["roleId"])]}

    @route("POST", rf"{ADMIN}/customer/([^/]+)/roleassignments")
    def assign(call: Call) -> Any:
        refused = scoped(call)
        if refused:
            return refused
        made = {**dict(call.body or {}), "roleAssignmentId": str(len(cloud.role_assignments) + 1)}
        cloud.role_assignments.append(made)
        return made

    # -- IAM custom roles and service-account policies; Cloud Run services -------------------------
    @route("GET", rf"{IAM}/projects/([^/]+)/roles/([^/]+)")
    def custom_role(call: Call) -> Any:
        return cloud.roles.get(f"projects/{call.group(1)}/roles/{call.group(2)}") or error(404, "NOT_FOUND",
                                                                                           call.group(2))

    @route("POST", rf"{IAM}/projects/-/serviceAccounts/([^/:]+):getIamPolicy")
    def sa_policy(call: Call) -> Any:
        email = call.group(1)
        if email in cloud.account_policies:  # an authoritative account policy (qa_identity.tf)
            return {"bindings": cloud.account_policies[email], "etag": "BwE2E"}
        creators = sorted(m for (m, sa) in cloud.token_creators if sa == email)
        users = sorted(m for (m, sa) in cloud.act_as if sa == email)
        bindings = []
        if creators:
            bindings.append({"role": "roles/iam.serviceAccountTokenCreator", "members": [_member(m) for m in creators]})
        if users:
            bindings.append({"role": "roles/iam.serviceAccountUser", "members": [_member(m) for m in users]})
        return {"bindings": bindings, "etag": "BwE2E"}

    @route("GET", r"https://run\.googleapis\.com/v2/projects/([^/]+)/locations/([^/]+)/services")
    def run_list(call: Call) -> Any:
        return {"services": list(cloud.run_services.values())}

    @route("GET", r"https://run\.googleapis\.com/v2/(projects/[^/]+/locations/[^/]+/(?:services|jobs)/[^/:]+)"
                  r":getIamPolicy")
    def run_policy(call: Call) -> Any:
        return cloud.run_policies.get(call.group(1)) or {"etag": "BwE2E"}

    @route("GET", r"https://cloudscheduler\.googleapis\.com/v1/projects/([^/]+)/locations/([^/]+)/jobs")
    def scheduler_jobs(call: Call) -> Any:
        return {"jobs": list(cloud.scheduler_jobs.values())}

    knative = r"https://([a-z0-9-]+)-run\.googleapis\.com/apis/serving\.knative\.dev/v1/namespaces/([^/]+)"

    @route("GET", rf"{knative}/services")
    def knative_services(call: Call) -> Any:
        return {"items": [{"metadata": {"name": name},
                           "status": {"latestReadyRevisionName": s["latestReadyRevision"],
                                      "traffic": [{"latestRevision": True, "percent": 100}]}}
                          for name, s in cloud.run_services.items()]}

    @route("GET", rf"{knative}/revisions/([^/]+)")
    def knative_revision(call: Call) -> Any:
        return cloud.revisions.get(call.group(3)) or error(404, "NOT_FOUND", call.group(3))

    monitoring = r"https://monitoring\.googleapis\.com/v3/projects/([^/]+)"

    @route("GET", rf"{monitoring}/notificationChannels")
    def channels(call: Call) -> Any:
        return {"notificationChannels": cloud.channels}

    @route("GET", rf"{monitoring}/metricDescriptors")
    def descriptors(call: Call) -> Any:
        wanted = str((call.params or {}).get("filter", "")).partition('starts_with("')[2].rstrip('")')
        return {"metricDescriptors": [{"type": t} for t in cloud.descriptors if t.startswith(wanted)]}

    @route("GET", rf"{monitoring}/alertPolicies")
    def alert_policies(call: Call) -> Any:
        return {"alertPolicies": cloud.alert_policies}

    @route("GET", r"https://compute\.googleapis\.com/compute/v1/(projects/[^/]+/regions/[^/]+/subnetworks/[^/]+)")
    def subnetwork(call: Call) -> Any:
        return cloud.subnetworks.get(call.group(1)) or error(404, "NOT_FOUND", call.group(1))

    @route("GET", r"https://run\.googleapis\.com/v2/projects/([^/]+)/locations/([^/]+)/services/([^/]+)")
    def run_service(call: Call) -> Any:
        return cloud.run_services.get(call.group(3)) or error(404, "NOT_FOUND", call.group(3))

    # -- the release home (public objects) ---------------------------------------------------
    @route("GET", rf"{RELEASE_HOME}/(.+)")
    def release(call: Call) -> Any:
        found = cloud.releases.get(call.group(1))
        return Response(200, found, "OK") if found is not None else error(404, "NOT_FOUND", call.group(1))

    @route("GET", r"https://(api\.ipify\.org|ipv4\.icanhazip\.com|checkip\.amazonaws\.com|ifconfig\.me)(/.*)?")
    def my_ip(call: Call) -> Any:
        return Response(200, (INSTALLER_IP + "\n").encode(), "OK")
