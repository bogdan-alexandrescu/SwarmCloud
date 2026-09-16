# Destroy-plan guard.
#
# Input : the output of `terraform show -json <destroy plan>`
# Args  : $deny        - array of resource names owned by other teams
#         $allow_types - array of resource types that CANNOT carry labels
#         $project     - the project this environment is allowed to touch
# Output: one verdict object. The shell script decides; this file only judges.
#
# The rule being enforced: every resource terraform proposes to DELETE must
# either carry managed-by=swarm-terraform, or be of a type that physically
# cannot carry a label (IAM bindings, API enablements, Firestore indexes) and
# reference nothing on the deny-list. Anything else is an offender and the
# destroy aborts. Fail-closed is deliberate -- an unrecognised unlabelable type
# is an offender until a human adds it to the allow-list in a reviewed change.

def before: (.change.before // {});

def label_map:
  ( before.labels
    // before.effective_labels
    // before.terraform_labels
    # google_container_cluster calls them resource_labels, monitoring calls them
    # user_labels, and every kubernetes_* resource hides them under metadata.
    // before.resource_labels
    // before.user_labels
    // ( (before.metadata // []) | if type == "array" then (.[0].labels // null) else (.labels // null) end )
    // null );

def managed_label:
  ( label_map // {} ) | if type == "object" then (.["managed-by"] // null) else null end;

# Every string in the before-state that could name a real resource.
def tokens:
  before as $b
  | [ $b.name?, $b.id?, $b.email?, $b.account_id?, $b.bucket?, $b.cluster?,
      $b.network?, $b.subnetwork?, $b.repository?, $b.secret_id?, $b.service?,
      $b.job?, $b.cluster_id?, $b.database?, $b.topic?, $b.subscription? ]
  + ( ($b.id? // "")   | tostring | split("/") )
  + ( ($b.name? // "") | tostring | split("/") )
  + ( ($b.member? // "") | tostring | sub("^serviceAccount:"; "") | [.] )
  | map(select(. != null and . != "")) | map(tostring) | unique;

def deny_hits($deny): [ tokens[] | select(IN($deny[])) ] | unique;

# The shared VPC's default network is matched on the field, not on the token
# list: "default" is too common a string to compare blindly against every id.
def default_network_hit:
  ( ((before.network    // "") | tostring | test("(^|/)default$"))
    or ((before.subnetwork // "") | tostring | test("(^|/)default$")) );

def is_deletion: (.change.actions // []) | index("delete") != null;

# A type is exempt from the label rule only if it physically cannot carry one.
# Every *_iam_member / *_iam_binding / *_iam_policy is a policy edge rather than
# a resource, so they are matched by shape instead of being listed one by one.
def is_unlabelable($allow_types):
  . as $t
  | (($allow_types | index($t)) != null)
    or ($t | test("_iam_(member|binding|policy)$"));

def data_bearing:
  [ "google_firestore_database", "google_storage_bucket", "google_bigquery_dataset",
    "google_sql_database_instance", "google_redis_instance", "google_filestore_instance" ];

def summarise($deny; $allow_types; $project):
  [ .resource_changes[]? | select(is_deletion) ] as $deletions
  | [ .resource_changes[]?
      | select((.change.actions // []) | (index("create") != null or index("update") != null)) ] as $mutations
  | {
      total_changes: ((.resource_changes // []) | length),
      deletions: ($deletions | length),

      # A destroy plan that also creates or updates something is not a destroy
      # plan; it means state and reality disagree. Surface it, never proceed.
      unexpected_mutations: [ $mutations[] | { address: .address, actions: .change.actions } ],

      denylist_hits: [ $deletions[]
        | . as $r
        | ($r | deny_hits($deny)) as $hits
        | select(($hits | length) > 0 or ($r | default_network_hit))
        | { address: $r.address, type: $r.type,
            matched: (if ($hits|length) > 0 then $hits else ["default-network"] end) } ],

      wrong_project: [ $deletions[]
        | select((.change.before.project // $project) != $project)
        | { address: .address, type: .type, project: .change.before.project } ],

      offenders: [ $deletions[]
        | . as $r
        | ($r | managed_label) as $m
        | select($m != "swarm-terraform")
        | select(($r.type | is_unlabelable($allow_types)) | not)
        | { address: $r.address, type: $r.type,
            managed_by: ($m // null),
            labels: ($r | label_map),
            reason: (if ($r | label_map) == null
                     then "no labels at all, and the type is not on the unlabelable allow-list"
                     else "labels present but managed-by is \($m // "absent"), not swarm-terraform"
                     end) } ],

      unlabelable_allowed: [ $deletions[]
        | . as $r
        | select(($r | managed_label) != "swarm-terraform")
        | select($r.type | is_unlabelable($allow_types))
        | { address: $r.address, type: $r.type } ],

      labelled_ok: [ $deletions[]
        | select((. | managed_label) == "swarm-terraform")
        | { address: .address, type: .type } ],

      data_bearing: [ $deletions[]
        | . as $r
        | select((data_bearing | index($r.type)) != null)
        | { address: $r.address, type: $r.type,
            name: ($r.change.before.name // $r.change.before.id // "?") } ],

      by_type: ( [ $deletions[] | .type ] | group_by(.) | map({ type: .[0], count: length })
                 | sort_by(-.count) )
    };

summarise($deny; $allow_types; $project)
