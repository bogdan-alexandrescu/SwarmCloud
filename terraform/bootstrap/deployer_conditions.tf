# THE DEPLOYER'S REMAINING PROJECT-LEVEL ROLES, SCOPED -- WRITTEN, NOT APPLIED.
#
# Sixteen predefined roles and one custom role reach the CI deployer as
# unconditioned project-level grants, in a project that holds another team's
# production. This file gives every one of them that CAN carry a working
# condition a resource block of its own, and says -- with the live listing it
# was checked against -- why each of the others cannot.
#
# NOTHING HERE TAKES EFFECT ON MERGE, and that is the point of the design.
# Every block below has `count = 0` until its role is named in
# `deployer_scoped_roles` (terraform.tfvars). Naming one does two things in the
# SAME plan: it removes that role from `deployer_roles`' project-wide for_each
# (wif.tf) and creates the conditioned grant here. The plan for one release is
# therefore exactly one destroy and one create, for one role:
#
#     - google_project_iam_member.deployer_roles["roles/container.admin"]
#     + google_project_iam_member.deployer_container_admin[0]
#
# A conditioned grant next to an unconditioned one for the same role does
# nothing at all ("Conditional role bindings do not override role bindings with
# no conditions", docs.cloud.google.com/iam/docs/conditions-overview), which is
# why the two halves are one switch and not two resources applied at different
# times.
#
# ONE PER RELEASE. Apply bootstrap between releases, never during one, and let
# the next release's plan -- which refreshes every managed resource -- be the
# proof. REVERTING IS REMOVING THE ROLE FROM THE LIST AND APPLYING AGAIN.
#
# ---------------------------------------------------------------------------
# THREE RULES EVERY CONDITION HERE FOLLOWS, and the failure behind each.
# ---------------------------------------------------------------------------
#
# 1. ONLY RESOURCE-NAME FORMATS GOOGLE DOCUMENTS. The iap.admin condition in
#    this directory was written from the shape of an error message and matched
#    nothing: IAP names a backend by project NUMBER and numeric id (PR #23,
#    release 35972131246). IAP is absent from the format table in
#    docs.cloud.google.com/iam/docs/conditions-resource-attributes; every
#    format used below is copied from that table, which also says Secret
#    Manager uses the project NUMBER and Compute, GKE, Firestore and Logging
#    use the project ID.
#
# 2. A TYPE GUARD IN FRONT OF EVERY NAME TEST. "If part of a condition uses an
#    attribute that isn't available for a resource, then that part of the
#    condition is never interpreted as granting access"
#    (docs.cloud.google.com/iam/docs/conditions-attribute-reference). A bare
#    `resource.name.startsWith(...)` therefore also refuses every permission in
#    the role that is checked on something IAM cannot name -- a list on the
#    project, an operation, a network. The documented remedy, used verbatim in
#    the IAM docs' own examples, is
#
#        (resource.type != A && resource.type != B) || resource.name.startsWith(P)
#
#    which "grants the role regardless of the resource name" for every type
#    other than A and B. So each condition names the types it governs and
#    leaves the rest of the role alone.
#
#    WHAT IS NOT MEASURED: that `resource.type != X` is true for a resource
#    type IAM's table does not list (a compute Network, a GKE operation). The
#    documentation says so in prose and in two worked examples; nobody has
#    watched it happen in this project. If it is wrong, the failure is loud and
#    immediate -- the very next plan refreshes every managed resource and 403s
#    on the first one it cannot read -- and the revert is one line.
#
# 3. AT MOST 12 LOGICAL OPERATORS PER EXPRESSION. The limit is in
#    docs.cloud.google.com/iam/quotas ("Logic operators in a role binding's
#    condition expression: 12"). Exceeding it fails at apply, never at plan,
#    so tests/terraform/deployer_iam.tftest.hcl counts them.
#
# ---------------------------------------------------------------------------
# WHAT WAS MEASURED. Every listing below is read-only gcloud against
# saga-agents-staging on 2026-09-24 (list/describe/get only), and the "ours"
# column is cross-checked against the resource names in terraform/infra's state
# (gs://swarm-tfstate-saga-agents-staging/infra/dev/default.tfstate, names
# only). tests/terraform/deployer_iam.tftest.hcl replays every listed name
# against the prefixes below: each of ours admitted, each of theirs refused.
# ---------------------------------------------------------------------------

locals {
  # Secret Manager names a secret `projects/<NUMBER>/secrets/<id>` in a
  # condition, and "You can't substitute the project ID for the project number"
  # (resource attributes page, above). Read from the project rather than
  # pasted, as wif.tf already does for the Cloud Build service account.
  deployer_project_number = var.enable_github_wif ? data.google_project.this[0].number : ""

  # role -> the resource types the condition governs, and the name prefixes it
  # admits within them. Rendered into CEL once, below; the tests read these
  # lists back so the property they check is the one the expression encodes.
  deployer_type_scoped = {

    # -----------------------------------------------------------------------
    # roles/compute.networkAdmin -- their Keycloak and ArgoCD load balancer.
    #
    # IAM names only these Compute types, so only they can be scoped. The
    # listing, per type (global unless a zone is given):
    #
    #   backend services   ours    swarm-ui-backend, swarm-ui-ui-backend
    #                      theirs  gkegw1-7hi6-{api-service, argocd,
    #                              browser-engine, crawling-service,
    #                              gw-serve404, gw-serve500, keycloak,
    #                              promptlab-api, promptlab-web}-...  (9)
    #   forwarding rules   ours    swarm-ui-http, swarm-ui-https
    #                      theirs  gkegw1-7hi6-gateway-system-external-https-...
    #   target HTTP px     ours    swarm-ui-http-proxy
    #   target HTTPS px    ours    swarm-ui-https-proxy
    #                      theirs  gkegw1-7hi6-gateway-system-external-https-...
    #   target SSL/TCP px  none
    #   instances          theirs  gke-agents-staging-nap-e2-standard-2--b59db5d4-
    #                              {bnof, h2ug, xstq}  (us-central1-a; their GKE
    #                              nodes -- Autopilot's are not project VMs)
    #
    # ADMITS all six of ours; REFUSES all fourteen of theirs, including
    # backendServices.update/delete/setSecurityPolicy on the one routing their
    # Keycloak. Regional prefixes are absent because nothing of ours is
    # regional in these types.
    #
    # STAYS PROJECT-WIDE, because IAM gives these types no name: networks
    # (agents-staging-vpc, default, swarm-vpc), 46 subnetworks, routers
    # (staging-nat-router is theirs), 7 addresses, 3 URL maps, 9 NEGs and 9
    # health checks. This condition narrows the role; it does not make it safe.
    #
    # 7 && + 4 || = 11 operators, one under the limit. Adding a ninth type or a
    # fifth prefix needs a second binding, not a longer expression.
    # -----------------------------------------------------------------------
    "roles/compute.networkAdmin" = {
      types = [
        "compute.googleapis.com/BackendService",
        "compute.googleapis.com/ForwardingRule",
        "compute.googleapis.com/GlobalForwardingRule",
        "compute.googleapis.com/TargetHttpProxy",
        "compute.googleapis.com/TargetHttpsProxy",
        "compute.googleapis.com/TargetSslProxy",
        "compute.googleapis.com/TargetTcpProxy",
        "compute.googleapis.com/Instance",
      ]
      prefixes = [
        "projects/${var.project_id}/global/backendServices/${var.name_prefix}-",
        "projects/${var.project_id}/global/forwardingRules/${var.name_prefix}-",
        "projects/${var.project_id}/global/targetHttpProxies/${var.name_prefix}-",
        "projects/${var.project_id}/global/targetHttpsProxies/${var.name_prefix}-",
      ]
    }

    # -----------------------------------------------------------------------
    # roles/compute.securityAdmin -- their GKE firewalls.
    #
    #   firewalls   ours    swarm-fw-allow-gke-webhooks,
    #                       swarm-fw-allow-health-checks, swarm-fw-allow-internal,
    #                       swarm-fw-deny-all-ingress,
    #                       swarm-fw-deny-worker-ingress  (5, terraform/infra)
    #               GKE's   gke-swarm-autopilot-4b0b02c7-{all, vms}
    #               theirs  default-allow-{icmp, internal, rdp, ssh},
    #                       gke-agents-staging-a2455d7c-{all, exkubelet,
    #                       inkubelet, vms}, gkegw1-7hi6-l7-agents-staging-vpc-global
    #   instances   theirs  the three gke-agents-staging-nap-* nodes above
    #
    # ADMITS all five of ours. REFUSES all nine of theirs and all three of
    # their nodes (updateSecurity, setShieldedInstanceIntegrityPolicy). Also
    # refuses the two gke-swarm-autopilot-* rules, correctly: GKE's own service
    # agent creates and reconciles those, terraform does not manage them, and a
    # CI run editing them would be fighting GKE.
    #
    # STAYS PROJECT-WIDE: SSL certificates (swarm-ui-cert is ours, none
    # theirs), security policies, SSL policies and firewall policies (none
    # exist). 2 operators.
    # -----------------------------------------------------------------------
    "roles/compute.securityAdmin" = {
      types = [
        "compute.googleapis.com/Firewall",
        "compute.googleapis.com/Instance",
      ]
      prefixes = [
        "projects/${var.project_id}/global/firewalls/${var.name_prefix}-",
      ]
    }

    # -----------------------------------------------------------------------
    # roles/container.admin -- their live cluster.
    #
    #   clusters   ours    swarm-autopilot  (us-central1)
    #              theirs  agents-staging   (us-central1-a, zonal)
    #
    # ADMITS every cluster-level call on swarm-autopilot and its node pools
    # (the prefix covers `.../clusters/swarm-autopilot/...`). REFUSES
    # container.clusters.{get, update, delete, getCredentials} on
    # agents-staging under either of its names (`zones/` or `locations/`).
    # clusters.list and operations are checked on the project, not on a
    # Cluster, so the type guard lets them through.
    #
    # WHAT THIS DOES NOT SCOPE, measured rather than assumed: the Kubernetes API
    # inside a cluster. docs/incidents/2026-09-24-gke-dispatch.md (cause 4)
    # found that a name condition on the cluster path does not authorise a
    # NAMESPACED request, because the resource in that check is the namespaced
    # object. What IAM reports as that object's resource.type is undocumented,
    # so under this condition CI's in-cluster authority over agents-staging is
    # either unchanged (a non-Cluster type passes the guard) or gone in both
    # clusters (an unavailable type grants nothing). Neither breaks a release:
    # terraform/infra uses only the google provider and CI runs no kubectl
    # except in status.sh, whose failure is `|| true`. The real fix for the
    # Kubernetes layer is a narrower role (container.clusterAdmin), which is a
    # decision about what CI is for, not a condition. 1 operator.
    # -----------------------------------------------------------------------
    "roles/container.admin" = {
      types = [
        "container.googleapis.com/Cluster",
      ]
      prefixes = [
        "projects/${var.project_id}/locations/${var.region}/clusters/${var.name_prefix}-",
      ]
    }

    # -----------------------------------------------------------------------
    # roles/datastore.owner -- a second database, when one appears.
    #
    #   databases   ours    projects/saga-agents-staging/databases/swarm
    #               theirs  none
    #
    # REFUSES NOTHING TODAY. It exists for the case modules/iam/bindings.tf
    # warns about in capitals -- anyone creating a second Firestore database
    # here -- so that CI could not delete, export or rewrite it.
    #
    # THE DATA PLANE. bindings.tf records that a Firestore condition denied
    # every document read on 2026-09-16 and concludes that Firestore does not
    # evaluate conditions on the data plane. Firestore's own documentation says
    # the opposite (docs.cloud.google.com/firestore/native/docs/manage-databases,
    # "Configure per-database access permissions": `resource.name ==
    # "projects/<p>/databases/<db>"` on roles/datastore.user, "enforced when
    # accessing databases ... with the REST API or the client libraries"), and
    # the condition measured then tested `resource.type ==
    # "datastore.googleapis.com/Database"` -- a type IAM's table does not list;
    # the documented one is firestore.googleapis.com/Database. That condition
    # could never have been true. This one uses the documented type, and the
    # type guard means a data-plane request that reports some OTHER type is let
    # through rather than refused: if the guess about the data plane is wrong,
    # this fails open on protection, not closed on availability. 1 operator.
    # -----------------------------------------------------------------------
    "roles/datastore.owner" = {
      types = [
        "firestore.googleapis.com/Database",
      ]
      prefixes = [
        "projects/${var.project_id}/databases/${var.name_prefix}",
      ]
    }

    # -----------------------------------------------------------------------
    # roles/logging.configWriter -- the project's shared log buckets.
    #
    #   log buckets   _Default, _Required  (global; one per PROJECT, so shared
    #                 with the other team -- neither is ours)
    #   log views     _AllLogs, _Default   (on _Default)
    #   log metrics   swarm/{attempt-oom-near-miss, attempt-peak-rss-bytes,
    #                 checkpoint-completed, dead-lettered, generation-fenced,
    #                 lease-released, parked, quota-exhausted, starting}  (ours)
    #
    # terraform/infra manages log-based METRICS only, which are neither buckets
    # nor views. So this refuses every bucket and view operation outright --
    # no retention change to _Default, no view exposing it, no linked dataset
    # -- and admits nothing, because there is nothing of ours to admit.
    # logging.buckets.list is checked on the location, not a bucket, and passes.
    #
    # STAYS PROJECT-WIDE, and it is the larger half: sinks and exclusions. IAM
    # cannot name them, and a sink can route every log in the project,
    # including theirs, anywhere. 1 operator.
    # -----------------------------------------------------------------------
    "roles/logging.configWriter" = {
      types = [
        "logging.googleapis.com/LogBucket",
        "logging.googleapis.com/LogView",
      ]
      prefixes = []
    }

    # -----------------------------------------------------------------------
    # swarmSecretProvisioner (custom, wif.tf) -- their 63 secrets.
    #
    #   secrets   ours    swarm-tenant-{eng-anthropic, eng-openai,
    #                     u-bogdan-anthropic}[-refresh]            (6, terraform)
    #                     swarm-account-eng--{devops-main, devops-team,
    #                     saga-personal, team}[-refresh]    (8, the quota broker)
    #             theirs  agents-*  (57: keycloak admin password, argocd repo
    #                     SSH key, every service's database password, ...)
    #                     promptlab-*  (6: db-dsn, fernet-key, ...)
    #
    # ADMITS all fourteen of ours. REFUSES all sixty-three of theirs.
    #
    # THIS IS THE ONE THAT MATTERS MOST, and the reason is not obvious from
    # the role's description. swarmSecretProvisioner withholds
    # secretmanager.versions.access so that CI cannot read a payload -- but it
    # holds secretmanager.secrets.setIamPolicy project-wide, so today CI can
    # bind ITSELF secretAccessor on agents-keycloak-admin-password and then
    # read it. Scoping setIamPolicy to our secrets is what makes the withheld
    # permission mean anything for theirs. (For ours it cannot: managing a
    # secret's readers is the job.)
    #
    # secrets.create and secrets.list are checked on the project, which the type
    # guard admits: CI can create a secret of any name, and cannot touch one it
    # did not create. 2 operators.
    # -----------------------------------------------------------------------
    "swarmSecretProvisioner" = {
      types = [
        "secretmanager.googleapis.com/Secret",
        "secretmanager.googleapis.com/SecretVersion",
      ]
      prefixes = [
        "projects/${local.deployer_project_number}/secrets/${var.name_prefix}-",
      ]
    }
  }

  # -------------------------------------------------------------------------
  # roles/resourcemanager.projectIamAdmin -- CI can grant itself roles/owner.
  #
  # A project has one name, so resource.name cannot scope this. What can is the
  # attribute IAM provides for exactly this role: the list of roles a
  # setIamPolicy call MODIFIES (`iam.googleapis.com/modifiedGrantsByRole`,
  # recognised by Resource Manager for project policies). With `hasOnly`, CI
  # may add or remove members of the roles below and of no other role. A call
  # that modifies nothing -- every getIamPolicy -- sees `[]`, which passes.
  #
  # THE LIST IS EVERY PROJECT-LEVEL ROLE terraform/infra GRANTS, read from the
  # code and checked against its state on 2026-09-24:
  #
  #   modules/iam/bindings.tf  plain      logging.logWriter, monitoring.metricWriter,
  #                                       cloudtrace.agent, serviceusage.serviceUsageConsumer,
  #                                       monitoring.viewer, swarmJobDispatcher,
  #                                       swarmJobReaper, swarmSecretLister
  #                            gke        swarmGkeDispatcher, swarmGkeReaper
  #                            firestore  datastore.user
  #   modules/iam/custom_roles.tf         secretmanager.secretVersionAdder (the
  #                                       broker's scoped grant, I4)
  #   modules/tenancy/main.tf             swarmTenantWorkerFirestore + the three
  #                                       telemetry roles
  #   infra/verify.tf                     datastore.viewer, run.viewer
  #
  # tests/terraform/deployer_iam.tftest.hcl plans those modules and fails if
  # any of them grants a role missing from this list -- the failure that would
  # otherwise arrive as a 403 in the middle of a release.
  #
  # REFUSES 51 of the 65 roles in the live project policy, among them
  # roles/owner, roles/editor, roles/container.admin, roles/secretmanager.admin,
  # roles/iam.serviceAccountUser and roles/storage.admin -- every role CI does
  # not itself hand out. The deployer's own roles are among the refused: they
  # are granted by this root, which the owner applies, never by CI.
  #
  # NOT SCOPED BY THIS: two of the listed roles also have a member that is not
  # ours -- roles/logging.logWriter and roles/monitoring.metricWriter are held
  # by their staging-gke-nodes service account -- so CI could still revoke
  # those two grants. hasOnly limits WHICH roles, never whose. Nor does it
  # touch Principal Access Boundary bindings (createPolicyBinding and friends
  # are not setIamPolicy calls). 0 operators.
  # -------------------------------------------------------------------------
  deployer_grantable_project_roles = concat(
    [
      "roles/cloudtrace.agent",
      "roles/datastore.user",
      "roles/datastore.viewer",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
      "roles/monitoring.viewer",
      "roles/run.viewer",
      "roles/secretmanager.secretVersionAdder",
      "roles/serviceusage.serviceUsageConsumer",
    ],
    [
      # terraform/infra builds these as projects/<p>/roles/<id><suffix>; dev
      # sets no custom_role_suffix. An environment that does must add its
      # suffixed ids here, or its first apply after this role is scoped 403s.
      for id in [
        "swarmGkeDispatcher",
        "swarmGkeReaper",
        "swarmJobDispatcher",
        "swarmJobReaper",
        "swarmSecretLister",
        "swarmTenantWorkerFirestore",
      ] : "projects/${var.project_id}/roles/${id}"
    ],
  )

  deployer_conditions = merge(
    {
      for role, s in local.deployer_type_scoped : role => join(" || ", concat(
        ["(${join(" && ", [for t in s.types : "resource.type != \"${t}\""])})"],
        [for p in s.prefixes : "resource.name.startsWith(\"${p}\")"],
      ))
    },
    {
      "roles/resourcemanager.projectIamAdmin" = "api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([${join(", ", [for r in local.deployer_grantable_project_roles : "\"${r}\""])}])"
    },
  )

  # role -> whether its conditioned grant replaces the project-wide one.
  deployer_scoped = {
    for role in keys(local.deployer_conditions) :
    role => var.enable_github_wif && contains(var.deployer_scoped_roles, role)
  }
}

resource "google_project_iam_member" "deployer_network_admin" {
  count = local.deployer_scoped["roles/compute.networkAdmin"] ? 1 : 0

  project = var.project_id
  role    = "roles/compute.networkAdmin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm load balancer resources only"
    description = "Backend services, forwarding rules, target proxies and VMs are refused unless swarm-prefixed; nine of the eleven backend services here are another team's, Keycloak and ArgoCD among them."
    expression  = local.deployer_conditions["roles/compute.networkAdmin"]
  }
}

resource "google_project_iam_member" "deployer_security_admin" {
  count = local.deployer_scoped["roles/compute.securityAdmin"] ? 1 : 0

  project = var.project_id
  role    = "roles/compute.securityAdmin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm firewall rules only"
    description = "Firewall rules and VMs are refused unless swarm-prefixed; nine rules and three GKE nodes in this project belong to another team."
    expression  = local.deployer_conditions["roles/compute.securityAdmin"]
  }
}

resource "google_project_iam_member" "deployer_container_admin" {
  count = local.deployer_scoped["roles/container.admin"] ? 1 : 0

  project = var.project_id
  role    = "roles/container.admin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm clusters only"
    description = "Cluster-level calls on agents-staging, another team's live cluster, are refused. Does not scope the Kubernetes API inside a cluster."
    expression  = local.deployer_conditions["roles/container.admin"]
  }
}

resource "google_project_iam_member" "deployer_datastore_owner" {
  count = local.deployer_scoped["roles/datastore.owner"] ? 1 : 0

  project = var.project_id
  role    = "roles/datastore.owner"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm Firestore database only"
    description = "Any Firestore database not named for the platform is refused. Only `swarm` exists today; this is for the next one."
    expression  = local.deployer_conditions["roles/datastore.owner"]
  }
}

resource "google_project_iam_member" "deployer_logging_config_writer" {
  count = local.deployer_scoped["roles/logging.configWriter"] ? 1 : 0

  project = var.project_id
  role    = "roles/logging.configWriter"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "no log bucket or view administration"
    description = "The project's _Default and _Required log buckets are shared with another team and terraform manages neither. Log-based metrics, sinks and exclusions are unaffected."
    expression  = local.deployer_conditions["roles/logging.configWriter"]
  }
}

resource "google_project_iam_member" "deployer_project_iam_admin" {
  count = local.deployer_scoped["roles/resourcemanager.projectIamAdmin"] ? 1 : 0

  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "only the roles terraform infra grants"
    description = "A project policy change may add or remove members of the listed roles and no others, so CI cannot grant itself or anyone else owner, editor or another team's roles."
    expression  = local.deployer_conditions["roles/resourcemanager.projectIamAdmin"]
  }
}

resource "google_project_iam_member" "deployer_secrets_scoped" {
  count = local.deployer_scoped["swarmSecretProvisioner"] ? 1 : 0

  project = var.project_id
  role    = "projects/${var.project_id}/roles/${google_project_iam_custom_role.secret_provisioner[0].role_id}"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm secrets only"
    description = "63 of the 77 secrets in this project belong to another team. Refusing setIamPolicy on them is what stops CI granting itself read access to their payloads."
    expression  = local.deployer_conditions["swarmSecretProvisioner"]
  }
}

# ---------------------------------------------------------------------------
# THE TEN THAT STAY UNCONDITIONED, and why each one cannot be scoped here.
#
# Nine of them belong to services that are absent from IAM's resource-attribute
# list (docs.cloud.google.com/iam/docs/conditions-resource-attributes), so a
# resource.name condition would never grant anything -- the role would simply
# be revoked, as the first IAP condition was. The tenth manages something that
# has no per-owner resource at all. Their listings are recorded because an
# unscoped grant is only as safe as what currently sits in its reach.
#
#   roles/artifactregistry.admin      repositories: swarm-images (ours),
#       cloud-run-source-deploy (made by `gcloud run deploy --source`; not
#       ours). CI can delete the latter. A RESOURCE-LEVEL grant on swarm-images
#       plus a project-level create/list role would scope it the way #23 scoped
#       IAP; that is a change to what the role is, not a condition.
#   roles/cloudbuild.builds.editor    builds are named by server-generated UUID
#       and the service is unlisted. 20 most recent builds all ran as
#       209012342332-compute@developer; no triggers exist.
#   roles/cloudscheduler.admin        jobs (us-central1): swarm-reconciler-tick,
#       swarm-scheduler-tick, swarm-quota-refresh. All ours; none of theirs.
#   roles/iam.roleAdmin               IAM "resources don't provide the resource
#       name" (attribute reference). 9 custom roles, all swarm*. NOTE: CI can
#       UPDATE swarmSecretProvisioner, a role it holds, and add
#       secretmanager.versions.access to it; roleAdmin on the identity that
#       holds custom roles is self-escalation by construction.
#   roles/iam.serviceAccountAdmin     23 service accounts: 11 ours (swarm-*),
#       11 theirs (api-service, promptlab-runner, promptlab-deployer, publisher,
#       crawler, external-secrets, staging-gke-nodes, aipipeline,
#       saga-storage-ro, saga-storage-rw, tournament-digest) and the default
#       compute account. THE LARGEST REMAINING HOLE: setIamPolicy on
#       promptlab-runner lets CI grant itself actAs on their production
#       identity. modifiedGrantsByRole limits which ROLES, not which accounts,
#       so it cannot close this.
#   roles/iam.workloadIdentityPoolAdmin   pools: swarm-github (ours, but made by
#       THIS root, which the owner applies), github-actions (theirs),
#       saga-agents-staging.svc.id.goog (GKE's). terraform/infra manages no
#       pool at all, so CI appears not to need this role -- and holding it lets
#       CI add a provider to THEIR github-actions pool.
#   roles/monitoring.editor           alert policies: 6, all swarm-dev-*;
#       dashboards: 1 (ours); channels, uptime checks, groups: none.
#   roles/pubsub.admin                Pub/Sub is unlisted (only Pub/Sub Lite is
#       listed). topics: swarm-scheduler-wake[-dlq] (ours),
#       container-analysis-{notes,occurrences}-{v1,v1beta1} (made by the
#       Container Analysis API). subscriptions: both ours.
#   roles/run.admin                   services: swarm-api, swarm-authprobe,
#       swarm-quota-broker, swarm-reconciler, swarm-scheduler, swarm-ui; jobs:
#       12, all swarm-*. Nothing of theirs in any region. (swarm-authprobe is
#       not in terraform state.)
#   roles/serviceusage.serviceUsageAdmin   52 enabled services, one set per
#       PROJECT -- there is no "their" container.googleapis.com to scope.
#       Disabling it would take their cluster down; what stops that is
#       modules/project_services refusing disable_on_destroy, not IAM.
#
# storage.admin and iap.admin are handled in wif.tf.
# ---------------------------------------------------------------------------
