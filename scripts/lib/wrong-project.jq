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
# A resource change names a project other than `$project` when ANY of these
# holds, in either half (`.change.before`, `.change.after`):
#   * an attribute whose name is `project`, `project_id` or ends in `_project`
#     (host_project, service_project, subnetwork_project, ...) holds another id;
#   * any string anywhere in it holds a `projects/<id>/...` path (service
#     account and member paths, crypto_key_id, secret_id, topic, ...) whose id
#     is not `$project`, not the install's own project number (read from the
#     plan's own google_project data), not `-`/`_`, and not a Google-published
#     image project;
#   * such an attribute is not known until apply (`.change.after_unknown`): an
#     unknown project is not proof it is the install's, so the rule fails
#     closed and says "(unknown at plan time)";
#   * the resource is organization, folder, billing or project level, which is
#     outside one project by nature.
# A resource that sets no project at all reads as `$project` itself: the
# provider fills it from its block, which `provider_projects` reads.
# (`false // x` is `x` in jq; a project is never `false`.)

def google_published_projects: ["deeplearning-platform-release"];

# The install's own project number, which paths use in place of the id: read
# from the plan's google_project data, and from the identity (`name`/`id`) of
# a resource whose own `project` is `$project` (a project's resource names its
# own project's number there). A number read from any other attribute is not
# trusted: it could be another project's.
def own_numbers($plan; $project):
  ( [ $plan | .. | objects | select(.type? == "google_project") | .values?.number? // empty | tostring ] )
  + ( [ $plan | .resource_changes[]? | .change | (.before // {}), (.after // {})
        | select(.project? == $project) | (.name?, .id?) | strings
        | capture("^projects/(?<n>[0-9]+)/") | .n ] ) | unique;

def project_attribute_name: test("^project(_id)?$|_project(_id)?$");

# Ids named by `projects/<id>/` paths inside every string of a value.
def path_projects:
  [ .. | strings | scan("(?:^|[^A-Za-z0-9_-])projects/([A-Za-z0-9_.:-]+)") | .[0] ];

# Ids held by project-named attributes anywhere in a value.
def attribute_projects:
  [ .. | objects | to_entries[] | select(.key | project_attribute_name)
    | .value | select(type == "string" and . != "") | sub("^projects/"; "") ];

def scope_level:
  test("^google_(organization|folder|billing)_|^google_(folder|project|organization)$|^google_billing_");

def foreign_projects($project; $numbers):
  . as $r
  | ( [ ($r.change.before // {}), ($r.change.after // {}) ]
      | map(attribute_projects + path_projects) | add // []
      | map(select(. != $project and . != "-" and . != "_"
                   and (. as $id | ($numbers | index($id)) == null)
                   and (. as $id | (google_published_projects | index($id)) == null))) ) as $named
  | ( [ ($r.change.after_unknown // {}) | .. | objects | to_entries[]
        | select((.key | project_attribute_name) and .value == true) | "(unknown at plan time)" ] ) as $unknown
  | ( if ($r.type // "" | scope_level) then ["(\($r.type) is outside one project)"] else [] end ) as $scoped
  | ($named + $unknown + $scoped) | unique | .[];

# A list of resource changes -> one `{address, type, project}` per foreign
# project a change names.
def wrong_project($project; $numbers):
  [ .[] | . as $r | foreign_projects($project; $numbers)
    | { address: $r.address, type: $r.type, project: . } ];

def wrong_project($project): wrong_project($project; []);

# A provider block that names another project, read from the plan's
# `.configuration.provider_config` (aliased providers included). A project the
# configuration only references (a variable) is not a literal and is not read.
def provider_projects($project):
  [ (.configuration.provider_config? // {}) | to_entries[] | . as $p
    | ($p.value.expressions? // {}) | to_entries[]
    | select(.key | project_attribute_name)
    | (.value.constant_value? // empty) | select(type == "string" and . != $project)
    | { address: "provider[\($p.key)]", type: "provider", project: . } ];

# The whole verdict for a plan: its resource changes (`$changes`) and its providers.
def plan_wrong_project($project; $changes):
  . as $plan
  | ($changes | wrong_project($project; own_numbers($plan; $project))) + provider_projects($project);
