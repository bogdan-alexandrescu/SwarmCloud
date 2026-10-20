# Runner git tools: the platform does the git work a runner asks for mid-run, and the runner never holds a token

**Status: DESIGN, for the owner's approval (#1225). Nothing described here is
built.** The owner decided on 2026-10-13 at about 02:05 PDT, on #1225: "design
first, then build". This document is that design. Build lanes start only after
the owner approves it, and the credential-path lanes get one security review
([§7](#7-the-build-plan)). The choices the owner still has to make are in
[§8](#8-open-questions-for-the-owner). Every one of them has a recommended
answer, so the plan can go ahead as written once the owner has answered them.

Code is cited as `path::Symbol`, never by line number. The 2026-10-02 merge
wave moved `lifecycle.py` by hundreds of lines and broke every line citation
(`tests/unit/scripts/test_docs_cite_worker_symbols.py`). All of the code was
read on 2026-10-13 at `cd49267`.

---

## 0. What exists today, and the constraint that shapes the rest

**A runner holds no forge credential while its agent runs, and that is
deliberate.** The worker publishes at the end of the attempt
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._publish_git`). It does
not read the agent's forge token until three things have happened:

* every process the agent started is dead (`Worker.reap_before_publish`; the
  reasoning is in `apps/agent-worker/agent_worker/procman.py`, in the block
  above `_sigkill_all_other_processes`);
* the worker has made itself non-dumpable
  (`apps/agent-worker/agent_worker/hardening.py::make_non_dumpable`; without
  that, `Worker._git_token_refusal` will not read the token at all);
* the work has been copied into a worker-owned publish repository
  (`apps/agent-worker/agent_worker/gitops.py::prepare_publish_repo`,
  `mirror_worktree`).

The reason is spelled out in `procman.py` and repeated in
`Worker._push_carrier_branch`. **The agent and the worker run as the same
uid.** A surviving agent process could read the 0600 credential file that
`gitops.py::_write_credentials` writes. It could also add an `insteadOf` to a
repository the token is about to be used in. For the same reason the carrier
push refuses to run "while the runner is running"
(`Worker._carrier_push`). The owner accepted that narrowing on 2026-10-02.

**Consequence: a mid-run tool cannot run in the runner's container.**
Anything the worker does with a token while the agent is alive, the agent can
reach: through the credential file, through `/proc/<pid>/environ` of a git
subprocess, or through a loopback proxy it can connect to as well as the
worker can. So in this design the token is used only by a platform service
outside the container. What moves between the container and that service is
git objects, as bundles, and signed requests. A secret never makes that trip.

What else exists and is reused here, not rebuilt:

| piece | where | what it already does |
|---|---|---|
| attempt key | `apps/agent-worker/agent_worker/children.py::AttemptKey`, `apps/swarm-api/swarm_api/childkey.py` | An Ed25519 key that lives only in the worker's non-dumpable heap. The worker registers it with a one-use nonce while the agent is STARTING, before the agent exists. It signs method, path, body hash and timestamp. **This is the only thing that proves "the worker, not the agent"**: the agent can mint the same Google ID token its worker mints (childkey.py's module docstring). |
| worker → API auth | `apps/swarm-api/swarm_api/children.py::ChildService.authenticate_worker`, `ChildService.verify_proof` | The ID token must be audience-pinned and must name the tenant's worker service account. The proof is then checked against the registered key. Every failure is the same 403. |
| fence check | `apps/swarm-api/swarm_api/children.py::_lease_is_live` | In one transaction it checks: `task.current_lease_id`, `current_generation`, the lease's agent, attempt and generation, `released_at is None`, and that the lease has not expired. A failure is 409 `child_submit_fenced`. The worker then ends the attempt without touching the lease (`Worker._exit_fenced_mid_run`). |
| agent → worker channel | `children.py::ChildPath` (`prepare`, `has_requests`, `tick`) | A file spool under `work/.swarm-children/`, announced in `SWARM_CHILDREN`. The worker validates each request and relays it. The worker's run loop ticks it on every control poll. |
| slot read by swarm-api | `apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens.read_slot` | Reads `swarm-tenant-<t>-git`, `-git-u-<hex>` and `-git-r-<hex>` through bindings Terraform already grants swarm-api (`terraform/bootstrap/forge_user_slots.tf`, `forge_refresh_reader`). The secret name always comes from `Tenant.secret_name`. |
| CI read at a head sha | `apps/swarm-api/swarm_api/forgewrite.py::GitHubWriter.check_runs`, `commit_statuses`, `branch_rules`, `job_log_tail`; `apps/swarm-api/swarm_api/forgechecks.py::evaluate` | What `apps/swarm-api/swarm_api/issueci.py::_read_ci` uses to call a pull request green, pending or red. |
| the push rules | `gitops.py::push_branch` | Refuses a branch outside `swarm/`, refuses the forge's own default branch, never forces, never pushes tags, never runs hooks. |
| which branch is "the task's own" | `apps/agent-worker/agent_worker/continuation.py::publish_branch`, `Worker._pr_author_branch` | Derived from task ids in the **signed** dispatch block, never read as a name. |
| leak scans | `lifecycle.py::final_tree_leak`, `replay_agent_commits` (`_leaks_in_added_text`) | Every commit and the final tree are scanned before a push. |
| App installation token | `apps/agent-worker/agent_worker/forge.py::mint_installation_token`, `revoke_installation_token` | One repository, explicit permissions, `DELETE /installation/token` in a `finally`. Its only caller today is post-verdict (`apps/agent-worker/agent_worker/post_verdict.py::run_post_verdict`). |

**What does not exist yet:**

* No runner is given an MCP server. `agent_worker.graphmcp` exists, but its
  docstring says "Nothing starts it today", and
  `apps/agent-worker/agent_worker/runners/claude_code.py` passes no
  `--mcp-config`.
* There is no per-tenant list of *other* repositories an agent may read. The
  `repositories` collection (`apps/swarm-api/swarm_api/repositories.py`)
  calls itself "OPT-IN CONTEXT, NOT A GATE".
* Nothing records the forge calls an agent makes.

---

## 1. The tool surface

### 1.1 The tools

The runner sees **five tools** in one MCP server named `swarm-git`. All five
take only what the agent legitimately chooses. None of them takes a
repository URL for the agent's own repository, a token, a remote or a refspec.

| tool | arguments | returns |
|---|---|---|
| `git_fetch` | `ref`: a branch or tag name, or a 40-hex sha, in **the agent's own repository** | the sha `ref` resolved to. The objects land in the agent's clone as `refs/remotes/origin/<ref>` (a sha lands as `refs/swarm/fetched/<sha>`). |
| `git_rebase_onto` | `ref`, as for `git_fetch` | `{onto, head, conflicts: [paths]}`. It is `git_fetch(ref)` followed by `git rebase <fetched>` in the agent's clone. On a conflict the rebase is **left in progress** with the conflicted paths listed, for the agent to resolve and `git rebase --continue` itself. |
| `git_push_branch` | `branch`, optional. When given, it must be one of the branches this agent may push (§1.2); otherwise it is the agent's own branch. | `{branch, old_sha, new_sha}` |
| `read_private` | `repo` (`owner/name`), `path`, `ref` (a sha is recommended; a branch or tag is resolved and the sha returned) | a file's text (at most 1 MiB), or a directory listing (names, types, sizes) |
| `pr_checks` | `pr`, optional: a pull request number **in the agent's own repository**. Without it, the pull request whose head is this agent's branch. | `{head_sha, state: green\|pending\|red\|none, rows: [{name, status, conclusion, required}], log_tail?}`, where `log_tail` is up to 8 KiB of each red row's job log, redacted |

**Why these five and no more.** These are #1225's operations, and the tools
are the whole capability. In particular:

* There is no `git_push(ref, refspec)`, no `git_clone(url)` and no "run this
  git command for me". A general tool would turn every refusal below into a
  parsing problem.
* `git_rebase_onto` is kept separate from `git_fetch` even though the agent
  could run `git rebase` itself after a fetch. The stacked-branch catch-up
  after a squash merge, which #1225 names as one of the most common
  follow-ups, needs the worker to know what was fetched in order to report
  the conflicts. The tool also leaves one record per rebase in the audit
  (§4).
* `log_tail` is part of `pr_checks` because #1225 asks that a lane "fix red
  rows". A red row's name alone does not say what failed.
  `GitHubWriter.job_log_tail` already reads it.

### 1.2 What each tool refuses

Each refusal is checked twice: first by the worker, so the agent gets a fast
and specific answer, and then by swarm-api, which is the one that counts. The
worker's check can be wrong, or the worker can be the agent's target; the
API's check holds either way.

| refusal (code) | tools | rule |
|---|---|---|
| `not_this_tasks_branch` | push | The branch must be one the agent may push. That set is **derived server-side from the agent's signed spec**: `continuation.publish_branch` (its own `swarm/<task id>`, or the `swarm/<continued task id>` it continues), and for a `single-pr` amender `Worker._pr_author_branch`. Nothing in the request is used as a branch name except to compare it with that set. Another agent's branch, `main`, and any branch outside `swarm/` are all refused here. |
| `protected_branch` | push | The repository's default branch, read from the forge and never assumed to be `main`. Also any branch whose ruleset or protection the forge reports (`GitHubWriter.branch_rules`). That check is defence in depth: a `swarm/` branch is never a protected branch. |
| `non_fast_forward` | push | **The push is never forced.** If the new tip does not descend from the branch's current tip, GitHub refuses the push and the refusal is relayed with both shas, so the agent can run `git_rebase_onto` or `git_fetch` again. A rebase of a branch that is already pushed therefore needs the owner's answer to [Q1](#q1-a-rebased-branch-that-is-already-pushed). |
| `strategy_pushes_nothing` | push | The agent's strategy promises not to push: `collect` without `carrier: branches`, or a `single-pr` reader. This is the promise `Worker._publish_git` keeps at the end ("THE CALLER'S STRATEGY IS A PROMISE"). A mid-run tool must not break it either. |
| `forge_write_refused` | push | The submitter's grant is read again before every push, as `Worker._forge_write_refusal` does at the end: no `can_push` grant, a revoked grant, or a read-only `forge_credential` (`apps/agent-worker/agent_worker/secrets.py::forge_read_only`). |
| `leak_found` | push | The worker's own gate, before anything is uploaded. Each new commit's added text and the final tree go through the same scans as the end-of-agent publish (`_leaks_in_added_text`, `final_tree_leak`). The answer names `path:line rule`, never the match. |
| `foreign_author` | push | `gitops.py::verify_worker_authorship`, as at the end. |
| `repo_not_allowlisted` | read_private | `repo` must be on the tenant's allowlist (§2.3). A repository that does not exist, or that the credential cannot read, gets **the same answer**, so the tool cannot be used to probe which repositories exist. |
| `other_repository` | fetch, rebase, push, pr_checks | These four act on the agent's own repository only. `read_private` is the only tool that takes a repository. |
| `bad_ref` / `bad_path` | all | `ref` goes through `gitops.py::validate_ref`. `path` is refused if it is absolute or contains `..`, an empty segment, a backslash or a control character (the rule `checkpoint_content.py` uses). |
| `too_large` | fetch, push, read_private | Bundle cap (default 256 MiB, owner [Q6](#q6-limits)), file cap of 1 MiB. Both are said in the refusal. |
| `fenced` (HTTP 409) | all | The attempt is no longer the lease holder (§1.4). This is not a tool error the agent can act on. The worker ends the attempt (invariant 5). |
| `rate_limited` | all | Per attempt: 60 calls per hour and 20 pushes per hour by default (owner [Q6](#q6-limits)). This keeps a looping agent from hammering the tenant's forge quota. |

**The fallback's refusals are GitHub's, not these** (§3). That is a large
part of why the tools are the primary path.

### 1.3 How a call reaches the platform

```
agent ──MCP stdio──> swarm-git shim ──spool file──> worker (non-dumpable)
                     (holds nothing)                 │ validate, scan, bundle
                                                     │ sign with the attempt key
                                                     ▼
                              swarm-api  POST /v1/attempts/{attempt_id}/git/{tool}
                                  │ ID token = tenant worker SA, audience-pinned
                                  │ attempt proof verifies against child_keys
                                  │ _lease_is_live(tenant, task, attempt, lease, generation)
                                  │ policy (§1.2) from the SIGNED task spec
                                  │ read the slot (§2) — in swarm-api's memory only
                                  │ git / GitHub API, in a per-request scratch dir
                                  │ audit row (§4)
                                  ▼
                                github.com
```

1. **The shim.** `python -m agent_worker.gitmcp` is a stdio MCP server, built
   the way `agent_worker.graphmcp` is: its own newline-delimited JSON-RPC
   loop, no MCP SDK, no network, no credential. For claude-code it is named in
   `--mcp-config`; for codex it goes in codex's MCP config. The shim writes
   `requests/<id>.json` into `work/.swarm-git/` and waits for
   `responses/<id>.json`. It contains no secret, and **the agent can bypass
   it and write the spool directly. That is fine**, because the spool accepts
   exactly the five tools with exactly the arguments above, and the shim is
   only an interface. A runner without MCP support (the `generic` runner) can
   use the same spool through `swarm-git <tool> ...`, a CLI form of the shim.

   *Why a spool and not a Unix socket or a loopback port:* a socket the
   worker listens on is one the agent can connect to as well. Since the
   worker checks every request regardless, that would only be equivalent to
   the spool, and the spool is the shape the child-agent path already has, has
   had reviewed, and ticks in the run loop.

2. **The worker.** The relay is part of the object that already holds the
   attempt key (`ChildPath`), not a second holder of it. For each request it:
   1. validates it, with the worker-side refusals in §1.2;
   2. for a push, mirrors the agent's commits into a worker-owned repository
      with object checking (`prepare_publish_repo`), runs the scans and the
      authorship check, and writes a bundle of
      `<branch's remote tip>..<agent HEAD>`;
   3. signs the request with the attempt key and sends it with the ID token
      (`children.py::ChildApi.call`).

   A bundle goes in the body when it is at most 24 MiB. A larger one is
   uploaded to `tenants/<t>/attempts/<attempt>/git/<call id>.bundle` under the
   tenant's own GCS prefix, and its sha256 goes in the signed body. Fetch
   bundles come back the same way, and the worker applies them with
   `git fetch <bundle>` in the agent's clone. `git_rebase_onto` then runs
   `git rebase` there. Nothing the worker runs in the agent's clone carries a
   secret, so a hook or alias the agent planted in `.git/config` gains
   nothing it could not already do.

   While a call is in flight the lease is beaten
   (`Worker._heartbeat_meanwhile`, as for the carrier push).

3. **swarm-api** executes every tool. The route is
   `POST /v1/attempts/{attempt_id}/git/{tool}`, and it is worker-only, like
   `/v1/attempts/{attempt_id}/child-key`. Before anything else it runs, in
   order:
   1. `authenticate_worker` (the ID token is the tenant's worker SA);
   2. `verify_proof` (the signature verifies against the key this attempt
      registered);
   3. `_lease_is_live` (invariant 5).

   Only then does it read the agent, verify its spec signature
   (`apps/swarm-api/swarm_api/specsigning.py`) and derive the policy. The
   handlers:

   * **fetch:** in a per-request scratch directory under swarm-api's tmpfs,
     with the token in a credential file `O_EXCL|0600`, removed in
     `finally`, using the `gitops.py` discipline:
     1. `git init --bare`;
     2. fetch the worker-reported `have` shas at depth 1;
     3. fetch `ref`; the shallow negotiation then sends only what the worker
        lacks;
     4. `git bundle create <ref> ^<haves>`.
   * **push:** fetch the bundle's prerequisite commit at depth 1, verify and
     unbundle, re-check the fence, then run `git push --no-verify
     --no-follow-tags -- <url> <sha>:refs/heads/<derived branch>`, which is
     never forced. "Re-check the fence immediately before the push" is #453's
     rule for the carrier push. A reclaim can land while the bundle is being
     prepared.
   * **read_private:** `GET /repos/{o}/{r}/contents/{path}?ref=` with the
     credential §2.3 picks. No git is needed.
   * **pr_checks:** `GitHubWriter.read_pull`, `branch_rules`, `check_runs`,
     `commit_statuses`, then `forgechecks.evaluate`, exactly as
     `issueci._read_ci` does. `log_tail` comes from `job_log_tail` and is
     passed through swarm-api's redaction before it is returned.

**Why swarm-api executes the tools, and not a new service.** swarm-api
already holds every piece a call needs, and each has already been reviewed:
the worker authentication and attempt proof (children), the fence
transaction, the slot reads and their IAM, the GitHub reads, spec-signature
verification, and the redaction. A separate service would need its own grant
on every forge slot. It would also need the `swarm-child-key` HMAC to check
an attestation, which today only swarm-scheduler and swarm-api hold
(childkey.py). Both of those widen the credential's reach, which is what this
design exists to narrow.

What swarm-api gains is the `git` binary in its image and longer requests,
bounded by the caps in §1.2. The owner may still prefer the isolation of a
separate service: [Q4](#q4-which-service-executes).

### 1.4 How the call is bound to this agent and this fencing generation

* **This attempt.** Only the worker that registered the attempt key can sign
  for it. The registration is one-use and attested
  (`childkey.py::AttemptTuple`), and it happens before the agent exists. An
  agent that mints the worker's ID token still cannot sign, so it cannot call
  the route directly. It can only use the spool, which is the tool surface.
* **This generation (invariant 5).** The body carries
  `tenant_id, attempt_id, lease_id, generation` (the `_AttemptFields` shape),
  and `_lease_is_live` compares them with the agent and lease in one
  transaction. A stale worker gets 409 `git_fenced`. Its relay raises the
  same way `ChildSubmitFenced` does, and the worker ends the attempt through
  `Worker._exit_fenced_mid_run`: it does not run the agent further and it
  does not touch the lease.
* **A push that races a reclaim.** The fence is checked again inside the
  push handler after the bundle is prepared and before `git push`. There is
  still a residual between that check and GitHub accepting the push. It is
  the same window the carrier push has, and it is bounded the same way: a
  non-forced push cannot overwrite what the new holder pushed. It can only be
  refused as `non_fast_forward`.
* **Every attempt needs a key.** Today the scheduler issues
  `SWARM_CHILD_NONCE` only when `child_key` is configured and the agent is not
  itself a child (`apps/scheduler/scheduler/dispatch.py`, "THE CHILD PATH").
  Git tools need the attempt key on child agents too. So lane G2 issues the
  nonce to every attempt of an agent whose profile offers git tools, and the
  worker keeps "the key is registered" separate from "the child path is
  offered". A child still may not submit children (`ChildDepthExceeded` is
  unchanged).

---

## 2. The credential

### 2.1 Which one: the agent's own forge slot, read by swarm-api

The tools use **exactly the credential the agent's end-of-run publish would
use**. That is the slot the agent's **signed** `forge_credential` names:

* `git` → `swarm-tenant-<t>-git`;
* `git-u-<hex>` → the submitter's user slot;
* `git-r-<hex>` → the repository slot.

This is resolved at submission by
`apps/swarm-api/swarm_api/service.py::SubmissionService._resolve_forge`, and
read at publish by the worker's `secrets.py::forge_suffix` and
`resolve_git_token`.

*Why this one and not the GitHub App:*

* **No new rights.** A mid-run push can do nothing the final push of the
  same agent could not do. The tools add *when*, not *what*.
* **Attribution and grants are kept.** On a personal tenant `u-<user>`, and
  on any group tenant with `REPOSITORY_GRANTS_ENFORCED`, the push is the
  submitter's and is limited by their `forge_grants` row. A platform App
  would push as the platform for everyone, so a person whose grant is
  read-only would gain a write path.
* **The IAM exists.** swarm-api already reads `-git` (the per-secret
  `forge_readers` binding, docs/multi-tenancy.md) and the tenant's `-git-u-`
  slots (`forge_refresh_reader`, `forge_personal_grants`). Repository slots
  `-git-r-` need the same conditional binding. Lane G4 adds it in
  `terraform/bootstrap/forge_user_slots.tf`, and that is the only IAM change.
* The merge App (`-git-merge`) and the review App (`-git-review`) are never
  used here. Their keys keep their sole accessors (docs/merge-step.md §2,
  docs/multi-tenancy.md: "swarm-api never needs to sign as an App"). This
  design does not change that.

### 2.2 Where it is read, and where it is never written

**It is read in swarm-api's process**, per call, at `latest`, through
`SecretManagerForgeTokens.read_slot`. It is never cached across requests
(the worker's re-read rule, `secrets.py::call_reading_again`, applies the
same way). A user slot's grant is checked again on every write call.

**It is never written to** the runner's container, its environment, the
spool, the bundle, an API response, the audit row, a log line, a Job's
environment or the transcript (the forge-token rule, CLAUDE.md, 2026-09-25).
How each of those is held:

* **container, environment, spool and transcript:** nothing the worker
  receives contains it. A response is assembled from shas, branch names,
  check names and file contents.
* **logs:** swarm-api registers the value with its redaction before the
  first git call. Git's stderr is read back only through a scrubbing filter
  that also strips URL userinfo, as `gitops.py::_withheld_note` does.
* **disk:** the credential file lives in the request's scratch directory, is
  created `O_EXCL|O_NOFOLLOW` and mode 0600, and is unlinked in `finally`.
  The whole scratch directory is removed at the end of the request.
* **the forge host:** the token is sent only to the host
  `forge.may_receive_forge_token` accepts (#307).

**A read-only slot:** `read_private` and `pr_checks` work with read
permission alone. `git_push_branch` with a slot that cannot push is refused
`forge_write_refused` and names what the token lacks. It never falls back to
another slot.

### 2.3 The private-read allowlist

`read_private` reads a repository **other than** the agent's own, so it needs
a gate the push does not:

* **The allowlist:** a per-tenant list of `owner/name` on the `repositories`
  collection. Each entry is a repository record with a new field
  `readable_by_tasks: true`, set by a tenant admin through
  `PATCH /v1/repositories/{repo_id}`, and audited in `admin_audit` the way
  `repositories._platform_audit` audits a platform change. Nothing an agent
  carries can add an entry.
* **The credential:** the allowlisted repository's own `git-r-<hex>` slot if
  one is registered (`gittokens.py::provider_suffix`); otherwise the agent's
  slot from §2.1. The read is always a GET. If the credential cannot read the
  repository, the answer is `repo_not_allowlisted`, as for a repository not
  on the list.

*Why a list on the repository record and not on the tenant:* the record
already exists per tenant and per repository, with an id no caller chooses
(`repositories.py::repo_id_for`), and it is where a tenant admin already
manages a repository. A new field on `Tenant` would be a frozen-contract
change (`apps/common/swarm_common/models.py::Tenant`), and this does not need
one. The owner may prefer per-agent declaration as well:
[Q3](#q3-the-private-read-allowlist).

### 2.4 Additional repositories: a checkout at a pinned commit, not a tool

`read_private` answers one file or one listing at a time. A step that has
to reason over the code of several repositories names them at submission
instead, as `additional_repositories`
([workflows.md](workflows.md#reading-other-repositories-additional_repositories),
#1434). The worker checks each one out read-only at `work/repos/<name>`
before the agent starts. No tool is involved, and nothing above changes:

* **The same gate.** An additional repository must be registered **and**
  `readable_by_tasks`, the allowlist of §2.3. It is refused
  `repository_not_registered` (422) or `repository_not_readable_by_tasks`
  (403) at submission otherwise. There is one rule for "a task may read a
  repository that is not its own", and the admin sets it in one place.
* **A different credential rule, on purpose.** `read_private` may use the
  repository's own `git-r-` slot, because swarm-api makes that read. An
  additional repository is cloned **by the worker**, and the worker never
  reads a `git-r-` slot: only swarm-api does (#1225). So swarm-api picks the submitter's
  user slot for that repository's owner, or the service slot, and pins the
  ref with that same slot. A repository only a `git-r-` slot can read is
  refused `additional_repository_unreadable` (422) at submission. That
  answer, like `repo_not_allowlisted`, does not say whether the credential
  or the ref was the problem. The frozen contract cannot hold a `git-r-`
  slot on an entry at all (`ADDITIONAL_FORGE_CREDENTIAL`).
* **Pinned and signed.** Each ref is resolved at submission to a 40-hex
  commit and signed into the agent (spec format 4), so a fetch or a
  `read_private` call mid-run cannot move what the agent is reading.
* **GKE agent profiles only.** The checkouts live on the pod's cache volume,
  which a Cloud Run attempt does not have. Other profiles are refused
  `additional_repositories_unsupported_profile`.
* **How many.** At most the platform setting `max_additional_repositories`
  per step: default 5, edited live by an admin, 1 to 10. A step over it is
  refused `too_many_additional_repositories` (422), naming the limit. The
  frozen contract's ceiling of 10 is a guard on the stored shape, not the
  limit.
* **Never written to.** The tools above still act on the agent's own
  repository only (`other_repository`). No push credential for an
  additional repository is ever issued. The worker points each checkout's
  push URL nowhere. Only `work/repo` is harvested, pushed or checkpointed.

---

## 3. The fallback: a per-agent GitHub App installation token

### 3.1 When it is used instead of the tools

**Recommended: not built now.** It would be built only if the owner answers
[Q2](#q2-the-fallback-token) with B, and it would be used only for a
repository whose record says `runner_token: true` **and** an agent whose
profile allows it. It is never a default, and it is never used on a
repository where the tools are available. It is for a repository whose
workflow needs a git operation the five tools do not cover, until a tool
does.

### 3.2 How it is minted

* **The App.** A new GitHub App, `swarm-runner`, with exactly the
  permissions *Contents: write*, *Pull requests: write* and *Metadata: read*.
  It has no Administration, Actions, Secrets, Workflows or Checks write. Its
  key is in `swarm-tenant-<t>-git-runner`, stored with
  `scripts/create-secrets.sh --stdin`. It is neither the review App nor the
  merge App, so revoking its installation affects nothing else.
* **Who mints it.** **swarm-api mints it, not the worker**, on a
  worker-signed `POST /v1/attempts/{attempt_id}/git/token`, with the same
  authentication and fence as §1.4. It uses
  `forge.py::mint_installation_token`'s request shape:
  `repositories: [<the task's one repository>]`, with the three permissions
  listed above. The App key is therefore read only in swarm-api and never
  enters a runner's container. That matters because a leaked App key could
  mint a token for every repository the App is installed on, while a leaked
  token reaches one repository. (This makes swarm-api sign as an App for the
  first time. That is a reversal of a recorded position, so the owner decides
  it as part of Q2.)
* **When.** Before the agent starts, after the attempt key is registered.
* **How it reaches the agent.** The worker writes it to a git credential
  file the agent's clone is configured to use, and to `GH_TOKEN` in the
  runner's environment (`Worker._build_child_env`). It is registered with the
  worker's and the runner's redaction (`redact.py::collect_secrets`), so a
  transcript or log that echoes it is scrubbed. The credential file is
  excluded from checkpoint archives and artifacts.

### 3.3 How it is revoked at attempt end

* **Any end of the attempt** (finished, parked, cancelled, fenced, crashed
  into the worker's `finally`): the worker sends `DELETE /installation/token`
  (`forge.py::revoke_installation_token`) and records whether GitHub answered
  204.
* **A worker that dies without reaching `finally`:** GitHub expires the
  token after one hour. The audit row records `expires_at`, so the console
  can show that a token was left to expire rather than revoked.
* **Never re-minted on resume.** A parked agent's next attempt asks for a new
  token with its own generation.

### 3.4 Why it is acceptable, and what it gives up

* **It is acceptable** because a token that leaks reaches one repository,
  for at most an hour, with no admin rights. The base branch stays guarded by
  GitHub's branch protection, which this App cannot bypass because it is not
  on any bypass list. That is #1225's own threat model.
* **It gives up three things the tools keep:**
  * **Invariant 5.** A GitHub token cannot be fenced. A stale worker,
    partitioned from Firestore, keeps a working token until it is revoked or
    expires.
  * **Branch scope.** The token can push any unprotected branch in the
    repository, including another agent's `swarm/` branch, and can force-push
    one. GitHub's only limit is branch protection on the repository.
  * **The audit.** The platform sees only the mint and the revoke, not the
    calls made with the token.

  That is why it is a fallback and not the design.

---

## 4. Audit

Every tool call writes **one row** in a new collection `git_tool_audit`,
which only swarm-api writes. It follows the shape of `live_audit`
(`apps/swarm-api/swarm_api/handoff.py::_audit`), because that collection
already records an attempt and its generation.

* **id:** `{attempt_id}:{inverted ms}:{random}`, so an attempt's calls list
  newest first, the way `schedaudit.py` orders schedule rows.
* **fields:** `tenant_id`, `task_id`, `attempt_id`, `lease_id`, `generation`,
  `tool`, `repository` (`owner/name`), `ref` as asked, `resolved_sha`,
  `branch`, `old_sha`, `new_sha`, `path` (read_private), `pr` (pr_checks),
  `result` (`ok` / `refused` / `failed` / `fenced`), `code` (the §1.2 code),
  `bytes`, `duration_ms`, `slot` (the secret's **suffix**: `git`,
  `git-u-<hex>`, `git-r-<hex>`; a name, never a value), `at`.
* **For the fallback:** `tool: "token"`, `expires_at`, `revoked` (true,
  false, or null when it was left to expire).
* **What never goes in a row:** the token, a URL with userinfo, file
  contents, log text, the agent's arguments beyond the fields above, or a
  forge error body that has not been through redaction.

**When it is written.** A refusal before the forge is called is written in
the same transaction as the fence check, so a fenced call is recorded
whichever way it ends. A call that reaches the forge writes its row after the
forge answers. If that write fails, the call's answer still returns, and the
failure is logged as `git_audit_write_failed` with the ids. A push that
happened but was not recorded is a gap, but refusing to report a push that
happened would be worse.

**Where it is read.** A tenant member reads it through
`GET /v1/tasks/{task_id}/git-calls`, tenant-scoped like the agent itself, 404
otherwise. The worker also writes counts into the agent's
`result_summary.git_tools` (`pushes`, `fetches`, `reads`, `refusals`), so the
existing agent views show that the tools were used without a new screen. A
console screen is a later choice, not part of this plan.

---

## 5. What stays off runners forever

None of the following is given to a runner, now or by a later tool, and a
tool that would wrap one is not added:

* **`kubectl`**, and any Kubernetes credential. Runners have no access to any
  cluster, least of all `agents-staging` in `saga-agents-staging` (CLAUDE.md
  rule 2).
* **`gcloud`**, and any Google credential beyond the worker's own metadata
  identity. The agent can mint that identity's ID token, which is exactly why
  the attempt key exists. A tool never hands out an access token.
* **Production:** no deploy, no release, no Cloud Run or Job mutation, no
  Terraform.
* **Secrets:** no secret creation, rotation or read, of any tenant, including
  the tenant's own forge slots. `scripts/create-secrets.sh` stays a human
  path.
* **Repository and organisation settings:** branch protection, rulesets,
  collaborators, Actions secrets, webhooks, App installations. No
  Administration permission exists in either credential path.
* **Merging:** a runner never merges. Merging stays with the `merge` step
  (docs/merge-step.md) and its own identity.

---

## 6. The evidence in #1225, and which part fixes each

### 6.1 Two lost final pushes (wf_e55610ded71c4d9d9fae, wf_0260c920acf8414395b2)

**What the code says happened.**

* The review step failed with "Remote branch swarm/task_… not found". That
  is `gitops.py::shallow_clone` meeting a pattern `_GIT_PERMANENT` classes as
  permanent (`r"remote branch \S+ not found"`). `Worker._maybe_clone` then
  wraps it with "this step builds on task …, whose branch … is pushed when
  that step publishes".
* The branch it cloned is `swarm/<build task id>`
  (`Worker._builds_on_base`). `_builds_on_base` steps past an upstream only
  when that upstream "left nothing". It trusts every SUCCEEDED upstream that
  changed files to have pushed.
* A build step that a review `builds_on` is an `integrate` **contributor**
  (docs/workflows.md, the review/fix pattern). **A contributor whose push
  failed still ends SUCCEEDED**, because `Worker._publish_failed` and
  `Worker._published_nothing` both return None unless
  `Worker._opens_pull_request` is true, and that is false for a contributor
  ("its branch is its deliverable"). The #872 fix covered only steps that
  open a pull request. docs/workflows.md says so: a contributor is
  "unaffected".
* The same symptom follows from four other paths:
  * a publish that declined without setting `publish_failed`: no token
    (`Worker._git_token_refusal`), no write access (`access.can_push` false),
    no forge access (`access is None`), or `git_publish_enabled` false;
  * a harvest `GitError`;
  * survivors of the reap;
  * the catch-all in `Worker._publish_checked`.

  In each case the step reads SUCCEEDED with no branch.
* `push_branch` runs `git push` once and never retries. A transient failure
  (a reset connection, a GitHub 5xx) is therefore as final as a refusal.

**Which of these happened to these two workflows has not been measured
here.** The answer is in each build agent's
`result_summary.git.publish_reason` and `publish_failed`, and the build lane
reads them first.

**What fixes it.**

1. **Lane G1, independent of the tools.** Every step whose *branch* is a
   deliverable has to fail when its branch was not pushed:
   * a step that opens a pull request;
   * an `integrate` contributor;
   * a `carrier: branches` step;
   * a `single-pr` amender;
   * any step another step `builds_on`.

   G1 ends such a step FAILED with `publish_failed:` whenever it ends without
   a pushed branch, by any of the paths above. It also retries the push once
   for transient failures, using `retry_transient`'s classes. This is #1225's
   acceptance item "a failed final push surfaces as an error on the task,
   never as SUCCEEDED with no branch".
2. **The tools.** An agent can `git_push_branch` as soon as it has a
   coherent commit, and again before it finishes. The final publish is then
   a fast-forward of a branch that already exists. If the final push is still
   lost, the review clones the last mid-run push instead of finding no
   branch.

### 6.2 No git operations on existing branches; no private reads; cannot watch CI

`git_fetch`, `git_rebase_onto` and `git_push_branch` cover the rebase onto
main, the conflict fix and the stacked catch-up, on a branch the agent may
push (§1.2). Pushing a *rebased* branch that is already on the remote needs
[Q1](#q1-a-rebased-branch-that-is-already-pushed).

`read_private` covers the agents-backend-at-a-sha case (§2.3). `pr_checks`
with `log_tail` covers watching CI and fixing red rows.

### 6.3 Three workflow status reads that returned 404 after the build step

**This is a separate defect. Nothing in this design fixes it, and it should
be filed on its own.**

**What the code shows.**

* The only 404s on `GET /v1/workflows/{workflow_id}`
  (`apps/swarm-api/swarm_api/routes/workflows.py::get_workflow`) are raised
  in `apps/swarm-api/swarm_api/store.py::Store.get_workflow`. All three raise
  the same `workflow '<id>' not found`:
  * the document is missing;
  * the document's `tenant_id` differs from the tenant the call resolved;
  * `submitted_by` differs, for a continuation-scoped caller only
    (`deps.py::submission_scope`).
* No step transition rewrites, moves or re-keys a workflow document. The
  only writers are `Store.set_workflow_state`, `Store.cancel_workflow`, the
  reconciler's state repair, and the purge, which refuses a workflow with a
  live step. So "after the build step" is a coincidence of time, not the
  cause.
* **The likeliest cause is a tenant switch between launch and status read.**
  * Since #1204 (owner decision 2026-10-12 15:55 PDT, the day these runs
    happened), a request with no `X-Swarm-Tenant` resolves to the caller's
    personal workspace `u-<caller>` once that workspace is ready
    (`apps/swarm-api/swarm_api/auth.py::Authenticator._with_own_workspace`,
    with a 30 s readiness cache in `PersonalTenants`). Before that, or before
    the workspace was ready, the same request resolved to the first group.
  * The MCP bridge reads `SWARM_TENANT` once at start
    (`apps/swarm-mcp/swarm_mcp/client.py::chosen_tenant`, whose docstring
    records exactly this split: "a tool that sends no header moved off the
    group it had always read").
  * So a workflow launched in the group, and read later by a client that
    sends no header (or by a bridge restarted with a different
    `SWARM_TENANT`), answers 404 while its steps keep running. The
    coordinator then re-ran the review locally, which produced the duplicate
    PRs.
* This is **a hypothesis consistent with the code, not a measurement.** The
  measurement: compare `workflows/<id>.tenant_id` for the three workflows
  with the tenant the coordinator's status calls resolved to (swarm-api's
  request log for those calls, or `GET /v1/tenants/mine` from the same
  client).

**The defect to file, whatever the measurement shows.** A 404 that cannot
tell "no such workflow" from "this workflow is in another tenant you belong
to" sends the caller to re-run work. The fix is mechanical:

1. `Store.get_workflow`'s tenant-mismatch branch answers `404
   workflow_not_in_tenant`, naming **the tenant the call resolved to**. That
   is the caller's own choice, so it is not an oracle, and the caller learns
   where to look.
2. The plugin's status path treats a 404 for a workflow id it launched
   itself as "re-read with the launch's tenant", not as "gone". The launch
   response already carries the tenant.

The call sites: `apps/swarm-api/swarm_api/store.py::Store.get_workflow`, and
`apps/swarm-mcp/swarm_mcp/workflows.py::fetch` as reached from
`apps/swarm-mcp/swarm_mcp/server.py` (`swarm_workflow_status`,
`swarm_workflow_result`).

---

## 7. The build plan

**Constraints.**

* **Ordering.** Each lane starts after the lanes it depends on have merged.
* **Files.** No file is in two lanes. Every lane names its territory, and
  the integration points are placed so that no shared file is needed:
  * the worker relay goes inside `ChildPath`, which `lifecycle.py` already
    ticks, so no tools lane edits `lifecycle.py`;
  * the API's two halves meet through a library lane.
* **Red first.** Each lane pushes its tests alone first, and they must fail
  on CI for the stated reason before the fix lands (CLAUDE.md, "Red first").
* **Security review.** G4 and G5 are the credential path and get the one
  security review the owner asked for, as one review over both.

| # | lane | depends on | territory (files) | red-first tests (each must fail first, for the stated reason) |
|---|---|---|---|---|
| **G1** | **DELIVERABLE-BRANCH**: a step whose branch is its deliverable fails when the branch was not pushed; transient push failures are retried once | none | `apps/agent-worker/agent_worker/lifecycle.py` (`_publish_failed`, `_opens_pull_request`, `_published_nothing`, `_publish_checked`), `apps/agent-worker/agent_worker/gitops.py` (`push_branch`), `docs/workflows.md` (the #872 paragraph), new `tests/unit/worker/test_publish_deliverable_branch.py` | An `integrate` contributor whose push raises `GitError` ends FAILED with `publish_failed:` (today SUCCEEDED). A `carrier: branches` collect step, a `single-pr` amender, and a step another step `builds_on` behave the same way. A PR step whose publish declined for `can_push=False` ends FAILED. A push that fails once with a connection reset and then succeeds is published. A `collect` step without a carrier still SUCCEEDS with nothing pushed (the control). |
| **G2** | **ATTEMPT-KEY-FOR-ALL**: every attempt of an agent whose profile offers git tools registers an attempt key, children included | none | `apps/scheduler/scheduler/dispatch.py` (the child-path nonce block), `apps/swarm-api/swarm_api/children.py` (`ChildService.register_key` accepting a child agent's registration while submits stay depth-refused), new `tests/unit/control_plane/test_attempt_key_every_attempt.py` | The nonce is in a child agent's environment when git tools are offered (today it is absent). A child task's key registers. A child's `submit` is still `child_depth_exceeded`. An agent that is not offered git tools gets no nonce when it is a child (the control). |
| **G3** | **WORKER-GIT-LIB**: the worker's half of each tool as a library (validation, scan, bundle build and apply, local rebase, signed call) and the `swarm-git` shim | none | new `apps/agent-worker/agent_worker/gittools.py`, new `apps/agent-worker/agent_worker/gitmcp/` (`__init__.py`, `__main__.py`, `server.py`), new `tests/unit/worker/test_gittools.py`, new `tests/unit/worker/test_gitmcp.py` | The tests are against a local bare repository standing in for origin and a fake API, with no network. A spool push naming `main`, another agent's branch, or a name outside `swarm/` is refused before any request. A commit adding a credential-shaped line (built at runtime) is `leak_found` with `path:line rule` and no match text. The bundle holds exactly `<remote tip>..HEAD`. A rebase conflict leaves the rebase in progress and lists the paths. A request is signed and its proof verifies with `childkey.py`'s verifier. A 409 raises the fenced error. The shim's tool schemas fit the 2k-token budget `graphmcp` holds. |
| **G4** | **API-GIT-EXEC** (security review): swarm-api's git execution as a library: scratch repository, credential file, fetch→bundle, bundle→push, scrubbing | none | new `apps/swarm-api/swarm_api/gitexec.py`, `images/swarm-api/Dockerfile` (adds `git`), `terraform/bootstrap/forge_user_slots.tf` (swarm-api reads `-git-r-` slots), new `tests/unit/control_plane/test_gitexec.py` | Against `file://` bare repositories: a push of a non-fast-forward bundle is refused `non_fast_forward` and the remote is unchanged. A push never sends `--force`; the argv is asserted. The credential file is gone after success, failure and exception. A git stderr carrying `https://x-access-token:<runtime-built>@` comes back scrubbed. A fetch bundle excludes the haves. A bundle over the cap is `too_large` before anything is unpacked. |
| **G5** | **API-GIT-TOOLS** (security review, with G4): the route, authentication, fence, policy, allowlist, audit and audit read; all five tools wired | G2, G4 | new `apps/swarm-api/swarm_api/gittools.py`, new `apps/swarm-api/swarm_api/routes/gittools.py`, `apps/swarm-api/swarm_api/main.py` (one `include_router`), `apps/swarm-api/swarm_api/repositories.py` (`readable_by_tasks`), new `tests/unit/control_plane/test_git_tools_api.py` | An unproven request is 403, the same for every cause. A stale generation is 409 and writes a `fenced` audit row. A push to `main` is `protected_branch` (#1225 acceptance). A push to another agent's `swarm/` branch is `not_this_tasks_branch` (#1225 acceptance). A push from a `collect` agent is `strategy_pushes_nothing`. A read-only grant is `forge_write_refused`. `read_private` on a repository not on the list, and on one that does not exist, get identical answers. No audit row contains the fake token, built at runtime. `pr_checks` returns red with the failing row's redacted log tail. |
| **G6** | **WORKER-WIRING**: the relay goes inside `ChildPath`, and the runners name the shim | G2, G3, G5 | `apps/agent-worker/agent_worker/children.py` (`ChildPath.prepare`, `has_requests`, `tick` compose `gittools`; the key is registered separately from the child path being offered), `apps/agent-worker/agent_worker/runners/claude_code.py`, `apps/agent-worker/agent_worker/runners/codex.py`, new `tests/unit/worker/test_git_tools_wiring.py` | claude-code's argv carries `--mcp-config` naming `agent_worker.gitmcp` when the attempt is offered git tools (today it carries none). A child agent gets git tools but not `SWARM_CHILDREN`. A fenced git call ends the attempt through the fenced exit without releasing the lease. **The runner's environment contains no variable whose name or value matches `token`, case-insensitive** (#1225 acceptance: `env \| grep -i token` empty). |
| **G7** | **ACCEPTANCE**: the #1225 acceptance on a deployed environment | G1, G6 | new `tests/acceptance/test_runner_git_tools.py`, and the acceptance group it joins (the lane reads `accept.yml`'s group list; `.github/workflows/` is edited only if the group list lives there, and then only that list) | Five checks. 1: a lane rebases its PR branch onto main, resolves a planted conflict, pushes, and reads `pr_checks`, with `env \| grep -i token` empty in the runner. 2: `read_private` reads an allowlisted repository at a sha. 3: a push to `main` is refused. 4: a push to another agent's branch is refused. 5: a build step whose push is made to fail ends FAILED, not SUCCEEDED. |
| G8 | *Only if [Q2](#q2-the-fallback-token) = B*: **RUNNER-TOKEN** fallback | G5, G6 | new `apps/swarm-api/swarm_api/runnertoken.py`, `apps/swarm-api/swarm_api/routes/gittools.py` is **not** touched (G5 registers a `/git/token` route only if Q2 = B was answered before G5 started; otherwise this lane adds `routes/runnertoken.py` and one line in `main.py`, which G5 has finished with), new `apps/agent-worker/agent_worker/runnertoken.py`, `scripts/create-secrets.sh` (the `git-runner` provider), new tests | Requires a token mint naming one repository and exactly the three permissions. Requires a revoke on every attempt end, and an audit row with `revoked`. Requires that the App key is never read by the worker. |
| G9 | **STATUS-404** (the separate defect, §6.3), filed as its own issue | none | `apps/swarm-api/swarm_api/store.py` (`Store.get_workflow`), `apps/swarm-mcp/swarm_mcp/workflows.py` (`fetch`), new `tests/unit/control_plane/test_workflow_other_tenant_404.py` | A workflow stored under tenant A, read with tenant B resolved, answers `workflow_not_in_tenant` naming B (today a bare `not found`). The bridge's status for a workflow it launched re-reads with the launch's tenant. |

**The order:**

1. **G1, G2, G3, G4 and G9 in parallel.** Their files do not overlap.
2. **G5**, after G2 and G4.
3. **G6**, after G3 and G5.
4. **G7**, after G1 and G6.
5. **G8 last**, if it is built at all.

**One overlap, stated plainly.** G8, if it is chosen and its route did not
go into G5, touches `main.py` after G5 has merged. Two lanes then edit
`main.py` in sequence, never at once. The way to avoid even that is to
answer Q2 before G5 is dispatched.

**What no lane does:**

* change `apps/common/swarm_common/` (§9);
* add a TaskState or EndCause (`publish_failed:` keeps the `outputs_missing`
  cause #872 uses);
* change any other team's resource.

---

## 8. Open questions for the owner

The same questions, with options, are in this lane's `questions.json`.

### Q1. A rebased branch that is already pushed

A rebase rewrites history, so pushing the result over an already-pushed
`swarm/` branch is not a fast-forward. #1225 asks both "refuse force-push"
and "rebase its PR branch onto main … push".

* **A. Lease-guarded rewrite of the agent's own branch (recommended).**
  `git_push_branch` may replace the branch only with
  `--force-with-lease=<branch>:<sha>`, where `<sha>` is the tip swarm-api
  itself recorded for this agent (its last audited push, or the tip at the
  attempt's first fetch). It is never a sha the agent supplies. The rewrite
  can only replace this agent's own earlier work, never a commit someone else
  pushed meanwhile, and the audit records it as `rewrite: true`. A blind
  force is still refused.
* **B. Merge, never rewrite.** `git_rebase_onto` becomes `git_update_from`,
  which merges `ref` into the branch, and every push is a fast-forward.
  Squash merges make the merge commits invisible on `main`. This is simplest
  to reason about, but the stacked-branch catch-up after a squash merge
  conflicts more often under a merge.
* **C. Refuse.** A rebase can be pushed only to a new branch, and the PR is
  retargeted by hand.

### Q2. The fallback token

* **A. Do not build it (recommended).** Ship the tools. Every operation
  #1225 names is covered, and the fallback gives up the fence, branch scope
  and the audit (§3.4).
* **B. Build it, opt-in per repository**, as §3 designs it, with swarm-api
  minting from a new `swarm-runner` App whose key only swarm-api reads.
* **C. Build it, minted by the worker** before the agent starts, the
  post-verdict pattern, so swarm-api never signs as an App. The App key is
  then briefly in the heap of a container the agent will later run in.

### Q3. The private-read allowlist

* **A. Per tenant, on the repository record (recommended):**
  `readable_by_tasks`, set by a tenant admin and audited.
* **B. A + per agent:** the spec also names the repositories it will read,
  and the API takes the intersection.
* **C. Per agent only**, signed in the spec.

### Q4. Which service executes

* **A. swarm-api (recommended).** It already holds the attempt proof, the
  fence, the slot IAM and the GitHub reads (§1.3).
* **B. A new `swarm-gitgate` Cloud Run service** with its own service
  account. It is isolated from swarm-api's other routes, but it is a second
  holder of `swarm-child-key` and a second reader of every forge slot.

### Q5. Pushing a branch that is not `swarm/`

#1225 mentions "a declared branch" in the spec, for example a person's PR
branch that a lane fixes.

* **A. Not now (recommended).** The tools push only what `push_branch`
  pushes today: `swarm/` branches derived from task ids. A person's branch is
  fixed by continuing it through a `swarm/` PR.
* **B. A declared branch in the signed spec**, accepted only when the
  submitter's grant can push it and the branch has an open PR the submitter
  authored. Never the default branch.

### Q6. Limits

* **A. As written (recommended):** a 256 MiB bundle, a 1 MiB file, 60 calls
  and 20 pushes per attempt-hour, and an 8 KiB log tail per red row.
* **B. Different numbers**, given by the owner.

### Q7. Move the end-of-agent publish onto the same platform path

* **A. Later, as its own design (recommended).** After the tools ship, the
  final push can use the same swarm-api push. The worker would then hold no
  forge token at all, which removes the reap-then-read ordering. This changes
  the publish path every agent uses, so it is its own decision with its own
  measurements.
* **B. Now, inside this build.**
* **C. Never.**

---

### Decisions (owner, 2026-10-13 ~15:45 PDT)

Answered in one sitting; every recommendation was taken.

| # | question | answer |
|---|---|---|
| Q1 | A rebased branch that is already pushed | **A.** Lease-guarded rewrite of the agent's own branch, against the tip swarm-api recorded; blind force refused; audited as a rewrite. |
| Q2 | The fallback token | **A.** Do not build it. G8 is not built. |
| Q3 | Where the private-read allowlist lives | **A.** Per tenant, on the repository record (`readable_by_tasks`), set by a tenant admin and audited. |
| Q4 | Which service executes | **A.** swarm-api, which gains the git binary. |
| Q5 | Branches outside `swarm/` | **A.** Not now. Only agent-derived `swarm/` branches. |
| Q6 | Limits | **A.** As written in §1.2. |
| Q7 | The end-of-agent publish | **A.** Later, as its own design. |

The build therefore runs G1–G7 and G9 (§7), and no G8.

## 9. The frozen contract

**No change is needed, and none is requested.**

* The attempt tuple, the key and the nonce are swarm-api's and the
  scheduler's own (`childkey.py`: "Not frozen; only swarm-api writes it").
* The allowlist field is on the `repositories` collection, which is not
  frozen.
* The audit collection is new and owned by swarm-api.
* `publish_failed:` keeps the existing `outputs_missing` end cause, which is
  #872's precedent.
* The `git-runner` provider, if Q2 = B, is a secret suffix through the
  existing `Tenant.secret_name`.

If the owner wants a dedicated `publish_failed` end cause instead of
`outputs_missing`, that is contract request 46's question
(docs/contract-change-requests.md), not a new one.
