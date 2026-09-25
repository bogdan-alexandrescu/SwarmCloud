# THE PLATFORM'S EIGHT CUSTOM ROLES AND THE QUOTA BROKER'S swarmSecretLister
# GRANT LEAVE THIS ROOT -- FORGOTTEN, NOT DESTROYED.
#
# Owner decisions, 2026-09-25: #79 takes roles/iam.roleAdmin off the CI deployer
# and moves every custom role this root and its modules defined into
# terraform/bootstrap, which the owner applies; #69 moves the broker's
# swarmSecretLister grant there too. terraform/bootstrap/platform_roles.tf now
# defines the roles byte for byte as they were (tests/terraform/
# platform_roles.tftest.hcl holds it to a fixture read from the live project)
# and IMPORTS them.
#
# These blocks are what stops the release DELETING them. Without them, a role
# that has left the configuration is planned for destroy -- and a deleted
# custom role grants nothing to anyone bound to it, cannot be re-created under
# the same id for 7 days, and would take the scheduler's, the reconciler's, the
# broker's and every tenant worker's access with it. `destroy = false` makes the
# release drop each one from this root's state and touch nothing live. Every
# binding that names these roles stays here, unchanged: the role names are the
# same strings, read from terraform/modules/custom_role_ids.
#
# THE ORDER, DERIVED FROM THE CODE (docs/runbooks/custom-roles-to-bootstrap.md):
#
#   1. merge; the release's infra apply FORGETS these nine objects. CI still
#      holds roleAdmin at that point, which the refresh before a forget needs
#      (iam.roles.get). It also writes `custom_roles_owner` below.
#   2. the owner's bootstrap apply IMPORTS them and destroys the deployer's
#      roleAdmin binding. Bootstrap refuses to adopt until step 1's output is in
#      this root's state -- two states managing one role is the failure this
#      order exists to prevent.
#   3. the next release plans with no google_project_iam_custom_role anywhere in
#      it, and so needs no iam.roles.* permission.
#
# THE BROKER'S GRANT IS ONE INSTANCE OF A for_each, and a `removed` block cannot
# name an instance: it forgets every instance of the resource it names, and
# modules/iam's `plain` holds seventeen other grants (18 instances in the dev
# state, read-only, 2026-09-25). So the instance is first
# MOVED to an address of its own that nothing declares, and that address is
# removed. The key is a literal because a `moved` address cannot be computed:
# "swarm-quota-broker:<role name>", with the project both environments' tfvars
# name and the suffix neither sets (terraform/modules/custom_role_ids). A
# deployment of this root under another project or suffix does not match it,
# and there the grant would be DESTROYED rather than forgotten -- only
# saga-agents-staging's infra/dev state exists (read-only `gcloud storage ls` of
# the state bucket, 2026-09-25), so that deployment does not exist today.
#
# Terraform >= 1.7 for `removed`; CI and the owner run 1.16.2.

removed {
  from = module.iam.google_project_iam_custom_role.job_dispatcher

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.iam.google_project_iam_custom_role.job_reaper

  lifecycle {
    destroy = false
  }
}

# count-indexed ([0]) in state; a `removed` block names the resource, which
# covers every instance.
removed {
  from = module.iam.google_project_iam_custom_role.gke_dispatcher

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.iam.google_project_iam_custom_role.gke_reaper

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.iam.google_project_iam_custom_role.secret_lister

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.tenancy.google_project_iam_custom_role.worker_firestore

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.tenancy.google_project_iam_custom_role.bucket_metadata_reader

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.artifact_registry.google_project_iam_custom_role.image_puller

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.iam.google_project_iam_member.plain["swarm-quota-broker:projects/saga-agents-staging/roles/swarmSecretLister"]
  to   = module.iam.google_project_iam_member.broker_secret_lister_moved_to_bootstrap
}

removed {
  from = module.iam.google_project_iam_member.broker_secret_lister_moved_to_bootstrap

  lifecycle {
    destroy = false
  }
}

# Read by terraform/bootstrap before it imports any of the nine (platform_roles.tf,
# `adopt_from_infra_states`). An output reaches this root's state only when an
# APPLY writes it, and the apply that writes it is the one that runs the
# `removed` blocks above. So its presence is the evidence that this root has
# let go -- a plan (terraform.yml's `plan (dev)`) writes nothing.
output "custom_roles_owner" {
  description = "Which root defines the platform's custom roles and grants the broker swarmSecretLister. terraform/bootstrap refuses to adopt them until this reads terraform/bootstrap in this root's state."
  value       = "terraform/bootstrap"
}
