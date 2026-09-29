# Does a terraform plan change IAM? ONE implementation, reached through
# `scripts/lib/plan-guard.sh --classify-iam`.
#
# Owner decision, 2026-09-28 (#268; docs/ci.md, "A dev release that changes IAM
# waits for the owner"): a dev release whose plan changes IAM is applied only in
# the `dev-iam` environment, behind the owner's review; every other dev release
# applies un-gated, as before. This filter is the line between the two.
#
# Input: `terraform show -json <saved plan>`.
# Output: {"iam": <bool>, "rows": [{address, type, actions, target, detail}]}
#
# AN IAM CHANGE is a resource change whose type is one the owner named --
#
#   google_project_iam_custom_role       google_organization_iam_custom_role
#   google_<anything>_iam_member | _iam_binding | _iam_policy | _iam_member_remove | _iam_audit_config
#   google_iam_workload_identity_pool | _workload_identity_pool_provider
#   google_iam_deny_policy               google_iam_principal_access_boundary_policy
#   google_service_account_key           google_service_account
#   google_storage_bucket_acl            google_storage_bucket_access_control
#   google_bigquery_dataset_access
#
# -- and whose actions include create, update, delete or forget. Replace is
# written as ["delete","create"] or ["create","delete"], so it is covered by
# either half. no-op and read change nothing; a `google_iam_policy` DATA source
# (read) is not a resource change to IAM and its type does not match anyway.
#
# Owner decision, 2026-09-28 (#274): the second widening, same rule, more
# families -- every one of these also decides who can do what, or what they
# can do it as, in a project shared with another team: workload identity
# (who can federate in as what), deny policies and principal access boundary
# policies (IAM v2's own gates), a service account's key (a long-lived
# credential) or its deletion, an ACL on a bucket or a BigQuery dataset's
# access list (the pre-IAM-conditions grant mechanisms, still live on
# resources this platform did not migrate off them), a custom role owned by
# the organization rather than the project, an audit config (who is logged,
# which is itself a security control), and `_iam_member_remove` (deletes a
# grant Terraform does not otherwise manage).
#
# google_service_account IS THE ONE EXCEPTION TO "changes something". Its
# create and update do not grant or revoke anything by themselves -- what a
# service account can do is decided by the _iam_member/_iam_binding/_iam_policy
# resources already gated above, not by the account resource itself. Only
# its DELETION (or FORGET) is an access change: the identity, and everything
# granted to it, stops being usable. See `changes_by_removal`.
#
# FORGET IS THE ONE THAT WOULD BE MISSED. A `removed` block with
# `destroy = false` is planned as the action "forget": nothing live is deleted,
# the resource just leaves this state -- which is exactly how
# terraform/infra/custom_roles_moved_to_bootstrap.tf hands eight custom roles to
# the owner's bootstrap root. Who administers a role is an IAM decision, so a
# forget is gated with the rest.
#
# FAILS CLOSED. "false" means "apply in dev, ask nobody", so anything this
# cannot read is an error, never a quiet false: an input that is not a plan
# (no format_version), a resource_changes that is not a list, a change with no
# type or no list of actions. Terraform omits resource_changes entirely from a
# plan that changes nothing; that, and only that, reads as no changes.

def fail($why): error("iam-plan.jq: " + $why);

def iam_type:
  . == "google_project_iam_custom_role"
  or . == "google_organization_iam_custom_role"
  or . == "google_iam_workload_identity_pool"
  or . == "google_iam_workload_identity_pool_provider"
  or . == "google_iam_deny_policy"
  or . == "google_iam_principal_access_boundary_policy"
  or . == "google_service_account_key"
  or . == "google_service_account"
  or . == "google_storage_bucket_acl"
  or . == "google_storage_bucket_access_control"
  or . == "google_bigquery_dataset_access"
  or test("^google_[a-z0-9_]+_iam_(member(_remove)?|binding|policy|audit_config)$");

def changes_something:
  any(.[]; . == "create" or . == "update" or . == "delete" or . == "forget");

# google_service_account only: delete or forget removes an identity (and
# revokes what everything granted to it could do); create and update grant
# nothing by themselves. Explicit comparisons, not `changes_something` filtered
# after the fact -- `false // true` traps taught this file to compare directly.
def changes_by_removal:
  any(.[]; . == "delete" or . == "forget");

# The attribute `$k` as the plan will leave it, or as it was when the change
# removes the resource. `//` would read a present `false` as absent -- none of
# these attributes is boolean, and the null checks are written out anyway.
def attr($k):
  if (.change.after | type) == "object" and .change.after[$k] != null then .change.after[$k]
  elif (.change.after_unknown | type) == "object" and .change.after_unknown[$k] == true then "(known after apply)"
  elif (.change.before | type) == "object" and .change.before[$k] != null then .change.before[$k]
  else null end;

def text: if type == "string" then . elif . == null then "" else tojson end;

# What the IAM change is attached to: the most specific resource it names.
def target:
  . as $rc
  | [ "bucket", "secret_id", "repository", "service_account_id", "topic",
      "subscription", "service", "name", "project" ]
  | map(. as $k | $rc | attr($k)) | map(select(. != null)) | (.[0] // null) | text;

def detail:
  if .type == "google_project_iam_custom_role" or .type == "google_organization_iam_custom_role" then
    "role_id: \(attr("role_id") | text)"
    + (if (attr("permissions") | type) == "array" then ", \(attr("permissions") | length) permission(s)" else "" end)
  elif (.type | test("_iam_member$")) then
    "role: \(attr("role") | text), member: \(attr("member") | text)"
  elif (.type | test("_iam_binding$")) then
    "role: \(attr("role") | text), members: \(attr("members") | if type == "array" then join(", ") else text end)"
  else
    "policy_data: the resource's whole IAM policy"
  end;

if type != "object" then fail("the input is not a JSON object")
elif (.format_version | type) != "string" then fail("the input has no format_version: it is not `terraform show -json` of a plan")
elif .resource_changes != null and (.resource_changes | type) != "array" then fail("resource_changes is not a list")
else
  [ (.resource_changes // [])[]
    | if (.type | type) != "string" then fail("a resource change has no type: \(.address // "?")")
      elif (.change.actions | type) != "array" then fail("a resource change has no list of actions: \(.address // "?")")
      else . end
    | select(
        (.type | iam_type)
        and (if .type == "google_service_account" then (.change.actions | changes_by_removal)
             else (.change.actions | changes_something) end)
      )
    | { address: (.address // "" | text),
        type,
        actions: .change.actions,
        target: target,
        detail: detail } ]
  | { iam: (length > 0), rows: . }
end
