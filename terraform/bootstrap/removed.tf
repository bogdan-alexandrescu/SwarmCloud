# u-bogdan's GRANTS LEAVE THIS ROOT -- FORGOTTEN, NOT DESTROYED
# (docs/workspaces.md §3.3; lane W9 of #847, 2026-10-09).
#
# This root reads the tenant ids from the tenants block of
# terraform/environments/dev/dev.tfvars (deployer_service_accounts.tf,
# local.infra_tenant_ids). W9 takes u-bogdan out of that block, because a
# person's workspace lives outside Terraform state (owner decision WD3,
# 2026-10-08). Without the blocks below the owner's next bootstrap apply would
# DESTROY the five grants this root made for it:
#
#   google_service_account_iam_member.deployer_admin  the release deployer's
#       serviceAccountAdmin on swarm-agent-worker-u-bogdan (#334). Forgotten,
#       it stays live until the owner removes it (§3.3): it grants nothing the
#       release still uses, because no release manages that account again.
#   google_project_iam_member.forge_slot_version_adder,
#   .forge_slot_version_manager, .forge_refresh_reader
#       swarm-api's grants over swarm-tenant-u-bogdan-git-u-*, its GitHub user
#       slots. Removing them would stop swarm-api publishing, refreshing and
#       disabling the owner's GitHub tokens.
#   google_project_iam_member.forge_slot_reader  the worker's read of those
#       slots. Removing it would fail every u-bogdan task that clones.
#
# `destroy = false` drops each from this root's state and touches nothing live.
# NONE OF THESE BLOCKS MAY EVER BE CHANGED TO DESTROY: from the apply that
# forgets them, these grants are the workspace record's
# (`workspaces/u-bogdan`, `migrated = true`).
#
# Each is one instance of a for_each, and Terraform 1.16 refuses an instance
# key in a `removed` block, so each is first MOVED to an address nothing
# declares, and that address is removed -- as
# terraform/infra/custom_roles_moved_to_bootstrap.tf does for the broker's
# grant. A `moved` whose `from` is not in state is a no-op.
#
# The addresses are exactly what this root planned for u-bogdan before W9, read
# from a mock-provider plan of terraform.tfvars against dev.tfvars
# (2026-10-09); tests/terraform/u_bogdan_removed.tftest.hcl holds this file to
# that set. This root is applied by the owner, from main, after merge.
#
# Terraform >= 1.7 for `removed`; CI and the owner run 1.16.2.

moved {
  from = google_project_iam_member.forge_refresh_reader["u-bogdan"]
  to   = google_project_iam_member.forge_refresh_reader_w9_u_bogdan
}

removed {
  from = google_project_iam_member.forge_refresh_reader_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_project_iam_member.forge_slot_reader["u-bogdan"]
  to   = google_project_iam_member.forge_slot_reader_w9_u_bogdan
}

removed {
  from = google_project_iam_member.forge_slot_reader_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_project_iam_member.forge_slot_version_adder["u-bogdan"]
  to   = google_project_iam_member.forge_slot_version_adder_w9_u_bogdan
}

removed {
  from = google_project_iam_member.forge_slot_version_adder_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_project_iam_member.forge_slot_version_manager["u-bogdan"]
  to   = google_project_iam_member.forge_slot_version_manager_w9_u_bogdan
}

removed {
  from = google_project_iam_member.forge_slot_version_manager_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_service_account_iam_member.deployer_admin["swarm-agent-worker-u-bogdan"]
  to   = google_service_account_iam_member.deployer_admin_w9_swarm_agent_worker_u_bogdan
}

removed {
  from = google_service_account_iam_member.deployer_admin_w9_swarm_agent_worker_u_bogdan

  lifecycle {
    destroy = false
  }
}
