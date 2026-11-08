# Turn on the merge train

**Status: prepared, not applied.** Nothing in this runbook has been run. The
ruleset change below is the owner's to apply, after a dry-run train has been
seen going green, as the owner decided on 2026-10-14 ("A1 Merge train").
Until then `vars.MERGE_TRAIN` is unset, `auto-merge.yml` behaves exactly as
before, and the train workflow only runs when someone dispatches it.

## Why

Ruleset `main-protection` requires strict up-to-date checks. A
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
number, so main ends up with exactly the tree the train tested. A red train
goes through three rules, in order ([below](#a-red-train)): it re-runs its
failed jobs once and stays open; red again while main is red on the same
checks, it is held and ejects nobody; otherwise it is split in half until the
pull request that broke it is found, and that one is ejected: it gets a
comment linking the failing job, and `ready` is removed.

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
* The SwarmCloud worker's merge step reads the same switch (owner decision,
  2026-11-03; [merge-step.md](../merge-step.md), "Revised 2026-11-03"). With
  `MERGE_TRAIN` `on`, a merge step that is green and told MERGE adds `ready`
  to its pull request and parks until the train has merged it; it no longer
  takes the repository's merge slot or updates its branch. Ejected (the
  train's comment and `ready` removed), it goes to its fix round if the pull
  request now conflicts or is red, or else fails `train_ejected` with the
  train's reason. Nothing else to do for that, with one thing to know: the
  step reads the variable with the tenant's `-git` token, and the tenant
  GitHub App does not have the *Variables: Read* permission today. Without
  it, the step treats the train as on only once step 2's ruleset change
  requires `merge-train`. So, with `merge-train` required, the step follows
  the train from the moment the ruleset changes; without that requirement,
  grant the App *Variables: Read* (App settings, Permissions, Repository
  permissions, Variables) before step 2, or the step keeps merging directly.
  A merge step on the train says which in its result summary:
  `repository_train.on_by` is `variable` when it read the variable, and
  `required_check` when it could not and followed the ruleset.

* Merge steps parked on the train before the variable is turned off again
  take the direct path at their next wake (at most 15 minutes later):
  nothing needs undoing on them.

## 1. Run a train without landing anything

```bash
gh workflow run merge-train.yml -R example-org/example-repo -f dry_run=true
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
gh pr list -R example-org/example-repo --state open --json number,autoMergeRequest \
  --jq '.[] | select(.autoMergeRequest != null) | .number' |
while read -r n; do gh pr merge "$n" -R example-org/example-repo --disable-auto; done
```

Then turn the train on:

```bash
gh variable set MERGE_TRAIN -R example-org/example-repo --body on
```

Then change the ruleset: strict up-to-date OFF, and `merge-train` added to the
required checks. Every other rule, check and bypass is the one the ruleset has
today (read on 2026-10-14). A `PUT` replaces the whole ruleset, so this sends
all of it:

```bash
RULESET_ID="$(gh api repos/example-org/example-repo/rulesets --jq '.[] | select(.name=="main-protection") | .id')"
gh api -X PUT repos/example-org/example-repo/rulesets/"$RULESET_ID" --input - <<'JSON'
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
RULESET_ID="$(gh api repos/example-org/example-repo/rulesets --jq '.[] | select(.name=="main-protection") | .id')"
gh api repos/example-org/example-repo/rulesets/"$RULESET_ID" \
  --jq '.rules[] | select(.type == "required_status_checks") | .parameters'
```

`strict_required_status_checks_policy` should be `false`, and the list should
have six contexts, ending with `merge-train`.

### What requiring `merge-train` costs

The train posts `merge-train` on a pull request's head only once that pull
request has been in a green train. With the context required, **anything that
merges into main some other way is refused**:

* the SwarmCloud worker's merge step, unless it is on the train. Since
  2026-11-03 it is: with the train on it labels its pull request `ready` and
  waits for the train instead of merging (see "Before you start"), so this
  requirement no longer refuses it;
* a person pressing "Squash and merge";
* the hotfix lane, if it merges a pull request.

You can apply the same `PUT` with the `{"context": "merge-train"}` line taken
out. That turns strict off without the requirement: those three keep working,
but main is then a tested tree only for whatever goes through the train. That
choice is the owner's.

## A red train

[the owner], 6 November 2026, after CLD measured the train from 4 November 06:28Z to
6 November 05:30Z: 74 of 139 trains were red, mostly on checks known to fail
at random (`format / unit tests`, `swarm-ui typecheck / component tests`,
single shards such as `unit tests (1/10)`), and 6 of the 18 ejected pull
requests later landed with no change. The decision was to keep the train and
fix red trains. `scripts/merge-train.sh` (`red_rules`) applies three rules in
order, and every comment and step-summary line names the rule that acted:

1. **Retry once.** The first time a train head is red, the train re-runs only
   the failed jobs of its failing Actions runs (`rerun-failed-jobs`), once,
   and stays open. It records `retry=<epoch>:<head>` in its state block, and
   its 90-minute age limit counts from the retry. A green after the retry
   lands every member, just as a first green would. GitHub re-runs failed
   jobs only of a finished run. So while any run at the head is still queued
   or running, a red reading of the head waits and does not bisect. This
   covers a shard that fails while the rest of its run is still going, and a
   re-run that is still in progress.
2. **Hold while main is red.** If the train is red again, the train judges
   main's head on the failing check names, using the same
   latest-run-per-event rule. If main is red on any of them, the train closes
   with `Held: main is red on <names>`. It ejects nobody, removes no `ready`
   label and holds nothing back. No new train boards until main's head is
   green on those checks (pending does not count). A main that stays red for
   `MERGE_TRAIN_MAIN_RED_WAIT_MINUTES` (60) lets a train board anyway, so a
   pull request that fixes main is not kept out by the red it fixes. Such a
   train is held again if it is red on the same checks.
3. **Bisect**, as before. A train of one is ejected after its retry.

**The re-run needs `actions: write`.** The tick's own token has it
(`.github/workflows/merge-train.yml`, the owner's pick of 6 November 2026), and
it makes the re-run by default (`MERGE_TRAIN_RERUN_BY=workflow`). If GitHub
refuses the re-run anyway, the train says `the retry could not run` and goes on
to rules 2 and 3, as it did before the retry existed.

Alternatively, set `MERGE_TRAIN_RERUN_BY=app` in the tick's `env:`. The
merge App's token then makes the re-run instead. That needs the App's
installed **Actions: write** permission and `permission-actions: write` on
its token step. It also has a benefit: a re-run started by the App wakes the
train through `workflow_run` as any App-started run does.

## 3. Watch the first live trains

* Actions → `merge-train`: each tick either says what it is waiting for,
  lands a train, re-runs a red one's failed jobs (rule 1), holds one while
  main is red (rule 2), or bisects one (rule 3). With the train on, a tick runs when an
  application or security run finishes on a train, when one finishes green on
  any other pull request of this repository, and when `ready` is added to a
  pull request of this repository (through `auto-merge`'s run on that label;
  a fork's pull request starts no tick). With no train open, such a tick
  starts one, as a schedule tick does; a train's own run only judges it. The
  five-minute schedule is only the fallback, because GitHub ran it only every
  four to seven hours (2026-11-03). Many events close together make at most two
  ticks: the one running and one pending behind it.
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
gh variable set MERGE_TRAIN -R example-org/example-repo --body off
```

Then run the same `PUT` as above with `"strict_required_status_checks_policy": true`
and without the `{"context": "merge-train"}` line. That is the ruleset as it
was on 2026-10-14. A train already in flight is closed without landing on its
next tick, because `off` means off. Pull requests that were labelled `ready`
while the train was on were never armed, so take `ready` off each one and put
it back to queue it for auto-merge.
