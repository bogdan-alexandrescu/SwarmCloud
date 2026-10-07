# Create and install the merge GitHub App

**Who:** the owner. **When:** once. **Takes:** about ten minutes.
**Changes:** one GitHub App, one repository variable, one repository secret.

## Why this exists

Measured 2026-10-01: `.github/workflows/auto-merge.yml` refuses to queue a
`ready` pull request because the merge App is not configured. Its comment says
"The merge App is not configured (vars.MERGE_APP_ID and
secrets.MERGE_APP_PRIVATE_KEY). A merge made with the workflow's own
GITHUB_TOKEN would start no build and no release on main, so this does not
fall back to it." (the refusal is item 4 of the `enable` job's `gate` step).

Until this runbook is done, a `ready` pull request merges only while an
operator's session runs the merge watcher. The reason an App is needed and the
GITHUB_TOKEN is not enough is in
[docs/ci.md](../ci.md#why-the-merge-uses-a-github-app-token-not-the-github_token):
a merge made with the GITHUB_TOKEN raises no `push` event, so `application.yml`
(the one image build) and `release.yml` would never run for main.

This App merges **this repository's** pull requests. It is not the SwarmCloud
merge step ([docs/merge-step.md](../merge-step.md), #295), which is the separate,
per-tenant way SwarmCloud's own chains merge, with the tenant's own forge
token from Secret Manager. The two do not share credentials.

## What the workflow expects

Step names below are `auto-merge.yml`'s; line numbers drift, step ids do not.

| Thing | Name | Read at |
|---|---|---|
| Actions variable | `MERGE_APP_ID` | the `enable` job's `gate` step (presence check), both `app-token` steps (`app-id`), and the `disable` job's `app-token` condition |
| Actions secret | `MERGE_APP_PRIVATE_KEY` | the `gate` step (presence check) and both `app-token` steps (`private-key`) |

`actions/create-github-app-token@v2` (the `app-token` steps) mints an
installation token scoped to this repository only (`repositories:`). In the
`enable` job it carries exactly three permissions and enables native squash
auto-merge (`gh pr merge --auto --squash`, the `merge` step) or, for an
already-green pull request that GitHub will not queue, merges directly. It is
also what turns auto-merge **off** -- see below. The `disable` job's token
asks for contents and pull requests only: turning auto-merge off writes no
workflow file.

## The App also turns auto-merge off (#795)

Measured 2026-10-07: the `disable` job, on a push after `ready`, ran
`gh pr merge --disable-auto` with the workflow's GITHUB_TOKEN, which held only
`pull-requests: write`. GitHub answered
"Resource not accessible by integration" every time, and an `|| true` after
the call hid it. So `ready`
was removed and auto-merge stayed armed: 17 of the 24 pull requests merged
that day merged with no label, at a head nobody had labelled -- the #269 rule
that `ready` binds to the head it was given for was not enforced for the
merge itself.

Why the GITHUB_TOKEN was refused: that token held `pull-requests: write`
only, and the auto-merge mutation evidently needs more -- most likely
`contents: write`, which the App's token has always carried for enabling. Rather
than widen a `pull_request_target` job's GITHUB_TOKEN to `contents: write`,
the identity that armed auto-merge, holding both, turns it off: the `disarm` step (the same
script in both jobs, which a test holds equal) reads whether auto-merge is
armed with the GITHUB_TOKEN, turns it off with the App's token, and reads it
back. Each of these fails the job, with an `::error::` saying auto-merge is
still on:

* GitHub refuses the disable (for example the App lost its pull requests or
  contents permission);
* GitHub still reports auto-merge armed after a disable it accepted;
* auto-merge is armed and the App is not configured (`MERGE_APP_ID` unset), so
  nothing here can turn it off.

The `ready` label is still removed after a failed disable, and the comment
says auto-merge could not be turned off. The fix is the App's permissions
(step 1 below); re-run the failed job once they are back.

Not verified from this repository: exactly which grant GitHub found missing
(the error does not say). The App's token holds contents and pull requests
write, so the fix does not depend on it; a GITHUB_TOKEN with `contents: write`
might also have worked, and was deliberately not tried. The first push to an
armed pull request after this lands is the measurement: the `disarm` step's
log says `turned off auto-merge`, or the job is red.

### A pure base merge keeps `ready` (owner decision, 2026-10-07)

A push whose new head is a **pure base merge** -- a merge commit whose first
parent is the head that carried `ready`, whose second parent is already on the
base branch, and whose tree is exactly the merge of those two, which is what
GitHub's "Update branch" makes -- keeps `ready` and auto-merge: the `disable`
job's `classify` step says so and every later step is skipped. It reads the
commits by sha into a scratch bare repository (`contents: read`, nothing
checked out, nothing run) and compares the head's tree with
`git merge-tree --write-tree` of its parents. Any other push -- a commit, a
merge that also changes something, a merge of a branch not on the base, a
force-push, a reopen, a new base -- turns auto-merge off and removes `ready`.
If the classifier cannot read the commits it says so in a warning and treats
the push as any other.

The `enable` job uses the same classifier when GitHub refuses to arm with
"expected head oid does not match" (the runner queue was 400-600 s on
2026-10-07, long enough for an Update branch to land): if the head moved by a
pure base merge of the labelled head it arms once more at the new head; if it
moved by anything else it arms nothing, because that head was never labelled.

The disable job classifies a push against `before`, the head just before it,
so the exemption is only safe if **every push's own run executes**. In one
shared concurrency group a newer pending run cancels the older pending one:
label L, push a foreign commit A, click Update branch (B merges main into A),
and B's run -- correctly a pure merge over A -- would have cancelled A's run and
left auto-merge armed with `ready` on an unreviewed A. So every
`pull_request_target` run except `labeled` has a concurrency group of its own
(the run id is in it) and is never cancelled; A's run disarms and strips in
whichever order the two runs land. Two `labeled` runs still share a group.
Running a disable beside a `labeled` run is safe because every arming is
pinned with `--match-head-commit`, and the enable job re-arms only over a pure
base merge of the head it was labelled at.

### Fewer runs (#795)

`auto-merge.yml` re-evaluates `ready` pull requests when a CI run completes.
It used to listen to application, ci-gate, security and terraform, which
started 294+ runs of it in one night, nearly all skipped. ci-gate is the last
job of application.yml and waits for terraform's run at its head, so only
application's (that is, ci-gate's) and security's completions can be the last
one on a head; those are the two it listens to now.

### Only a required check holds the merge (#815)

On 2026-10-07 #811 got `ready` while code scanning's **Trivy** check was still
running. Trivy is not one of the five checks ruleset `main-protection`
(24160219) requires, and no application or security run reports it, so the
gate waited on it and nothing looked again when it finished: the merge queue
stalled 10:30-15:40Z until an operator ran `gh workflow run auto-merge.yml -f
pr=811`. Two changes since:

* **The gate waits only for a required check still running** — the names it
  already reads for its "no required checks" refusal: the branch's effective
  rules plus any classic protection. Anything else still running does not
  hold it; native auto-merge waits for every required check anyway. A check
  that already **failed** still refuses, required or not. With every required
  check green and a non-required one still running, GitHub reports the pull
  request `UNSTABLE` and will not *enable* auto-merge on it, so the workflow
  merges it directly (or enqueues it on a merge-queue base), as it does on
  `CLEAN`; the ruleset still decides.
* **`check_suite: completed` is a second trigger** of the `requeue` job, which
  dispatches the re-evaluation for each open `ready` pull request at the
  suite's head. GitHub runs no workflow on `check_suite` for a suite GitHub
  Actions created, so it fires only for another App's suite (code scanning's,
  for one); `workflow_run` stays the way in for application and security.
  Whether GitHub raises it for a code-scanning suite created by an upload the
  workflow's own token made has not been observed here; the narrowed wait
  above does not depend on it.

To check it: label a pull request while a non-required check runs and its
required ones are green. The label run's `queue for auto-merge` job should
arm or merge it, not comment "waiting". A pull request still waiting on a
required check names only required checks in its comment. If one ever sits
labelled with every required check green, `gh workflow run auto-merge.yml
--repo bogdan-alexandrescu/SwarmCloud -f pr=<number>` makes it look again.

## 1. Create the App

Open <https://github.com/settings/apps/new> (as the user that owns the
repository).

* **GitHub App name:** `swarmcloud-merge` (any unused name).
* **Homepage URL:** the repository URL.
* **Webhook:** untick **Active**. The workflow is pull and mint; nothing calls
  the App.
* **Repository permissions** (everything else stays "No access"):

| Permission | Level | Why, and the line that needs it |
|---|---|---|
| Contents | Read and write | `permission-contents: write`. Squash-merging writes the commit to `main`; enabling and disabling auto-merge need it too. |
| Pull requests | Read and write | `permission-pull-requests: write`. Enabling auto-merge, turning it off (the `disarm` steps, #795) and merging are pull request mutations. |
| Workflows | Read and write | `permission-workflows: write`, the `enable` job only. A pull request that touches `.github/workflows` cannot be merged by a token without it (owner decision, 2026-09-28). |

  Metadata: Read is added by GitHub automatically. The App does **not** need
  Checks or Actions: the gate reads check runs with the workflow's
  own GITHUB_TOKEN, which holds `checks: read`, not with the App token.
  The token mint requests only permissions the App already holds, so an App
  with fewer than these three makes the mint step fail.
* **Where can this GitHub App be installed:** **Only on this account.**

Click **Create GitHub App** and note the **App ID** on the next page (a number;
it is not a secret, and it is not written in this repository).

**The `workflows` permission is the sensitive one.** A leaked key could
rewrite what `.github/workflows/*.yml` do on the next push. Hence: install on
one repository, keep the key only in the Actions secret.

## 2. Install it on this repository only

App settings, **Install App**, the owner account, **Install**, then choose
**Only select repositories** and pick `bogdan-alexandrescu/SwarmCloud`. Do not
choose "All repositories". Confirm on the installation page that exactly one
repository is listed.

## 3. Generate a private key

On the App's settings page, **Private keys**, **Generate a private key**. The
browser downloads a `.pem`. Move it somewhere outside any repository checkout
(for example `~/merge-app.private-key.pem`).

## 4. Store the id and the key, then delete the file

```bash
gh variable set MERGE_APP_ID --repo bogdan-alexandrescu/SwarmCloud --body '<the App ID>'
gh secret set MERGE_APP_PRIVATE_KEY --repo bogdan-alexandrescu/SwarmCloud < ~/merge-app.private-key.pem
rm ~/merge-app.private-key.pem
```

The key lives only in that Actions secret: never in this repository, a tfvars
file, a Job environment or a log. Also confirm native auto-merge is allowed on
the repository (once, if not already done):

```bash
gh api --method PATCH repos/bogdan-alexandrescu/SwarmCloud -F allow_auto_merge=true
```

## 5. Verify

1. Open a small docs-only pull request and let its checks finish.
2. Add the `ready` label.
3. `gh run list --repo bogdan-alexandrescu/SwarmCloud --workflow auto-merge.yml --limit 3`
   shows a run for the label event. Its `queue for auto-merge` job should be
   green and the pull request should show "Auto-merge enabled", or be merged
   at once if it was already green.
4. After the merge, check that `application.yml` and `release.yml` started on
   the push to `main`. That is the property the App exists for; a merge with no
   workflow runs after it means the merge went through the GITHUB_TOKEN path and
   something is wrong.

If the run instead comments "The merge App is not configured", the variable or
the secret is missing: `gh variable list` and `gh secret list` show names (never
values).

5. Then push a commit to that pull request (or one like it) while it is
   armed: the `disable auto-merge on a new head` job should be green, its log
   should say `turned off auto-merge`, and the pull request should no longer
   show "Auto-merge enabled". A red job with "stays enabled" means the App
   cannot turn auto-merge off -- check its pull requests and contents
   permissions. "Update branch" instead should leave both the label and
   auto-merge on.

## Rotate the key

1. App settings, **Private keys**, **Generate a private key** (a second key is
   allowed alongside the first).
2. `gh secret set MERGE_APP_PRIVATE_KEY --repo bogdan-alexandrescu/SwarmCloud < new.pem`, then `rm new.pem`.
3. Verify as above with a `ready` label.
4. Delete the old key in the App's **Private keys** list.

## Revoke

* **One key:** delete it in the App's **Private keys** list. Tokens already
  minted expire within the hour.
* **The whole App:** App settings, **Advanced**, or the installation page,
  **Suspend** or **Uninstall**; then `gh secret delete MERGE_APP_PRIVATE_KEY`
  and `gh variable delete MERGE_APP_ID`. `auto-merge.yml` goes back to refusing
  with the "not configured" comment, and `ready` merges go back to the operator's
  merge watcher.
* A suspected leak is both: delete the key first, then review recent changes
  to `.github/workflows` on `main`.
