# Jev in the work contract: a review at submission, a judgment at the end

**Status, 3 November 2026.** Kept apart, as the program requires:

| | State |
|---|---|
| Proposed | this document |
| Implemented | **no**: nothing in this design is built; the build lanes are [section 13](#13-build-lanes-in-order) |
| Behaviourally tested | **no** |
| Enabled in configuration | **no**: the work contracts themselves ship off (`SWARM_WORK_CONTRACTS` unset everywhere) |
| Live-verified | **not read from here** |

The owner decided on 3 November 2026 (BGD) to use Jev to make the work
contract better and more enforceable, both ways:

1. **At submission**, Jev reviews the contract and flags the parts no worker
   could enforce, before anything runs. The submission is refused or amended
   on that judgment. A refusal is **422 `validation_failed`** with the
   contract's reason in its `detail` (the owner's choice for every contract
   refusal), plus Jev's reason.
2. **At the end of a step**, Jev judges the contract's acceptance criteria that
   the worker cannot check mechanically, and the step is accepted or sent back
   on that verdict, with its evidence.

This document is the design only. It builds on the work contract package:
`docs/design/agent-quality/work-contract.md` and
`apps/swarm-api/swarm_api/workcontract.py`, on branch
`swarm/task_bebb5bf41b054bc1b584` (part of #2688, not yet on main when this was
written), and on main's Jev machinery: [jev-data-flow.md](../../jev-data-flow.md),
the Jev rows of the [capability matrix](capability-matrix.md#2-jev-accounting-and-adapter),
[baseline.md](baseline.md), [known-failures.md](known-failures.md) and
`apps/agent-worker/agent_worker/judge/`. Every choice that is the owner's is
in the pull request's `questions.json` with options and a recommendation;
this document names each one where it arises (**Q1** to **Q12**, listed in
[section 14](#14-open-choices)) and never decides it. The owner answered
all twelve on 3 November 2026; the answers are section 14's last column and
override this document where they differ.

## 1. The rules this design keeps

From the program ([experiment-protocol.md §1](experiment-protocol.md#1-the-programs-rules))
and the work contract design, restated only to say how each binds Jev here:

* **A model judgment is not an authorization decision.** Jev's verdict never
  grants a merge, an approval, a secret, a repository or a wider scope. What
  it can do is add a proof requirement, refuse a submission, and send a step
  back for another round. Each of those costs the caller something and
  grants nothing.
* **Missing verification is not a pass.** A judgment that did not run is
  recorded as not run. A requirement that needs a judgment and has none is
  unmet (`workcontract.unmet` already reads it so: no passing record, unmet).
* **A strict contract never silently degrades to advisory.** Jev never turns a
  strict contract advisory, and a failed Jev call never does either.
* **Jev help has a bounded, deterministic fallback.** Every Jev call here has
  a deterministic path when Jev fails, and the path is written down per call
  site in [section 8](#8-when-jev-is-unavailable-times-out-or-is-over-its-cap).
* **The platform acts on typed values only.** Jev answers closed choices and
  probabilities. Its free text, where kept, is shown to a person and is never
  parsed, matched or acted on.
* **The mechanical check always runs first.** Whatever the compiler, the
  verification kinds or the diff can decide is decided without Jev. Jev is
  asked only what code cannot answer, and never re-asked what code has already
  answered.

## 2. What Jev is asked, in one table

| | Submission review (**JC1**) | Acceptance judgment (**JC2**) |
|---|---|---|
| When | after `decode` and the mechanical `compile_contract` pass, before the revision is stored and signed | after the agent's process tree has exited and the step has a published commit |
| Where the call is made | swarm-api, synchronously, inside the submission request (Q1) | the worker's post-step, posting the verdict through a state route (Q4) |
| Who writes the result | swarm-api | swarm-api, on the worker's fenced post; the worker writes nothing trusted |
| What it judges | each requirement's text against the verification kinds it names | each requirement that names the `judgment` verification kind, against the diff and the mechanical results at that commit |
| What it may change | add the `judgment` kind to a requirement's verification; refuse a strict contract | write `judgment` evidence for those requirements; send the step back |
| Typed answer | per requirement: class, reason code, probability | per requirement: verdict, reason code, cited evidence items, probability |
| Default when Jev fails | Q2 | Q5 |

The two call kinds are new `kind` values in the Jev ledger and in `jev_usage`
(`contract_review`, `contract_judgment`), so the usage page
(`swarm_api/jevusage.py`) shows them apart from J0-J5 and `jev_decide`.

## 3. The contract fields this needs

All of them are in `swarm_api/workcontract.py`, which is swarm-api's, not the
frozen `swarm_common`. None is a frozen-contract change
([section 12](#12-what-the-frozen-contract-would-need)).

* **A new verification kind, `judgment`**, added to `VERIFICATION_KINDS`:
  "a judge reading the diff and the mechanical results at the commit decides
  it". It is a name, like every other kind (invariant 10): a caller names it,
  never supplies the judge or its question. `unmet` needs no change: a
  `judgment` requirement is met only when a passing `Evidence` record of kind
  `judgment` binds the current digest and the commit, exactly as for
  `unit_tests`.
* **`judgment` joins the enforced set only where Jev can run.** A strict
  contract naming `judgment` compiles only for a workspace where the signed
  opt-in is on (Q9; the owner chose `contract_review`, [section 14](#14-open-choices))
  and a backend is configured; elsewhere it is refused
  mechanically, with no Jev call, as `work_contract_not_enforceable` with
  `unenforced: ["judgment"]`. That is today's rule (a strict contract that
  names what nothing enforces is refused), applied to one more component.
* **A new top-level field, `acceptance`**: `strict` (the default when the field
  is absent) or `auto`. See [section 6](#6-the-strict-acceptance-default).
  Weakening it (`strict` to `auto`) is a widening under `widening()`, refused
  from an agent, like `strict` to `advisory` today.
* **A third amendment author, `reviewer`**, beside a person and an agent, used
  only by JC1's amendment ([section 4.4](#44-amended-the-only-change-jev-may-cause)).
  `amend(..., author="reviewer")` accepts exactly one kind of change -- adding
  `judgment` to an existing requirement's `verification` -- and refuses
  everything else with `work_contract_amendment_refused`, so a fault in the
  review code can never widen, rewrite or drop anything. A person's and an
  agent's amendments keep their rules unchanged.
* **The review record**, stored beside the revision (not inside the canonical
  bytes, so it never moves the digest): `jev_review: {digest, outcome,
  call_id, backend, classes: [{id, class, reason, p}], at}`. `outcome` is
  `ok`, `refused`, `amended`, or `not_run` with its reason.

## 4. JC1: the submission review

### 4.1 Where it sits

In swarm-api's submission path, in this order:

1. `workcontract.decode` (closed schema; `work_contract_invalid` as today).
2. `workcontract.compile_contract` with today's enforced set. A strict
   contract with an unenforced component is refused here, mechanically, and
   **Jev is not called**: no tokens are spent on a contract code has already
   refused.
3. **JC1**, only when the workspace's signed Jev opt-in is on (Q9) and the
   contract has at least one requirement. **The owner's answer differs**
   ([section 14](#14-open-choices), Q9): the opt-in is the new signed
   workspace setting `contract_review`, not `code_intelligence.jev`.
4. Apply JC1's result: accept, amend (revision 2 by `reviewer`) or refuse.
5. Compile the final revision, attach its reference to the signed dispatch
   block, store, sign, return.

swarm-api must make this call itself because the refusal is a synchronous 422
on the submission. Today swarm-api never holds a Jev client
([jev-data-flow.md](../../jev-data-flow.md), "Where Jev runs": "Never
swarm-api"), and the internal decision backends are reached only from the
cluster through the tailnet egress Service, which a Cloud Run service cannot
reach. So JC1 needs both a client in swarm-api and a route from swarm-api to
the workspace's backend. How it gets them is **Q1** (the owner chose the
direct VPC route, carried by the Day One install, [section 14](#14-open-choices);
the options were a direct VPC route to the
egress, TypeSafe only, a tailnet sidecar, or an asynchronous review job), and
which code it shares with the worker is **Q10**. The data-flow record's "Never
swarm-api" line changes in the lane that builds it, with its test.

### 4.2 What Jev is given

The contract's own text and nothing else. There is no checkout at submission,
so no code is sent:

```json
{
  "contract": {"strictness": "strict", "acceptance": "strict"},
  "verification_kinds": {
    "unit_tests": "the step's named unit tests ran at the commit and passed",
    "ci": "the repository's CI conclusion at the commit is success",
    "review": "a review step's verdict at the commit is MERGE",
    "browser_check": "a browser check ran against the deployed change and passed",
    "artifact_present": "the named artifact exists at the step's end",
    "judgment": "a judge reading the diff and the mechanical results decides it"
  },
  "requirements": [
    {"index": 0, "text": "...", "verification": ["unit_tests"]}
  ],
  "artifacts": ["pull_request"],
  "write_scope": ["apps/swarm-api/swarm_api/workcontract.py"]
}
```

The requirement ids are replaced by indexes (an id carries nothing a judge
needs), and the tenant, the owner, the contract id, the repository URL and the
commit are never sent. Everything goes through the shared sanitizer (rules 1
to 5 of the data-flow record) like every other Jev call; a requirement text
that the sanitizer drops is reported as not judged, never as enforceable.

### 4.3 What Jev returns

Two closed questions per requirement, on the wire shape `client.py` already
speaks:

* `c<k>`, `choice`, criteria:
  * `mechanical`: "the verification kinds it names can prove it as written";
  * `judgeable`: "a reader of the diff and the test results at the step's end
    could decide it, and the named kinds cannot";
  * `unenforceable`: "nothing observable when the step ends decides it".
* `r<k>`, `choice`, criteria: the closed reason codes `vague_outcome`,
  `unobservable_at_step_end`, `outside_the_repository`,
  `depends_on_a_later_event`, `subjective`, `proof_kind_mismatch`,
  `several_requirements_in_one`, `none`.

A contract of more than 32 requirements is asked in ceil(n / 32) calls
(`client.MAX_QUESTIONS` is 64). Each answer comes back as a choice and its
probability; the reader (`swarm_api/contractjudge.py::read_review`, new) keeps
`{index, class, reason, p}` and drops anything else. A missing or malformed
answer for one requirement makes that requirement `not_judged`, never
`mechanical`.

**Acting threshold.** The platform acts on a class only at p >= 0.6, the
same floor as J0's classification (`shapes.CLASSIFY_MIN_CONFIDENCE`). Below
it the class is recorded and shown, and nothing is refused or amended on it.
The value is a starting point the experiment calibrates
([section 11](#11-how-the-experiment-measures-it)), not an owner's decision.

### 4.4 Amended: the only change Jev may cause

A requirement Jev classes `judgeable` (p >= 0.6) that does not already name
`judgment` gets `judgment` added to its `verification`. Nothing else is ever
changed. The platform makes that change from the typed class, through
`amend(author="reviewer")`, which:

* makes revision 2, naming revision 1's digest as `previous_digest`;
* keeps every requirement's id, text and order, and every other field, byte
  for byte;
* only adds a proof kind, so the requirement becomes harder to meet, never
  easier: nothing is widened, which `widening(old, new)` confirms and the test
  asserts.

The response carries both digests, the requirement ids that changed and
Jev's classes, and the console shows them. Whether revision 2 then needs the
person's approval of its digest follows the rule approvals already have: a
contract that needed a person's approval needs it for revision 2, because
approvals bind a digest. Whether the review amends at all, or instead refuses
with the amendment as a proposal for the person to resubmit, is **Q3**.

### 4.5 Refused

| Contract | Jev classes a requirement `unenforceable` (p >= 0.6) | Result |
|---|---|---|
| strict | yes | **422 `validation_failed`**, nothing stored |
| advisory | yes | accepted; the requirement is named in `CompiledContract.advisory` and in the brief's "Advisory, not enforced by the platform" line, as the advisory rule already does for components |
| either | no, only `mechanical` and `judgeable` | accepted, amended where 4.4 says |

The refusal body:

```json
{
  "code": "validation_failed",
  "message": "contract wc-example is strict, and Jev found requirements no worker could enforce: REQ-2",
  "detail": {
    "reason": "work_contract_not_enforceable",
    "contract_id": "wc-example",
    "revision": 1,
    "digest": "<64 hex>",
    "unenforced": [],
    "jev": {
      "call_id": "<uuid>",
      "backend": "internal-jev",
      "requirements": [
        {"id": "REQ-2", "class": "unenforceable", "reason": "vague_outcome", "p": 0.83}
      ]
    }
  }
}
```

`detail.reason` is the contract's own refusal code, the one `compile_contract`
already raises for an unenforced component, so a caller branches on one name
whichever check refused; `detail.jev` is Jev's part and is present only when
Jev refused. A requirement's text is never echoed back, and neither is
anything from Jev that is not one of its closed values.

**Shipping it off.** A new refusal in swarm-api ships switched off
(`swarm_api/refusals.py`), and its switch is keyed on the error's `code`. This
refusal's code is the established `validation_failed`, so it cannot get a
switch of its own without changing that mechanism. How it ships off is
**Q7**; until it is on, the review runs in report-only mode, logs the refusal
it would have made, and lets the submission through. **The owner's answer
differs** ([section 14](#14-open-choices), Q7): its own mode setting,
`SWARM_CONTRACT_REVIEW=off|report|enforce`, outside `refusals.py`.

### 4.6 Re-review on amendment

Every new revision is a new digest, and the review record binds the digest it
judged. A person's or an agent's amendment is reviewed again, but only the
requirements `Amendment.added` and `Amendment.changed` name; the unchanged
requirements' classes are carried from the previous revision's record, by
requirement id and text, so an amendment that adds one requirement costs one
requirement's questions, not the whole contract's.

## 5. JC2: the acceptance judgment at the end of a step

### 5.1 Where it sits

The recommended placement (**Q4**) is the worker's post-step, next to J2, J3
and J5 in `runners/cliagent._jev_post_steps`:

* it runs after the agent's process tree has exited, so the agent cannot see
  or answer the call, and before the attempt ends;
* it runs on GKE, where the workspace's backend is reachable today, through
  the same `JudgeClient`, sanitizer and per-agent ledger as J1-J5;
* the worker does **not** write the verdict. It posts it to a new state route,
  `POST /v1/attempts/{attempt_id}/state/contract-verdict`, signed by the
  attempt proof like every state route, and swarm-api runs every check and
  writes the evidence and the transition in one transaction
  ([section 9](#9-fencing-a-stale-or-superseded-attempts-judgment)).

The other placements, a control-plane judgment that reads the published diff
from the forge, and a separate judge step in its own pod, are in Q4 with what
each costs.

Only requirements naming `judgment` are asked. A requirement whose kinds are
all mechanical is decided by its mechanical evidence, never by Jev; a
requirement naming both is met only when both pass.

### 5.2 What Jev is given

```json
{
  "requirements": [{"index": 0, "text": "..."}],
  "commit": "<40 hex>",
  "changed": [
    {"item": 0, "path": "apps/x/y.py", "status": "modified", "hunk": "@@ ... (at most 1,200 characters)"}
  ],
  "mechanical": [
    {"kind": "unit_tests", "passed": true, "named": ["tests/unit/x/test_y.py"]},
    {"kind": "ci", "passed": null}
  ],
  "artifacts": [{"kind": "pull_request", "present": true}]
}
```

* `changed`: the commit's diff against the step's base, file by file, each
  hunk cut to `shapes.HUNK_CHARS` (1,200 characters) and the whole state held
  under `client.MAX_STATE_CHARS`; files past the cap are listed with their path
  and status and no hunk, and the state says how many were cut.
* `mechanical`: the mechanical evidence the platform already holds at this
  commit, with `null` for a check that did not run (never `false`, never
  omitted: missing is not a pass, and it is not a fail either).
* **Never** the agent's own summary, its PR body, its claims about what it did,
  or its transcript. Those are what is being judged, and an agent's narrative
  is the easiest place to argue a judge into a pass. The diff itself is the
  agent's too, and is sent as data: the instructions say so, and the platform
  acts only on closed values, so text in the diff that addresses the judge
  can at most move a probability ([section 10](#10-what-jev-cannot-do-and-the-residuals)).
* Every string passes the shared sanitizer, so a denied path, a
  credential-shaped string or a registered secret in the diff is dropped
  before the call, as for J3 today; the opt-in to send code at all is the
  workspace's signed `code_intelligence.jev`.

### 5.3 What Jev returns

Three closed questions per requirement:

* `v<k>`, `choice`: `met`, `unmet`, `cannot_judge` ("the state does not hold
  what deciding it needs").
* `w<k>`, `choice`: the closed reason codes `not_in_diff`, `partially_done`,
  `contradicted_by_tests`, `wrong_place`, `scope_exceeded`,
  `evidence_missing`, `satisfied`.
* `e<k>`, `choice` over the `changed` items' indexes and the `mechanical`
  entries, plus `none`: the one item the verdict rests on.

The reader (`read_judgment`, new, beside `read_review`) keeps
`{requirement_id, verdict, reason, evidence_item, p}`. A verdict citing an
item that was not in the state, or `none` for a `met`, is malformed and read
as `cannot_judge`. A `met` counts only at p >= 0.7 and an `unmet` sends back
only at p >= 0.7; between, the verdict is `cannot_judge`. Like 0.6 above,
these are starting values the experiment calibrates.

### 5.4 What the platform does with it

swarm-api, in the state route's transaction:

| Every judged requirement | Result |
|---|---|
| `met` | one `Evidence(requirement_id, digest, commit, kind="judgment", passed=True)` each; the step's contract status is `unmet(...)` over all its evidence, and acceptance continues under [section 6](#6-the-strict-acceptance-default) |
| any `unmet` | **sent back**: the `unmet` requirements get `passed=False` evidence, and the agent takes the existing retry transition (`RetryBody`) with cause `contract_unmet`, back to `READY` (`AttemptState.retry`) |
| any `cannot_judge`, no `unmet` | no evidence for those requirements; the step's acceptance is `judgment_pending`, re-asked by the control plane three times over 30 minutes and then sent to the owner (Q5, the owner's answer in [section 14](#14-open-choices)) |

**A send-back** gives the next attempt, in its brief, one block per unmet
requirement: its id, Jev's reason code, the cited file and hunk, and the
words "Jev judged this requirement unmet at commit <sha>". It resumes from
the last checkpoint (invariant 8), under the same contract revision. The
number of send-backs per agent is capped (**Q11**); past the cap, the step goes
to the owner as `judgment_pending` with every verdict attached, never into a
loop. The retry transition spends one of the agent's signed `max_attempts`
(3 unless the spec says otherwise) and turns the agent `FAILED` when they are
exhausted, so the state route checks the cap and the remaining attempts
together: a send-back that would exhaust `max_attempts` goes to the owner as
`judgment_pending` instead, because a step that did its work and was judged
short should reach a person, not fail as if it had crashed.

**The evidence** is what the console's approval screen shows beside each
requirement: the verdict, the reason, the cited hunk and the probability,
labelled as Jev's judgment, with the call id that produced it.

## 6. The strict acceptance default

The brief for this lane says the strict acceptance default "stays the owner's
approval gate". Nothing in the tree names an acceptance default yet (searched
on main at `2f62296` and on the work contract branch), so this design reads
it as the `acceptance` field of [section 3](#3-the-contract-fields-this-needs)
and asks the owner to confirm the reading (**Q6**):

* **`acceptance: strict`, the default.** A step is accepted only by a person's
  approval of its result at this contract digest. Jev's `met` is evidence on
  the approval screen; it never approves, and an approval is never inferred
  from it. Jev's `unmet` sends the step back **before** the owner is asked, so
  the owner's time goes to steps that passed every check. The owner may
  accept a step Jev judged unmet, or that went past the send-back cap: that is
  an `Approval` by a person with a reason, recorded and shown, and the
  experiment counts it as an override.
* **`acceptance: auto`.** A step is accepted when `unmet(contract, evidence,
  commit)` is empty, with Jev's `judgment` records counting for the
  requirements that name it, and only once the workflow's existing gates hold
  (the review's MERGE verdict, CI green). Jev's `met` is then one of the
  proofs, never the only one: a requirement whose only kind is `judgment` is
  refused at submission under `auto` unless the workflow has a review step, so
  no step is ever accepted on a model judgment alone.

Either way, a Jev judgment is never an authorization (rule 1): `strict`
keeps a person as the gate, and `auto` keeps the review and CI as the gate,
with Jev able only to stop a step, not to pass one by itself.

## 7. Cost and latency

**Per call**, measured from the shapes, not from traffic (no traffic exists):

| | Fixed (instructions and criteria) | Per requirement | Typical | Ceiling |
|---|---|---|---|---|
| JC1, per revision | about 600 input tokens | about 150 tokens plus its text | a ten-requirement contract: 2,500 to 3,500 tokens, one call | 64 questions per call; `MAX_STATE_CHARS` per call |
| JC2, per step | about 900 input tokens | about 250 tokens plus the hunks it reads | five judged requirements and a 30-file diff: 8,000 to 15,000 tokens, one call | one call per step attempt; `MAX_STATE_CHARS` (about 20,000 tokens) |

`budget.MEASURED_TOKENS_PER_CALL` (2,850) is the measured mean of the existing
calls; JC1 sits near it and JC2 above it, because JC2 carries diff hunks.

**Monthly**, as an illustration only: 300 contract revisions and 1,000 judged
step attempts are about 1 million and 12 million input tokens. Dollars come
from the platform setting `jev_token_rates`, which has no default; until it is
set, the usage page prints no figure, never $0.

**Which budget pays.**

* JC1 has no agent yet. Its usage is charged to the workspace's month in
  `jev_usage` under kind `contract_review`, in a row of its own
  (`<tenant>_contract_<contract_id>_r<revision>`), written by swarm-api in
  the same transaction that stores the revision. It is admitted only while the
  month's spend is under `code_intelligence.jev_monthly_cap_tokens`.
* JC2 is a call of the agent, so it is in the per-agent ledger (8 calls and
  120,000 input tokens across attempts) and charged to the month by
  `settle_attempt`. Because the agent's `jev_decide` and J0-J5 spend from the
  same ledger, a talkative agent could leave nothing for its own acceptance
  judgment. Whether JC2 gets a reserved slot is **Q8**. **The owner's answer
  differs** ([section 14](#14-open-choices), Q8): the reserve is on top of
  today's budget, 150,000 tokens and nine calls in total, one call and 30,000
  tokens of them reserved for JC2.

**When the cap is reached** the call is skipped as `skipped:budget_monthly`
(or `budget_calls`, `budget_tokens` for the agent), exactly as the client
skips today, and the site's unavailability rule in section 8 applies. A cap
never turns into a pass.

**Latency.** `client.CALL_TIMEOUT_SECONDS` is 10 s with two retries, about 32
s worst case. JC1 runs inside a person's request, so it gets one retry and an
overall 15 s deadline; a typical call adds 1 to 3 s to a submission. JC2 runs
after the agent, so it adds its call time (typically 2 to 6 s) to the
attempt's end and no wait anywhere else; a send-back adds a whole attempt,
which is the point of it, and the experiment counts it in end-to-end latency.

## 8. When Jev is unavailable, times out or is over its cap

"Unavailable" here is any `Decision` that is not `ok`: a timeout, a 429, a
5xx, a malformed answer, an open breaker, a spent per-agent or monthly budget, no
route to the backend, no key, or the workspace not opted in. Each site has
options; the owner chooses (**Q2**, **Q5**). This design does not.

### 8.1 At submission (Q2)

| Option | Strict contract | Advisory contract | Trade-off |
|---|---|---|---|
| **A. Open, recorded** (recommended) | accepted on the mechanical compile alone; `jev_review.outcome: not_run` with the reason, shown on the console and in the response | same | Submissions never depend on an optional service. Nothing is silently weakened: the strict contract is exactly as enforceable as it is today, and a requirement that needs a judge is still unmet at the end until a judgment passes (section 8.2). |
| B. Closed for strict | refused with **503** `jev_unavailable`, retryable; not a 422, because the contract is not invalid | accepted as in A | Every strict submission waits on Jev's availability. |
| C. Closed for all | 503 for every contract with requirements | 503 | Jev becomes a hard dependency of submission. |
| D. Park the submission | stored as `review_pending`; reviewed on the next try; nothing dispatched until then | same | Breaks the synchronous 422 the owner chose, and needs a retry pass. |

### 8.2 At the end of a step (Q5)

**The owner chose option B, then A** ([section 14](#14-open-choices), Q5),
not the recommended A alone: the control plane re-asks three times over 30
minutes, through Q1's route, before the owner's approval screen.

| Option | What happens | Trade-off |
|---|---|---|
| **A. The owner decides** (recommended) | the judged requirements get no evidence, the step's acceptance is `judgment_pending` with the reason, and it goes to the owner's approval with "Jev did not run" against each; the worker exits as usual | Missing verification stays unmet (rule 2) and costs no agent round, because the agent did nothing wrong. Under `auto`, those steps wait for a person. |
| B. Retry later in the control plane | a pass re-asks Jev from the published diff, bounded (three tries over 30 minutes), then A | Needs the control-plane client of Q1 and Q4's option B; more machinery. |
| C. Send back | treat it as `unmet` | Spends an agent attempt on Jev's outage; the agent cannot fix it. |
| D. Fail open | treat it as `met` | Breaks rule 2; listed only so the choice is complete. |

The worker never waits for Jev beyond the one call's deadline (invariant 4):
whatever happens, it posts what it has, or `not_run`, and ends the attempt.

## 9. Fencing a stale or superseded attempt's judgment

**JC2's verdict carries what it judged**: the attempt id, its fencing
generation, the contract digest from the signed dispatch reference, the 40-hex
commit it read, and the Jev call id. The state route (`swarm_api/attemptstate.py`,
new method `contract_verdict`) checks, in the one Firestore transaction that
writes the evidence:

1. the attempt proof, as every state route does;
2. the agent's current generation equals the posted one, and the attempt is the
   agent's current attempt: otherwise **409, nothing written**, and the worker
   exits without running anything further and without touching the lease
   (invariant 5);
3. the digest equals the signed reference's digest on the agent: a contract
   amended while the attempt ran makes the verdict stale, and it is refused
   the same way;
4. the commit equals the commit the attempt reported at finish: a verdict
   about another commit counts for nothing, as `unmet` already says;
5. every requirement id is one of the revision's `judgment` requirements, and
   every cited item is one the worker says it sent (the route stores the
   state's sha256 and the cited paths, which must be among the commit's
   changed paths).

The Jev tokens a stale attempt spent are not charged to the month: its settle
writes nothing (`budget.settle_attempt`), which is today's rule and today's
residual (the backend's own records are the authority on spend).

**JC1's review binds the digest it judged.** The revision is stored only if
its `previous_digest` is still the contract's head, in the same transaction
that stores the review record, so two racing submissions or amendments cannot
both land, and a review of a superseded revision is never attached to the new
one.

## 10. What Jev cannot do, and the residuals

**JC1 may touch** only: the `verification` list of an existing requirement,
by adding `judgment`; and the submission's outcome (refuse a strict contract).

**JC1 may never touch**: requirement ids, text or order; `strictness`;
`acceptance`; `scope`; `protected`; `capabilities` and plugins; `limits`;
`repositories`; `artifacts`; `ownership`; withdrawals; approvals. The
`reviewer` author in `amend` refuses any of these, whatever the review code
passes it.

**JC2 may touch** only: `Evidence` records of kind `judgment`, for
requirements that name it, at the current digest and the attempt's commit;
and the send-back transition.

**JC2 may never touch**: evidence of any other kind; the contract (no
amendment, no withdrawal); approvals; the budget or limits; the merge.

**Residuals**, stated so a reviewer can weigh them:

* **The verdict is worker-reported** under Q4's recommended placement. The
  worker process makes the call after the agent has exited, but a worker that
  is not the platform's could post any verdict the route's checks allow. That
  is the same trust a review step's MERGE verdict has today, and it is why a
  Jev `met` never accepts a step by itself (section 6). Q4's option B removes
  it, at the cost Q4 states.
* **The diff can address the judge.** Text the agent wrote in a changed file
  can try to argue for `met`. The platform acts on closed choices with an
  acting threshold, never on the text, and a `met` alone never accepts; the
  experiment measures how often a `met` meets a failing hidden test.
* **Requirement text leaves the platform at submission** to the workspace's
  chosen backend, which is why JC1 is gated on a signed opt-in (Q9; the
  owner chose its own setting, `contract_review`, apart from the one that
  governs code, [section 14](#14-open-choices)) and goes through the same
  sanitizer.
* **`protected.interfaces`** ("never change the public API of X") is a
  component no worker enforces today; Jev could judge it at the end. Whether
  that judgment may count as enforcing it for a strict contract is **Q12**;
  this design does not count it. Write scope and protected paths are not
  Jev's: the diff's changed paths decide them mechanically, and that check
  belongs to the worker packages the work contract design names.

## 11. How the experiment measures it

Under the protocol's rules ([experiment-protocol.md](experiment-protocol.md)):

* **The arm** is a new signed workflow `dispatch` switch, `contract_jev`
  (`on`/`off`), assigned per whole workflow by `sha256(batch id, item id)`
  like the other switches, and read back from each attempt's
  `result_summary.switches`. Both arms submit the same contracts; arm off
  compiles them with JC1 and JC2 skipped and recorded as `skipped:arm`, so
  arm off is exactly today's behaviour.
* **JC1's measures.** The refusal rate and the amendment rate per contract;
  for each refusal, whether the person resubmitted a narrower contract or
  appealed (a person's re-submission of the same requirement marked
  "enforceable", recorded as an override). A refused requirement that a
  person overrides and that the end judgment then decides cleanly is a
  false refusal.
* **JC2's measures**, against the held-out hidden tests and the review's
  verdict: false send-back (Jev `unmet`, hidden tests pass and the review is
  MERGE), missed defect (Jev `met`, hidden tests fail), `cannot_judge` rate,
  and calibration (Brier score of p against the hidden-test outcome), which is
  what moves the 0.6 and 0.7 thresholds.
* **Outcome and cost**, as the protocol defines them: the accepted-item success rate
  with its Wilson interval, cost per accepted item with Jev tokens priced at
  `jev_token_rates` (an unpriced arm reports no cost, never $0), and
  end-to-end latency p50 and p95 with send-back rounds included.
* **Owner time**: approvals per accepted item and owner overrides of Jev, the
  thing the strict default is meant to save.
* Historical items with Jev on are reported apart, because whether a backend
  keeps anything between calls is not read from here (protocol §5).

## 12. What the frozen contract would need

**Nothing new.** Every field here lives in `swarm_api/workcontract.py`, the
state route's body is swarm-api's, and the Firestore shapes (`jev_review`,
the `judgment` evidence, the `contract_review` usage rows) are swarm-api's
documents. The worker learns the contract body for JC2 from swarm-api's
attempt context route and posts the digest it was given; swarm-api compares it
with the signed reference, so the worker does not need to compute a digest
itself. Contract request 136 (the digest in `swarm_common`) would let the
worker check the body before calling Jev, which saves a wasted call on a
mismatch; it is a nice-to-have here, not a dependency.

## 13. Build lanes, in order

Each lane is one territory; two lanes that edit the same file are one lane.

| # | Lane | Files | Depends on | Proves |
|---|---|---|---|---|
| 1 | **JEV-CLIENT-SHARE** (Q10) | new `apps/jev-client/` (`swarm_jev`: the wire shape, `validate_questions`, `JudgeClient`, `sanitize`), `pyproject.toml` sources, `apps/agent-worker/agent_worker/judge/client.py` and `sanitize.py` re-exporting it | none | the worker's existing judge tests pass unchanged; a new `tests/unit/jev_client/test_shared_client.py` holds the worker's names identical to the shared ones |
| 2 | **CONTRACT-JUDGE-SHAPES** | `apps/swarm-api/swarm_api/workcontract.py` (`judgment` kind, `acceptance`, the `reviewer` author), new `apps/swarm-api/swarm_api/contractjudge.py` (pure: `review_state`, `review_questions`, `read_review`, `judgment_state`, `judgment_questions`, `read_judgment`, the thresholds) | the work contract branch merged | new `tests/unit/control_plane/test_contract_judge.py`: the reviewer may only add `judgment`, malformed answers read as `not_judged` / `cannot_judge`, the state never carries the tenant, the owner or the agent's summary |
| 3 | **JEV-REACH-API** (Q1) | `terraform/infra/` (swarm-api's egress to the workspace backend), `swarm_api/judgment.py` (backend and key resolution for a call, not only availability) | 1 | a terraform test of the route; a unit test that an unreachable backend reads `no_route` |
| 4 | **CONTRACT-REVIEW-SUBMIT** | the submission route the work contract lane wires (`swarm_api/service.py`), new `swarm_api/contractreview.py`, `swarm_api/refusals.py` (Q7), `swarm_api/jevusage.py` (kind `contract_review`), `docs/jev-data-flow.md` ("Where Jev runs") and its test | 2, 3, and the work contract submission route | report-only mode logs the 422 it would make; the 422 body is the shape of 4.5; a failed call follows Q2; usage lands in the month |
| 5 | **CONTRACT-VERDICT-ROUTE** | `swarm_api/attemptstate.py`, `swarm_api/routes/attemptstate.py` (`contract-verdict`), the evidence store, the send-back through the retry transition | 2 | every check of section 9 refuses with nothing written; a send-back lands in `READY`; the cap of Q11 hands to the owner |
| 6 | **WORKER-CONTRACT-JUDGE** | `agent_worker/judge/poststeps.py` (`contract_post_step`), `agent_worker/judge/shapes.py` (JC2's state from the checkout), `agent_worker/judge/budget.py` (Q8's reservation), `agent_worker/runners/cliagent.py` | 1, 2, 5 | offline: the agent's summary never reaches the state; a failed call posts `not_run`; a stale generation posts nothing further and exits |
| 7 | **CONSOLE-CONTRACT-JEV** | `apps/swarm-ui/` approval screen and submission error, with its vitest files | 4, 5 | the refusal shows Jev's reasons per requirement; the approval screen shows each verdict with its cited hunk, labelled as Jev's |
| 8 | **CONTRACT-JEV-EXPERIMENT** | `swarm_api/validation.py` (the `contract_jev` dispatch switch), `scripts/benchstat.py` (section 11's measures), [experiment-protocol.md](experiment-protocol.md) | 4, 6 | the arm reads back from `result_summary.switches`; an unpriced arm prints no cost |

Lane 2 also adds the capability-matrix rows (section 1 of the matrix) for
each piece, "not implemented" until lanes 4 to 6 land.

## 14. Open choices

All were in the pull request's `questions.json`, with options and a
recommendation. The owner answered all twelve on 3 November 2026 (BGD, about
12:39 universal time). **The owner's answer overrides this document wherever
the two differ**: four answers (Q5, Q7, Q8 and Q9) differ from the
recommendation, Q1's adds that the Day One install carries the route, and the
sections they change point here.

| | Choice | Recommended | Owner's answer, 3 November 2026 | Blocks lane |
|---|---|---|---|---|
| Q1 | How swarm-api reaches the workspace's Jev backend at submission | a direct VPC route from swarm-api to an internal load balancer in front of the tailnet egress Service | **As recommended, and more:** Direct VPC egress from swarm-api and an internal passthrough load balancer in front of the tailnet egress Service in `swarm-system`. The Day One install must carry it (`apps/swarm-install` and its stages), so a fresh deployment gets the route. | 3 |
| Q2 | Submission when Jev is unavailable, times out or is over its cap | open, recorded as `not_run` | **As recommended:** the contract is accepted open, recorded as `jev_review.outcome: not_run` with its reason. | 4 |
| Q3 | Whether JC1 amends automatically or refuses with a proposal | amend automatically, as revision 2 by `reviewer`, adding only `judgment` | **As recommended:** a requirement Jev classes `judgeable` is amended automatically, revision 2 by `reviewer`, adding only `judgment`. | 2, 4 |
| Q4 | Where JC2 runs | the worker's post-step, posting through the state route | **As recommended:** the worker's post-step, posting through the state route. | 5, 6 |
| Q5 | The end of a step when Jev is unavailable or cannot judge | the owner decides on the approval screen | **Differs (option B, then A):** when Jev is down, over its cap or answers `cannot_judge` at the end of a step, the **control plane** re-asks Jev three times over 30 minutes from the published diff, reaching it through Q1's route, and only then sends the step to the owner's approval screen. Section 8.2's option B is the one to build; the step is `judgment_pending` while the retries run. | 5 |
| Q6 | What the strict acceptance default is | the `acceptance` field, `strict` by default: a person's approval, with Jev's verdict as evidence and its `unmet` sending back first | **As recommended:** the `acceptance` field, `strict` by default. | 2, 5 |
| Q7 | How a 422 `validation_failed` refusal ships switched off | a refusal switch keyed on `detail.reason` in `refusals.py` | **Differs:** the submission refusal ships switched off through its **own** mode setting, `SWARM_CONTRACT_REVIEW=off\|report\|enforce`, outside `refusals.py`. `off` runs no review, `report` reviews and logs the refusal it would make, `enforce` refuses. | 4 |
| Q8 | Whether JC2 has a reserved slot in the per-agent ledger | reserve one call and 30,000 tokens that `jev_decide` and J0-J5 cannot spend | **Differs:** the reserve is added **on top of** today's per-agent budget, not carved out of it: 150,000 input tokens in total (today's 120,000 plus a 30,000 reserve) and one reserved call on top of today's eight, which `jev_decide` and J0-J5 cannot spend. | 6 |
| Q9 | Which opt-in gates JC1 | the same signed `code_intelligence.jev` | **Differs:** a **new** signed workspace setting, `contract_review` (on/off), gates JC1, not `code_intelligence.jev`, so a workspace can have its contracts reviewed without sending code. JC2 still sends code and stays gated on `code_intelligence.jev`. | 4 |
| Q10 | How swarm-api and the worker share the Jev client | a new shared package, as `swarm-redaction` is | **As recommended:** a new shared package, as `swarm-redaction` is. | 1 |
| Q11 | The send-back cap per agent | two | **As recommended:** two send-backs per agent, then the owner (`contractjudge.MAX_SEND_BACKS`). | 5 |
| Q12 | Whether a Jev judgment of `protected.interfaces` counts as enforcing it | no | **As recommended:** never. `workcontract.enforced_with_judgment` adds only the `judgment` component, and JC2 judges requirements, never `protected`. | 2 |

## 15. CONTRACT.md invariants

1. **Demand.** A send-back takes the existing retry transition to `READY`,
   which costs nothing; JC1 runs inside a request and creates no agent, lease
   or pod. No pending pod is used as a backlog.
2. **All-or-nothing reservation.** Untouched: a send-back's next attempt is
   leased by the existing transaction like any other.
3. **Concurrency from `LEASED`.** Untouched.
4. **No sleeping through a wait.** JC2 is one bounded call after the agent has
   exited; the worker never waits for Jev past its deadline, posts what it
   has, and exits. JC1 has a 15 s deadline inside the request.
5. **Fencing.** JC2's verdict carries the generation, and a stale one is
   refused with nothing written; the worker then exits without touching the
   lease (section 9).
6. **Spot.** Untouched.
7. **Requests and limits.** Untouched.
8. **Checkpointing.** A sent-back step resumes from its last checkpoint; Jev's
   availability never relaxes it.
9. **Per-tenant isolation.** Every Jev call uses the workspace's own backend
   choice and key, carries only that workspace's contract and diff, and its
   usage is filed under its tenant; the state never names the tenant, the
   owner or the repository URL.
10. **Callers pick by name.** `judgment` and `acceptance` are names from closed
    lists; Jev answers closed choices; nothing a caller or Jev supplies becomes
    an image, a command, a resource spec or a backend parameter.
