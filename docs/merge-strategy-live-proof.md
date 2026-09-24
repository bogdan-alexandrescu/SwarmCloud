# Proving the merge strategies against a real repository

`collect`, `direct-pr` and `integrate` are all implemented and all tested
offline against real git. Exactly one of them has ever run against a real
repository, and that one pushes nothing.

This document exists because the gap is not a coding task. Closing it needs a
credential that does not exist, on a repository somebody owns, with write
permission somebody has to grant — three decisions, none of them an
engineer's to take quietly. What follows is what each one is, so the owner can
decide with the facts rather than authorise "whatever it needs".

**Nothing in this document has been executed.** No token was created, no branch
was pushed, no pull request was opened. Every "verified" line below is a
read-only observation, with the command that produced it.

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

No `-git` secret exists for either tenant. So today a **private** repository
cannot even be cloned, and a public one clones unauthenticated — which is why
`collect` against a public repository is the only combination that has worked.

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
   needs no new mechanism.

**No Terraform change is required**, and that is worth stating because it looks
like it should be. `terraform/infra/locals.tf` builds `secret_env` from a
profile's declared providers and mounts those secrets into the Cloud Run Job.
The git token is **not** mounted: the worker reads it at runtime through the
Secret Manager client and deletes the credential file it writes before the
function returns. Adding `git` to a profile's `secret_env` would put the token
in the agent's own environment — the opposite of what `gitops.py` goes to
trouble to avoid.

> I could not read the `tenants/eng` Firestore document to confirm what its
> `credentials` array currently holds; the read was refused by this session's
> tooling. Point 2 above is derived from the code and from the secret listing,
> not observed. Check it before concluding the secret alone is enough.

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

`Contents: write` alone is a real and useful half-step: the branch pushes, the
pull request fails with a stated reason, and `_publish_git` records
`published: true` with "the branch was pushed but no pull request was opened".
That is a legitimate way to test the push path without granting PR rights.

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

1. Mint a fine-grained PAT on `bogdan-alexandrescu/SwarmCloud` with
   `Contents: Read and write` and `Pull requests: Read and write`.
2. `scripts/create-secrets.sh --tenant eng --provider git --stdin`
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
common agent behaviour and the reason that function exists.

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

A token that can write to a repository, held by a platform that runs agent code
written by a language model against untrusted repository contents. The
mitigations are real — the token is per-tenant, it reaches git through a 0600
file deleted in a `finally`, it is never in argv, the branch name is derived
from the task id and re-checked before the push, the default branch is refused,
nothing is ever force-pushed, and hooks are disabled on every git invocation so
a `.git/hooks/pre-commit` the agent wrote cannot execute.

They are mitigations, not a boundary. The smallest version of this decision is
a fine-grained PAT on one repository that nothing important depends on, and
that is what section 5 assumes.
