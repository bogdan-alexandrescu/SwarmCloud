# Onboarding acceptance: the four checks of #780, run by the owner

**Who:** the deployment's owner, with a second person in the same tenant.
**Where:** dev, against the release that carries OB10. **Takes:** about an
hour. That covers two GitHub orgs, two people, one issue run per person per
org, and an org removal. **Why:**
[#780](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/780) says how
it would be known to work, in four lines. They need real GitHub accounts, an
org that enforces SAML SSO (`sagaxyz`), and the App's installation there. No
lane can do that, so each line is a check below. Each check names what you
should see, and the observation that would show it **failed**. The design
is [docs/onboarding.md](../onboarding.md). This page is lane OB10 of its §5.

The two people are **A**, who sets up from the console, and **B**, who sets up
from `/sc:setup` in Claude Code. Each has a personal GitHub account and is a
member of `sagaxyz`. Each needs one scratch repository on their personal
account, plus one `sagaxyz` repository they may push to and one they will not
choose. Below, `<login>` is a person's GitHub login and `<repo>` a scratch
repository. Nothing here is a token: a fallback token is typed at a prompt
and never pasted into this page, an issue, a pull request or a chat.

## Before you start

1. **The refusal is on in dev.** It comes from tfvars
   (`repository_grants_enforced = true` in
   `terraform/environments/dev/dev.tfvars`), so read the value the running
   revision actually has:

   ```bash
   gcloud run services describe swarm-api --region us-central1 \
     --format='value(spec.template.spec.containers[0].env)' \
     | tr ';' '\n' | grep REPOSITORY_GRANTS_ENFORCED
   ```

   You should see `'value': 'true'`. If it says `false`, or prints nothing,
   the release with OB10 has not been applied, and check 3 cannot pass. Stop
   here.
2. **The App is registered and its refresh sweep runs**
   ([github-app.md](github-app.md), steps 1-7).
3. **Neither person has connected yet.** For A and B, `uv run sc setup status`
   (B) and the Overview's setup card (A) show `github_connected` as `[ ]`.
   If either already shows `[x]`, disconnect first
   (`uv run sc access disconnect`, typed). Otherwise check 1 tests a
   returning user, not a new one.

## Check 1. A new user clones, pushes and opens a pull request as themselves in two orgs

The issue's line: a new user, starting from the console or from `/sc`, ends
with SwarmCloud able to clone, push and open PRs as themselves, on
repositories they chose, in two orgs: their personal account and `sagaxyz`.

**A, from the console.**

1. Open the console. The Overview shows the setup checklist. Press **Connect
   GitHub**, authorise the App at GitHub, and come back. `github_connected`
   now says *connected as `<login>`*.
2. Install the App on your personal account, selecting `<repo>`. For
   `sagaxyz`: if you are one of its owners, install it there. If not, press
   **Request install** on Access. The owner shows `ORG_APPROVAL_PENDING` with its
   copy until an org owner approves. If `sagaxyz` will not install the App,
   use the fallback: the **GitHub fallback token** card under Admin › Pool
   limits (the Admin settings page), owner `sagaxyz`, with a fine-grained
   token whose resource owner is `sagaxyz`. The form
   clears the field whatever the answer, and shows the token's name, never
   its value.
3. On Work › Access, enable both owners. Choose `<login>/<repo>` and one
   `sagaxyz` repository, each **Write**.
4. Press **Verify** on both grants. Then, on one write grant, run the
   **push test**. It creates and deletes `swarmcloud/onboarding-check-<nonce>`
   in that repository.
5. Start an issue run on a small issue in each repository, from Work ›
   Submit from a GitHub issue, or by
   `uv run sc run --issue <login>/<repo>#<n> --follow`. Approve
   the plan and let it open its pull request.

**B, from `/sc`.** In Claude Code, run `/sc:setup`. It drives the same
checklist through the bridge tools: connect in the browser, then enable
owners and grant repositories. For `sagaxyz`, B uses the fallback token path
so that D5's CLI route is exercised:

```bash
uv run sc setup token --owner sagaxyz      # the token is read from stdin, with no echo
uv run sc access orgs
uv run sc access grant <login>/<repo> --write
uv run sc access grant sagaxyz/<repo> --write
uv run sc access verify <login>/<repo> sagaxyz/<repo>
uv run sc access verify sagaxyz/<repo> --push-test   # type the repository to confirm
uv run sc run --issue sagaxyz/<repo>#<n> --follow
```

**You should see**, for each person and each of the two owners:

* the issue run's pull request open on GitHub, **authored by `<login>`**,
  that person's own account. Through the App, GitHub shows `<login>` with the
  App's name beside it;
* the run's tasks cloned and pushed: the branch is on GitHub, and the pull
  request's commits are there;
* the task record the API serves, `GET /v1/tasks/<task_id>`, answering
  `forge_credential_source: "the submitter's GitHub credential"`;
* the push test reporting its branch created and deleted, and no
  `swarmcloud/onboarding-check-*` branch left in the repository afterwards.

**It failed if** a pull request is authored by any account other than the
submitter's (the tenant token's account means the task ran as the tenant).
It also failed if `forge_credential_source` reads "tenant token, service
submission" for a person's task. Other failures: a clone, push or pull
request that only one of the two owners allows; the `sagaxyz` leg refused
with `SSO_NOT_AUTHORISED`, `CLASSIC_PAT_BLOCKED` or `FINE_GRAINED_PAT_PENDING`
after its recovery copy was followed; and a `swarmcloud/onboarding-check-*`
branch still present after the push test.

## Check 2. Every step shows its verified state

The issue's line: every step shows its verified state.

1. At each stage of check 1, read the checklist on both surfaces: the console
   setup card, and `uv run sc setup status` (exit 0 ready, 3 not ready).
   Every step is one of `[x]` done, `[ ]` todo, `[~]` in progress, `[!]`
   failed or `[?]` stale. A failed step carries its code and its recovery
   copy, word for word the same on both surfaces.
2. On Access (or `uv run sc access list`), each grant shows its `clone`,
   `push` and `pull_request` checks as `ok`, `missing` or `unknown`, each with
   when it was checked. The push test shows its own result.
3. **The control: the state must be able to come out the other way.** Before
   step 1 of check 1, `github_connected` is `[ ]`. Once the App is installed
   on the personal account only, `orgs_enabled` names `sagaxyz` as not
   installed, or as `ORG_APPROVAL_PENDING` once requested, and is not done
   for it. On a **Read** grant, `push` and `pull_request` are not required,
   and the push test is refused.

**It failed if** a step shows `[x]` before the thing it checks happened (for
example `github_connected` before anyone authorised). It also failed if a
check shows `ok` for a repository the token cannot reach, the console and
`sc` disagree on a step's state or copy, or a failed step shows no code. A
step that answers `FORGE_UNREACHABLE` is not a failure of this check: it says
the result is unknown and re-checks.

## Check 3. A repository the user did not choose is refused

The issue's line: a repository they did not choose is refused.

1. As B, pick a `sagaxyz` repository B can reach at GitHub but did **not**
   grant under Access, and start a run on it:

   ```bash
   uv run sc run --issue sagaxyz/<not-chosen>#<n>
   ```

2. As A, do the same from the console's Work › Submit from a GitHub issue.
3. As B, start a run on a repository granted **Read** only.

**You should see** steps 1 and 2 refused at submission, before anything is
stored or queued: 403 `REPOSITORY_NOT_GRANTED`, "repository
sagaxyz/<not-chosen> is not granted to you: choose it under Access". This is
the refusal `REPOSITORY_GRANTS_ENFORCED` turns on (checked in "Before you
start"). For step 3, the run is accepted, because a read grant may read.
Its step that pushes or opens a pull request ends before its agent runs,
refused by the worker with `forge_read_grant` (D9: a read grant never
pushes). No branch appears on GitHub.

**It failed if** the run on the unchosen repository is accepted, whatever it
does next. While the switch is off, it runs with the tenant token, so an
accepted run means the switch is not on in the running revision. It also
failed if the refusal names a different code, or a branch from the read-only
grant's run appears on GitHub.

## Check 4. Removing an org revokes SwarmCloud's access to it

The issue's line: removing an org revokes SwarmCloud's access to it.

1. As B, who enabled `sagaxyz` through the fallback token, remove it:

   ```bash
   uv run sc access remove-org sagaxyz       # type the owner to confirm
   ```

   This is `DELETE /v1/access/orgs/{owner}`. It deletes the owner and every
   grant under it in one write. Because B enabled it through a token, it
   also disables every version of B's `sagaxyz` slot. The answer says
   `token_revoked` and how many slot versions were disabled.
2. As A, who enabled `sagaxyz` through the App, remove it from Work › Access.
   The page offers the installation's settings link. The App stays installed
   until an org owner removes it, and the page says so.
3. Each of them starts a run on the `sagaxyz` repository they had granted:
   `uv run sc run --issue sagaxyz/<repo>#<n>`.
4. For B's token, read Secret Manager's record of the slot (names and
   states only, never `access`):
   `gcloud secrets versions list swarm-tenant-<tenant>-git-u-<16 hex>`, where
   the slot's name is the one the token answer named in check 1.

**You should see** step 3 refused with `REPOSITORY_NOT_GRANTED` for both. The
`sagaxyz` owner and its grants should be gone from Access and from
`uv run sc access list`, while the personal-account owner and grant remain.
Every version of B's `sagaxyz` slot should be `DISABLED`. A run already going
when the org was removed is refused at its next push or pull request, where
the worker re-reads the grant.

**It failed if** a run on a `sagaxyz` repository is accepted after the
removal. It also failed if any version of B's `sagaxyz` slot is still
`ENABLED`, if `sagaxyz` still lists in Access, or if the personal-account
grant went too (a removal reaches only the owner removed).

## Recording the result

Write the outcome on #780 as a comment, one line per check: passed, or the
observation that failed. Name the date, the release (the swarm-api revision)
and the two people's roles, never their tokens. A failed check is a `bug`
issue in the form's sections, linked from that comment.
