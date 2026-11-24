# The wrong-project rule, written once (issue 344).
#
# Two readers include this file and neither restates it:
#   * scripts/lib/destroy-guard.jq, and through it scripts/lib/plan-guard.sh,
#     scripts/destroy.sh and scripts/verify-destroy-guard.sh (the operator
#     scripts' `wrong_project` verdict);
#   * the install engine, apps/swarm-install/swarm_install/planguard.py, which
#     ships this file in its wheel as swarm_install/_guard/wrong-project.jq and
#     runs it over every plan it makes, before any apply.
#
# A resource change names a project other than `$project` when either half of
# it, `.change.before.project` or `.change.after.project`, holds one. An absent,
# null or not-yet-known project reads as `$project` itself: the provider fills
# it from the provider block, which is the install's own. (`false // x` is `x`
# in jq; a project is never `false`, so `//` is the right test here.)

# Every project one resource change names that is not `$project`.
def foreign_projects($project):
  [ (.change.before // {} | .project?), (.change.after // {} | .project?) ]
  | map(. // $project)
  | map(select(. != $project))
  | unique
  | .[];

# A list of resource changes -> one `{address, type, project}` per foreign
# project a change names.
def wrong_project($project):
  [ .[] | . as $r | foreign_projects($project) | { address: $r.address, type: $r.type, project: . } ];
