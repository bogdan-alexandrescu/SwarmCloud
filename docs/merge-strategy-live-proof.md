# Proving the merge strategies against a real repository

`collect`, `direct-pr` and `integrate` are all implemented and all tested
offline against real git. Exactly one of them has ever run against a real
repository, and that one pushes nothing.

This document exists because the gap is not a coding task. Closing it needed a
credential that did not exist, on a repository somebody owns, with write
permission somebody has to grant — three decisions, none of them an
engineer's to take quietly. What follows is what each one is, so the owner can
decide with the facts rather than authorise "whatever it needs".

**Where this stands (2026-09-25).** The owner has taken the decisions. The
credential exists: a **classic** personal access token (the decision is recorded
in section 3), stored by `scripts/create-secrets.sh` as `swarm-tenant-eng-git`,
version 1, at 23:23Z. When this was written, no platform run had pushed a branch
or opened a pull request. Every "verified" line below is a read-only observation,
with the command that produced it and the date it was made.

---

## 1. What has actually run

| strategy | run live? | why |
|---|---|---|
| `collect` | yes, with no repository attached | needs no credential and pushes nothing |
| `direct-pr` | never | needs a git credential with push |
| `integrate` | never | needs the same, on every step |

`collect` is also the default (`DEFAULT_STRATEGY`, `apps/swarm-api/swarm_api/validation.py`),
so every dispatch this platform has ever served took the one path that cannot
push. The other two are proven by `tests/unit/worker/test_strategy_end_to_end.py`,
which runs clone → agent edit → harvest → commit → push → merge → pull request
against a real bare repository on disk with only the HTTP forge faked. That is
the strongest offline proof available; it is not a live one, and the difference
is in section 4.

---

## 2. The credential that is missing

`resolve_git_token` (`apps/agent-worker/agent_worker/secrets.py`) resolves a
**per-tenant** token from `swarm-tenant-<tenant>-git`. Deliberately per-tenant:
a platform-wide clone token would make one malicious repository in one tenant a
credential compromise for all of them (invariant 9).

Verified read-only on 2026-09-22:

```
$ gcloud secrets list --project=saga-agents-staging --filter="name~swarm-tenant" --format="value(name)"
swarm-tenant-eng-anthropic
swarm-tenant-eng-anthropic-refresh
swarm-tenant-eng-openai
swarm-tenant-eng-openai-refresh
swarm-tenant-u-bogdan-anthropic
swarm-tenant-u-bogdan-anthropic-refresh
```

No `-git` secret existed for either tenant on that date. So a **private**
repository could not even be cloned, and a public one cloned unauthenticated.
That is why `collect` against a public repository was the only combination that
had worked.

Read again on 2026-09-25 at 23:25Z (`gcloud secrets describe` and
`get-iam-policy`, metadata only): `swarm-tenant-eng-git` exists, with one
enabled version and the `component=tenant-credential,provider=git,tenant=eng`
labels, and **no IAM binding**. So point 1 below is true for `eng`, and point 3
is not yet true.

Three things have to be true, and the second is the one that gets forgotten:

1. **The secret exists**, created by `scripts/create-secrets.sh`, which owns
   secret values precisely so plaintext never reaches a Terraform state file.
2. **The tenant document lists `git`.** `resolve_git_token` returns `None`
   when `GIT_PROVIDER not in tenant.credentials`, before it asks Secret Manager
   anything. A secret that exists and a tenant that does not claim it produces
   the same silent `None` as no secret at all.
3. **The tenant's service account may read it**
   (`roles/secretmanager.secretAccessor` on that one secret).
   `scripts/register-tenant.sh` already does this per `--provider`, so `git`
   needs no new mechanism. Re-running it to add `git` is not free, though.
   `--providers` REPLACES the tenant's `credentials`, so it has to name every
   provider the tenant keeps. A re-run also rewrites `display_name`,
   `max_active` and `capacity_units` (to defaults of 20 and 40 unless they are
   given), writes `gcs_prefix` without the trailing slash that terraform writes,
   and creates the prefix marker object. So make the two changes on their own:
   `scripts/register-tenant.sh --tenant eng --add-provider git` is exactly the
   one secret binding and a Firestore PATCH whose update mask is `credentials`
   alone, conditional on the document not having changed since it was read.
   Points 2 and 3 are then one command.

**No Terraform change is required**, and that is worth stating because it looks
like it should be. `terraform/infra/locals.tf` builds `secret_env` from a
profile's declared providers and mounts those secrets into the Cloud Run Job.
The git token is **not** mounted: the worker reads it at runtime through the
Secret Manager client and deletes the credential file it writes before the
function returns. Adding `git` to a profile's `secret_env` would put the token
in the agent's own environment — the opposite of what `gitops.py` goes to
trouble to avoid.

Read 2026-09-25 at 23:09Z (Firestore REST `GET` on `tenants/eng` in database
`swarm`, read-only): `credentials` is `["anthropic", "openai"]`. Point 2 is
therefore observed, not only derived. The secret alone is not enough until
`git` is added there.

---

## 3. What the forge has to grant

`probe_repository` (`apps/agent-worker/agent_worker/forge.py`) asks
`GET /repos/{owner}/{repo}` and requires `permissions.push === true`. It reads
the permissions object rather than the `X-OAuth-Scopes` header because that
header exists for classic PATs only, so a platform trusting it would read every
fine-grained token as unscoped.

For `bogdan-alexandrescu/SwarmCloud`, verified read-only and unauthenticated:

```
$ curl -s https://api.github.com/repos/bogdan-alexandrescu/SwarmCloud
private=False  default_branch=main  permissions=None
```

Public, default branch `main`, and an unauthenticated request carries no
`permissions` object at all — which is exactly why an unauthenticated probe is
not attempted: with no token `probe_repository` returns
`can_push=False, reason="no git credential is registered for this tenant"`
without making the request.

The minimum grant per strategy:

| strategy | needs |
|---|---|
| `collect` | nothing. A public clone, or a token with `Contents: Read` for a private one |
| `direct-pr` | `Contents: Read and write` **and** `Pull requests: Read and write` |
| `integrate` | the same, on the one token every step of the workflow shares |

A fine-grained PAT scoped to that single repository is the smallest thing that
works. A classic PAT needs `repo`, which is every repository the account can
see — a much larger blast radius for the same proof.

**Owner decision, 2026-09-25: a classic PAT.** The owner chose a classic
personal access token deliberately, after being told its scope, because
SwarmCloud's agents are to reach repositories in both his personal account and
the saga organisation. The token `eng`'s workers use therefore reaches every
repository that account can reach with `repo` scope, not one repository.
Neither of the following has been checked: whether the saga organisation
accepts classic tokens (an organisation can restrict them), and whether it
requires SAML SSO authorisation per token.

`Contents: write` alone is a real and useful half-step: the branch pushes, the
pull request fails with a stated reason, and `_publish_git` records
`published: true` with "the branch was pushed but no pull request was opened".
That is a legitimate way to test the push path without granting PR rights. It
exists for fine-grained tokens only. A classic token's `repo` scope carries
pull-request rights with it, so this half-step is not available with the token
chosen above.

---

## 4. What a live run would do that the offline tests do not

The offline suite fakes exactly two functions: `probe_repository` and
`open_pull_request`. So what a live proof adds is precisely those two, plus
three things no local remote can reproduce:

* **A real token through a real `credential.helper store` file** over HTTPS.
  Locally the remote is `file://` and the credential file is written, used and
  deleted without ever being consulted. A malformed entry would pass locally
  and fail against GitHub.
* **GitHub's own refusals.** Branch protection on the default branch, a
  required status check, an org policy forbidding PATs — none of which a bare
  repository on disk has. Note that `push_branch`'s `protected` list contains
  only the repository's *default* branch; GitHub's protection **rules** are not
  consulted, so a rule on some other branch surfaces as a push failure, not as
  a refusal.
* **Pushing from a shallow clone over HTTPS.** Verified locally that a depth-1
  clone pushes cleanly to a `file://` remote and that
  `fetch --depth=2147483647` unshallows before the integrator merges. Whether
  GitHub's receive path accepts the same shallow push is **not verified** —
  it is the single most likely live-only failure.

---

## 5. The proof, if it is authorised

Three runs, in this order, each checking one thing the previous one did not.
Stop at the first that does not behave as stated.

**Preparation** (one time, by the repository owner):

1. Mint a personal access token. **Done 2026-09-25**: a classic PAT, not the
   fine-grained single-repository token this step first named (see section 3
   for why).
2. `scripts/create-secrets.sh --tenant eng --provider git --stdin`.
   **Done 2026-09-25 at 23:23Z.**
3. Add `git` to the `eng` tenant's `credentials` and bind
   `roles/secretmanager.secretAccessor` for its GSA — `scripts/register-tenant.sh`
   covers both; confirm the tenant document afterwards.

**Run 1 — `collect`, with a repository attached.** The control. It must clone,
harvest a patch into the task's GCS prefix, and leave the remote untouched even
though the token can now push. Check `git.published == false`, the reason names
`direct-pr`, and `git ls-remote` shows no new `swarm/` ref.

**Run 2 — `direct-pr`, one task.** One branch `swarm/<task-id>` and one pull
request against `main`. Check the branch actually contains the agent's edits:
an empty diff means `commit_dirty` did not stage uncommitted work, which is the
common agent behaviour and the reason that function exists. Check also that
every commit on the branch is authored and committed by the worker's identity
and carries no `Co-Authored-By` trailer or "Generated with" line. Nothing this
platform puts on GitHub may carry Claude attribution. #209 makes the worker
write every commit it pushes. Until #209 is deployed, the proof prompt tells the
agent not to commit, so the only commit is the worker's.

**Run 3 — `integrate`, a workflow of at least three steps.** swarm-api requires
exactly one integrator (`validation.py`). The proof is a count and a tree:
**one** pull request for the whole workflow, whose branch contains every
contributor's file. Two pull requests is the defect that already happened once.

**Cleanup is part of the proof, not after it.** Each run leaves a real branch
and a real pull request on a real repository. Close them and delete the
branches, or the next run's `open_pull_request` adopts the open one it finds
(which is correct behaviour and would make run 2 look like it did nothing).

---

## 6. What this would cost, stated plainly

A token that can write to every repository its account can reach (a classic
PAT, section 3), held by a platform that runs agent code written by a language
model against untrusted repository contents. The
mitigations are real — the token is per-tenant, it reaches git through a 0600
file deleted in a `finally`, it is never in argv, the branch name is derived
from the task id and re-checked before the push, the default branch is refused,
nothing is ever force-pushed, and hooks are disabled on every git invocation so
a `.git/hooks/pre-commit` the agent wrote cannot execute.

They are mitigations, not a boundary. The smallest version of this decision
would have been a fine-grained PAT on one repository that nothing important
depends on, and section 5 was first written assuming it. The owner chose a
classic PAT instead (section 3). The proofs therefore run with a token that
reaches every repository the account can reach, and each of the mitigations
above matters in proportion.
