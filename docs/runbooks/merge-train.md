# Turn on the merge train

**Status: prepared, not applied.** Nothing in this runbook has been run. The
ruleset change below is the owner's to apply, after a dry-run train has been
seen going green, as the owner decided on 2026-10-14 ("A1 Merge train").
Until then `vars.MERGE_TRAIN` is unset, `auto-merge.yml` behaves exactly as
before, and the train workflow only runs when someone dispatches it.

## Why

Ruleset `main-protection` (id 24160219) requires strict up-to-date checks. A
green `ready` pull request that is behind main cannot merge until its branch
is brought up to date and its CI runs again, so every merge to main sends the
other green pull requests round once more. Measured on 2026-10-14: a median
of 17 minutes from green to merged, a p90 of 173 minutes, and 2.93 CI runs per
pull request. GitHub's merge queue fixes exactly this, but GitHub does not
offer it on a repository owned by a user account
([docs/ci.md](../ci.md), "Merging through the merge queue").

The train does the same job from a workflow
([`.github/workflows/merge-train.yml`](../../.github/workflows/merge-train.yml),
[`scripts/merge-train.sh`](../../scripts/merge-train.sh)). Up to four `ready`
pull requests that are green at their head are squashed in order onto main as
`train/<id>`, which is opened as a draft pull request so that application,
security and terraform run their pull-request checks on it. Once the train is
green, each pull request is squash-merged by GitHub with its own title and
number, so main ends up with exactly the tree the train tested. A red train is
split in half until the pull request that broke it is found, and that one is
ejected: it gets a comment linking the failing job, and `ready` is removed.

Strict up-to-date and the train do the same job. With both turned on, the
train's second merge in each batch is refused, because main moved when the
first one landed. So turning the train on also means turning strict off. The
ruleset then requires the train's own `merge-train` commit status instead, so
nothing reaches main without having been in a green train.

## Before you start

* The merge App is configured (`vars.MERGE_APP_ID`,
  `secrets.MERGE_APP_PRIVATE_KEY`; [merge-app.md](merge-app.md)). The train
  pushes, opens pull requests and merges with its token, exactly as
  `auto-merge.yml` does. It never uses a personal token.
* The pull request that adds the train has merged. The train's
  `workflow_run` trigger only works once the workflow file is on the default
  branch.

## 1. Run a train without landing anything

```bash
gh workflow run merge-train.yml -R bogdan-alexandrescu/SwarmCloud -f dry_run=true
```

While `MERGE_TRAIN` is not `on`, every train is a dry run, whatever the input
says. The dispatched run boards up to four green `ready` pull requests and
opens a draft pull request titled `Merge train <id>: #a, #b, ...`. Nothing is
said on the boarded pull requests. When that pull request's CI finishes, the
train workflow wakes up again, judges the train, and closes it with a comment
of either `Green. **Dry run:** nothing landed. ...` or `Red. **Dry run:** ...`
followed by the failing jobs.

Check these before going further:

* the train's pull request ran `ci-gate`, `secret scan`, `trivy (repo)`,
  `checkov (terraform + kubernetes)` and `platform policy assertions` at its
  head;
* the closing comment matches what those checks said;
* `train/<id>` has been deleted.

## 2. Turn the train on, then change the ruleset

Do these two straight after each other. Between them, either the train lands
only the first pull request of each batch (if the variable goes first), or
nothing merges at all (if the ruleset goes first).

First, turn off native auto-merge that is already armed on open pull
requests. Once strict is off it would merge them on their own, without the
train having tested them together:

```bash
gh pr list -R bogdan-alexandrescu/SwarmCloud --state open --json number,autoMergeRequest \
  --jq '.[] | select(.autoMergeRequest != null) | .number' |
while read -r n; do gh pr merge "$n" -R bogdan-alexandrescu/SwarmCloud --disable-auto; done
```

Then turn the train on:

```bash
gh variable set MERGE_TRAIN -R bogdan-alexandrescu/SwarmCloud --body on
```

Then change the ruleset: strict up-to-date OFF, and `merge-train` added to the
required checks. Every other rule, check and bypass is the one the ruleset has
today (read on 2026-10-14). A `PUT` replaces the whole ruleset, so this sends
all of it:

```bash
gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219 --input - <<'JSON'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [
    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
  ],
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "required_reviewers": [],
      "require_extra_approval_for_unattributed_changes": true,
      "allowed_merge_methods": ["merge", "squash", "rebase"]
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": false,
      "do_not_enforce_on_create": false,
      "required_status_checks": [
        {"context": "secret scan"},
        {"context": "trivy (repo)"},
        {"context": "checkov (terraform + kubernetes)"},
        {"context": "platform policy assertions"},
        {"context": "ci-gate"},
        {"context": "merge-train"}
      ]
    }}
  ]
}
JSON
```

Read it back:

```bash
gh api repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219 \
  --jq '.rules[] | select(.type == "required_status_checks") | .parameters'
```

`strict_required_status_checks_policy` should be `false`, and the list should
have six contexts, ending with `merge-train`.

### What requiring `merge-train` costs

The train posts `merge-train` on a pull request's head only once that pull
request has been in a green train. With the context required, **anything that
merges into main some other way is refused**:

* the SwarmCloud worker's merge step. It merges with the tenant's own token,
  and this repository's `merge_policy` is `on_merge_verdict`. Its pull
  requests would then need `ready` and would land through the train;
* a person pressing "Squash and merge";
* the hotfix lane, if it merges a pull request.

You can apply the same `PUT` with the `{"context": "merge-train"}` line taken
out. That turns strict off without the requirement: those three keep working,
but main is then a tested tree only for whatever goes through the train. That
choice is the owner's.

## 3. Watch the first live trains

* Actions → `merge-train`: each scheduled tick either says what it is waiting
  for, lands a train, or bisects one.
* Every landed pull request is marked **merged** by GitHub, with the subject
  `<title> (#N)`. `auto-merge.yml`'s `close-issues` job closes the issues it
  names, as before.
* A comment on the train's pull request starting `**Warning:** ... main's tree
  ... is not the train's tested tree` means something else merged into main
  while the train was landing. Find out what.
* A train still pending after 90 minutes (`vars.MERGE_TRAIN_MAX_AGE_MINUTES`)
  is abandoned and rebuilt. `vars.MERGE_TRAIN_MAX_PRS` changes the batch size
  from 4.

## Rolling back

Strict comes back on and `merge-train` is no longer required. Then
`auto-merge.yml` arms native auto-merge on `ready` again:

```bash
gh variable set MERGE_TRAIN -R bogdan-alexandrescu/SwarmCloud --body off
```

Then run the same `PUT` as above with `"strict_required_status_checks_policy": true`
and without the `{"context": "merge-train"}` line. That is the ruleset as it
was on 2026-10-14. A train already in flight is closed without landing on its
next tick, because `off` means off. Pull requests that were labelled `ready`
while the train was on were never armed, so take `ready` off each one and put
it back to queue it for auto-merge.
