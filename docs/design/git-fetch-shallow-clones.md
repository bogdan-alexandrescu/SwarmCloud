# How the git_fetch route serves a shallow clone

Status: **design only, for the open half of #2901.** No code changes here.
The owner chose on 2026-11-13 to "start a design first: how the platform's git
fetch route serves a shallow clone (shallow and deepen handling), with a
recommended choice at each fork; the build follows the design." The build
lanes in §7 follow the owner's answers to §8.

File and line citations are to `main` at `e5e0a2a` (2026-11-13). Line numbers
move, so each one also names the symbol.

---

## 0. What is already fixed, and what this design is for

The case #2901 reported is fixed. A `publish_to_pr` agent asked to "merge main
and resolve the conflicts" got a one-commit clone of the pull request's head.
The platform's fetch of the base answered `too_large`, and a plain `git fetch`
inside the container had no credential. Pull request 2904 (merged 2026-10-31)
changed the clone: it now fetches the pull request's base as `origin/<base>`
and deepens both sides in bounded rounds until they share a merge base. It
does this at clone time, with the clone's own tenant credential
(`agent_worker/gitops.py::fetch_pull_base`, lines 1328-1416;
`PULL_BASE_FIRST_DEPTH = 64`, `PULL_BASE_MAX_ROUNDS = 8`,
`PULL_BASE_MAX_BYTES = 256 MiB`, lines 1301-1309). Its tests are in
`tests/unit/worker/test_publish_to_pr_pull_base.py`. That fix is the reason a
publish_to_pr merge works today, and nothing here replaces it.

What is still open is the route the owner's 2026-10-31 comment kept open on
purpose. It "stays as it is until a design changes what a shallow clone can
accept". That route is:

```
agent ── git_fetch(ref) / git_rebase_onto(ref)
   └─ worker  agent_worker/gittools.py  GitTools.fetch          (lines 643-665)
        └─ swarm-api  swarm_api/gittools.py  GitTools._fetch    (lines 1005-1062)
             └─ swarm_api/gitexec.py  GitExec.fetch_bundle      (lines 517-556)
```

This route still answers `too_large` on a large repository. It also has a
second defect that no one has reported yet, measured in §2.2: when the bundle
does fit, a shallow clone still finds no merge base. An agent that is not a
publish_to_pr agent, or that wants a ref other than the clone-time base (a
sibling branch, a release tag, the base again an hour later), has only this
route. This design makes the route serve a shallow clone. It keeps every
property of the git-tools security boundary
([runner-git-tools.md](../runner-git-tools.md) §0, §1.3, §2.2).

---

## 1. Goals and non-goals

Goals:

* **A size limit counts what is fetched and sent, not the repository.** If a
  clone is three commits behind, the route moves about three commits of
  objects, not the history and not a second full snapshot.
* **The answer reaches a merge base when the budget allows it**, so
  `git merge` and `git rebase` work on the result in a depth-1 clone. When the
  budget does not allow it, the answer says so in words the agent can act on.
* **The forge token never leaves swarm-api.** The token handling in
  `gitexec.py` stays as it is (module docstring, "HOW THE TOKEN IS HELD, PER
  CALL"): a per-call `mkdtemp` scratch, a 0600 `O_EXCL|O_NOFOLLOW` credential
  file, https to the forge only, scrubbed stderr.
* **Kernel limits stay.** Every git still runs under `_LIMIT_SHIM`'s
  RLIMIT_AS and RLIMIT_FSIZE, within the call's one deadline and the
  per-instance slots.
* **Invariant 9.** A large answer is an object under
  `tenants/<tenant>/attempts/<attempt>/git/` (`FETCH_OBJECT`), read with the
  tenant worker's own GSA. Nothing is shared across tenants.

Non-goals:

* No change under `apps/common/swarm_common/` (§5.10).
* No credential reaches the agent or its container, and none is held by the
  worker while the agent runs.
* No caller-chosen depth, refspec or remote. The agent still names a ref and
  nothing else (runner-git-tools.md §1.1; invariant 10 in spirit).
* `fetch_pull_base` is not changed. It stays the clone-time path for
  publish_to_pr.
* The push half of the route (`push_bundle`) is not changed. The push of a
  shallow merge already works through #2559's boundary confirmer (§6).

---

## 2. The problem, with the code's evidence

### 2.1 The scratch repository writes the remote's whole history

`fetch_bundle` (`gitexec.py:517-556`) does three things in order:

1. Each have is fetched at depth 1 (`_fetch_tip`, `gitexec.py:558-563`:
   `fetch --depth=1 -- <url> <sha>`). A depth-1 fetch is a whole snapshot of
   the tree. PR #3964 measured one repository's snapshot packing to about
   276 MiB.
2. `ref` is then fetched with **no depth at all** (`gitexec.py:535-538`:
   `fetch --quiet --no-tags --no-write-fetch-head -- <url> <ref>:refs/swarm/want`).
   The scratch repository is shallow at each have. A have cut off at depth 1
   hides all of its ancestors from the negotiation, so the remote sends every
   commit of `ref` that is not the have itself: the whole history.
3. `bundle create <out> refs/swarm/want ^<haves>` (`gitexec.py:545-546`).

Today every git in the call writes under one RLIMIT_FSIZE, the bundle cap
(`_run`, `gitexec.py:473-474`; `MAX_BUNDLE_BYTES = 256 MiB` at line 99). So
the scratch packs of steps 1 and 2 are refused `too_large` before a bundle
exists. #2926 reported this. Open PR #3964 gives fetches from the forge their
own file cap, `MAX_FETCH_FILE_BYTES = 512 MiB`, and refuses a have stopped by
a limit instead of dropping it silently. That moves the wall, but it does not
remove it: step 2 still lands the whole history. On a repository whose history
packs larger than 512 MiB, the fetch is refused however small the gap is.

### 2.2 The bundle's parents sit below the clone's shallow boundary

`bundle create want ^have` leaves out everything reachable from the have. In a
full repository that is right. The agent's clone, though, is **shallow at the
have**: `.git/shallow` lists the head commit H, and git treats H as having no
parents. The commits the bundle starts from have parents (the merge base M
and everything below it) that the clone does not hold. So there are only two
outcomes:

* the bundle names M as a prerequisite, and the clone refuses it ("lacks these
  prerequisite commits"); or
* the bundle holds the whole history down to the root. This is what
  `fetch_bundle` builds, because its scratch repository is shallow at H too,
  so `^H` excludes H alone.

`gitops.py:1290-1297` records the production measurement: a 3-commit gap
cost the repository's full 8 MB. The local measurement below repeats it, and
adds what the production one could not see. **Even when that whole-history
bundle fits, the clone finds no merge base.** H stays grafted in
`.git/shallow`, so `git merge origin/main` refuses "unrelated histories". A
`git rebase` of a one-commit clone happens to work, because it replays HEAD's
only commit. So the route is wrong for a shallow clone, and not only too
large.

**Measured** (2026-11-13, git 2.39.5, local repositories under `$TMPDIR`, no
network). The origin has 1,000 linear commits on `main`, each changing
`log.txt`, plus a 4 MiB incompressible `big.bin`. `feature` = M + one commit
H, where M = `main~3`. The clone is
`git clone --depth 1 --single-branch --branch feature file://…/origin.git`.

| experiment | commands (abridged) | result |
|---|---|---|
| E1, today's route | scratch: `fetch --depth=1 <H>`; `fetch main:refs/swarm/want`; `bundle create b1 refs/swarm/want ^H`; clone: `fetch b1 +refs/swarm/want:refs/remotes/origin/main` | the scratch repository is 8,668 KB (H's snapshot plus the whole history); the bundle is **4,419,618 bytes**; it applies; `git merge-base HEAD origin/main` finds **no merge base** |
| E2, a bundle from a full repository | origin: `bundle create b2 main ^H`; clone: `fetch b2 …` | **805 bytes**, prerequisite `-<M>`; the clone refuses: "Repository lacks these prerequisite commits", exit 1 |
| E3a, `--shallow-exclude` at a sha | clone: `fetch --shallow-exclude=<H> origin main` | `fatal: git upload-pack: ambiguous deepen-not` (deepen-not takes a ref name) |
| E3b, `--shallow-exclude` at a ref | clone: `fetch --shallow-exclude=feature origin main` | 3 commits fetched and cut **above** M: no merge base |
| E4, git's own deepen (the reference) | clone: `fetch --depth=8 origin main`; `fetch --depth=8 origin <H>` | 125,933 bytes added; merge base M |

### 2.3 too_large gives the agent no next step

A `too_large` refusal comes back non-retryable, carrying the cap
(`gitexec.py:_must`, lines 493-501, relayed as a 422 by
`swarm_api/gittools.py`). The refusal says what failed and nothing about what
to do. In #2901 the agent then ran a plain `git fetch`, found no credential,
and published nothing. In a publish_to_pr clone the base it wanted was often
already there as `origin/<base>`, put there by `fetch_pull_base`, and the
refusal did not say so.

---

## 3. The options

### (a) The worker sends its shallow boundary; swarm-api fetches a bounded slice and returns a bundle plus shallow lines

**How it works.**

1. The worker sends its clone's shallow boundary beside the haves, in a new
   field: the shas in `.git/shallow`, bounded like haves, as 40-hex shas.
   Which shas it trusts is a fork (§5.6).
2. swarm-api fetches each boundary commit at depth 1, as it fetches a have
   today.
3. In rounds, it fetches `ref` and each boundary commit at `--depth=d`. That
   is two fetches per round, for the reason `fetch_pull_base` gives at
   `gitops.py:1384-1386`. `d` doubles until `git merge-base` finds a commit,
   or until the round or byte budget runs out (§5.3, §5.5).
4. It builds `bundle create out refs/swarm/want <parents of each unshallowed
   boundary commit> ^<each boundary commit's tree> ^<other haves>`. Excluding
   the boundary commit's **tree**, not the commit, leaves out what the clone
   already holds (H's snapshot) and keeps H's parents, which the clone now
   needs. The scratch repository is shallow at its own boundary, so the bundle
   names no prerequisite.
5. The answer is the bundle, delivered exactly as today (inline up to 24 MiB,
   else `FETCH_OBJECT`), plus `shallow` (the scratch repository's new
   boundary), `unshallow` (boundary commits that now have their parents), and
   `merge_base` and `depth`.
6. The worker writes the new `.git/shallow` and then runs today's
   `git fetch <bundle>`. If that fails, it puts the old file back (§5.7).

**Measured, E5/E6** (same repositories as §2.2). Round 1, at depth 4,
reached M. The scratch repository grew by **80,551 bytes** past H's
depth-1 snapshot. `git bundle create b6 refs/swarm/want <M> ^<H^{tree}>`
was **3,456 bytes**, with no prerequisite line. On a fresh depth-1 clone,
`git fetch b6` **failed (exit 1) before** the shallow file was updated. After
the worker wrote `(old − {H}) ∪ scratch boundary`, it **succeeded** with
`transfer.fsckObjects=true`. `git merge-base HEAD origin/main` = M,
`git merge origin/main` succeeded, and `git rebase origin/main` succeeded. So
the transfer is about 3 KB, against 4.4 MB today.

* **Cost.** The scratch repository holds one depth-1 snapshot per boundary
  commit, plus the slice. The bundle holds the slice minus what the clone
  has. It is one call, one slot and one deadline.
* **Security.** The token path is unchanged. The new inputs are shas, bounded
  and validated (§6). The worker writes one more file in the clone, and it is
  data (§5.7).
* **Failure modes.** A boundary the remote does not know is skipped, as a
  have is today (the bundle grows, or the apply fails `fetch_failed`). A
  forged boundary only spoils the agent's own fetch. Budget exhausted → §5.8.
  An apply that fails leaves the clone exactly as it was.

### (b) git_fetch takes a depth/deepen argument; swarm-api returns a packfile plus shallow lines

**How it works.** `git_fetch` gains `depth` (or `deepen`). swarm-api runs a
shallow fetch to that depth and answers with a thin pack (`pack-objects
--revs --thin --stdout`), the ref's sha, and the shallow and unshallow lines.
The worker runs `git index-pack --stdin --fix-thin` in the clone, writes
`.git/shallow` and runs `update-ref`.

**Measured, E5.** The same slice as a thin pack was **3,454 bytes**. After
`index-pack --fix-thin` and the shallow update, `rev-list --objects <T>
--not --all` exited 0 and `fsck --connectivity-only` was clean. The merge
succeeded. Before the shallow update the connectivity check exited 128.

* **Cost.** The transfer is the same as (a). If the agent picks the depth,
  it finds the merge base by trial: N calls, each against the 60-per-hour
  call budget. Each call re-fetches every boundary snapshot into a fresh
  scratch repository, so N calls cost N snapshots.
* **Security.** An agent-chosen depth is a resource knob in the caller's
  hands. The route has refused that kind of argument from the start (§1.1:
  "None of them takes ... a refspec"). The pack needs new delivery plumbing
  beside the bundle's: its own size check, sha256, GCS object and ref name.
* **Failure modes.** The same as (a), plus an agent that guesses too shallow
  gets no merge base and calls again.

(a) and (b) are close in git terms. They differ in who chooses the depth and
in what the wire carries. §5.1 and §5.2 decide both.

### (c) No route change: a worker-side deepen through the clone's credential

**How it works.** A new tool makes the worker run `fetch_pull_base`-style
`git fetch --depth=d origin <ref>` in the agent's clone, with the tenant
credential the clone used.

**What that needs, checked on main.** The clone's credential is **not
present while the agent runs**. `shallow_clone` writes the credential file
(`_clone_env` → `_write_credentials`) and removes it in its `finally`
(`gitops.py:1169`, `_remove_credentials(cred_file, logger)`). `fetch_pull_base`
runs inside that `try` exactly so that "the base is fetched with the
credential the clone used, and loses it with the clone" (`gitops.py:1157-1158`).
All of this happens before the agent starts. So (c) would need the worker to
hold the forge token **while the agent is alive**, and to write it to a file
or a git child's environment in a repository the agent controls.
runner-git-tools.md §0 rules that out: "The agent and the worker run as the
same uid … Anything the worker does with a token while the agent is alive,
the agent can reach: through the credential file, through
`/proc/<pid>/environ` of a git subprocess, or through a loopback proxy." The
owner accepted that narrowing on 2026-10-02 and built the whole git-tools
route (Q4 = A) to keep it.

* **Cost.** It is the cheapest in bytes, because it is git's own protocol (E4).
* **Security.** It breaks §0. An agent-planted `insteadOf`, hook or
  `core.sshCommand` in `.git/config` runs with the token in reach.
* **Failure modes.** The same as `fetch_pull_base`. The failure that matters
  is a leaked token.

---

## 4. What stays as it is

* The token's lifecycle inside `gitexec.py`, the `_LIMIT_SHIM`, the per-call
  deadline and the slots.
* Delivery: inline up to `INLINE_BUNDLE_BYTES` (24 MiB), else `FETCH_OBJECT`
  under the tenant prefix, checked by sha256 on the worker
  (`_received_bundle`, worker `gittools.py:667-699`).
* `_apply_bundle`'s `git fetch` with `transfer.fsckObjects` and
  `fetch.fsckObjects`, and its check that `local` landed on the resolved sha
  (worker `gittools.py:701-726`).
* A call with no boundary (an old worker, or a full clone) gets today's
  answer. Old workers and new swarm-api interoperate in both directions.

---

## 5. The forks

### 5.1 Which option

(a) keeps the route's shape (one ref in, a bundle out) and keeps the depth a
platform decision. (b) gives the agent a resource knob and N round trips for
the same bytes. (c) breaks §0.

**Recommended: (a), because** it is the only option that serves a shallow
clone in one call without a new caller-chosen parameter and without a token
in the container. E6 measured it moving about 3 KB where today's route moves
4.4 MB and still leaves no merge base.

### 5.2 Wire format: a bundle with shallow lines in the JSON body, or a raw pack

The bundle path already has a size check, inline/GCS delivery under the
tenant prefix, a sha256, a header the worker reads (`bundle list-heads` names
the ref, worker `gittools.py:702-711`) and a reviewed apply. A raw pack needs
all of that again and an `update-ref` of its own. E6 showed a git-made bundle
with no prerequisite applying once the shallow file is written. The shallow
lines are shas, so they fit in the JSON answer beside `resolved_sha`.

**Recommended: a bundle, with `shallow`, `unshallow`, `merge_base` and `depth`
in the JSON body, because** every delivery and apply step that the security
review covered stays as it is, and only the shallow-file write is new.

### 5.3 How the depth is chosen

* *Fixed depth.* Too small misses the merge base, and too large lands a
  slice that nobody needed.
* *Doubling rounds,* as in `fetch_pull_base` (64, doubling, at most 8
  rounds = 8,192 commits on each side). This is proven in production since
  2026-10-31, and its stop test is the property the agent needs: a merge base
  exists.
* *`--shallow-exclude` at the haves.* E3a: upload-pack refuses a sha
  ("ambiguous deepen-not"). The haves are shas, and the remote's ref names for
  them are not stable. E3b: with a ref name it cuts **above** the merge base,
  so a merge finds none.
* *`--shallow-since`/deepen-since.* It is bounded by commit dates, not by
  commits. A busy repository lands thousands of commits in a day, a quiet one
  never reaches its base, and it does not stop at the merge base.

**Recommended: doubling rounds with `fetch_pull_base`'s numbers (first depth
64, at most 8 rounds), as gitexec's own constants commented to match, because**
it is the only rule whose stop test is the merge base itself, and the numbers
are already the platform's. The constants are restated, not imported: swarm-api
does not import `agent_worker`.

### 5.4 Where the rounds run: one swarm-api call, or the worker looping calls

The worker could ask for depth 64, then 128, and so on. Each call would
re-fetch the boundary snapshots into a fresh scratch repository, spend a call
from the 60-per-hour budget, write an audit row and take a slot. swarm-api
keeps the scratch repository across its own rounds, so each round fetches
only the new commits.

**Recommended: the rounds run inside one `fetch_bundle` call, within
`CALL_DEADLINE_SECONDS` (600 s), because** each snapshot is fetched once, the
call stays one audited operation, and the worker's contract stays "one ref
in, one answer out".

### 5.5 What the cap counts

There are three numbers, and they count different things:

| limit | counts | where |
|---|---|---|
| `MAX_FETCH_FILE_BYTES`, 512 MiB (PR #3964) | **each pack file** a forge fetch writes into the scratch repository (RLIMIT_FSIZE is per file) | the kernel, per git |
| a new `MAX_FETCH_ROUND_BYTES`, 512 MiB | **the scratch object store's growth over all rounds**: what was fetched from the forge for this call | checked after each round, as `fetch_pull_base` checks `PULL_BASE_MAX_BYTES` (`_object_bytes`) |
| `MAX_BUNDLE_BYTES`, 256 MiB (Q6) | **the bundle**: what is sent to the worker | the existing check after `bundle create` (`gitexec.py:550-553`) and the worker's (`_received_bundle`) |

RLIMIT_FSIZE alone does not bound a call that makes eight packs. Without the
round total, eight rounds could each land just under 512 MiB in tmpfs. The
footprint of one call is then at most the boundary snapshots, plus the round
total, plus one bundle. That fits #3964's own arithmetic (2 calls × (512 MiB
of packs + 512 MiB of git) in a 2 GiB instance) only if the round total
includes the snapshots. So the total is measured from the empty repository,
not from after the snapshots.

**Recommended: what is fetched (the round total, from the empty scratch
repository) and what is sent (the bundle) are each capped, and a `too_large`
names which one it hit, because** #2901 asks exactly that ("the size limit
counts what is fetched"), and a refusal that names the limit tells the agent
whether a nearer ref could work. The repository's own size no longer appears
in either number.

### 5.6 Which boundary the worker sends

The agent owns `.git/shallow` and can write anything there. The worker's rule
for "origin has it" is already "never read from the clone"
(`_Origin`, worker `gittools.py:260-270`). Before the agent starts, though,
the file is the worker's own work: the clone and `fetch_pull_base` wrote it.

**Recommended: the worker records `.git/shallow` once, after the clone and
before the agent starts (beside the clone base it already records). It then
adds every `shallow` line and removes every `unshallow` line that an answer
gave it. It sends that set (the newest `MAX_HAVES`), never a fresh read of the
file, because** a forged boundary could otherwise steer the scratch fetches.
It could not leak anything, but it would spend the call's budget on the
remote's snapshots, and the recorded set is the true boundary unless the agent
changed the clone, in which case the apply fails safe (§5.7). The answer's
`shallow`, `unshallow` and `merge_base` shas also join `_Origin.haves`, since
swarm-api read them from the forge.

### 5.7 How the clone applies the shallow lines safely

The write is the worker's, never the agent's: the worker is the process that
holds the spool and runs `_apply_bundle`, and it writes no secret. Its steps:

1. Validate the answer: every line is a 40-hex sha; `unshallow` ⊆ the
   boundary sent; `shallow` has at most `MAX_HAVES` × 2 lines.
2. `lstat` `<clone>/.git`: it must be a real directory, not a symlink and not
   a `gitdir:` file. Otherwise refuse `fetch_failed`. A gitfile would let the
   agent point the write at a path of its choosing, the worker's private
   directory included.
3. Read the old `.git/shallow` (absent = not shallow). Write the new set,
   `(old − unshallow) ∪ shallow`, to `.git/shallow.lock` opened
   `O_CREAT|O_EXCL|O_NOFOLLOW` (git's own lock, so a concurrent git in the
   clone sees it as busy), then `rename` it over `.git/shallow`.
4. Run today's `git fetch <bundle>` (fsckObjects on). E6: it fails before
   step 3 and succeeds after it.
5. On any failure in step 4, restore the old content the same way, so the
   clone is as it was.

**Recommended: those five steps, in `GitTools._apply_bundle`, because** the
order is the one git's connectivity check needs (measured), and a forged or
stale boundary leaves the clone unchanged. The publish path is unaffected.
`prepare_publish_repo` still builds its own worker-owned repository and reads
the clone's `.git/shallow` only as data, confirmed against origin
(`confirm_on_origin`, #2559). The new lines are origin commits, so they
confirm.

### 5.8 too_large, and a base the clone already reaches

With (a), `too_large` is left for two cases: a slice that does not reach a
merge base within the round budget, and a bundle over the send cap. For the
first case there are two sub-choices.

*When no merge base is reached in budget.* Either refuse, or deliver the ref
at the depth reached with `merge_base: null`. The second is useful for reading
files and for `git diff`, and is wrong for a merge.

**Recommended: `git_fetch` delivers what fits with `merge_base: null` and
says so; `git_rebase_onto` refuses `too_large` before it rebases when
`merge_base` is null, because** a fetch for reading should not fail for want of
history it does not need, and a rebase with no merge base would replay
commits onto an unrelated line.

*When the ref is the publish_to_pr base the clone already holds.* The worker
knows: `CloneResult.base` (`gitops.py:345-347`) holds `fetch_pull_base`'s
record, `{ref, remote_ref, merge_base, …}`, and the lifecycle keeps it under
`publish_to_pr.base` (`lifecycle.py:6632-6633`). There are three ways to
answer:

* *Answer with that sha.* That would be wrong: `resolved_sha` means "the tip
  now", and the clone-time tip may be hours old.
* *Point at it.* Keep `too_large`, and add to the message: "origin/<base> is
  in your clone at <sha>, fetched at clone time with merge base <mb>; merge
  or rebase onto that".
* *Stay terminal,* as today. That is the #2901 dead end.

**Recommended: point at it, because** the refusal stays true (the platform
could not fetch the current tip), and the agent gets the one next step that
works without a credential. This is the same move as `fetch_pull_base`'s
own records, which say what was reached.

### 5.9 git_rebase_onto on a shallow result

`rebase_onto` (worker `gittools.py:730-757`) is `fetch` + `git rebase <sha>`.
After a successful apply (E6), the rebase computes its upstream from a real
merge base, and the measured rebase succeeded. The worker's `rebased_onto`
record and the publish check that HEAD descends from it are unchanged.

**Recommended: no change beyond §5.8's refusal when `merge_base` is null,
because** the rebase only needs the merge base, and the apply gives it that.

### 5.10 The frozen contract

`FetchBody`, the route's answer, `GitExec` and the worker's `GitTools` are
swarm-api's and the worker's own. `grep -rln "git_fetch\|FetchBody\|haves\|shallow"
apps/common/swarm_common/` finds nothing (2026-11-13).

**Recommended: no change, and none requested, because** every new field is on
a route body or an answer outside `swarm_common`. If a build lane finds
otherwise, it files a request in `docs/contract-change-requests.md`. It does
not edit the frozen package.

---

## 6. Security review notes

* **The token.** It is unchanged: read per call, held in the per-call scratch
  credential file, unlinked in `finally`. The new rounds run inside the
  existing `with self._credential(...)` block. The answer's new fields are
  shas and integers.
* **New inputs.** `shallow` in `FetchBody` is at most `MAX_HAVES_SENT` long on
  the wire and trimmed to the newest `MAX_HAVES` (16), the same as `haves`
  (`gittools.py:1044`). Each line is a 40-hex sha (`_SHA`), so nothing git
  could read as an option or a refspec. Each costs one depth-1 fetch, as a have
  does today. **There is no caller-chosen depth**: the depth is swarm-api's
  doubling rule.
* **Kernel limits.** Every round's git runs under `_LIMIT_SHIM` (RLIMIT_AS
  `MAX_GIT_MEMORY_BYTES`, RLIMIT_FSIZE as in #3964), inside the one
  `CALL_DEADLINE_SECONDS` deadline, in a slot. The round total of §5.5 bounds
  what RLIMIT_FSIZE cannot, because it is per file.
* **No new egress.** The rounds are fetches from the same URL that passed
  `forge.may_receive_forge_token`, under `protocol.allow=never` with https
  only.
* **Tenant isolation (invariant 9).** A large bundle is still `FETCH_OBJECT`
  under `tenants/<tenant>/attempts/<attempt>/git/`. The worker reads only
  that prefix, with its own GSA (`_received_bundle`).
* **The clone write.** The worker writes 40-hex lines through an `O_EXCL|
  O_NOFOLLOW` lock in a `.git` it has `lstat`ed as a real directory. It
  restores the old file on failure. What it writes gives the same-uid agent
  nothing it could not already do.
* **The publish path.** It is unchanged. It still trusts only shas the worker
  learnt or swarm-api confirmed.

The review this needs, per CLAUDE.md "One review, where it matters", is the
credential and tenant-isolation one. The token path does not change, so the
review is of the new input bounds and the clone write.

---

## 7. The build that follows

Order: after **#3964** lands (it adds `MAX_FETCH_FILE_BYTES` and the
`fetch=True` runs that these rounds use), and after **#3979** lands (it edits
`gitexec.py`, both `gittools.py` files and `docs/runner-git-tools.md`). Each
lane pushes its tests first, red, then the fix (CLAUDE.md, "Red first").

### Lane S: swarm-api (`apps/swarm-api/swarm_api/gitexec.py`, `apps/swarm-api/swarm_api/gittools.py`)

`fetch_bundle(..., shallow=())` gets the boundary fetches, the doubling
rounds, the round total, `bundle create` with the `^<tree>` exclusions, and
`FetchedBundle.shallow/unshallow/merge_base/depth`. `FetchBody.shallow` and
the answer fields go in `gittools.py`, and the audit row gains `depth`,
`rounds` and `bytes`. The two files go in one lane because the body and the
library change together.

Tests written first, in a new `tests/unit/control_plane/test_gitexec_shallow.py`.
They use local `file://` origins and `local_remotes=True`, as `test_gitexec.py`
does:

* `test_depth1_clone_three_behind_gets_a_small_bundle_that_applies`: an
  origin of 1,000 commits with a 4 MiB blob, and a depth-1 clone 3 commits
  behind. The bundle for `main` is under 16 KiB. After the answer's shallow
  lines are written it applies, and `merge-base` is the fork point.
* `test_no_forge_fetch_without_a_depth`: a recording runner. Every `fetch`
  argv to the remote carries `--depth=`, so the whole history never lands.
* `test_rounds_double_until_a_merge_base`: a 100-commit gap reaches the base
  in round 2 (64, then 128).
* `test_round_budget_refuses_too_large_naming_what_was_fetched`: with
  `max_rounds=2` and a 1,000-commit gap the answer is `merge_base: None`,
  and with the round total set low it is `too_large`, naming the fetch total.
* `test_unrelated_ref_is_delivered_without_a_merge_base`.
* `test_shallow_lines_are_bounded_full_shas`: 17 lines, a short sha or
  `--upload-pack=x` gives `bad_ref`.
* `test_boundary_unknown_to_the_remote_is_skipped`.
* `test_no_boundary_keeps_todays_bundle`: the answer is byte-for-byte today's.

In `tests/unit/control_plane/test_git_tools_api.py`:
`test_fetch_passes_the_newest_shallow_lines`,
`test_fetch_answer_carries_shallow_unshallow_and_merge_base`,
`test_large_shallow_fetch_bundle_is_a_tenant_prefixed_object`.

### Lane W: the worker (`apps/agent-worker/agent_worker/gittools.py`, `apps/agent-worker/agent_worker/gitmcp/server.py`, `docs/runner-git-tools.md`)

The recorded boundary (§5.6), `shallow` in the request, the five-step apply
(§5.7), `merge_base`-null handling in `rebase_onto` and the §5.8 message that
names `origin/<base>`. The tool descriptions say that a fetch brings the
history to a merge base. In `runner-git-tools.md`, §1.1 and §1.3's fetch
steps describe the new answer and link here. Lane W starts after lane S is
deployed. A worker that sends `shallow` to an older swarm-api gets today's
answer (the field is ignored), so the order is safe either way.

Tests written first, in a new `tests/unit/worker/test_gittools_shallow.py`:

* `test_fetch_sends_the_recorded_boundary_not_the_clones_file`: the agent
  rewrites `.git/shallow`, and the request carries the recorded set.
* `test_apply_writes_shallow_then_fetches_and_reaches_the_merge_base`
  (local origin, real git).
* `test_failed_apply_restores_the_shallow_file`.
* `test_refuses_a_symlinked_or_gitfile_git_dir`.
* `test_rejects_malformed_shallow_lines_from_the_api`, and
  `test_rejects_unshallow_outside_the_boundary_sent`.
* `test_too_large_names_origin_base_when_the_pull_base_was_recorded`.
* `test_rebase_onto_refuses_without_a_merge_base` and
  `test_rebase_onto_a_shallow_result`.
* In `tests/unit/worker/test_gitmcp.py`: the `git_fetch` description names
  the merge base.

### Existing tests that stay green

`tests/unit/control_plane/test_gitexec.py`,
`tests/unit/control_plane/test_gitexec_prerequisite_refetch.py`,
`tests/unit/control_plane/test_git_tools_api.py`,
`tests/unit/worker/test_gittools.py`,
`tests/unit/worker/test_publish_to_pr_pull_base.py`, and
`tests/unit/worker/test_gittools_push_range.py` (the publish boundary that
confirms shallow lines).

---

## 8. Open questions for the owner

### Q1. Build the route change at all?

* **A. Yes, as §5 recommends (recommended).** publish_to_pr is covered, but
  any other fetch from a shallow clone is not, and §2.2 shows that even a
  fetch that fits gives no merge base.
* **B. No.** Record #2901 as done by PR 2904, and keep the route's
  `too_large` with §5.8's message only.

### Q2. When the round budget reaches no merge base

* **A. `git_fetch` delivers the ref with `merge_base: null`;
  `git_rebase_onto` refuses (recommended).**
* **B. Both refuse `too_large`.**

### Q3. The publish_to_pr base the clone already has

* **A. Refuse, and name `origin/<base>`, its sha and its merge base
  (recommended).**
* **B. Answer with the clone-time sha.** That is simpler, but it makes
  `resolved_sha` mean two things.
* **C. Stay terminal, as today.**

### Q4. The fetch round total

* **A. 512 MiB, from the empty scratch repository, beside #3964's per-file
  cap (recommended).**
* **B. A different number.**

### Q5. Shrink the boundary snapshots later

A have or boundary fetched with `--filter=blob:none` would hold trees only.
That is not measured here, and it needs the forge's partial-clone support and
`rev-list --missing=allow-promisor` in the exclusion.

* **A. Not in this build. Measure it as its own issue if the snapshot is the
  next limit hit (recommended).**
* **B. Include it.**
