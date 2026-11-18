# Versioned work contracts

**Status, 3 November 2026.** Kept apart, as the program requires:

| | State |
|---|---|
| Proposed | this document |
| Implemented | `apps/swarm-api/swarm_api/workcontract.py`, and `DispatchOptions.work_contract` in `apps/swarm-api/swarm_api/validation.py` |
| Behaviourally tested | `tests/unit/control_plane/test_work_contract.py` (offline: schema, canonical bytes, compiler, signed reference, tamper, amendments, refusal, compatibility matrix) and `tests/unit/control_plane/test_work_contract_submission.py` (the submission doors and issue runs) |
| Enabled in configuration | **no**: `SWARM_WORK_CONTRACTS` is unset everywhere, so no agent's task document carries a contract |
| Wired into submission | **yes, behind the switch**: an agent's or a workflow step's `metadata.work_contract` (`SubmissionService._build_task`), and every implementer step of an issue run (`issueruns._attach_plan_contract`) |
| Enforced by the worker | the repository binding and the limits only, through fields it already enforces; everything else waits for contract request 136 and the worker packages |
| Live-verified | **unknown**: never run against a deployment |

The owner's agent quality program (the owner, 3 November 2026) asks SwarmCloud to
complete, harden, connect and measure what it already has, never to build a
second agent platform. This is its work-contract package: a step's brief stops
being only a prompt and becomes a versioned document that says what must be
true when the step is done, where the agent may work, what it may use, and how
that will be proven.

## Why a contract, and why not a new platform

Today a step is a prompt plus signed fields: `runner_profile`, `model_class`,
`budget`, `max_turns`, `timeout_seconds`, `repository_url`, `repository_ref`,
and the `dispatch` block. The prompt carries everything else -- requirements,
the files a lane may touch, what it must not change, the tests it must run --
as prose. Prose cannot be amended with an audit trail, cannot be checked
against evidence, and an agent can talk itself out of a sentence.

A contract keeps all of today's machinery. It **compiles** into the same step
fields the existing validation resolves and the existing signature covers, and
adds one reference inside the already-signed `dispatch` block. Nothing new
decides a limit: the budget, model class and turn cap in a contract are the
step's ask, resolved by `validate_max_turns`, the budget resolver and the model
class check exactly as a hand-written step's are. One source of truth for each.

## The components

| Component | Field | Enforced today by |
|---|---|---|
| Stable requirement ids | `requirements[]`: `id` (`REQ-1`), `text`, `verification` | the evidence check below, after the attempt |
| Repository and commit bindings | `repositories[]`: `repository_url`, `ref`, `commit` | the worker, through the signed `repository_url` and `repository_ref` (a commit wins over a ref) |
| Read and write scope | `scope.read`, `scope.write`: repository-relative globs | nothing yet (advisory) |
| Protected paths and interfaces | `protected.paths`, `protected.interfaces` | nothing yet (advisory) |
| Permitted capabilities | `capabilities.runtime`, `capabilities.plugins` | nothing yet (advisory) |
| Required verification | per requirement, kinds from `VERIFICATION_KINDS` | `unmet`, after the attempt |
| Budget and deadline references | `limits`: `runner_profile`, `model_class`, `budget`, `max_turns`, `timeout_seconds` | the worker, through the signed task fields they compile into |
| Expected artifacts | `artifacts[]`: `pull_request`, `report`, or a `file` with a path | nothing yet (advisory) |
| Ownership and dependencies | `ownership.owner`, `ownership.depends_on` (other contract ids) | recorded |
| Revision and approval lineage | `revision`, `previous_digest`, `ownership.withdrawn`; approvals by digest | `amend` and `approved` |

Every map is **closed**: an unknown key is refused, never ignored, so a field a
later schema adds cannot ride along in this one. Verification and artifacts are
**names**, never commands, and limits name a profile and a class, never an
image or a resource spec (invariant 10).

### Capabilities: runtime names and plugins

The routing package matches runners on capability names:
`dom_automation`, `javascript_execution`, `pixel_rendering`, `screenshots`,
`accessibility_tree`, `persistent_session`, `human_takeover`,
`checkpoint_support`, `write_isolation`.

The owner's agent plugins decisions of 3 November 2026
(docs/design/agent-plugins.md on the `agent-plugins-plan` branch) put a step's
plugins in this section rather than in a contract request of their own: per
step, plugins named from a closed platform catalogue (gstack first), each with
a mode -- `unattended`, `planner` or `designer` -- and the skills it may offer.
`PLUGIN_CATALOGUE` is that plan's first offer, mode by mode; a skill the plan
never offers (`careful`, `guard`, `land-and-deploy` ...) is refused, as is a
skill from another mode's list.

## Canonical form and the signed reference

A contract's bytes are RFC 8785 (JCS) of
`{"purpose": "swarm.work-contract/v1", "contract": <canonical document>}`,
through the frozen `swarm_common.specsign.jcs`: one encoder for everything this
platform signs, and a purpose that can never be read as a step spec's or a
settings block's. Sets (scopes, capabilities, skills, artifacts, dependencies)
are deduplicated and sorted, so key and list order never move the digest;
requirements keep their order, because the order is part of what a person
approved. A test pins schema 1's bytes: a change to the projection or the
encoding is a new schema, never an edit.

The compiler writes `{id, revision, digest, strictness, requirements}` at
`metadata.dispatch.work_contract`. `dispatch` is one of
`SIGNED_METADATA_KEYS`, so the step-spec signature swarm-api makes with Cloud
KMS already covers it, at today's spec format. That gives two tamper checks:

* a rewritten reference (another digest, revision, strictness or requirement
  list, or the reference removed) fails the step-spec signature;
* a rewritten contract body fails `verify_reference`, because its digest no
  longer equals the signed one.

Evidence names the exact revision and source it verifies: an `Evidence`
record carries the contract digest and the 40-hex commit, and counts for
nothing at another commit or another revision.

## Strict and advisory

A **strict** contract is refused (`work_contract_not_enforceable`) when it
sets any component outside the enforced set; it **never** silently compiles as
advisory. Today the enforced set (`WORKER_ENFORCED_TODAY`) is the repository
binding and the limits. As a worker package enforces another component (a
write scope through write isolation, plugins through the catalogue loader),
that component joins the set and strict contracts using it compile.

An **advisory** contract compiles, names every unenforced component in
`CompiledContract.advisory`, and its brief says, in its own line, that those
are "not enforced by the platform". Advisory is a label the agent and the
person both see, never a quiet downgrade.

## Missing verification is not a pass

`unmet(contract, evidence, commit=...)` returns every requirement not yet
proven: a requirement is met only when, for every kind it names, a passing
record binds this contract's digest and that commit. A requirement that names
no kind still needs one passing record. No evidence is never success.

## Amendments

`amend(old, proposed, author_is_agent=..., withdraw=...)`:

* makes a **new immutable revision**, `old.revision + 1`, naming `old`'s
  digest as `previous_digest`; the caller's own revision and parent are
  overwritten, so the lineage cannot fork;
* **invalidates** every earlier approval (approvals bind a digest, and
  `approved` holds only those of the current one) and all earlier evidence
  (evidence binds the digest too); `Amendment.reverify` names the
  requirements to prove again, `added` and `changed` what moved;
* refuses a requirement that is missing and not withdrawn: **no silent drop**.
  A withdrawal names a reason, and the withdrawn id is retired for good;
* refuses, from an **agent**: any widening (`widening`: a write or read
  pattern not covered by an old one, a removed protected entry, a new runtime
  capability, a new plugin, mode or skill, another repository, a higher or
  removed limit, another profile or class, strict to advisory), any rewrite of
  an existing requirement's text or proof, the withdrawal of a requirement
  that is not met at a commit, a change of `ownership.owner`, a dropped
  `ownership.depends_on` entry and a dropped expected artifact. Each of the
  last three drops an unmet obligation as surely as removing a requirement
  would, so a revision can never silently lose one. Adding a dependency or
  an artifact only asks more and is allowed. A person may do each of these;
  the new revision then waits for a person's approval of its digest.

**An empty scope is unrestricted.** A contract with no `scope.write` (or no
`scope.read`) lets the agent write (or read) anywhere the step can. So going
from an empty scope to any pattern is **narrowing**, not widening, and an
agent may do it; going back from a pattern to an empty scope is widening and
is refused from an agent. Between two non-empty scopes the coverage rule
above applies.

The model's judgment is never the authorization: whether an amendment widens
scope is computed from the two revisions, and who authored it comes from the
caller's verified identity, never from what the agent says.

## Refusal codes ship off

The module's refusals (`work_contract_invalid`, `work_contract_not_enforceable`,
`work_contract_mismatch`, `work_contract_amendment_refused`,
`work_contract_scope_widening`) are domain errors, `WorkContractError`, not
`ApiError`s, and a new refusal in swarm-api ships switched off
(`swarm_api.refusals`).

Submission raises them as the established 422 `validation_failed`, with the
contract's own code in `detail.work_contract`, `detail.field` set to
`metadata.work_contract` (the contract's own field, when it names one, is
`detail.contract_field`). That is not a new refusal code shipping on: every
one of them is reachable only while `SWARM_WORK_CONTRACTS` is `on`, which is
the switch, and off by default. Giving each its own `ApiError` code and
`refusals.Switch` needs `swarm_api/refusals.py` and its guard test, which
were outside the wiring lane; its pull request asks for it.

## On submission

With `SWARM_WORK_CONTRACTS` exactly `on`, an agent (`POST /v1/tasks`, a batch
entry) and a workflow step carry a contract as `metadata.work_contract`.
`SubmissionService._build_task` calls `workcontract.prepare` before any other
check, which:

* decodes it (closed at every level) and refuses one whose `tenant_id` is not
  the submitting workspace's (invariant 9);
* compiles it with `compile_contract`, so a strict contract a worker cannot
  enforce is refused here, before anything is stored;
* checks it against the step: a limit the step also names must be the same
  value, and a limit the step leaves unset becomes the step's field, which
  the existing validation then resolves as if the caller had named it, so
  the contract is never a second source of truth (`work_contract_mismatch`
  otherwise). The bound repository must be the one the step runs on; a
  pinned ref must be the step's, except that an advisory contract may pin a
  ref on a step that names none (the pin stays in its text and digest);
* stores the canonical document at `metadata.work_contract`, and
  `workcontract.attach` writes the reference into the dispatch block, which
  the step-spec signature covers.

A whole workflow's `metadata.work_contract` is refused while the switch is
on: copied onto every step, each step would claim the same requirements.

**With the switch off, a contract is ignored**: `prepare` returns None,
nothing is decoded or refused, and `metadata.work_contract` is the caller's
free-form metadata, stored as sent, exactly as before contracts existed. A
spec without a contract is unchanged either way.

## Issue runs

`from_plan_step` turns one step of an approved issue-run plan into an advisory
contract: the plan's requirements become `REQ-1`.. in the order the person
approved, the step's files its write scope, its tests `unit_tests` proof (else
`ci`), and the repository is bound at the commit the run planned against
(the run's `index_sha`, or the repository alone when the plan used no index).
`issueruns._attach_plan_contract` does that for every implementer step of the
chain and of `_compile_staged`, while the switch is on, owned by the plan's
approver; the review, the fix and the requirements check carry none. The
submission path then compiles each one as it does a caller's.

## Compatibility

A spec without a contract behaves exactly as today: `DispatchOptions` without
a contract stores the same dispatch block, signs the same bytes and verifies
under the same key (the compatibility matrix in the tests covers the default,
`direct-pr`, `integrate`, the workflow switches and a labelled block). With
`SWARM_WORK_CONTRACTS` off, `attach` returns the options it was given.

## What the frozen contract would need

Contract request 136 in docs/contract-change-requests.md: the digest in
`swarm_common` so the worker can check a body against the signed reference
with the same code, a signed workspace `"plugins"` settings block, and
`RunnerProfile.runtime_capabilities`. None is needed for what is built here.

## CONTRACT.md invariants

No contract changes demand, capacity reservation, lease counting, fencing,
spot, resource requests and limits, checkpointing or tenant isolation: the
module is pure and writes nothing, and its only stored effects (with the switch
on) are one key inside the signed dispatch block and the canonical document
beside it in the agent's metadata. A contract's limits only fill step fields
the existing validation resolves, so a contract can lower a limit or name a
profile by name but never raise one past what a caller could ask. Invariant 9
is held at submission: a contract of another workspace is refused. Invariant
10 is strengthened: every name in a contract is checked against a closed list.
