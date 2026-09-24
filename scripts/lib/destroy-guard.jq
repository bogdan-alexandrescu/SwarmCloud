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
def after:  (.change.after  // {});

# The same six spellings, read out of whichever half of the change we are
# judging: `before` for a deletion, `after` for a creation.
#
# `//` alone is NOT enough here, and the reason is the jq trap CLAUDE.md warns
# about, in its mirror image. That note is about `false // true` being `true`
# because `false` reads as absent; this is the opposite -- an EMPTY OBJECT is
# truthy, so `{} // .effective_labels` stops at `{}` and never falls through.
#
# The google provider reports `after.labels` as {} on an UPDATE to a resource
# whose labels are unchanged, keeping the real values in `effective_labels`.
# So every update to a correctly-labelled resource read as unlabelled. Measured
# on 2026-09-19: the guard refused a legitimate plan because
# module.scheduler.google_pubsub_subscription.wake appeared to have no
# managed-by, while the live subscription carries
# managed-by=swarm-terraform. A guard that is wrong about ordinary applies is
# one people learn to bypass, which is worse than not having it.
#
# `nonempty` therefore treats an empty object as absent, so the chain keeps
# looking. A resource that genuinely has no labels anywhere still ends at null
# and is still caught.
def nonempty: if type == "object" and (length > 0) then . else null end;

def label_map_of($state):
  ( ($state.labels | nonempty)
    // ($state.effective_labels | nonempty)
    // ($state.terraform_labels | nonempty)
    # google_container_cluster calls them resource_labels, monitoring calls them
    # user_labels, and every kubernetes_* resource hides them under metadata.
    // ($state.resource_labels | nonempty)
    // ($state.user_labels | nonempty)
    // ( ($state.metadata // []) | if type == "array" then (.[0].labels // null) else (.labels // null) end | nonempty )
    // null );

def label_map: label_map_of(before);
def after_label_map: label_map_of(after);

def managed_of($map):
  ( $map // {} ) | if type == "object" then (.["managed-by"] // null) else null end;

def managed_label: managed_of(label_map);
def after_managed_label: managed_of(after_label_map);

# Whether terraform says a label field's value is not yet known at plan time.
# `after_unknown` mirrors the resource's shape: `true` for a wholly unknown
# value, an object or array for a partially unknown one. A create whose labels
# are computed from something unknown is not evidence of a missing label, so it
# is not reported -- the destroy-side check catches it later with real values.
def _unknown_at($k):
  ((.change.after_unknown // {})[$k] // false) as $v
  | ($v == true)
    or ((($v | type) == "object") and (($v | length) > 0))
    or ((($v | type) == "array")  and (($v | length) > 0));

def labels_unknown:
  ( _unknown_at("labels") or _unknown_at("effective_labels")
    or _unknown_at("terraform_labels") or _unknown_at("resource_labels")
    or _unknown_at("user_labels") );

# Every string in one half of the change that could name a real resource.
def tokens_of($b):
  [ $b.name?, $b.id?, $b.email?, $b.account_id?, $b.bucket?, $b.cluster?,
    $b.network?, $b.subnetwork?, $b.repository?, $b.secret_id?, $b.service?,
    $b.job?, $b.cluster_id?, $b.database?, $b.topic?, $b.subscription? ]
  + ( ($b.id? // "")   | tostring | split("/") )
  + ( ($b.name? // "") | tostring | split("/") )
  + ( ($b.member? // "") | tostring | sub("^serviceAccount:"; "") | [.] )
  | map(select(. != null and . != "")) | map(tostring) | unique;

def tokens: tokens_of(before);

# A create plan never has a `before`, so naming another team's resource in a
# CREATE -- an IAM binding on their bucket, a subnetwork in their VPC -- would be
# invisible to a before-only scan. Both halves are read.
def tokens_both: (tokens_of(before) + tokens_of(after)) | unique;

def deny_hits($deny): [ tokens[] | select(IN($deny[])) ] | unique;
def deny_hits_both($deny): [ tokens_both[] | select(IN($deny[])) ] | unique;

# The shared VPC's default network is matched on the field, not on the token
# list: "default" is too common a string to compare blindly against every id.
def default_network_hit:
  ( ((before.network    // "") | tostring | test("(^|/)default$"))
    or ((before.subnetwork // "") | tostring | test("(^|/)default$")) );

def default_network_hit_both:
  ( default_network_hit
    or ((after.network    // "") | tostring | test("(^|/)default$"))
    or ((after.subnetwork // "") | tostring | test("(^|/)default$")) );

def is_deletion: (.change.actions // []) | index("delete") != null;

# A create or an update. `create`+`delete` (a replacement) is both, deliberately:
# the new object still has to be labelled and the old one still has to be ours.
def is_creation: (.change.actions // []) | (index("create") != null or index("update") != null);

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

# ---------------------------------------------------------------------------
# THE ALLOWLIST HALF: is this resource OURS?
# ---------------------------------------------------------------------------
#
# Everything above this point is a BLOCKLIST -- it refuses a change that names
# one of 21 resources someone wrote down. That leaves two holes, and the second
# is the one that matters:
#
#   1. anything the other team creates TOMORROW is not on the list;
#   2. `offenders` only reads DELETIONS, and unlabelable types (IAM bindings,
#      API enablements) are exempt from the label rule entirely -- so an IAM
#      binding granting something on a service account created next week passes
#      both checks in a project holding another team's production.
#
# So: a resource is OURS if it says so, and the ways it can say so are
# deliberately few.
#
#   * it carries managed-by=swarm-terraform, in either half of the change.
#     This is the primary signal and the one `make destroy` already depends on.
#   * it is a CREATE. We are the thing creating it, and `unlabelled_creations`
#     separately requires the label, so a create that is not ours fails there.
#   * its own name begins with the platform prefix. This is the fallback for
#     unlabelable types, which cannot carry the label at all.
#
# `name_of` reads the resource's OWN name rather than `tokens_both`, and that
# distinction is the whole design. `tokens_both` deliberately includes every
# referenced name -- a network, a secret id, a member -- which is right for the
# blocklist, where naming a denied resource is the offence. It is wrong here:
# an IAM member is a *@project.iam.gserviceaccount.com address and a region is
# `us-central1`, so requiring every token to start with `swarm-` would refuse
# every plan this repository has ever produced.
def name_of:
  ( (after.name? // before.name? // "") | tostring ) as $n
  | ( ($n | split("/") | last) // "" );

# $ARGS.named, NOT $prefix, AND THIS IS A COMPILE-TIME DISTINCTION.
#
# `$prefix` is a compile-time binding: jq refuses to COMPILE a program that
# references it when no `--arg prefix` was passed. `plan-guard.sh` passes one;
# `destroy.sh --self-test` does not, and neither does anything else that loads
# this file. So referencing `$prefix` directly took out three CI jobs at once --
# the destroy-guard self-test, the integration suite and the policy assertions --
# and it did so before a single assertion ran, which is why the failure looked
# nothing like "the new check is wrong".
#
# I tested the predicate by invoking jq WITH `--arg prefix`, which is the one
# way not to see this. The lesson is the session's own: a check has to be
# exercised the way its callers call it, not the way its author does.
#
# `$ARGS.named` is always defined, so the `//` default makes the argument
# genuinely optional and the program compiles for every caller.
def platform_prefix:
  ( $ARGS.named.prefix // "swarm-" );

def is_ours($prefix):
  . as $r
  | ( ($r | managed_of(label_map_of(before))) == "swarm-terraform" )
    or ( ($r | managed_of(label_map_of(after))) == "swarm-terraform" )
    or ( (($r.change.actions // []) | index("create")) != null )
    or ( ($r | name_of) | startswith($prefix) );

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

      # Every change -- create, update or delete -- that names something on the
      # deny-list. `denylist_hits` above is the deletion-only view make destroy
      # uses; this is the view an APPLY plan needs, because creating an IAM
      # binding on another team's bucket deletes nothing and would otherwise
      # pass.
      denylist_touches: [ .resource_changes[]?
        | select((.change.actions // []) | index("no-op") | not)
        | . as $r
        | ($r | deny_hits_both($deny)) as $hits
        | select(($hits | length) > 0 or ($r | default_network_hit_both))
        | { address: $r.address, type: $r.type, actions: $r.change.actions,
            matched: (if ($hits|length) > 0 then $hits else ["default-network"] end) } ],

      # WARN-ONLY IN THIS CHANGE. The shell reports it and does not abort on it.
      #
      # The reason is written a few lines up in this file, about a different
      # rule, and applies exactly: "a guard that is wrong about ordinary applies
      # is one people learn to bypass, which is worse than not having it." This
      # predicate has never been run against a real `terraform show -json`
      # apply plan, and the deploy pipeline reached a working state for the
      # first time on 2026-09-24. A false positive here aborts it.
      #
      # It becomes fatal once a real plan has reported it empty. That is one
      # line in plan-guard.sh and it is named in the report it prints.
      foreign_touches: [ .resource_changes[]?
        | select((.change.actions // []) | index("no-op") | not)
        | . as $r
        | select(($r | is_ours(platform_prefix)) | not)
        | { address: $r.address, type: $r.type, actions: $r.change.actions,
            name: ($r | name_of),
            reason: "no managed-by=swarm-terraform in either half, not a create, and its name does not begin with \(platform_prefix)" } ],

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

      # The forward-looking half of the same rule, for an APPLY plan rather than
      # a destroy plan: everything this repository creates must carry
      # managed-by=swarm-terraform, because `make destroy` refuses to delete
      # anything that does not. An unlabelled resource merging into a SHARED
      # project is one nobody can ever clean up -- it has to be found by hand and
      # removed by hand, in a project holding another team's production.
      #
      # Reported here and gated by scripts/lib/plan-guard.sh --mode apply; a
      # destroy plan has no creations, so this is empty there.
      unlabelled_creations: [ $mutations[]
        | . as $r
        | select(($r.type | is_unlabelable($allow_types)) | not)
        | select(($r | labels_unknown) | not)
        | ($r | after_managed_label) as $m
        | select($m != "swarm-terraform")
        | { address: $r.address, type: $r.type,
            actions: $r.change.actions,
            managed_by: ($m // null),
            reason: (if ($r | after_label_map) == null
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
