# Create and install the merge GitHub App

**Who:** the owner. **When:** once. **Takes:** about ten minutes.
**Changes:** one GitHub App, one repository variable, one repository secret.

## Why this exists

Measured 2026-10-01: `.github/workflows/auto-merge.yml` refuses to queue a
`ready` pull request because the merge App is not configured. Its comment says
"The merge App is not configured (vars.MERGE_APP_ID and
secrets.MERGE_APP_PRIVATE_KEY). A merge made with the workflow's own
GITHUB_TOKEN would start no build and no release on main, so this does not
fall back to it." (the refusal is at `auto-merge.yml` line 171-172).

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

| Thing | Name | Read at |
|---|---|---|
| Actions variable | `MERGE_APP_ID` | `auto-merge.yml` line 94 (presence check) and line 207 (`app-id`) |
| Actions secret | `MERGE_APP_PRIVATE_KEY` | `auto-merge.yml` line 95 (presence check) and line 208 (`private-key`) |

`actions/create-github-app-token@v2` (line 205) mints an installation token
scoped to this repository only (`repositories:`, line 210) with exactly three
permissions (lines 211-213). That token enables native squash auto-merge
(`gh pr merge --auto --squash`, line 233) or, for an already-green pull request
that GitHub will not queue, merges directly (line 244).

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
| Contents | Read and write | `permission-contents: write`, line 211. Squash-merging writes the commit to `main`. |
| Pull requests | Read and write | `permission-pull-requests: write`, line 212. Enabling auto-merge (line 233) and merging (line 244) are pull request mutations. |
| Workflows | Read and write | `permission-workflows: write`, line 213. A pull request that touches `.github/workflows` cannot be merged by a token without it (owner decision, 2026-09-28). |

  Metadata: Read is added by GitHub automatically. The App does **not** need
  Checks or Actions: the gate reads check runs (line 185) with the workflow's
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
