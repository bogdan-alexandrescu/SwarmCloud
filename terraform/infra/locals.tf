# ---------------------------------------------------------------------------
# The execution catalogue, mirrored from the frozen Python contract.
#
# swarm_common.profiles is authoritative. Terraform cannot import it, so these
# maps restate it -- and tests/terraform/catalogue.tftest.hcl asserts the
# restatement still matches. If the two ever drift, the symptom is a Job
# resource sized for a profile the worker is not running, which is exactly the
# class of bug nobody finds by reading.
# ---------------------------------------------------------------------------

locals {
  labels = merge(
    {
      # destroy.sh ABORTS if a plan would delete anything without this label.
      "managed-by" = "swarm-terraform"
      "swarm-env"  = var.environment
    },
    var.extra_labels,
  )

  # --- swarm_common.profiles.RESOURCE_CLASSES ------------------------------
  #
  # `disk_gib` is a slice OF `memory_gib`, not capacity on top of it. These once
  # read 20/40/100 GiB of disk-backed ephemeral storage, which the Terraform
  # google provider cannot express: empty_dir.medium accepts only "MEMORY". The
  # workspace is therefore a tmpfs and comes out of the container's memory.
  #
  # These are the code defaults, and the size every Cloud Run Job here runs at
  # (docs/design/runner-sizes.md §4.7). An admin's runner-size version
  # (control/runner_sizes) never reaches Terraform: Cloud Run fixes resources
  # on the Job, so the Cloud Run profiles and the GKE profiles' fallback Jobs
  # stay at these numbers whatever the console says. There is no cpu_limit
  # column on purpose: a Cloud Run container is billed its limit, so Cloud
  # Run renders the request alone, and the GKE burst limit
  # (CPU_BURST_MAX_RATIO x the request) is the scheduler's to render.
  #
  # Keep cpu, memory_gib, disk_gib and units equal to
  # swarm_common.profiles.RESOURCE_CLASSES; tests/terraform/catalogue.tftest.hcl
  # reads the Python at test time and fails on drift, which is how this
  # mismatch was caught.
  resource_classes = {
    standard = { cpu = 4, memory_gib = 8, disk_gib = 4, units = 1 }
    browser  = { cpu = 4, memory_gib = 8, disk_gib = 4, units = 1 }
    large    = { cpu = 8, memory_gib = 32, disk_gib = 16, units = 4 }
  }

  # --- swarm_common.profiles.RUNNER_PROFILES -------------------------------
  #
  # `secret_env` maps an environment variable NAME to a provider id. Neither is
  # a credential: the value is the string "anthropic" or "openai", and the real
  # key lives only in Secret Manager, added out of band. No secret material
  # exists anywhere in this repository.
  #
  # `model_classes` and `default_model_class` mirror contract request 101
  # (#1435): the model classes a step on the profile may name, and the one it
  # runs when it names none. The IDs are `runner_models` below, by class.
  # tests/terraform/catalogue.tftest.hcl and check-contract-parity.sh hold them.
  # checkov:skip=CKV_SECRET_6:env-var name to provider id, not a credential -- see above.
  runner_profiles = {
    "mock" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = null
      secret_env      = {}
      timeout_seconds = 600

      model_classes       = []
      default_model_class = null
    }
    # GKE Autopilot since contract request 64 (owner, 2026-10-10, option A of
    # #939): a new Cloud Run instance's internet path opens a median 20.2 s
    # after start against GKE's 1.17 s. Its tenants' Cloud Run Jobs are still
    # created, as the rollback, by `cloud_run_fallback_profiles` below.
    "generic" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "GKE_AUTOPILOT"
      provider        = null
      secret_env      = {}
      timeout_seconds = 3600

      model_classes       = []
      default_model_class = null
    }
    "claude-code" = {
      image          = "agent-runtime-base"
      resource_class = "standard"
      # GKE Autopilot since contract request 53 (applied 2026-10-08): it starts
      # in about 23 s there against Cloud Run's 128 s median. Its tenants'
      # Cloud Run Jobs are still created, as the rollback, by
      # `cloud_run_fallback_profiles` below.
      backend  = "GKE_AUTOPILOT"
      provider = "anthropic"
      # Both names map to the SAME tenant secret, and that is deliberate: a
      # tenant holds one credential per provider and it is either metered API
      # access or a Claude subscription token from `claude setup-token`. The
      # worker picks the right variable from the value's shape
      # (agent_worker.runners.cliagent._credential_env) -- `sk-ant-oat...` is a
      # subscription token, `sk-ant-api...` is a key -- and passes only that one
      # to the CLI. Projecting just one name would force a tenant who already
      # pays for a subscription to buy metered access as well.
      # checkov:skip=CKV_SECRET_6:env-var names mapped to a provider id, not credentials. The values are literally the string "anthropic"; the credential lives only in Secret Manager.
      secret_env = {
        ANTHROPIC_API_KEY       = "anthropic"
        CLAUDE_CODE_OAUTH_TOKEN = "anthropic"
      }
      timeout_seconds = 7200

      model_classes       = ["opus", "fable", "sonnet", "haiku"]
      default_model_class = "opus"
    }
    # Contract request 113, accepted by the owner 2026-10-22 (Q6 of
    # docs/design/agent-autonomy.md): claude-code on the `large` class with a
    # 14,400 s timeout, for a lane whose cause was an out-of-memory kill or a
    # timeout. A GKE profile, not in `cloud_run_fallback_profiles`, so no Cloud
    # Run Job is made for it; mirrored so the catalogue comparison holds and so
    # its `runner:claude-code-large` pool exists.
    "claude-code-large" = {
      image           = "agent-runtime-base"
      resource_class  = "large"
      backend         = "GKE_AUTOPILOT"
      provider        = "anthropic"
      secret_env      = { for name in ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"] : name => "anthropic" }
      timeout_seconds = 14400

      model_classes       = ["opus", "fable", "sonnet", "haiku"]
      default_model_class = "opus"
    }
    # Contract request 127, accepted by the owner 2026-10-31 (LIGHTPANDA-CONTRACT,
    # docs/design/lightpanda-runtime.md): claude-code on agent-runtime-lightpanda,
    # on the standard class, and nothing else different. A GKE profile, not in
    # `cloud_run_fallback_profiles`, so no Cloud Run Job is made for it;
    # mirrored so the catalogue comparison holds, so its
    # `runner:claude-code-lightpanda` pool exists, and so agent-runtime-lightpanda
    # is in runner_images: its digest is pinned and handed to the scheduler
    # with the others.
    "claude-code-lightpanda" = {
      image           = "agent-runtime-lightpanda"
      resource_class  = "standard"
      backend         = "GKE_AUTOPILOT"
      provider        = "anthropic"
      secret_env      = { for name in ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"] : name => "anthropic" }
      timeout_seconds = 7200

      model_classes       = ["opus", "fable", "sonnet", "haiku"]
      default_model_class = "opus"
    }
    # GKE Autopilot since contract request 65 (owner, 2026-10-10, option A of
    # #939); its Cloud Run Jobs stay as the rollback, as generic's do.
    "codex" = {
      image          = "agent-runtime-base"
      resource_class = "standard"
      backend        = "GKE_AUTOPILOT"
      provider       = "openai"
      # Contract request 130 (owner, 2026-11-01): the tenant's key or a pool
      # ChatGPT plan's two names, interchangeable (secrets_any_of).
      secret_env      = { for name in ["OPENAI_API_KEY", "CODEX_PLAN_ACCESS_TOKEN", "CODEX_PLAN_ACCOUNT_ID"] : name => "openai" }
      timeout_seconds = 7200

      model_classes       = []
      default_model_class = null
    }
    "browser" = {
      image          = "agent-runtime-browser"
      resource_class = "browser"
      # Chromium needs a large /dev/shm, which GKE lets us control directly.
      backend         = "GKE_AUTOPILOT"
      provider        = "anthropic"
      secret_env      = { ANTHROPIC_API_KEY = "anthropic" }
      timeout_seconds = 5400

      model_classes       = []
      default_model_class = null
    }
    # --- #295, contract requests 33, 35, 36 and 47 ---------------------------
    #
    # Mirrored so the catalogue comparison holds, and so their pools exist
    # (evaluate_capacity reads a missing pool as unlimited). post-verdict and
    # claude-code-review are RETIRED and get no Job: see
    # `profiles_without_a_job` below. merge and post-verdict mount no secret
    # -- the worker reads its credential at action time -- so their
    # secret_env is empty.
    #
    # merge reads the tenant's `-git` token since contract request 47 (owner,
    # 2026-10-04): its Job exists for a tenant that registers `git`, and runs
    # as the tenant's worker account, the secret's ordinary reader.
    #
    # GKE Autopilot since contract request 66 (owner, 2026-10-10, option A of
    # #939): the pod's KSA is bound to that same worker account, so it reads
    # the same secret. Its Cloud Run Jobs stay as the rollback, as generic's do.
    # post-verdict and claude-code-review stay on CLOUD_RUN_JOB: each was
    # designed to run as its own account, which a GKE pod cannot be.
    "merge" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "GKE_AUTOPILOT"
      provider        = "git"
      secret_env      = {}
      timeout_seconds = 600

      model_classes       = []
      default_model_class = null
    }
    "post-verdict" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = "git-review"
      secret_env      = {}
      timeout_seconds = 300

      model_classes       = []
      default_model_class = null
    }
    "claude-code-review" = {
      image          = "agent-runtime-base"
      resource_class = "standard"
      backend        = "CLOUD_RUN_JOB"
      provider       = "anthropic"
      # checkov:skip=CKV_SECRET_6:env-var names mapped to a provider id, not credentials, exactly as claude-code above.
      secret_env = {
        ANTHROPIC_API_KEY       = "anthropic"
        CLAUDE_CODE_OAUTH_TOKEN = "anthropic"
      }
      timeout_seconds = 7200

      model_classes       = ["opus"]
      default_model_class = "opus"
    }
    # Contract request 48, accepted by the owner 2026-10-05 (#625): claude-code
    # on the image that carries the repository index's toolchain, and nothing
    # else different. Its Job exists for a tenant that registers anthropic and
    # runs as the tenant's worker account, as claude-code's does; listing it
    # here also puts agent-runtime-indexer in runner_images, so its digest is
    # pinned and handed to the scheduler with the others.
    #
    # GKE Autopilot since contract request 63 (owner, 2026-10-10), the canary
    # for #939: a new Cloud Run instance's internet path opens a median 20.2 s
    # after start against GKE's 1.17 s. Its tenants' Cloud Run Jobs are still
    # created, as the rollback, by `cloud_run_fallback_profiles` below.
    "indexer" = {
      image          = "agent-runtime-indexer"
      resource_class = "standard"
      backend        = "GKE_AUTOPILOT"
      provider       = "anthropic"
      # claude-code's two env-var names, each mapped to the provider id (not a
      # credential), written as a comprehension so no line here has the shape
      # of an assignment of one.
      secret_env      = { for name in ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"] : name => "anthropic" }
      timeout_seconds = 7200

      model_classes       = ["opus", "fable", "sonnet", "haiku"]
      default_model_class = "opus"
    }
    # Contract request 67, accepted by the owner 2026-10-11: claude-code on
    # agent-runtime-browser, so an agent can render and screenshot pages. The
    # `browser` resource class because the image's 2 GiB /dev/shm tmpfs is
    # charged to the memory limit; GKE Autopilot because Cloud Run cannot size
    # /dev/shm. A GKE profile, so no Cloud Run Job is made for it.
    "claude-code-browser" = {
      image           = "agent-runtime-browser"
      resource_class  = "browser"
      backend         = "GKE_AUTOPILOT"
      provider        = "anthropic"
      secret_env      = { for name in ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"] : name => "anthropic" }
      timeout_seconds = 7200

      model_classes       = ["opus", "fable", "sonnet", "haiku"]
      default_model_class = "opus"
    }
    # Contract requests 93 and 94 (HW-CONTRACT, docs/design/human-steps.md):
    # a `control_plane` profile that no container ever runs -- swarm-api ends
    # its tasks. Mirrored so the catalogue comparison holds; listed in
    # `profiles_without_a_job` below, so no tenant gets a Job for it. Its
    # image is never pulled and its class never reserved.
    "await-input" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = null
      secret_env      = {}
      timeout_seconds = 300

      model_classes       = []
      default_model_class = null
    }
    # Contract request 124 (FL-CONTRACT, docs/design/fix-loop.md §7 request
    # A): `fix-loop`, a second `control_plane` profile. swarm-api drives its
    # rounds and ends it; nothing runs it. Mirrored and listed in
    # `profiles_without_a_job` exactly as await-input is. Shipped unavailable
    # in the Python catalogue until FL-ACCEPT passes.
    "fix-loop" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = null
      secret_env      = {}
      timeout_seconds = 300

      model_classes       = []
      default_model_class = null
    }
  }

  # --- the retired #295 profiles: no Job for any tenant ----------------------
  #
  # post-verdict and claude-code-review were to run as their own per-tenant
  # accounts (`swarm-<tenant>-post-verdict`, `-review`), because a Job running
  # one as the worker account is the hole contract requests 35 and 36 existed
  # to close. Those accounts are retired (owner decision MS0-Q4, 2026-10-06):
  # the merge runs as the worker on the tenant's `-git` token (contract request
  # 47) and trusts the review's verdict file, not an App's review. The frozen
  # catalogue still holds both entries, disabled (`available=False`), until
  # contract request 50 is decided, so they stay mirrored above -- and listed
  # here, so no tenant gets a Job for either. Without this, every tenant holding
  # anthropic would get a claude-code-review Job running as its worker account.
  # apps/common/swarm_common/profiles.py names this list.
  #
  # await-input is a `control_plane` profile (contract request 93): nothing
  # runs it on any backend, so a Job for it would be a resource nobody
  # executes. tests/unit/common/test_await_input_contract.py holds every
  # control_plane profile of the catalogue to this list; fix-loop (contract
  # request 124) is the second.
  profiles_without_a_job = ["post-verdict", "claude-code-review", "await-input", "fix-loop"]

  # --- GKE profiles that keep their Cloud Run Jobs as a rollback -------------
  #
  # claude-code: kept until 2026-10-15 as the request 53 rollback; remove then.
  # indexer: kept as the contract request 63 rollback (#939's canary, owner
  # 2026-10-10) until the owner decides the canary on its measurement.
  # generic, codex, merge: kept as the rollback of contract requests 64, 65
  # and 66 (#939 option A, owner 2026-10-10), on the same terms as indexer.
  #
  # The Job matrix below makes a Cloud Run Job only for a CLOUD_RUN_JOB
  # profile, so moving claude-code to GKE Autopilot (contract request 53,
  # applied 2026-10-08) would otherwise have DESTROYED every tenant's
  # `swarm-job-<tenant>-claude-code`. The owner kept them for one week of clean
  # GKE runs (request 53, condition 3): while they exist, rolling back is one
  # line in profiles.py (backend back to CLOUD_RUN_JOB) and a release, with no
  # Terraform apply. A Job listed here is idle: the scheduler routes by the
  # catalogue's backend, so nothing dispatches to it while the profile names
  # GKE_AUTOPILOT. tests/terraform/catalogue.tftest.hcl holds that the Jobs are
  # still planned.
  cloud_run_fallback_profiles = ["claude-code", "indexer", "generic", "codex", "merge"]

  # --- the model each model class runs (#226, #1435) ------------------------
  #
  # THE ONE PLACE A MODEL ID IS STATED, keyed by the model CLASS a step names
  # (`swarm_common.profiles.MODEL_CLASSES`, contract request 100). Owner
  # decisions of 2026-10-17 (docs/design/model-per-step.md §4.2): the class
  # names are the frozen catalogue's (Q1 (a)), and the IDs behind them are
  # here, so a model release is an edit to this map and a release, never a
  # contract request. A caller names a class, never an ID: `input.model` is
  # refused (invariant 10).
  #
  # WHY BY CLASS AND NOT BY PROFILE x CLASS: claude-code, indexer and
  # claude-code-browser run one CLI, pinned once in the base image, so an ID
  # accepted on one is accepted on all three. A profile x class map would be
  # twelve lines that must change together at every model release, and the
  # one thing it could add -- a profile lagging another -- is what #226
  # removed, so that a review never judges with another model than the one
  # that wrote the change.
  #
  # WHY claude-opus-5-5 FOR opus, THE DEFAULT (owner decision of 2026-09-26,
  # kept by R3 of 2026-10-17): it is the model the operator's local Claude
  # Code lanes run, and a step naming no class should behave like a local
  # lane. With no MODEL on any Job the CLI ran its own default instead -- the
  # QA task of that day ran claude-sonnet-5. fable is for planning, sonnet
  # and haiku for mechanical steps (Q3).
  #
  # HELD TO ITS CLASSES three ways: the scheduler and swarm-api refuse at
  # start a map whose keys are not exactly MODEL_CLASSES or whose IDs a
  # class's `ModelClass.admits` refuses (MODEL-SCHED, MODEL-API);
  # tests/terraform/runner_model.tftest.hcl asserts the four keys and each
  # ID's family word; scripts/lib/check-contract-parity.sh (section 17)
  # applies `admits` to this map.
  runner_models = {
    opus   = "claude-opus-5-5"
    fable  = "claude-fable-5-1"
    sonnet = "claude-sonnet-5-5"
    haiku  = "claude-haiku-5-5"
  }

  # Profile -> the pin of its `default_model_class` (the mirror above), for
  # every profile that has one: claude-code, claude-code-large, claude-code-lightpanda,
  # claude-code-review, indexer and claude-code-browser, all opus, so exactly the map #226 wrote by hand.
  # codex is an OpenAI CLI, where a Claude name would fail every run, and
  # mock, generic, browser, merge, post-verdict, await-input and fix-loop start no
  # model, so none of them has a default and none is listed.
  #
  # It is the `MODEL` of each classed profile's Cloud Run Jobs below (the
  # dispatcher's per-execution override replaces it with the step's class),
  # and the scheduler's `WORKER_MODELS`. That variable is DERIVED here for
  # ONE release, so that a scheduler image rolled back alone still finds the
  # profile-keyed map it reads; a follow-up lane removes it once a release
  # carrying MODEL-SCHED has applied (docs/design/model-per-step.md §4.2).
  profile_default_models = {
    for name, p in local.runner_profiles : name => local.runner_models[p.default_model_class]
    if p.default_model_class != null
  }

  # The class map itself, for the services that resolve a step's class to its
  # pin: the scheduler renders MODEL from it (MODEL-SCHED) and swarm-api fills
  # TaskCreate.model from it and serves it on /v1/runtimes (MODEL-API). Merged
  # into both services' environments in service_env below.
  #
  # NOT YET READ, BY LANE ORDER: this lane (MODEL-CONTRACT) is order 0 of the
  # design's build plan, and the scheduler's and swarm-api's readers are the
  # lanes after it, which hold neither this file nor any other the class map
  # could be wired from. Until they merge it is configuration nobody reads,
  # and that is the point of the order (owner decision R4, 2026-10-17: no
  # enablement flag; lane order alone keeps the classes unreachable).
  # scripts/lib/check-env-parity.sh's rule 1 reads only the literal
  # assignments of each service block, so it does not see a merged local and
  # will not flag that window; once both readers have merged, this can move
  # inline into both blocks, where it will.
  model_class_env = {
    WORKER_CLASS_MODELS = jsonencode(local.runner_models)
  }

  backends = ["CLOUD_RUN_JOB", "GKE_AUTOPILOT"]

  providers_in_catalogue = distinct(compact([
    for name, p in local.runner_profiles : p.provider
  ]))

  # --- naming --------------------------------------------------------------
  control_plane_services = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]

  # Derived here rather than read back from the scheduler module: the API needs
  # the topic name in its environment, and the scheduler module needs the API's
  # service account, so one of the two dependencies has to be a plain string.
  wake_topic = "${var.name_prefix}-scheduler-wake"

  # The same, for the topic swarm-api publishes a cancelled task's attempt to
  # and the reconciler stops its execution from (#627).
  execution_cancel_topic = "${var.name_prefix}-execution-cancel"

  image_base = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"

  # --- images, by digest ---------------------------------------------------
  #
  # Every image this root deploys. The platform images and the GATED images
  # are listed once, in images.json, because scripts/lib/image-refs.sh reads
  # the same file (#1878); the runner images come from the catalogue mirror
  # above, so a profile that moves to a new image is pinned the day it moves
  # rather than the day someone remembers this list.
  #
  # A gated image is one a resource or output is switched on by
  # (`lookup(var.image_refs, "<name>", "") != ""`): github_hooks.tf, egress.tf,
  # graph_service.tf, typesafe_fwd.tf, live.tf. images.json names, per gated
  # image, the state resources and outputs that need it, so that image-refs.sh
  # --applied can refuse a plan whose pinned digests would switch them off.
  # tests/unit/scripts/test_image_refs_gated.py holds every such lookup in this
  # root to that list.
  image_list      = jsondecode(file("${path.module}/images.json"))
  platform_images = local.image_list.platform
  gated_images    = sort(keys(local.image_list.gated))
  runner_images   = sort(distinct([for name, p in local.runner_profiles : p.image]))
  # The live relay (live.tf) only while it is on. On, it is deployed, so it
  # must have a digest and must be in the `image_refs` output: that output is
  # what scripts/plan.sh pins a non-deploy plan to, and a relay missing from it
  # would plan with no image. Off, nothing deploys it, and a state applied
  # before the release built swarm-live must still plan.
  live_images     = local.live_relay_enabled ? ["swarm-live"] : []
  deployed_images = sort(distinct(concat(local.platform_images, local.runner_images, local.live_images)))

  # Read by the precondition on google_cloud_run_v2_job.verify, which fails the
  # plan while this is non-empty.
  images_without_a_digest = [for name in local.deployed_images : name if !contains(keys(var.image_refs), name)]

  # THE ONLY SPELLING OF AN IMAGE IN THIS ROOT. An image with no digest maps to
  # "" rather than to anything plausible: the plan fails on the precondition
  # before that value can be used, and if a `-target` ever routed around the
  # precondition, "" is refused by the Cloud Run API instead of resolving to
  # whatever a tag points at.
  image = { for name in local.deployed_images : name => lookup(var.image_refs, name, "") }

  # What the scheduler names in every Job it creates itself -- every GKE Job,
  # and a Cloud Run Job for a tenant terraform does not know. Only the runner
  # images: the dispatcher never starts a control-plane image.
  worker_image_refs = { for name in local.runner_images : name => local.image[name] }

  # What the `image_refs` output records: every deployed image, and every gated
  # image this apply was given a digest for. Before #1878 it was the deployed
  # images alone, so state never recorded swarm-github-hooks, a laptop plan
  # (scripts/plan.sh pins this output) had no digest for it, and
  # local.github_hooks_serving planned the push receiver away. A gated image
  # with no digest is left out rather than mapped to "": "" is not a digest,
  # and var.image_refs refuses it on the next plan.
  applied_image_refs = merge(
    { for name in local.gated_images : name => var.image_refs[name] if contains(keys(var.image_refs), name) },
    { for name in local.deployed_images : name => local.image[name] },
  )

  tenant_ids = keys(var.tenants)

  # Resolved once, used by both the tenant slot pool and the tenant document, so
  # the ceiling Firestore records and the ceiling admission enforces cannot
  # disagree. `max_active` is nullable on purpose (see variables.tf): without
  # that, this coalesce has nothing to fall through to and
  # `pool_limits.default_tenant` is dead configuration that both tfvars files
  # set as though it worked.
  tenant_max_active = {
    for t, cfg in var.tenants :
    t => coalesce(cfg.max_active, var.pool_limits.default_tenant)
  }

  # Per-tenant secret administrators, falling back to the platform list. The
  # fallback may not name any tenant's own principal -- var.secret_admin_members
  # validates that -- because it is applied to every tenant that declares none.
  tenant_secret_admins = {
    for t, cfg in var.tenants :
    t => length(cfg.secret_admins) > 0 ? cfg.secret_admins : var.secret_admin_members
  }
}

# ---------------------------------------------------------------------------
# Slot pools.
#
# One document per pool name swarm_common.models.pool_names_for() can produce.
# Admission is all-or-nothing across the whole list for a task (invariant 2), so
# a pool that is missing is not "unlimited by intent" -- it is a ceiling nobody
# chose. Every name the catalogue can generate is materialised here.
# ---------------------------------------------------------------------------

locals {
  pool_global = {
    "global" = { hard_limit = var.pool_limits.global }
  }

  # THE SMALLER OF THE TENANT'S TWO LIMITS, as the API writes it.
  # `max_active` and `capacity_units` bound one count -- the units the
  # tenant's running work holds, where every task costs at least one -- so the
  # smaller is the one that binds. swarm_api/store.py writes the tenant pool as
  # min(max_active, capacity_units) in `ensure_tenant` and on every
  # `set_tenant_limits`, scripts/register-tenant.sh does, and the console's
  # Tenants screen prints that minimum as the ceiling admission enforces
  # (AH-12 in #86). This wrote max_active alone, so a tenant declared with
  # capacity_units below it was bootstrapped with a pool admitting more than
  # the record, the API and the console say it may hold.
  #
  # No live pool moves: every tfvars tenant has capacity_units at twice
  # max_active, and the pool documents are under ignore_changes, so this only
  # decides the ceiling of a pool Terraform is creating.
  # tests/terraform/infra_guards.tftest.hcl holds it.
  pool_tenants = {
    for t, cfg in var.tenants :
    "tenant:${t}" => { hard_limit = min(local.tenant_max_active[t], cfg.capacity_units) }
  }

  pool_resource_classes = {
    for name, rc in local.resource_classes :
    "resource:${name}" => {
      hard_limit = lookup(var.pool_limits.resource_classes, name, var.pool_limits.global)
    }
  }

  pool_runner_profiles = {
    for name, p in local.runner_profiles :
    "runner:${name}" => {
      hard_limit = lookup(var.pool_limits.runner_profiles, name, var.pool_limits.global)
    }
  }

  pool_backends = {
    for b in local.backends :
    "backend:${b}" => {
      hard_limit = lookup(var.pool_limits.backends, b, var.pool_limits.global)
    }
  }

  pool_providers = {
    for p in local.providers_in_catalogue :
    "provider:${p}" => {
      hard_limit = lookup(var.pool_limits.providers, p, var.pool_limits.global)
    }
  }

  # Provider quota is tracked per tenant because tenants bring their own keys:
  # one tenant's 429 must never throttle another's.
  pool_provider_tenants = {
    for pair in flatten([
      for t, cfg in var.tenants : [
        for p in cfg.providers : {
          name  = "provider:${p}:tenant:${t}"
          limit = var.pool_limits.provider_tenant
        }
      ]
    ]) : pair.name => { hard_limit = pair.limit }
  }

  pools = merge(
    local.pool_global,
    local.pool_tenants,
    local.pool_resource_classes,
    local.pool_runner_profiles,
    local.pool_backends,
    local.pool_providers,
    local.pool_provider_tenants,
  )
}

# ---------------------------------------------------------------------------
# Cloud Run Job resources, one per (tenant, Cloud-Run-backed profile).
#
# A profile that needs a provider is only materialised for tenants that hold a
# credential for it. Creating the rest would produce Job resources whose every
# execution fails at start on a missing secret, when the correct behaviour is
# for the task to PARK as CREDENTIAL_MISSING and cost nothing.
# ---------------------------------------------------------------------------

locals {
  job_matrix = flatten([
    for tenant_id, cfg in var.tenants : [
      for profile_name, profile in local.runner_profiles : {
        key             = "${var.name_prefix}-job-${tenant_id}-${profile_name}"
        tenant_id       = tenant_id
        runner_profile  = profile_name
        resource_class  = profile.resource_class
        image           = local.image[profile.image]
        timeout_seconds = profile.timeout_seconds
        model           = lookup(local.profile_default_models, profile_name, null)
        secret_env = {
          for env_name, provider in profile.secret_env :
          env_name => "swarm-tenant-${tenant_id}-${provider}"
        }
      }
      if(profile.backend == "CLOUD_RUN_JOB" || contains(local.cloud_run_fallback_profiles, profile_name)) && (
        profile.provider == null || contains(cfg.providers, profile.provider)
      ) && !contains(local.profiles_without_a_job, profile_name)
    ]
  ])

  jobs = {
    for job in local.job_matrix : job.key => {
      tenant_id      = job.tenant_id
      runner_profile = job.runner_profile
      resource_class = job.resource_class
      # Every profile, merge included (contract request 47), runs as the
      # tenant's worker account.
      service_account_email = module.tenancy.worker_service_accounts[job.tenant_id]
      image                 = job.image
      timeout_seconds       = job.timeout_seconds
      secret_env            = job.secret_env

      # Identity of the ATTEMPT -- TASK_ID, ATTEMPT_ID, LEASE_ID, GENERATION --
      # is supplied per execution by the dispatcher, never here. What the
      # environment carries is only which tenant and which profile, because
      # agent_worker.config reads the image and the command from the frozen
      # catalogue by NAME and refuses to take them from the environment at all
      # (invariant 10).
      # QUOTA_BROKER_URL is what makes the account pool exist at runtime.
      # Without it `WorkerConfig.quota_broker_url` is None, the worker builds
      # no AccountBroker, and every agent runs on the one shared per-tenant
      # secret -- the exact contention the pool was built to remove -- with
      # nothing anywhere saying so.
      #
      # DERIVED HERE, and it can be: this module reads module.cloud_run's
      # outputs and nothing in module.cloud_run reads local.jobs, so there is
      # no cycle. The SCHEDULER's own environment lives inside module.cloud_run
      # and cannot read the uri of the resource it is part of, so it is handed
      # the deterministic URL of the same service instead (main.tf
      # `local.quota_broker_url`).
      #
      # The dispatcher's per-execution overrides MERGE with this, and
      # `worker_env()` omits the name entirely when the scheduler has no URL,
      # so a Cloud Run Job keeps the value below either way.
      env = merge(local.common_env, {
        RUNNER_PROFILE = job.runner_profile
        TENANT_ID      = job.tenant_id
        WORKSPACE_ROOT = "/workspace"

        QUOTA_BROKER_URL = module.cloud_run.service_urls["swarm-quota-broker"]
        # The broker declares a custom audience, so a token minted for the
        # service URL alone is one its own `aud` check rejects.
        QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
        },
        # The model the agent CLI runs when a step names no class: the pin of
        # the profile's default class (`local.profile_default_models`; see why
        # at `local.runner_models`). Absent, not empty, on a profile with none:
        # the worker reads an
        # empty MODEL as "no model", but a Job with no such variable says so.
        { for name, value in { MODEL = job.model } : name => value if value != null },
        # The step-spec verification settings (contract request 34,
        # spec_signing.tf): the public keys, the key, the rollout mode. On the
        # Job, where only the platform writes; never in the dispatcher's
        # per-execution overrides, which a task shapes.
        local.spec_worker_env,
      )
    }
  }
}

# ---------------------------------------------------------------------------
# Process environment, mirroring swarm_common.config.Settings.from_env().
# ---------------------------------------------------------------------------

locals {
  common_env = merge({
    PROJECT_ID            = var.project_id
    REGION                = var.region
    ENVIRONMENT           = var.environment
    FIRESTORE_DATABASE    = var.firestore_database
    ARTIFACT_BUCKET       = module.storage.artifact_bucket_name
    ALLOWED_DOMAINS       = join(",", coalesce(var.allowed_domains, []))
    GLOBAL_CAPACITY_UNITS = tostring(var.pool_limits.global * 2)
    MAX_ACTIVE_AGENTS     = tostring(var.pool_limits.global)
  }, var.settings_env)

  # A Cloud Run service's OIDC audience is normally its own URL -- which cannot
  # be written into that same service's environment, because the URL is an
  # attribute of the resource the environment is part of. Terraform reports that
  # honestly as a cycle, and the way it was resolved was to not set the variable
  # at all, leaving both audience checks inert.
  #
  # A custom audience is the documented way out: a literal string known before
  # apply, accepted by Cloud Run IN ADDITION to the service URL, and minted by
  # the Pub/Sub and Cloud Scheduler OIDC tokens. Both sides then name the same
  # constant and nothing needs to know a URL.
  #
  # swarm-api's is for the WORKERS calling its child routes (child_tasks.tf);
  # people's clients keep using API_AUDIENCE, the service URL.
  push_audiences = {
    for name in ["swarm-scheduler", "swarm-quota-broker", "swarm-api"] :
    name => "https://${name}.${var.environment}.swarm.internal"
  }

  # THE CONSOLE'S PUBLIC ORIGIN, rendered once (owner decision 2026-10-01: the
  # API is the one source of a console link; nothing downstream rebuilds the
  # host). EMPTY WITHOUT A FRONTEND, and empty means no link -- the API serves
  # null rather than a run.app guess, because swarm-ui behind IAP does not open
  # at its run.app address. A hostname with enable_frontend off is a name that
  # serves nothing yet, so it is not a link either.
  console_url = var.enable_frontend && var.frontend_hostname != "" ? "https://${var.frontend_hostname}" : ""

  # THE QA IDENTITY's account (docs/design/qa-identity.md §4.1, §4.8; lane
  # QI-API, part of #1649), rendered for swarm-api as
  # QA_IDENTITY_CONSOLE_ACCOUNT. Empty means the deployment has no QA identity:
  # swarm-api refuses every submission naming one (422) and every signing
  # request (503) `qa_identity_unconfigured`.
  #
  # The account is terraform/bootstrap/qa_identity.tf's `qa-console` (its
  # `qa_account_id`), made by the owner's bootstrap apply with
  # enable_qa_identity, which also binds swarmQaJwtSigner on it for swarm-api
  # alone and admits it at IAP. This root does not read the bootstrap's state
  # and has no switch of its own for it, so the address is spelt from the same
  # project here, in DEV ONLY: owner decision Q8 (a), dev first; prod gets its
  # own account and workspace only after QI-ACCEPT is green on dev. Without a
  # console URL there is no host to sign the JWT for, so it is empty too. A
  # switch in variables.tf would replace the environment test; that file is
  # Track C's and is asked for in QI-API's questions.json.
  qa_identity_console_account = var.environment == "dev" && local.console_url != "" ? "qa-console@${var.project_id}.iam.gserviceaccount.com" : ""

  # Saga's decision API through the cluster's ONE Tailscale egress Service in
  # swarm-system (JUDGE-NET; owner decision 2026-10-14). Non-empty is what
  # makes the internal Jev judgment backends available (swarm_api.judgment)
  # and what the scheduler builds a live-browser pod's JEV_API_URL from
  # (scheduler.judgment). Empty while var.internal_decision_api_enabled is
  # off, so no workspace's Jev is offered a backend the cluster cannot reach.
  # BY ITS TAILNET NAME, over https (owner decision 2026-10-15, #1380): the
  # pod pins that name to the egress Service (tailscale.tf
  # local.decision_api_cluster_ip), which passes TLS through, so the API's
  # own certificate -- issued for that name -- verifies. The scheduler and
  # swarm-api accept only this origin (scheduler.judgment
  # INTERNAL_DECISION_ORIGIN, held equal by tests/unit/control_plane/
  # test_dispatch_judgment.py), so a changed tailscale_egress_target stops
  # the scheduler at start rather than failing in every pod.
  internal_decision_api_url = var.internal_decision_api_enabled ? "https://${var.tailscale_egress_target}" : ""

  # swarm-api's address for the live relay (live.tf, LB-L8d). PLAIN ws on 8080,
  # on the internal load balancer's reserved address: owner decision
  # 2026-10-11 (b) proxies every browser through swarm-api, so this hop never
  # leaves the VPC, and the NetworkPolicy admits only swarm-api's egress range
  # and the tenant gates to it. Port 8080 is fixed for both integration lanes
  # (the relay's swarm_live.main.PORT and its manifest name the same one).
  live_relay_url = local.live_relay_enabled ? "ws://${google_compute_address.live_relay[0].address}:8080" : ""

  # swarm-live-key for swarm-api, BY REFERENCE: Cloud Run resolves the secret
  # at start, so the value is in no tfvars file, no plan and no state -- only
  # its name and "latest" are. Only once the relay is on: live.tf's order adds
  # the first version (`create-secrets.sh --live-key`) before the switch, and
  # a reference to a secret with no version would stop swarm-api starting.
  # Off, swarm-api reads SWARM_LIVE_KEY empty and offers no live path (503).
  live_key_secret_env = local.live_relay_enabled ? {
    SWARM_LIVE_KEY = {
      secret  = google_secret_manager_secret.live_key.secret_id
      version = "latest"
    }
  } : {}

  # Every Secret Manager reference swarm-api's environment carries, merged
  # once so main.tf and tests/terraform/live.tftest.hcl read one value.
  # SWARM_EGRESS_KEY joins them once swarm-egress is deployed (egress.tf), and
  # SWARM_TYPESAFE_FWD_KEY once the TypeSafe forwarder is served (typesafe_fwd.tf),
  # SWARM_WORKSPACE_GRANT_KEY once grant attestation is (workspace_grant_key.tf),
  # SWARM_GRAPH_TICKET_KEY once the graph service is (graph_service.tf),
  # and SWARM_SETTINGS_KEY always (settings_mac.tf).
  api_secret_env = merge(local.child_key_secret_env, local.live_key_secret_env, local.egress_key_secret_env, local.typesafe_fwd_key_secret_env, local.workspace_grant_key_secret_env, local.graph_ticket_key_secret_env, local.settings_mac_secret_env)

  # The scheduler's Secret Manager references: SWARM_CHILD_KEY once child
  # tasks are on (child_tasks.tf) and SWARM_SETTINGS_KEY always
  # (settings_mac.tf). Merged once so main.tf and the settings_mac output
  # read one value.
  scheduler_secret_env = merge(local.scheduler_child_key_secret_env, local.settings_mac_secret_env)

  # The swarm-api refusal switches (swarm_api.refusals; docs/api-refusals.md).
  # A refusal added to swarm-api ships off; var.api_refusals turns one on.
  # Rendered as a map merged in, not as KEY = lines, because the names come
  # from the variable: swarm-api itself refuses to start on one that names no
  # switch, which is the check scripts/lib/check-env-parity.sh cannot make here.
  api_refusal_env = {
    for code, on in var.api_refusals :
    "REFUSAL_${trim(replace(upper(code), "/[^A-Z0-9]+/", "_"), "_")}" => on ? "on" : "off"
  }

  service_env = {
    # local.api_browser_egress_env: the browser egress switch (egress.tf).
    # local.api_grant_attestation_env: GRANT_ATTESTATION_REQUIRED (workspace_grant_key.tf).
    # local.settings_mac_env / scheduler_settings_mac_env: the one
    # SETTINGS_MAC_TENANTS cutover list, and the scheduler's
    # SETTINGS_MAC_REQUIRED (settings_mac.tf).
    # local.api_graph_stats_env: SWARM_GRAPH_STATS_URL, the graph service's
    # stats address (graph_service.tf).
    "swarm-api" = merge(local.common_env, local.api_refusal_env, local.api_browser_egress_env, local.api_grant_attestation_env, local.settings_mac_env, local.model_class_env, local.api_graph_stats_env, local.api_day_one_env, {
      # DISPATCH_TOPIC, not WAKE_TOPIC. Both apps read `DISPATCH_TOPIC`
      # (swarm_api.settings, scheduler.settings); terraform set `WAKE_TOPIC`,
      # which nothing has ever read. swarm_api.deps falls back to NullWaker()
      # when the value is empty, so every submission silently skipped the
      # publish and waited for the one-minute Cloud Scheduler safety tick.
      #
      # Nothing failed, which is why it survived: the queue still drained, just
      # up to 60s later than designed, and tail latency is not a thing anyone
      # alerts on. Renaming the env key is the whole fix -- the topic, the IAM
      # and the subscription were always correct.
      DISPATCH_TOPIC = local.wake_topic

      # Where a cancel asks for its task's execution to be stopped now rather
      # than at the worker's next poll or the reconciler's next pass (#627).
      # The reconciler stops it: swarm-api holds no compute permission, by
      # design (modules/scheduler/execution_cancel.tf says why).
      EXECUTION_CANCEL_TOPIC = local.execution_cancel_topic

      # The addresses a registered MCP server's operator allowlists
      # (docs/design/registered-mcp-servers.md §4.9): every worker's MCP call
      # leaves through the NAT's reserved addresses, so they are read from
      # the same resource as the mcp_egress_addresses output, never typed.
      # swarm_api.settings reads them and GET /v1/mcp-servers serves them as
      # `egress_addresses`; "" serves [], which the console says in words.
      MCP_EGRESS_ADDRESSES = join(",", module.network.nat_addresses)

      # The job-made workspaces' Firestore cutover list (variables.tf
      # `worker_firestore_off_tenants`, #1457 design B). swarm_api.settings
      # reads it as worker_firestore_off_tenants and writes the record's
      # worker_firestore_access from it. "" -- the default -- is every
      # workspace keeping its grant, exactly what an unset variable read.
      WORKER_FIRESTORE_OFF_TENANTS = join(",", var.worker_firestore_off_tenants)

      # Neither name appeared anywhere in terraform, so swarm_api.settings read
      # empty tuples, resolve_tenant() had no groups to check, and EVERY caller
      # fell through to the personal `u-<email>` tenant.
      #
      # That fallback is working, legitimate code -- which is exactly why this
      # hid. Every request still succeeded and still got *a* tenant; it was just
      # never the group one. A member of eng@saga.xyz spent their own personal
      # quota and wrote to their own GCS prefix while the shared tenant this
      # file provisions sat unused.
      #
      # Derived from var.tenants rather than restated: a tenant of kind "group"
      # IS a tenant group, and keeping a second list in sync by hand is how the
      # two would drift.
      # Behind the load balancer a browser sends NO Authorization header -- IAP
      # has already authenticated the person and forwards the result in
      # x-goog-iap-jwt-assertion. Without these pinned, swarm-api has nothing to
      # verify that against and answers 401 to every request from the web UI:
      # a signed-in user, a valid certificate, a healthy load balancer, and an
      # API that cannot see any of it.
      #
      # DERIVED from module.frontend, which is no longer a cycle. It was one
      # while module.frontend waited for module.cloud_run (its depends_on in
      # main.tf); nothing in that module reads a Cloud Run output -- it names
      # the services by string -- so the edge went, and the graph is now
      # backend service -> audience -> this environment
      # (docs/design/day-one-install.md §5.2). Until then this was a value
      # copied by hand out of `terraform output` into tfvars.
      #
      # The audience contains a GCP-GENERATED backend service id, so unlike the
      # push audiences above it cannot be a constant: on a new project it is
      # known after the backend services are created, in the same apply. The
      # ids are stable for the life of the backend service. With the frontend
      # off there is no IAP path, and empty means OFF (swarm-api pins no
      # audience and refuses every assertion), never "accept any".
      IAP_AUDIENCES = var.enable_frontend ? join(",", module.frontend[0].iap_audiences) : ""

      # `directory_group` as well as `kind`: a tenant can be group-KINDED for
      # naming and isolation while its principal is not a resolvable directory
      # group. Listing one that does not exist 503s every request, not just that
      # tenant's -- see the variable's description.
      TENANT_GROUPS = join(",", sort([
        for t, v in var.tenants : v.principal if v.kind == "group" && v.directory_group
      ]))
      # Every listed service account -> its tenant's kind and principal, and
      # its own unique id (contract request 30). A JSON LIST, not an object
      # keyed by email (a duplicate key in an object would silently collapse;
      # a list carries duplicates through to settings.py's own explicit
      # refusal). "[]" when no tenant lists one, which settings.py reads as ().
      TENANT_SERVICE_ACCOUNTS = jsonencode(flatten([
        for t, v in var.tenants : [
          for sa in v.service_accounts : {
            email     = lower(sa)
            kind      = v.kind
            principal = lower(v.principal)
            uid       = data.google_service_account.listed[lower(sa)].unique_id
          }
        ]
      ]))
      # ADMIN_USERS and ADMIN_POOL_USERS are taken from the environment's
      # tfvars AS IS, and the verification identity is deliberately NOT
      # appended to either the way ALLOWED_USERS below derives it. Admitting
      # the gate is needed in every environment; letting it change a runner
      # ceiling is an owner decision taken for dev on 2026-09-24 (so race-test
      # can narrow runner:mock through the admin API), and deriving it here
      # would grant it everywhere at once.
      #
      # The gate is on ADMIN_POOL_USERS, never ADMIN_USERS. The first is an
      # allow-list of admin routes (swarm_api.auth.POOL_ADMIN_ROUTES: the
      # runner ceiling alone); the second is full admin, which can disable any
      # tenant, and the owner reversed putting the gate there the same day.
      # See terraform/environments/dev/dev.tfvars and
      # docs/audits/2026-09-22/race-test-needs-a-write.md.
      ADMIN_GROUPS            = join(",", sort(var.admin_groups))
      ADMIN_USERS             = join(",", sort(var.admin_users))
      PLATFORM_OWNER          = var.platform_owner
      ADMIN_POOL_USERS        = join(",", sort(var.admin_pool_users))
      GROUPS_IMPERSONATE_USER = var.groups_impersonate_user

      # The workflow-rollup jobs' identity (modules/scheduler, D17): may call
      # POST /v1/admin/workflows/rollup and no other route
      # (swarm_api.auth.ROLLUP_SWEEPER_ROUTES). Derived, not a tfvars entry,
      # because it is exactly one account this root creates.
      ROLLUP_SWEEPER_USERS = module.scheduler.rollup_sweeper_email

      # The schedule tick's identity (modules/scheduler, docs/schedules.md
      # SD10): may call POST /v1/admin/schedules/tick and no other route
      # (swarm_api.auth.SCHEDULE_TICK_ROUTES, lane S2). Derived, not a tfvars
      # entry, for the reason ROLLUP_SWEEPER_USERS is. Until S2 ships, ApiSettings
      # reads it and no route consults it, so it changes nothing the API does.
      SCHEDULE_TICK_USERS = module.scheduler.schedule_tick_email

      # D4's refusal of a person's task on a repository they have not chosen
      # (swarm_api.settings `repository_grants_enforced`, #780 OB10). Until
      # OB10 it reached swarm-api only by hand, so an apply put it back to
      # off. Off unless the environment's tfvars sets
      # var.repository_grants_enforced; service submissions are never refused.
      REPOSITORY_GRANTS_ENFORCED = var.repository_grants_enforced ? "true" : "false"

      # A step's additional repositories (swarm_api.settings
      # `additional_repositories_enabled`, #1434 MR-API). Until this line it
      # reached swarm-api only by hand, which the next apply would have put
      # back to off. Off unless the environment's tfvars sets
      # var.additional_repositories_enabled. Their count and size limits are
      # platform settings in Firestore, not environment.
      ADDITIONAL_REPOSITORIES_ENABLED = var.additional_repositories_enabled ? "true" : "false"

      # A platform admin's reach into group workspaces they are not a member
      # of (swarm_api.settings `platform_admin_reach`,
      # docs/design/workspace-roles.md §4.2). Until ROLES-ON-DEV nothing in
      # Terraform set it, so it was off in every environment and turning it
      # on was a hand edit the next apply put back. Off unless the
      # environment's tfvars sets var.platform_admin_reach. "true" is one of
      # the spellings swarm_api.settings._bool reads as on.
      PLATFORM_ADMIN_REACH = var.platform_admin_reach ? "true" : "false"

      # The forge broker's switch (child_tasks.tf `forge_broker`, #1041;
      # swarm_api.settings `forge_broker`). Off, POST
      # /v1/attempts/forge-credential answers 503 before reading anything.
      FORGE_BROKER = var.forge_broker ? "true" : "false"

      # The live browser's search tool (swarm_api.settings
      # `search_tool_enabled`, `search_provider`; docs/search-tool.md, lane
      # SEARCH-TF, #1030). Off unless the environment's tfvars sets
      # var.search_tool_enabled, so turning it on is a tfvars change an apply
      # keeps rather than a hand edit the next apply puts back. Off, the route
      # is a 404 and no agent is offered the tool. The provider's key is not
      # here: it is a platform secret read per call from Secret Manager
      # (scripts/create-secrets.sh --search-api-key --stdin).
      SEARCH_TOOL_ENABLED = var.search_tool_enabled ? "true" : "false"
      SEARCH_PROVIDER     = var.search_provider

      # Registered MCP servers (swarm_api.mcp_servers FLAG,
      # docs/design/registered-mcp-servers.md §6, #1433). "on" is the only
      # value mcp_servers_enabled() reads as on; anything else keeps the
      # feature dark. Off unless the environment's tfvars sets
      # var.mcp_servers_enabled. swarm-api alone reads it: the scheduler and
      # the worker act only on a task whose signed input names a server, and
      # swarm-api refuses every such submission while this is off.
      SWARM_MCP_SERVERS = var.mcp_servers_enabled ? "on" : "off"

      # The personal-workspace job's trigger (docs/workspaces.md §2.1-2.2,
      # #847): approve, retry and the dispatch sweep publish a workspace id to
      # this topic, which terraform/bootstrap creates with its Cloud Build
      # trigger when enable_workspace_deployer is on. A literal, the name
      # bootstrap's workspace_deployer.tf and modules/monitoring both spell.
      # Publishing stays OFF until var.workspace_apply_publish: off, an
      # approval sits `approved` and the swarm-workspace-sweep job's sweep
      # logs it as `workspace_stuck` (reason publishing_off) for the
      # workspace-stuck alert, rather than nothing saying so (2026-10-09).
      WORKSPACE_APPLY_TOPIC   = "swarm-workspace-apply"
      WORKSPACE_APPLY_PUBLISH = var.workspace_apply_publish ? "true" : "false"

      # The submission gate (swarm_api.workspaces.gate_from_env, WD8). "on" and
      # "off" are its words; anything else stops swarm-api at start rather than
      # being guessed at. Off unless the environment's tfvars sets
      # var.workspace_gate (W10: dev, 2026-10-13). Before W10 it reached
      # swarm-api only by hand, and the next apply would have put it back off.
      WORKSPACE_GATE = var.workspace_gate ? "on" : "off"

      # The step-spec key version every submission is signed with (contract
      # request 34). A full version name, because an asymmetric key has no
      # primary version: local.spec_signing_key_version, derived in
      # spec_signing.tf (#354) from var.spec_signing_key_version. Set in the
      # same change as its reader (swarm_api.settings, swarm_api.specsigning),
      # which refuses to start outside local development without it, so
      # scripts/lib/check-env-parity.sh never sees one without the other.
      SPEC_SIGNING_KEY_VERSION = local.spec_signing_key_version

      # The public keys the workers verify step specs with, the same value
      # (spec_signing.tf). Read by swarm_api.verdictpublish, which must verify
      # a gated step's signed spec before it trusts the step's forge_credential
      # and forge_access for a control-plane push (#748). Public keys, not
      # secrets.
      SPEC_VERIFY_KEYS = jsonencode(local.spec_verify_keys)

      # The verification job's identity, admitted past the domain check.
      #
      # swarm-api admits a caller through ALLOWED_USERS or through the frozen
      # domain check against allowed_domains (saga.xyz). The swarm-verify job
      # authenticates as swarm-verify@<project>.iam.gserviceaccount.com, whose
      # domain is not saga.xyz and cannot be -- a service account is not a
      # Workspace principal. ALLOWED_USERS exists in settings.py and auth.py
      # for exactly this case and was set by no .tf and no .tfvars, so the
      # in-VPC gate's every request was answered 403 by design.
      #
      # This widens WHICH verified identities are admitted, never whether an
      # identity was verified: auth.py:331 consults this only after the token
      # has been checked, and the address it compares comes from that token.
      #
      # oracle-api, by owner decision 2026-10-18 (#1568), signed off at merge:
      # the orchestrating client application submits workflows as its own
      # tenant, u-or-9a0792 (dev.tfvars), and never as a member of eng. It is
      # in no registered group, so once admitted here it resolves to that
      # tenant alone. Named in var.allowed_users, not here: the account is not
      # this root's, the owner's decision names one deployment's address, and a
      # literal in this file would carry it into every install (DAYONE-TF-1).
      ALLOWED_USERS = join(",", concat(
        [local.verify_email],
        var.allowed_users,
      ))

      # The same principals terraform already refuses to let own a DECLARED
      # tenant, handed to the runtime so it can refuse the ones terraform
      # cannot see.
      #
      # variables.tf validates secret_admin_members against var.tenants, and
      # that is the whole check -- but Store.ensure_tenant creates a
      # self-service tenant for any allowed-domain caller on first sight, and
      # var.tenants does not contain those. On 2026-09-21 admin@saga.xyz signed
      # in to the web UI and became the principal of tenant u-admin.
      #
      # The `user:`/`group:` prefix is stripped because swarm-api compares
      # against a bare principal (an email), which is what both
      # tenant_id_for_group and tenant_id_for_user are given.
      SECRET_ADMIN_PRINCIPALS = join(",", sort([
        for m in var.secret_admin_members : replace(replace(m, "user:", ""), "group:", "")
      ]))

      # swarm-api PROXIES every account-pool call to the broker and never
      # writes a subscription credential itself, so without this the Settings
      # page cannot list or register an account at all.
      #
      # COMPUTED from the project number (main.tf `local.quota_broker_url`):
      # this environment is an input to module.cloud_run and the broker's
      # `uri` is an output of it, so reading the uri here would be a cycle.
      #
      # Unlike the silent failure the scheduler's comment warns about, an unset
      # value here is LOUD: BrokerClient refuses to start and /v1/accounts
      # answers 503 naming this variable. That is deliberate -- an account pool
      # that half-exists is worse than one that is plainly absent -- and it is
      # what the deployed API returned before this line was added.
      QUOTA_BROKER_URL      = local.quota_broker_url
      QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]

      # The console origin every served task and workflow's `links.console` is
      # built from (swarm_api.settings `console_url`). See local.console_url.
      SWARM_CONSOLE_URL = local.console_url

      # The QA identity's account (swarm_api.settings
      # `qa_identity_console_account`). See local.qa_identity_console_account.
      QA_IDENTITY_CONSOLE_ACCOUNT = local.qa_identity_console_account

      # Whether the internal Jev judgment backends are available
      # (swarm_api.judgment). See local.internal_decision_api_url.
      INTERNAL_DECISION_API_URL = local.internal_decision_api_url

      # The audience the child routes pin a worker's ID token to
      # (swarm_api.settings `child_audience`, routes/children.py): the custom
      # audience this service accepts, and the value the scheduler hands each
      # worker below. Harmless while child tasks are off: no worker calls.
      SWARM_API_AUDIENCE = local.push_audiences["swarm-api"]

      # The SwarmCloud GitHub App's PUBLIC settings (github_app.tf; #780).
      # swarm_api.forgeapp reads exactly these four names; without the first
      # three every /v1/onboarding/github call answers 503 "the App is not
      # configured". GITHUB_APP_NAME is the App's display name, served as
      # `app_name` so the console names the App by it (#1647).
      # Until 2026-10-07 they reached only the `github_app` output, so the
      # App registered that day was invisible to swarm-api (found by the
      # owner's test 1). Empty strings when no App is registered, which
      # forgeapp reads as "not configured".
      GITHUB_APP_ID        = var.github_app_id
      GITHUB_APP_CLIENT_ID = var.github_app_client_id
      GITHUB_APP_SLUG      = var.github_app_slug
      GITHUB_APP_NAME      = var.github_app_name

      # Where swarm-api proxies a console's live-browser socket, and POSTs the
      # relay's /internal/close (docs/design/live-browser.md §3.3; owner
      # decision 2026-10-11 (b)). See local.live_relay_url. Empty while the
      # relay is off, which swarm_api.settings reads as "no relay".
      SWARM_LIVE_URL = local.live_relay_url

      # Where swarm-api makes swarm-voice's internal start and end calls for
      # Voice it (docs/design/voice-it.md §10 phase 1): http on 8080 at the
      # internal load balancer voice.tf reserves (local.voice_url), and the
      # audience of the ID token swarm-api presents. Empty until voice_it is
      # "on"; empty is read by swarm_api.settings as "swarm-voice is not
      # deployed", and every Voice it route answers 503 voice_unavailable.
      # VOICE_IT_PEOPLE is unset on purpose: PLATFORM_OWNER, BGD alone (§1).
      SWARM_VOICE_URL = local.voice_url
    })
    "swarm-scheduler" = merge(local.common_env, local.spec_worker_env, local.scheduler_browser_egress_env, local.scheduler_typesafe_fwd_env, local.scheduler_settings_mac_env, local.model_class_env, {
      # local.scheduler_browser_egress_env: the browser egress switch the
      # dispatcher labels and routes a live-browser pod by (egress.tf).
      #
      # local.scheduler_typesafe_fwd_env: the TypeSafe forwarder switch the
      # dispatcher labels a TypeSafe-backed pod `swarm-typesafe: inject` by
      # (typesafe_fwd.tf).
      #
      # local.spec_worker_env, merged in above: the four step-spec settings
      # (contract request 34), which the scheduler passes VERBATIM onto every
      # Cloud Run Job it creates itself (#353, scheduler.dispatch.spec_job_env),
      # so those workers verify against the same keys as the Jobs this root
      # creates. Never into worker_env, which a task shapes.
      #
      # See the swarm-api block: the reader has always been DISPATCH_TOPIC.
      DISPATCH_TOPIC = local.wake_topic

      # PushVerifier is the second auth layer in front of /v1/push: Cloud Run
      # IAM proves the caller is authorised, this proves WHICH caller it is.
      # Neither name was ever set, so the verifier constructed itself disabled
      # and the comment in scheduler/main.py asserted a control that was not
      # running. The audience matters independently: google-auth SKIPS the
      # `aud` claim entirely when none is passed, so without it a token the
      # tick account minted for ANY other service was accepted here.
      PUSH_SERVICE_ACCOUNT = module.iam.tick_service_account
      PUSH_AUDIENCE        = local.push_audiences["swarm-scheduler"]

      # #748: how long a MERGE verdict's integrator stays PARKED for swarm-api
      # to open its pull request without a worker, after its last parent
      # ended. swarm-api hears the same wake through modules/scheduler's
      # `api_task_finished` subscription and decides in a few seconds; 60 s
      # bounds what a lost push costs. Inert until contract request 52 lets a
      # step end SUCCEEDED from PARKED (scheduler/loop.py checks it).
      CONTROL_PUBLISH_HOLD_SECONDS = "60"

      GKE_CLUSTER  = var.enable_gke_autopilot ? "${var.name_prefix}-autopilot" : ""
      GKE_LOCATION = var.region
      # Required for a client running OUTSIDE the cluster. Without both, the GKE
      # backend falls back to load_incluster_config(), which on Cloud Run fails
      # with "Service host/port is not set" on every reconciliation pass.
      GKE_ENDPOINT    = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64 = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""
      # ARTIFACT_REGISTRY_HOST, not ARTIFACT_REGISTRY or IMAGE_BASE. The
      # scheduler reads only this one (scheduler/settings.py), and the other two
      # names were read by nothing at all.
      #
      # This one was WORKING, which is the interesting part. Unset, the
      # scheduler falls back to building the host itself out of
      # `swarm_common.config.Settings.artifact_registry` -- a dataclass default
      # of "swarm-images" that is never read from the environment -- and
      # var.artifact_registry_repository also defaults to "swarm-images", so the
      # two agreed and every dispatch happened to pull the right image.
      #
      # `artifact_registry_repository`'s own description says "Must match
      # swarm_common.config.Settings.artifact_registry", and nothing enforced
      # that. Changing the repository name in tfvars would have silently sent
      # every dispatch at a registry path that does not exist, and the symptom
      # would have been a 404 on the image, pointing at the build rather than at
      # the variable. Passing the value makes it flow instead of coincide.
      ARTIFACT_REGISTRY_HOST = local.image_base
      # The runner images BY DIGEST, as a JSON object of image name -> ref.
      #
      # This was WORKER_IMAGE_TAG, and the dispatcher built
      # `<registry>/<image>:<tag>` for every GKE Job and every Cloud Run Job it
      # created. A tag is resolved when the image is PULLED, and the GKE
      # template pulls IfNotPresent, so a node holding a cached layer for an
      # older push of the same tag ran the older code (docs/security.md, the
      # worker-path paragraph, said as much). The scheduler now refuses a value
      # here that is not digest-pinned, and refuses to dispatch a profile whose
      # image is missing from it rather than fall back to a tag.
      #
      # Before that, WORKER_IMAGE_TAG existed because the dispatcher asked for
      # ":latest", which scripts/build-images.sh never pushes -- every dispatch
      # failed with `404 Image '...agent-runtime-base:latest' not found`. A
      # digest map removes that failure too: there is no default to fall to.
      WORKER_IMAGE_REFS = jsonencode(local.worker_image_refs)

      # The model each profile's agent runs when a step names no class, for
      # the Jobs this scheduler creates (tenants this root does not list). The
      # same `local.profile_default_models` sets MODEL on this root's own Jobs,
      # so both kinds run one model (#226). Derived from the class map for one
      # release (#1435): see `local.profile_default_models`.
      WORKER_MODELS = jsonencode(local.profile_default_models)

      # Passed THROUGH to each worker by `scheduler.dispatch.worker_env`, which
      # is the single source of a worker's execution environment for both
      # backends. The GKE path has no terraform-managed Job to carry it, so
      # without this every browser/GPU worker runs with no pool.
      #
      # COMPUTED from the project number (main.tf `local.quota_broker_url`),
      # for swarm-api's reason above. It used to be a value pasted from
      # `terraform output` after a first apply, with a check for a stale paste,
      # because the failure an empty one causes -- a pool configured
      # everywhere except in the one place that matters -- is completely
      # silent at runtime. A computed value cannot be empty or stale.
      QUOTA_BROKER_URL      = local.quota_broker_url
      QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]

      # Passed THROUGH to each worker by `scheduler.dispatch.worker_env`, the
      # same path QUOTA_BROKER_URL takes: the worker appends the console links
      # to a PR body it opens only when SWARM_PR_CONSOLE_LINKS is on
      # (var.pr_console_links, off by default), from the same origin the API
      # serves.
      SWARM_CONSOLE_URL      = local.console_url
      SWARM_PR_CONSOLE_LINKS = tostring(var.pr_console_links)

      # A live-browser pod's JEV_API_URL for a workspace that judges with an
      # internal backend (scheduler.judgment, JUDGE-SELECT). See
      # local.internal_decision_api_url.
      INTERNAL_DECISION_API_URL = local.internal_decision_api_url

      # Child tasks (child_tasks.tf): passed through to a worker the scheduler
      # also mints a registration nonce for -- only when it holds
      # SWARM_CHILD_KEY, and never to a child -- so the worker can reach the
      # worker-only child routes and mint its ID token for their audience.
      # COMPUTED, for QUOTA_BROKER_URL's reason (main.tf
      # `local.swarm_api_url`).
      SWARM_API_URL      = local.swarm_api_url
      SWARM_API_AUDIENCE = local.push_audiences["swarm-api"]

      # The forge broker's switch (child_tasks.tf `forge_broker`, #1041),
      # passed THROUGH to every worker by `scheduler.dispatch.worker_env` when
      # on, and the reason a child on a person's slot is keyed. Off, dispatch
      # is what it was before the broker.
      FORGE_BROKER = var.forge_broker ? "true" : "false"

      # The per-tenant state switch (child_tasks.tf `state_via_api_tenants`,
      # #1457, ISO-SCHED phase 2): every attempt of a listed tenant reaches
      # its state only through swarm-api's state routes
      # (scheduler.dispatch.worker_env). Comma-separated, the form
      # scheduler.settings.parse_state_via_api_tenants reads. Empty, the
      # default, switches nobody.
      SWARM_STATE_VIA_API_TENANTS = join(",", var.state_via_api_tenants)

      # The tenant-Job VPC switch (tenant_job_vpc.tf `tenant_job_vpc_egress`,
      # #1893): on, the Jobs this scheduler creates carry terraform's
      # vpc_access. The names are module.network's, the outputs module
      # "cloud_run_jobs" is given (main.tf), so the two kinds of Job cannot
      # name different subnets. The switch value REFERENCES the grant so the
      # scheduler's new revision is applied after it exists.
      TENANT_JOB_VPC_EGRESS   = var.tenant_job_vpc_egress && length(google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet) == 1 ? "true" : "false"
      TENANT_JOB_NETWORK      = module.network.network_id
      TENANT_JOB_SUBNETWORK   = module.network.subnetwork_id
      TENANT_JOB_NETWORK_TAGS = module.network.worker_network_tag

      # The live browser's display sidecar (scheduler.settings
      # `live_browser_sidecar`; docs/design/live-browser.md, LB-Lon). On, a
      # claude-code-browser GKE pod carries the `display` native sidecar and
      # the live gate; off, it is the pod it always was. Until LB-Lon nothing
      # set it, so turning the relay on streamed nothing.
      #
      # DERIVED from the relay (`local.live_relay_enabled`, live.tf), not a
      # second variable, because neither half works alone: a relay with no
      # sidecar has no browser to stream, and a sidecar with no relay costs
      # every browser pod a display's share of its resource class
      # (scheduler.dispatch LIVE_DISPLAY_SHARE) for a gate swarm-api will
      # never issue a tunnel ticket to (SWARM_LIVE_URL is empty). A second
      # switch would only add the two states that are always wrong.
      LIVE_BROWSER_SIDECAR = tostring(local.live_relay_enabled)
    })
    "swarm-quota-broker" = merge(local.common_env, {
      # WITHOUT THIS THE SWEEP HAS NEVER RUN. /v1/quota/sweep requires a
      # platform caller, the broker derives "platform" from this list, and the
      # list was empty -- so the Cloud Scheduler tick got 403 every five
      # minutes, silently, since the day it was created. Verified in the logs
      # before the fix: an unbroken run of 403s on /v1/quota/sweep.
      #
      # The visible cost was the AIMD quota state never being recomputed and
      # PARKED tenants never being un-parked. The refresher for subscription
      # credentials runs on the same tick, so it would have been dead on
      # arrival too, and its symptom -- credentials quietly stopping -- points
      # nowhere near the cause.
      #
      # The Cloud Scheduler tick, and swarm-api. NOT workers: they report quota
      # as their own tenant, and being on this list would let any one of them
      # set another tenant's hard max.
      #
      # swarm-api is here because it PROXIES the account-pool routes for a
      # browser, so it has to be able to act for whichever tenant the signed-in
      # caller resolved to -- it is not itself a tenant, and the broker's
      # WorkerIdentity derives a tenant from the caller's service account name,
      # which for swarm-api yields "caller is not a swarm worker service
      # account" and a 403 on every account request.
      #
      # This is safe only because swarm-api resolves the caller's tenant ITSELF,
      # through `ctx.submissions.tenant_for(auth)`, and sends that resolved
      # value as `owner_tenant` -- it never forwards a tenant the browser
      # supplied. swarm-api is the tenant boundary for these routes; the broker
      # is the single writer. If that ever stops being true, this line is the
      # one that turns it into a cross-tenant hole.
      #
      # swarm-reconciler is here for #380: when it fences an attempt it calls
      # /v1/holds/release-attempt, a platform-only route, to give back that
      # attempt's account holds at once rather than at their 3h TTL. It is
      # platform, not a tenant: like swarm-api it is not a worker SA, so the
      # broker could not resolve it to a tenant anyway. Being listed lets it
      # use every platform route (the sweep, a hard max); it calls only the
      # release, and it is a control-plane identity as trusted as the tick.
      #
      # swarm-doctor is here once the scheduled doctor runs (doctor.tf): it
      # probes the broker's platform routes as a control-plane identity, and an
      # identity the broker does not list is answered 403 on every call -- the
      # quota sweep's year of silent 403s (lesson 38) was exactly that. Only
      # while enable_scheduled_doctor is on, so the live broker's environment
      # does not change before the doctor exists.
      PLATFORM_SERVICE_ACCOUNTS = join(",", concat([
        module.iam.tick_service_account,
        module.iam.service_account_emails["swarm-api"],
        module.iam.service_account_emails["swarm-reconciler"],
      ], local.doctor_on ? [local.doctor_email] : []))

      # Provisioning an account's two secrets, which moved here when account
      # management became a Settings page rather than a shell script. A pool
      # account's secret name contains a LABEL chosen at registration time, so
      # terraform cannot declare it in advance and the component handling the
      # registration has to create it.
      BROKER_SERVICE_ACCOUNT = module.iam.service_account_emails["swarm-quota-broker"]

      # A TEMPLATE, not a pattern rebuilt in Python. modules/tenancy owns what a
      # tenant's service account is called -- `swarm-agent-worker-<tenant>`, with
      # an 11-character tenant limit because of the 30-character GCP cap -- and a
      # second copy of that rule inside the broker would be correct until the day
      # it was not. The symptom then is a pod that cannot read the credential it
      # was assigned, surfacing inside a job, nowhere near the cause.
      #
      # `{tenant}` is substituted by the broker. An unset value makes the
      # register route REFUSE rather than create a secret no pod can read.
      WORKER_SERVICE_ACCOUNT_TEMPLATE = "swarm-agent-worker-{tenant}@${var.project_id}.iam.gserviceaccount.com"

      # WorkerIdentity refuses to start without this when `hardened`, for the
      # same reason PUSH_AUDIENCE exists: no audience means google-auth does not
      # check `aud` at all. It was never set, and `hardened` is driven by
      # ENVIRONMENT -- which is "dev" for saga-agents-staging, a project that is
      # production-shaped in every way except that label. So the guard written
      # to make this impossible was itself disarmed in the only place it ran.
      BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
    })
    "swarm-reconciler" = merge(local.common_env, {
      # No GKE_CLUSTER/GKE_LOCATION here: the reconciler talks to the cluster
      # through the endpoint and CA bundle directly (reconciler/backends.py) and
      # has never read either name. The scheduler does read them; that is why
      # they are set there and not here.
      GKE_ENDPOINT    = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64 = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""

      # Where the reconciler gives back a fenced attempt's account holds
      # (#380, `repair.BrokerHoldReleaser`). Unset, the reconciler logs a
      # warning at start and the holds count against the pool until their TTL.
      # Computed from the project number for the same cycle reason as the
      # scheduler's copy above (main.tf `local.quota_broker_url`).
      QUOTA_BROKER_URL      = local.quota_broker_url
      QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]

      # Where the reconciler rings `task_finished` for a task a repair ended
      # (#636, `reconciler.finishwake`), so its dependants are released at once
      # rather than on the next safety tick. The same name and topic as
      # swarm-api's above; publish is already granted (the reconciler is one
      # of the wake topic's publisher_members in main.tf). Unset, the
      # reconciler logs a warning at start and the tick does the release.
      DISPATCH_TOPIC = local.wake_topic
    })
  }
}

# ---------------------------------------------------------------------------
# Service accounts a tenant lists (contract request 30).
# ---------------------------------------------------------------------------

locals {
  listed_service_accounts = toset(flatten([
    for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]
  ]))
}

# The account's unique id (`sub` on the token it presents), pinned beside its
# email so a service account deleted and recreated under the same address --
# which GCP permits -- does not inherit the old one's tenant. Read, never
# managed: the account is created and owned elsewhere (#273's
# terraform/bootstrap/ci_fix.tf for swarm-ci-fix), and a data source carries
# no labels to set.
#
# THE PIN HOLDS ONLY BETWEEN APPLIES. The uid is read at plan and rendered into
# swarm-api's environment at apply. An account deleted and recreated under the
# same address keeps resolving by the OLD uid until the next apply re-reads it:
# the new account is refused (its uid does not match), but an ID token the OLD
# account minted before it was deleted still carries the listed uid and is
# accepted until it expires (Google ID tokens live an hour). Deleting a listed
# account is therefore not a revocation until both the token lifetime has
# passed and the next apply has re-read the listing.
data "google_service_account" "listed" {
  for_each   = local.listed_service_accounts
  project    = var.project_id
  account_id = split("@", each.value)[0]
}

# swarm-api's Day One environment (DAYONE-TF-2). GROUPS_AUTH is always
# rendered (default "delegation", the live deployment's mode, so the live plan
# shows this one new variable and no behaviour change). The console's App
# setup values are rendered only once they mean something, so the live
# deployment's environment gains nothing else:
#   * SETUP_GITHUB_ORG when setup_github_org names an organisation;
#   * SWARM_HOSTNAME and GITHUB_HOOKS_URL under enable_console_app_setup.
#     GITHUB_HOOKS_URL is the receiver's deterministic URL WITH its route,
#     /github/events (apps/swarm-github-hooks receiver.py `PATH`; any other path
#     is a 404), rendered from infra pass 2 on, before the receiver exists: the
#     App's manifest names it (an empty hook URL may be refused by GitHub after
#     the App is made), and the hook stays inactive until the engine turns it
#     on after infra pass 4 makes the receiver.
# Unset, apps/swarm-api/swarm_api/settings.py explains each one's fallback.
locals {
  api_day_one_env = merge(
    { GROUPS_AUTH = var.groups_auth },
    var.setup_github_org == "" ? {} : { SETUP_GITHUB_ORG = var.setup_github_org },
    var.enable_console_app_setup && var.frontend_hostname != "" ? { SWARM_HOSTNAME = var.frontend_hostname } : {},
    # The receiver's deterministic run.app URL (main.tf says why), not its
    # resource's `uri`: the receiver reads swarm-api's URL, so naming its
    # resource here would be a dependency cycle.
    var.enable_console_app_setup ? { GITHUB_HOOKS_URL = "https://${local.github_hooks_name}-${data.google_project.this.number}.${var.region}.run.app/github/events" } : {},
  )
}
