// Shapes returned by swarm-api. Field names mirror
// apps/swarm-api/swarm_api/codec.py exactly -- if one changes there, this is
// the copy that has to follow, and there is no check that notices. Keep the
// comment references so the next person can find the source.

/** `pool_to_api` in codec.py. */
export interface Pool {
  name: string
  hard_limit: number
  /** AIMD target. null when nothing has ever lowered it. */
  adaptive_target: number | null
  /** Ceiling derived from provider quota. null when unknown. */
  quota_derived_limit: number | null
  /** min(hard_limit, adaptive_target, quota_derived_limit), floored at 0. */
  effective_limit: number
  /**
   * WEIGHTED UNITS IN USE, NOT AGENTS. Admission increments by the resource
   * class's `units` -- standard 1, browser 2, large 4 -- so `active: 8` may be
   * two large agents or eight standard ones. Never label this "agents".
   */
  active: number
  available: number
  enabled: boolean
  updated_at: string
}

/** `service.capacity()`. */
export interface Capacity {
  pools: Pool[]
  runner_profiles: Record<string, RunnerProfile>
  /** Present since the admission block was added. The caller's own tenant. */
  tenant_id?: string
  /**
   * False when the pool listing hit its page size, so a required pool missing
   * from `pools` may exist and be full rather than being unconfigured. The
   * server has already folded this into each profile's `admission.unread`;
   * it is carried here so a screen can say WHY a figure is unavailable.
   */
  pools_complete?: boolean
  /** `headroom.blocked_reason_groups()`. Served so nothing here restates it. */
  blocked_reason_groups?: Record<string, string[]>
  /** The instant the pool counts below were read. Everything derived from them
   * is a statement about this moment and no other. */
  generated_at: string
}

export interface RunnerProfile {
  resource_class: string
  backend: string
  provider: string | null
  units: number
  pools: string[]
  /**
   * Computed by `swarm_api/headroom.py` from `evaluate_capacity` itself.
   * Optional because an older API does not send it, and an absent block must
   * render as "not measured" rather than as a zero.
   */
  admission?: ProfileAdmission
  /**
   * What this profile's RUNNER refuses to start without (`swarm_api/
   * runnerinputs.py`). Optional for the same reason `admission` is: an API
   * older than the field sends none, and that is "this API did not say",
   * NOT "this profile needs nothing" -- the two have different remedies and
   * `requiredInputKeys` keeps them apart.
   */
  input_contract?: RunnerInputContract
}

/**
 * The keys a runner demands on `input`, each of which must be present and a
 * non-empty string. `claude-code` and `codex` demand `prompt` and raise
 * "requires a non-empty string input.prompt" without it, minutes into an
 * attempt that has already consumed a slot and mounted a credential.
 */
export interface RunnerInputContract {
  required_keys: string[]
}

/**
 * The required keys for a profile, or null when this API did not say.
 *
 * NULL IS NOT AN EMPTY LIST. An empty list is a measured "nothing is
 * required"; null is an unread rule, and a form must say so rather than
 * silently applying no check -- the same distinction `headroomFor` keeps for
 * an unread pool.
 */
export function requiredInputKeys(profile: RunnerProfile | undefined): string[] | null {
  const contract = profile?.input_contract
  if (contract === undefined || !Array.isArray(contract.required_keys)) return null
  return contract.required_keys.filter((k): k is string => typeof k === 'string')
}

/**
 * How a headroom number was arrived at. `swarm_api/headroom.py`.
 *
 * Three cases one integer cannot tell apart, with three different remedies:
 * a measurement, nothing capping this at all, and a pool nobody could read.
 */
export type HeadroomBasis = 'measured' | 'uncapped' | 'unknown'

/** One pool refusing a profile. `evaluate_capacity` + the remedy group. */
export interface ProfileBlocker {
  pool: string
  reason: string
  limit: number
  active: number
  /** 'needs_action' | 'no_room' | null when the reason is in neither. */
  group: string | null
}

/**
 * What ONE relaxed ceiling would have bought, at the instant `generated_at`
 * names. A prediction, and the place a screen like this most easily starts
 * lying: a lease can be released between the read and the render.
 */
export interface Counterfactual {
  pool: string
  /** 'resume' for a paused pool, 'raise' for one at its ceiling. */
  action: 'resume' | 'raise' | string
  headroom_after: number | null
  basis_after: HeadroomBasis
  /** null when either side is unbounded: that is not a delta. */
  delta: number | null
  /** What would bind INSTEAD. Empty means nothing else would. */
  next_binding: string[]
}

/** `analyse_profile` in `swarm_api/headroom.py`. */
export interface ProfileAdmission {
  units: number
  /** null means NOT MEASURED. Read `basis` to learn which kind. */
  headroom: number | null
  basis: HeadroomBasis
  /** EVERY pool refusing a task right now, in the order a task clears them. */
  blockers: ProfileBlocker[]
  /** Every pool capping the next task beyond what fits. */
  binding: string[]
  counterfactual: Counterfactual[]
  /** False means the list below is incomplete and must say so. */
  complete: boolean
  /** Required pools whose state could not be established. */
  unread: string[]
  /** Required pools that are not configured, therefore unlimited. */
  uncapped: string[]
}

/** The kind of pool, parsed from its name. `global` has no prefix. */
export type PoolKind = 'global' | 'tenant' | 'provider' | 'resource' | 'runner' | 'backend'

export function poolKind(name: string): PoolKind {
  if (name === 'global') return 'global'
  const prefix = name.split(':')[0]
  switch (prefix) {
    case 'tenant':
      return 'tenant'
    case 'provider':
      return 'provider'
    case 'resource':
      return 'resource'
    case 'runner':
      return 'runner'
    case 'backend':
      return 'backend'
    default:
      return 'global'
  }
}

/** `provider:anthropic:tenant:u-bogdan` -> `anthropic · u-bogdan`. */
export function poolLabel(name: string): string {
  if (name === 'global') return 'global'
  const parts = name.split(':')
  if (parts.length <= 2) return parts[1] ?? name
  // provider:X:tenant:Y
  return `${parts[1]} · ${parts[3] ?? parts[2]}`
}

/** What `headroomFor` hands a screen. Every field comes off the response. */
export interface Headroom {
  /**
   * How many more agents of this profile could be admitted. NULL MEANS NOT
   * MEASURED and must render as an em dash -- `basis` says which kind of
   * not-measured it is. A 0 here is a measured zero.
   */
  agents: number | null
  basis: HeadroomBasis
  /** The first binding pool, for the columns that have room for one name. */
  binding: string | null
  /** EVERY pool refusing a task right now. The fix for the single-name bug. */
  blockers: ProfileBlocker[]
  counterfactual: Counterfactual[]
  /** False when a required pool could not be read: the list is incomplete. */
  complete: boolean
  /** Required pools nobody could read. Non-empty => `complete` is false. */
  unread: string[]
  /** Required pools that are not configured, therefore unlimited. */
  missing: string[]
}

/**
 * How many more agents of one runner profile could be admitted right now.
 *
 * THIS FUNCTION NO LONGER COMPUTES ANYTHING. It reads `profile.admission`,
 * which `swarm_api/headroom.py` produced by calling `evaluate_capacity` -- the
 * same function the admission transaction calls.
 *
 * It used to re-derive the rule here, and that is where the bug was: the
 * conjunction is "clear EVERY pool at once", so `evaluate_capacity` returns a
 * LIST of everything that refused, and this function kept only the running
 * minimum. `binding = name` overwrote on each new minimum, so with
 * `resource:large` and `provider:anthropic` both at their ceilings the screen
 * named one, an operator raised it, and nothing moved.
 *
 * Serving it instead of restating it was chosen over pinning the restatement
 * with a parity check, for the reason `docs/contract-change-requests.md`
 * already records under "Why these requests keep arising": shell and jq
 * restatements are held to the Python by check-contract-parity.sh, Terraform's
 * by a tftest, and TypeScript by nothing at all -- neither `make lint` nor
 * `make test` runs `tsc`. A parity check can pin a table of constants; it
 * cannot pin an algorithm, and the counterfactual is an algorithm.
 *
 * TRAP D still holds and still has to be rendered: the `pools` list on a
 * runner profile is the CALLING TENANT'S, including for an admin, because
 * service.capacity() calls pool_names_for(tenant_id=ctx.tenant_id)
 * unconditionally. So this answers "how many more could I submit", never "how
 * much capacity does the platform have". Every caller must render the tenant
 * beside the number.
 */
export function headroomFor(profile: RunnerProfile): Headroom {
  const a = profile.admission
  if (!a) {
    // An API that does not send the block. NOT a zero: this screen does not
    // know, and inventing the old client-side sum here would reintroduce the
    // restatement that the block exists to delete.
    return {
      agents: null,
      basis: 'unknown',
      binding: null,
      blockers: [],
      counterfactual: [],
      complete: false,
      unread: [...profile.pools],
      missing: [],
    }
  }
  return {
    agents: a.headroom,
    basis: a.basis,
    binding: a.binding[0] ?? a.blockers[0]?.pool ?? null,
    blockers: a.blockers,
    counterfactual: a.counterfactual,
    complete: a.complete,
    unread: a.unread,
    missing: a.uncapped,
  }
}

/**
 * The two groups, split by REMEDY: somebody must act, versus waiting is a
 * valid answer. Read off the response when the server sent it.
 *
 * The fallback is not a second opinion about the split -- it is the statement
 * that this screen does not know, which renders as an ungrouped list rather
 * than as a confident one. A hard-coded copy here would be exactly the
 * TypeScript restatement `admission` was added to remove.
 */
export function blockerGroup(
  blocker: ProfileBlocker,
  groups: Record<string, string[]> | undefined,
): 'needs_action' | 'no_room' | null {
  if (blocker.group === 'needs_action' || blocker.group === 'no_room') return blocker.group
  if (groups) {
    if (groups.needs_action?.includes(blocker.reason)) return 'needs_action'
    if (groups.no_room?.includes(blocker.reason)) return 'no_room'
  }
  return null
}

/**
 * Whether a pool's number is about the whole platform or only this tenant.
 *
 * Trap E: /v1/stats counts are tenant-scoped while the `global` pool is
 * platform-wide (service.py:276 vs :296-310). Putting "4 agents" from one
 * beside "7 units" from the other is a scope error dressed as a comparison.
 * So every number declares its scope and may only sit beside a number of the
 * same scope.
 */
export function poolScope(name: string): 'platform' | 'tenant' {
  const kind = poolKind(name)
  if (kind === 'tenant') return 'tenant'
  // provider:X:tenant:Y is the per-tenant slice of a provider.
  if (name.includes(':tenant:')) return 'tenant'
  return 'platform'
}

/**
 * The order pools are grouped in, matching `pool_names_for`
 * (models.py:82-107). The grouping teaches the CONJUNCTION -- a task must
 * clear every pool in its list at the same moment -- rather than hiding it.
 */
export const POOL_FAMILY_ORDER: readonly PoolKind[] = [
  'global', 'tenant', 'resource', 'runner', 'backend', 'provider',
]

/**
 * `active` above `effective_limit`. Not merely "full": it means the pool is
 * carrying more than its ceiling allows, which admission cannot produce and
 * which therefore indicates drift -- a limit lowered under running work, or a
 * counter that was never released. `make pool-check` exists to find this.
 */
export function overCeiling(pool: Pool): boolean {
  return pool.active > pool.effective_limit
}

/**
 * Which configured value is BINDING on this pool right now, in words. This is
 * the "set by" column: knowing a pool is capped at 6 is useless without
 * knowing whether that is the operator's number, AIMD backing off, or the
 * provider's quota.
 */
export function setBy(pool: Pool): { term: string; detail: string } {
  const { hard_limit, adaptive_target, quota_derived_limit, effective_limit } = pool
  if (quota_derived_limit !== null && quota_derived_limit === effective_limit && quota_derived_limit < hard_limit) {
    return { term: 'provider quota', detail: `Provider quota caps this at ${quota_derived_limit}; configured is ${hard_limit}.` }
  }
  if (adaptive_target !== null && adaptive_target === effective_limit && adaptive_target < hard_limit) {
    return { term: 'AIMD back-off', detail: `AIMD lowered this to ${adaptive_target} after provider errors; configured is ${hard_limit}.` }
  }
  return { term: 'configured', detail: `The configured hard limit of ${hard_limit} is what applies.` }
}

/**
 * Why a pool's ceiling is lower than its configured limit, or null if it is not.
 * Worth surfacing: a pool sitting at `effective_limit < hard_limit` is being
 * held down by something, and which something is the whole diagnosis.
 */
export function limitedBy(pool: Pool): 'adaptive' | 'quota' | null {
  if (pool.effective_limit >= pool.hard_limit) return null
  const adaptive = pool.adaptive_target
  const quota = pool.quota_derived_limit
  if (quota !== null && quota === pool.effective_limit) return 'quota'
  if (adaptive !== null && adaptive === pool.effective_limit) return 'adaptive'
  return null
}

// --------------------------------------------------------------------------
// Tasks and workflows
// --------------------------------------------------------------------------

/** `task_to_api` in codec.py. Trimmed to what the UI actually renders. */
export interface Task {
  id: string
  tenant_id: string
  state: TaskState
  runner_profile: string
  resource_class: string
  provider: string | null
  priority: number
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  submitted_by: string | null
  attempt_count: number
  max_attempts: number
  park_reason: ParkReason | string | null
  blocked_by: BlockedEntry[] | null
  workflow_id: string | null
  step_id: string | null
  depends_on: string[] | null
  cancel_requested: boolean
  repository_url: string | null

  // The rest of what task_to_api actually sends. This file declared 21 of its
  // 31 keys, and the ten below are exactly the ones the remaining screens are
  // built on -- last_error for the trouble board, result_summary for run
  // output, latest_checkpoint for the attempt timeline, next_eligible_at for
  // parked work.
  model: string | null
  timeout_seconds: number | null
  /** When a PARKED task becomes eligible again. Null unless it is parked. */
  next_eligible_at: string | null
  metadata: Record<string, unknown> | null
  repository_ref: string | null
  input: unknown
  last_error: string | null
  result_summary: Record<string, unknown> | null
  latest_checkpoint: string | null

  /**
   * THE FENCING GENERATION. `models.py:177`, now served by `task_to_api`.
   *
   * It increments on admission (`admission.py:186`) AND on a reconciler fence
   * (`reconciler/store.py:220-227`), so `current_generation > attempt_count`
   * is exactly "a stale worker was fenced out" -- the only cheap signal that
   * CONTRACT invariant 5 has fired. `AttemptRow.generation` is a different and
   * narrower thing: the generation ONE attempt was minted with.
   *
   * NOT OPTIONAL and 0 IS AN ANSWER: the API sends it on every task, and zero
   * means never admitted. Read `> attempt_count` rather than truthiness.
   */
  current_generation: number
  /**
   * The lease holding this task's capacity, or null if it holds none.
   *
   * Null on a QUEUED, PARKED, READY or terminal task -- those cost nothing
   * (invariant 1). It is the join key onto `LeaseRow`, and comparing that
   * row's `generation` against `current_generation` above is how a stalled
   * task is diagnosed: a task at generation 2 whose named lease is still at
   * generation 1 and unreleased is holding a slot for work that will never
   * run. That is the twenty-minute DISPATCHED stall on
   * task_b5dc2568713a40158851, which no screen could explain because neither
   * number reached the browser.
   */
  current_lease_id: string | null

  /**
   * `codec.dispatch_of`, lifted out of `metadata.dispatch` by the API.
   *
   * OPTIONAL ON PURPOSE, and it is the one field in this interface whose
   * absence means something. `task_to_api` always sends it -- filling the
   * API's defaults for a task submitted before the feature existed -- so a
   * missing key here is not an old TASK, it is an older API than this bundle.
   * Those are different and `dispatchOf` below keeps them different: an old
   * task reads `collect`, an old API reads "not reported".
   */
  dispatch?: TaskDispatch | null
}

/**
 * `current_generation` and `current_lease_id` USED TO BE MISSING HERE, and this
 * block used to say so: "task_to_api does not send it, so no screen can show it
 * and no screen should imply it". That was true, and it was the standing reason
 * not to build the fencing indicator -- the same shape as the comment that kept
 * the cost columns off the attempts table by blaming a worker fix that had
 * already shipped. `task_to_api` serves both now and they are typed on `Task`
 * above; a screen that wants the fence glyph has the numbers.
 */

// --------------------------------------------------------------------------
// Dispatch: how a caller's work gets merged
// --------------------------------------------------------------------------
//
// Mirrors `swarm_api/validation.py`. Two fields chosen at submit time; neither
// is an execution parameter, so invariant 10 is untouched -- they say what
// happens to the work AFTER the agent has produced it.
//
// THE CARRIER VOCABULARY IS `checkpoints`, NOT `patches`. Typing the wrong word
// here is not a cosmetic slip: `_accepted_value` refuses an unknown carrier at
// submission, so every submission from this screen would 422. `patches` is a
// real trap because the WORKER uses it -- `lifecycle._dispatch_carrier` accepts
// `("patches", "branches")` and swarm-api accepts `("checkpoints", "branches")`,
// a live disagreement recorded in docs/contract-change-requests.md. This file
// mirrors the API, because the API is what this browser talks to.

/** `validation.DISPATCH_STRATEGIES`, in the order every refusal lists them. */
export const DISPATCH_STRATEGIES = ['collect', 'direct-pr', 'integrate'] as const
export type DispatchStrategy = (typeof DISPATCH_STRATEGIES)[number]

/** `validation.DISPATCH_CARRIERS`. */
export const DISPATCH_CARRIERS = ['checkpoints', 'branches'] as const
export type DispatchCarrier = (typeof DISPATCH_CARRIERS)[number]

/** `validation.DEFAULT_STRATEGY` / `DEFAULT_CARRIER` -- today's behaviour. */
export const DEFAULT_STRATEGY: DispatchStrategy = 'collect'
export const DEFAULT_CARRIER: DispatchCarrier = 'checkpoints'

export type DispatchRole = 'contributor' | 'integrator'

/** The four keys `codec.dispatch_of` returns. */
export interface TaskDispatch {
  strategy: DispatchStrategy
  carrier: DispatchCarrier
  /** Only an `integrate` workflow's steps have one. */
  role: DispatchRole | null
  /** Upstream TASK ids the integrator must apply, in topological order. */
  integrates: string[]
}

function isStrategy(v: unknown): v is DispatchStrategy {
  return typeof v === 'string' && (DISPATCH_STRATEGIES as readonly string[]).includes(v)
}

function isCarrier(v: unknown): v is DispatchCarrier {
  return typeof v === 'string' && (DISPATCH_CARRIERS as readonly string[]).includes(v)
}

/**
 * What this task CHOSE, or null when this API did not say.
 *
 * Null is the whole reason this is a function rather than a field read. Three
 * situations arrive as "no usable `dispatch` object" and only one of them is
 * an answer:
 *
 *  - the API sent the block: that is the answer, however old the task is.
 *    `dispatch_of` already substituted `collect`/`checkpoints` for a task that
 *    predates the feature, and that substitution is CORRECT -- an absent block
 *    means the worker will harvest and push nothing, which is what collect is.
 *  - the API sent no `dispatch` key at all: a deployment older than the field.
 *    Rendering `collect` here would invent a caller's choice out of a version
 *    skew, and "nothing was pushed because you asked for collect" is a
 *    different sentence from "nothing was pushed and we cannot say why".
 *  - the API sent a value neither vocabulary knows: same answer, same reason.
 *
 * So the second and third return null and every caller has to say so.
 */
export function dispatchOf(task: Task): TaskDispatch | null {
  const d = task.dispatch
  if (typeof d !== 'object' || d === null || Array.isArray(d)) return null
  if (!isStrategy(d.strategy) || !isCarrier(d.carrier)) return null
  const role = d.role === 'contributor' || d.role === 'integrator' ? d.role : null
  return {
    strategy: d.strategy,
    carrier: d.carrier,
    role,
    integrates: Array.isArray(d.integrates) ? d.integrates.filter((t) => typeof t === 'string') : [],
  }
}

/**
 * `DispatchOptions.needs_repository`, restated -- and deliberately only ever
 * used to WARN.
 *
 * validation.py is the decider and names its own refusal; a copy here that
 * disabled the submit button would, on drifting, block a submission the API
 * would have accepted. Warning fails the other way: the worst a stale copy can
 * do is show a caution that turns out to be wrong.
 */
export function needsRepository(strategy: DispatchStrategy, carrier: DispatchCarrier): boolean {
  return strategy === 'direct-pr' || strategy === 'integrate' || carrier === 'branches'
}

/**
 * WHAT PICKING THIS ACTUALLY PRODUCES, in pull requests, for `steps` steps.
 *
 * This is the reason the control exists. `integrate` on a six-step workflow is
 * ONE pull request and `direct-pr` on the same six steps is SIX, and a caller
 * who cannot see that at the moment of choosing is picking between three words.
 *
 * `pullRequests` is a CEILING, not a promise, and `atMost` says which. A step
 * whose agent changed nothing publishes nothing -- `_harvest_git` reports
 * "changed nothing" and the run ends -- so `direct-pr` over six steps opens
 * between zero and six. `integrate` is exactly one or none, because there is
 * only ever one step that opens one.
 */
export interface DispatchConsequence {
  pullRequests: number
  atMost: boolean
  pushes: boolean
  /** The headline, with the real step count already in it. */
  headline: string
  /** What happens to the work itself. */
  detail: string
}

export function consequenceOf(
  strategy: DispatchStrategy,
  steps: number,
): DispatchConsequence {
  const n = Math.max(1, steps)
  const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`
  switch (strategy) {
    case 'collect':
      return {
        pullRequests: 0,
        atMost: false,
        pushes: false,
        headline: 'No pull request. Nothing is pushed.',
        detail:
          `Each of the ${plural(n, 'step')} harvests its patch into the task's own GCS ` +
          'prefix and the run ends there. Nothing reaches the repository, so this is ' +
          'the only strategy that works with a read-only token.',
      }
    case 'direct-pr':
      return {
        pullRequests: n,
        atMost: true,
        pushes: true,
        headline: `Up to ${plural(n, 'pull request')} — one per step.`,
        detail:
          `Every step pushes its own branch and opens its own pull request, so ${plural(n, 'step')} ` +
          `means ${plural(n, 'review')} to do and ${plural(n, 'branch')} to merge. A step whose agent ` +
          'changed nothing opens none, which is why this is a ceiling and not a count.',
      }
    case 'integrate':
      return {
        pullRequests: 1,
        atMost: true,
        pushes: true,
        headline: 'Exactly one pull request for the whole workflow.',
        detail:
          n < 2
            ? 'One final step receives the others’ work and opens the single pull ' +
              'request. There are no other steps here yet, so there is nothing to integrate.'
            : `The final step merges the other ${plural(n - 1, 'step')}’ branches into its own ` +
              'and opens one pull request against the repository. Those steps push a branch each ' +
              'and open nothing.',
      }
  }
}

/** Label and one-line gloss for each strategy, for a picker. */
export const STRATEGY_LABEL: Readonly<Record<DispatchStrategy, string>> = {
  collect: 'Collect',
  'direct-pr': 'A PR per step',
  integrate: 'One PR for all steps',
}

/** Label and gloss for each carrier. See the honesty note in `CARRIER_NOTE`. */
export const CARRIER_LABEL: Readonly<Record<DispatchCarrier, string>> = {
  checkpoints: 'Checkpoints',
  branches: 'Branches',
}

/**
 * THE SENTENCE THAT KEEPS THE CARRIER CONTROL HONEST.
 *
 * `carrier` is validated at submission, stored on the task, and read back by
 * the API -- and then nothing does anything with it. `_dispatch_carrier` in
 * agent-worker has no caller in production code; its only references are its
 * own definition and one test. So the only effect `branches` has TODAY is that
 * `needs_repository` becomes true and the submission is refused without a
 * repository URL. Saying "work travels between steps as pushed branches" would
 * describe a mechanism that is not wired up, which is precisely the kind of
 * claim this UI exists to not make.
 */
export const CARRIER_NOTE =
  'Recorded on the task and returned by the API, but no worker code reads it yet. ' +
  'Choosing branches changes one thing today: it makes a repository URL required.'

export const CARRIER_DETAIL: Readonly<Record<DispatchCarrier, string>> = {
  checkpoints:
    'The default. Intended to carry a step’s work to the next one as the checkpoint ' +
    'tarballs the worker already writes every few minutes.',
  branches:
    'Intended to carry a step’s work to the next one as a pushed branch, which outlives ' +
    'the platform. Requires a repository URL.',
}

/** `_event_to_api`, routes/tasks.py. */
export interface TaskEvent {
  event_id: string
  task_id: string
  type: string
  at: string
  attempt_id: string | null
  lease_id: string | null
  generation: number | null
  detail: Record<string, unknown> | null
}

/**
 * What the worker puts in `task.result_summary` (lifecycle.py:946-960, written
 * through control.py:663-664).
 *
 * WHY THIS AND NOT `GET /v1/tasks/{id}/artifacts`: that route reads a
 * Firestore `artifacts` subcollection (store.py:513) that NOTHING IN THIS
 * REPOSITORY WRITES. `ARTIFACTS = "artifacts"` is declared once in swarm-api's
 * store and read once, and grep across apps/ finds no writer. The endpoint
 * therefore returns an empty list for every task forever. A panel built on it
 * would render "no artifacts" for a run that produced twenty.
 *
 * The artifacts are real -- they are uploaded to the tenant's own GCS prefix
 * and recorded here, by reference. Nothing is inlined and no download URL is
 * minted: the reader uses their own credentials against GCS, which keeps the
 * tenant boundary in one place.
 */
export interface ResultSummary {
  artifacts?: ArtifactRef[]
  artifact_bytes?: number
  /** `{stdout: "gs://...", stderr: "gs://..."}`. A stream that failed to upload is absent. */
  logs?: Record<string, string>
  /** Present only when something was dropped for exceeding max_artifact_bytes. */
  artifacts_skipped?: string[]
  checkpoint?: { checkpoint_id?: string; [k: string]: unknown }
  /**
   * What the agent did to the repository, and what happened to it.
   * `lifecycle.py::_harvest_git`. Absent when the task cloned nothing.
   *
   * THE FIELD THAT MATTERS MOST HERE IS `publish_reason`, and it is present on
   * success as well as failure. "No pull request" has at least six distinct
   * causes -- the token cannot push, the attempt parked, the agent changed
   * nothing, the forge is unknown, the push was rejected, the PR call failed --
   * and they need completely different responses. A panel that rendered a
   * missing PR as a single "none" would be the same failure the rest of this
   * UI is built to avoid.
   */
  git?: GitSummary
  /** Token and cost numbers live at `runner.usage`, untyped. See the note below. */
  runner?: { usage?: Record<string, number | string[]>; [k: string]: unknown }
  [k: string]: unknown
}

export interface GitCommit {
  sha: string
  subject: string
  author: string
  committed_at: string
  files_changed: number
  insertions: number
  deletions: number
  /** git prints `-` for both counts on a binary change; those are counted here
   *  instead of folded into a 0/0 line count that would read as "changed
   *  nothing". */
  binary_files: number
}

export interface GitSummary {
  /** The commit the clone landed on. Null when a resumed attempt lost its marker. */
  base?: string | null
  head?: string | null
  commits?: GitCommit[]
  commit_count?: number
  insertions?: number
  deletions?: number
  /** Changed-but-uncommitted paths. The COMMON case: most agents never commit. */
  dirty?: string[]
  dirty_count?: number
  dirty_truncated?: boolean
  /** Artifact name of the patch, matched against `artifacts[]` to get its URI. */
  patch?: string | null
  patch_bytes?: number
  /** True when a patch was produced and DISCARDED for exceeding the cap. A
   *  truncated patch applies cleanly and silently drops the rest of the
   *  change, so none is written. Not the same as "no patch". */
  patch_omitted?: boolean
  patch_note?: string
  note?: string
  error?: string
  repository?: string
  default_branch?: string | null
  can_push?: boolean
  branch?: string
  pushed_head?: string
  /** True when the WORKER made one of the commits, because the agent left work
   *  uncommitted and it would otherwise never have reached the branch. */
  auto_committed?: boolean
  published?: boolean
  publish_reason?: string
  pull_request?: { number: number; url: string; state: string; created: boolean }

  /**
   * WHAT THE RUN ITSELF RECORDED ABOUT ITS DISPATCH, written by `_publish` in
   * agent_worker/lifecycle.py. Both are partial by design, so neither may be
   * read as "the dispatch":
   *
   *  - `strategy` is written on the `collect` early return ONLY. Every other
   *    strategy gets past that branch and the field is never set.
   *  - `role` is written once the strategy is known to be `integrate`, and is
   *    null on the other two.
   *
   * `task.dispatch` is the complete record and `dispatchOf` reads it. These are
   * preferred over it where present for one reason: they say what the attempt
   * ACTUALLY DID, which is the subject of the panel that renders them.
   */
  strategy?: string | null
  role?: string | null

  /** `merge_branches`' outcome, written on an integrator only. */
  integrated?: {
    merged?: string[]
    conflicted?: string[]
    missing?: string[]
    complete?: boolean
  }
}

export interface ArtifactRef {
  name: string
  bytes: number
  uri: string
}

/**
 * Reads `result_summary.runner.usage` if it is there.
 *
 * Three things must stay true of anything rendered from this:
 *  - only claude-code and codex produce it; mock, generic and browser report
 *    nothing, and NOTHING IS NOT ZERO. Render an em dash, never $0.00.
 *  - result_summary is written only by finish(), so a RUNNING agent has no
 *    figure and a PARKED one never will for the attempt it lost -- that path
 *    puts the summary in the event detail and in blocked_by instead.
 *  - it is an untyped dict, so nothing can query or index it. A "top spenders"
 *    figure is a client-side sum over one page, not an aggregate.
 */
export function usageOf(task: Task): Record<string, number | string[]> | null {
  const runner = (task.result_summary as ResultSummary | null)?.runner
  const usage = runner?.usage
  return usage && typeof usage === 'object' ? usage : null
}

/** `GET /v1/tenants/me`, routes/tenants.py:24-38. */
export interface Me {
  tenant: Tenant
  principal: {
    email: string
    domain: string
    groups: string[]
    is_admin: boolean
  }
}

/**
 * A row-bounded window of tasks, plus what span those rows turned out to
 * cover.
 *
 * THE ONE DESIGN RULE for activity: bound by ROWS, label by the SPAN those
 * rows actually covered. The platform can serve "the most recent N tasks for
 * this tenant" cheaply and exactly; it cannot serve "everything in August" at
 * all -- list_tasks applies exactly one inequality, decoded from the page
 * token. So the control says "Last 500 tasks" and the header reports
 * "14 Sep 09:12 -> 19 Sep 08:44 (4d 23h)". If the tenant is busy that span is
 * six hours; if quiet, three months. Both are true. "Last 7 days" would be a
 * lie the moment the window truncates.
 */
export interface TaskWindow {
  tasks: Task[]
  /** True when a next_page_token remained, so older tasks exist beyond this. */
  moreExist: boolean
  /** How many pages were actually fetched, for the provenance line. */
  pages: number
  /** Oldest and newest created_at in the window, or null when empty. */
  from: string | null
  to: string | null
}

export type Bucket = 'hour' | 'day' | 'week' | 'month'

/**
 * Floor a timestamp to a bucket boundary IN THE VIEWER'S LOCAL ZONE.
 *
 * A day boundary silently in UTC moves seven hours of a Pacific engineer's
 * work into the wrong day, which is the kind of wrong that never gets
 * reported because it looks plausible.
 */
export function bucketStart(iso: string, bucket: Bucket): number | null {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return null
  const d = new Date(t)
  d.setMinutes(0, 0, 0)
  if (bucket === 'hour') return d.getTime()
  d.setHours(0)
  if (bucket === 'day') return d.getTime()
  if (bucket === 'week') {
    // ISO-8601: weeks start Monday.
    const dow = (d.getDay() + 6) % 7
    d.setDate(d.getDate() - dow)
    return d.getTime()
  }
  d.setDate(1)
  return d.getTime()
}

/**
 * Usage is only meaningful when PRESENT AND NON-EMPTY. `?? 0` here would
 * render "this engineer spent nothing", which is worse than omitting the
 * number -- a zero reads as a fact.
 */
export function hasUsage(task: Task): boolean {
  const u = usageOf(task)
  return u !== null && Object.keys(u).length > 0
}

/**
 * `lease_to_api`. One row of "who is holding capacity right now".
 *
 * NOTE `dispatch_state`, not `state`. Nothing ever writes STARTING or RUNNING
 * to a lease -- the worker advances the TASK through those and touches the
 * lease only to heartbeat -- so a column labelled "state" would show
 * DISPATCHED for an agent that has been running for an hour.
 */
export interface LeaseRow {
  lease_id: string
  task_id: string
  attempt_id: string
  tenant_id: string
  generation: number
  pools: string[]
  units: number
  dispatch_state: 'LEASED' | 'DISPATCHED' | string
  created_at: string
  dispatch_deadline: string
  expires_at: string
  heartbeat_at: string | null
  released_at: string | null
  release_reason: string | null
  released: boolean
  expired: boolean
  dispatch_overdue: boolean
  /** Computed server-side. Falls back to created_at when never beaten. */
  silent_seconds: number
  heartbeat_ever: boolean
  /** Denormalised from the task, so this is not an N+1. */
  last_error: string | null
}

/**
 * THE THRESHOLDS ARRIVE WITH THE DATA and the UI must colour from them.
 *
 * 90 and 120 are the reconciler's, and the grace is not even a constant --
 * it derives from the heartbeat interval. A `const GRACE = 90` here would be
 * the restatement drift check-contract-parity.sh exists to catch, one layer
 * further out, and it would colour a row amber at a threshold the reconciler
 * does not act on.
 */
export interface LeasePage {
  leases: LeaseRow[]
  thresholds: { heartbeat_grace_seconds: number; lease_timeout_seconds: number }
  evaluated_at: string
  active_only: boolean
  tenant_id: string | null
  /** Weighted units, not agents. */
  units_held: number
}

/** How a lease row reads, given the thresholds the API just sent. */
export type Liveliness = 'alive' | 'silent' | 'presumed-dead'

export function leaseLiveliness(
  row: LeaseRow,
  thresholds: LeasePage['thresholds'],
): { kind: Liveliness; copy: string } {
  // Past expires_at is a SECOND, INDEPENDENT signal, not a later stage of the
  // first: the lease TTL has run out as well as the heartbeat going quiet.
  if (row.expired) {
    return {
      kind: 'presumed-dead',
      copy: 'The lease TTL has run out as well. This slot is held by something that is almost certainly gone.',
    }
  }
  if (row.silent_seconds >= thresholds.heartbeat_grace_seconds) {
    return {
      kind: 'silent',
      copy: 'This is already the reconciler\u2019s trigger. Its next pass will reclaim this lease, and that pass is up to five minutes away.',
    }
  }
  return { kind: 'alive', copy: 'Beating normally. Nothing will touch this.' }
}

/** `attempt_to_api`. The per-attempt record result_summary cannot give you. */
export interface AttemptRow {
  attempt_id: string
  task_id: string
  tenant_id: string
  generation: number
  lease_id: string
  backend: string
  execution_name: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  exit_code: number | null
  error: string | null
  peak_rss_bytes: number | null
  peak_disk_bytes: number | null
  oom_near_miss: boolean
  checkpoints: string[]
  input_tokens: number | null
  output_tokens: number | null
  cache_read_input_tokens: number | null
  cache_creation_input_tokens: number | null
  cost_usd: number | null
}

// --------------------------------------------------------------------------
// Requested vs utilised
// --------------------------------------------------------------------------

/**
 * One entry of `GET /v1/resource-classes`, which serves `RESOURCE_CLASSES`
 * from the frozen catalogue.
 *
 * THE POINT OF THE ROUTE. An attempt has always recorded what it USED
 * (`peak_rss_bytes`, `peak_disk_bytes`) and nothing served what it was GIVEN,
 * so "peak RSS 6.1 GiB" was a figure with no scale -- comfortable on `large`,
 * one prompt from an OOM kill on `standard`, and no way to tell which.
 *
 * The numbers are NOT hand-copied into this file on purpose. A served value
 * cannot drift at all, which is strictly better than a copy something watches:
 * an asserted copy still has to be edited in two places when a class is resized.
 * See the same reasoning in Holders.tsx's ClassMix.
 *
 * (`check-contract-parity.sh` now has a TypeScript section and does assert the
 * copies this file does keep -- RESOURCE_UNITS, the state sets, the reason and
 * provider unions, the pool families. That is the second-best answer, used where
 * a route is not available. It is not a reason to start copying.)
 */
export interface ResourceClassSpec {
  name: string
  /** BOTH the request and the limit. `requests == limits` platform-wide. */
  cpu: number
  memory_gib: number
  /** A SLICE OF memory_gib, not capacity on top of it: the workspace is a tmpfs. */
  disk_gib: number
  /** Weighted capacity units admission counts. Never a number of agents. */
  units: number
}

/**
 * One entry of `GET /v1/runtimes` -- `routes/platform.py:90-181`.
 *
 * WHAT A RUNNER PROFILE NAME MEANS, served rather than copied. Invariant 10
 * makes the name a caller's ENTIRE vocabulary: they send `runner_profile` and
 * nothing else, so `claude-code` on its own does not say where it runs, how big
 * it is, how long it may run, or which credential it spends. This is the route
 * that answers that, and it is the remedy `docs/contract-change-requests.md`
 * ends on -- "where one reader is TypeScript, nothing ends it today, and a
 * route that serves the value is the only remedy this repository has actually
 * made work". Nothing below is a value; every field is a hole the API fills.
 */
export interface Runtime {
  /** The exact string to send as `runner_profile`. */
  name: string
  image: string
  /**
   * DECLARED. `AUTO` is a value the frozen `Backend` enum permits, and a caller
   * shown only "AUTO" learns nothing about where the task runs.
   */
  backend: string
  /**
   * What `resolve_backend` turned `backend` into -- never `AUTO`. Shown
   * alongside `backend` rather than instead of it, because a caller shown only
   * this one cannot tell that the PLATFORM, not the profile, chose it.
   */
  resolved_backend: string
  /**
   * null is not "unknown": the runtime consumes no external provider's quota,
   * so it runs for a tenant who has registered no credential at all.
   */
  provider: string | null
  /** Environment variable NAMES. The route reads no environment and no secret store. */
  secrets: string[]
  /**
   * true  -> the names in `secrets` are INTERCHANGEABLE; supply exactly one.
   * false -> all of them are required.
   * The list without this flag is actively misleading -- it would tell a tenant
   * who pays for a Claude subscription to buy metered API access as well.
   */
  secrets_any_of: boolean
  timeout_seconds: number
  resource_class: string
  /**
   * The class resolved inline. Field-identical to a `/v1/resource-classes`
   * entry, and `test_sizing_agrees_with_the_resource_classes_route` asserts the
   * two routes never disagree -- so the same type is reused rather than a
   * second one declared that could drift from it.
   */
  resources: ResourceClassSpec
}

/** GiB, binary. The ceilings are GiB, so a GB (1e9) denominator would overstate
 *  every utilisation figure by 7.4% -- against a ceiling with no burst headroom
 *  that is the difference between "fine" and "near miss". */
export const GIB = 1024 ** 3

/**
 * Bytes as a figure a person reads, or an em dash when there is no measurement.
 * NEVER "0 B" for an absent one: the whole point of this UI is that nothing
 * measured and nothing used are different claims.
 */
export function bytesLabel(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '—'
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(2)} GiB`
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KiB`
  return `${bytes} B`
}

// --------------------------------------------------------------------------
// Checkpoints
// --------------------------------------------------------------------------

/**
 * One checkpoint of one attempt, assembled from TWO records because neither is
 * complete on its own.
 *
 *  - `attempt.checkpoints` is the authoritative LIST, and it is `list[str]`:
 *    ids and nothing else (models.py:215, written by
 *    `control.record_checkpoint`).
 *  - the `checkpoint_completed` EVENT for that id carries `{checkpoint_id,
 *    uri, size_bytes, seq}` -- the only place the size and the location exist
 *    per checkpoint.
 *
 * So a row with an id and no uri is not a broken checkpoint. It means the
 * event that described it is not on the page of events we were handed, which
 * happens for real: the events route orders OLDEST first, caps the page and
 * returns no page token, and a worker heartbeats throughout, so the later
 * checkpoints of a long attempt are exactly the ones whose events fall off
 * the end. `uriKnown` is what lets the screen say that instead of drawing a
 * blank cell.
 *
 * CONTENTS ARE NOT RECORDED ANYWHERE. Only the id, the size and the uri. A
 * file listing would need a new route and a manifest read out of GCS.
 */
export interface CheckpointRow {
  checkpoint_id: string
  /** null when no `checkpoint_completed` event for this id is on this page. */
  bytes: number | null
  uri: string | null
  seq: number | null
  /** When the event was written, if we have the event. */
  at: string | null
  /**
   * True when only an event names this id and the attempt document does not.
   * Worth keeping separate: the document is written by a read-modify-write, so
   * an id present in one record and not the other is a real inconsistency and
   * not a paging artefact.
   */
  eventOnly: boolean
}

function detailString(e: TaskEvent, key: string): string | null {
  const v = e.detail?.[key]
  return typeof v === 'string' && v !== '' ? v : null
}

function detailNumber(e: TaskEvent, key: string): number | null {
  const v = e.detail?.[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * Every checkpoint of one attempt, ids from the attempt document enriched with
 * whatever the event page could supply.
 *
 * Events whose `attempt_id` is null are SKIPPED rather than attributed here.
 * The worker sets `attempt_id` on every event it emits (control.emit), so a
 * null one did not come from this attempt's worker, and guessing would put
 * another attempt's checkpoint under this one's heading.
 */
export function checkpointsFor(
  attempt: AttemptRow,
  events: TaskEvent[] | null,
): CheckpointRow[] {
  const fromEvents = new Map<string, { bytes: number | null; uri: string | null; seq: number | null; at: string }>()
  for (const e of events ?? []) {
    if (e.type !== 'checkpoint_completed') continue
    if (e.attempt_id !== attempt.attempt_id) continue
    const id = detailString(e, 'checkpoint_id')
    if (id === null) continue
    fromEvents.set(id, {
      bytes: detailNumber(e, 'size_bytes'),
      uri: detailString(e, 'uri'),
      seq: detailNumber(e, 'seq'),
      at: e.at,
    })
  }

  const rows: CheckpointRow[] = attempt.checkpoints.map((id) => {
    const found = fromEvents.get(id)
    fromEvents.delete(id)
    return {
      checkpoint_id: id,
      bytes: found?.bytes ?? null,
      uri: found?.uri ?? null,
      seq: found?.seq ?? null,
      at: found?.at ?? null,
      eventOnly: false,
    }
  })
  // Whatever is left was described by an event the attempt document does not
  // list. Dropping it would hide a checkpoint that demonstrably completed.
  for (const [id, found] of fromEvents) {
    rows.push({ checkpoint_id: id, ...found, eventOnly: true })
  }
  return rows
}

/** The checkpoint this attempt RESUMED from, which a previous attempt wrote. */
export interface RestoredFrom {
  checkpoint_id: string
  /** The attempt that wrote it. Null when the event did not record it. */
  from_attempt: string | null
  bytes: number | null
  /** How many files came back out of the archive. */
  files: number | null
}

export function restoredFrom(attempt: AttemptRow, events: TaskEvent[] | null): RestoredFrom | null {
  for (const e of events ?? []) {
    if (e.type !== 'checkpoint_restored') continue
    if (e.attempt_id !== attempt.attempt_id) continue
    const id = detailString(e, 'checkpoint_id')
    if (id === null) continue
    return {
      checkpoint_id: id,
      from_attempt: detailString(e, 'from_attempt'),
      bytes: detailNumber(e, 'bytes'),
      files: detailNumber(e, 'files'),
    }
  }
  return null
}

// --------------------------------------------------------------------------
// Live readings from the heartbeat
// --------------------------------------------------------------------------

/**
 * The newest `heartbeat` event belonging to one attempt.
 *
 * WHY THIS EXISTS. `attempt.peak_rss_bytes` is written by
 * `control.record_resource_usage` at the END of an attempt, so a RUNNING agent
 * has none and its resource panel would be entirely em dashes -- for exactly
 * the agent someone is watching because they are worried about it. The worker
 * puts `{elapsed_seconds, peak_rss_bytes, checkpoints}` in every fifth
 * heartbeat event (lifecycle._heartbeat), which is a real measurement of a
 * live process.
 *
 * It is NOT the same claim as the final figure and must never be rendered as
 * one: it is the high-water mark AS OF that event, and the event page is
 * oldest-first with no page token, so on a long attempt the newest heartbeat
 * available here can be old. Every caller therefore renders `at` beside it.
 */
export interface HeartbeatReading {
  at: string
  peakRssBytes: number | null
  elapsedSeconds: number | null
  checkpoints: number | null
}

export function newestHeartbeat(
  attempt: AttemptRow,
  events: TaskEvent[] | null,
): HeartbeatReading | null {
  let best: HeartbeatReading | null = null
  let bestAt = -Infinity
  for (const e of events ?? []) {
    if (e.type !== 'heartbeat') continue
    if (e.attempt_id !== attempt.attempt_id) continue
    const t = new Date(e.at).getTime()
    if (!Number.isFinite(t) || t <= bestAt) continue
    bestAt = t
    best = {
      at: e.at,
      peakRssBytes: detailNumber(e, 'peak_rss_bytes'),
      elapsedSeconds: detailNumber(e, 'elapsed_seconds'),
      checkpoints: detailNumber(e, 'checkpoints'),
    }
  }
  return best
}

/**
 * The outcome chip for one attempt.
 *
 * Exit 0 is the only success. A null exit code is THREE different things
 * depending on what else the document says -- never a zero, and never a
 * failure. `AttemptTimeline.tsx` carries a private copy of this; it should
 * switch to this one, which is why this lives here rather than in the screen.
 */
export function attemptOutcome(a: AttemptRow): { label: string; tone: Tone | 'unknown' } {
  if (a.exit_code === 0) return { label: 'exit 0', tone: 'ok' }
  if (a.exit_code !== null) return { label: `exit ${a.exit_code}`, tone: 'bad' }
  if (a.started_at === null) return { label: 'never started', tone: 'unknown' }
  if (a.completed_at === null) return { label: 'running', tone: 'live' }
  return { label: 'ended, no exit code', tone: 'wait' }
}

/** How long one attempt ran, in words. Not a number: three of the four cases
 *  are not durations at all and a "0s" for any of them would be a lie. */
export function attemptRan(a: AttemptRow, now: number): string {
  if (a.started_at === null) return 'never started'
  const started = new Date(a.started_at).getTime()
  if (!Number.isFinite(started)) return 'start time unreadable'
  if (a.completed_at === null) return `${duration(now - started)} so far`
  const done = new Date(a.completed_at).getTime()
  if (!Number.isFinite(done)) return 'finish time unreadable'
  return duration(done - started)
}

/** `SubmissionService.stats`, service.py:271-288. */
export interface Stats {
  tenant_id: string
  /** One key per TaskState -- count_tasks_by_state iterates the whole enum. */
  tasks_by_state: Record<string, number>
  dispatch_paused: boolean
  limits: Record<string, number>
  generated_at: string
  /** ADMIN ONLY. Absent for everyone else -- absent is not zero. */
  platform_tasks_by_state?: Record<string, number>
}

/** `GET /v1/admin/dispatch`, routes/admin.py:70-75. Admin-gated. */
export interface DispatchControl {
  dispatch_paused: boolean
  updated_at: string | null
  updated_by: string | null
  reason: string | null
}

/** `SubmissionService.providers`, service.py:334-350. */
export interface ProviderEntry {
  provider: string
  /** Provider NAMES only, from tenants/{id}.credentials. Never key material. */
  credential_registered: boolean
  runner_profiles: string[]
  /** null when no quota document exists for this tenant yet. Not zeros. */
  quota: QuotaState | null
}

export interface ProvidersPage {
  tenant_id: string
  providers: ProviderEntry[]
  generated_at: string
}

/**
 * `ProviderState`, models.py:259-265. SIX values, not five.
 *
 * THROTTLED is the one most easily missed and it is the common case during a
 * squeeze, so a chip that falls through to "unknown" for it mislabels exactly
 * the condition the panel exists for.
 *
 * UNKNOWN means "no worker has reported on this provider recently". That is
 * not the same as healthy and gets its own grey.
 */
export type ProviderStateName =
  | 'AVAILABLE' | 'THROTTLED' | 'EXHAUSTED' | 'COOLDOWN' | 'DISABLED' | 'UNKNOWN'

export function providerTone(state: string): Tone | 'unknown' {
  switch (state) {
    case 'AVAILABLE': return 'ok'
    case 'THROTTLED': return 'wait'
    case 'COOLDOWN': return 'wait'
    case 'EXHAUSTED': return 'bad'
    case 'DISABLED': return 'bad'
    default: return 'unknown'
  }
}

/**
 * The two park reasons that need a person. Everything else is the platform
 * working as designed (CONTRACT.md invariant 1), which is why this panel is
 * grey and never red: parked work costs nothing.
 */
export const NEEDS_A_HUMAN: ReadonlySet<string> = new Set([
  'CREDENTIAL_MISSING', 'BUDGET_EXHAUSTED',
])

/**
 * Reasons that are NOT BlockedReason members. `record_blockers` stores
 * whatever AdmissionDenied carried, and three of those are bare strings from
 * the pre-flight checks (admission.py:148,155,157). They mean something quite
 * different from a capacity refusal, so they get their own group rather than
 * being swallowed by an enum lookup.
 */
export const PRE_CAPACITY_REASONS: ReadonlySet<string> = new Set([
  'task_missing', 'not_ready', 'cancel_requested',
])

/** `tenant_to_api`, codec.py:283. Thirteen fields, all of them. */
export interface Tenant {
  tenant_id: string
  kind: 'group' | 'user' | string
  principal: string
  display_name: string | null
  created_at: string
  max_active: number
  capacity_units: number
  monthly_budget_usd: number | null
  enabled: boolean
  /** Providers this tenant has registered a key for. */
  credentials: string[]
  /**
   * null means NO IDENTITY, not an empty string. Render it as such -- a blank
   * cell here reads as "fine" and it is the opposite.
   */
  service_account: string | null
  gcs_prefix: string | null
  namespace: string | null
}

/**
 * `quota_to_api` = asdict(QuotaState) + the state enum value + effective_limit.
 * Every nullable field here is genuinely unknown rather than zero, which is
 * why they are typed `| null` and must render as an em dash.
 *
 * `state` reuses `ProviderStateName` rather than spelling the members again.
 * The second spelling had drifted: it listed `'HEALTHY'`, which is not a member
 * of `ProviderState` and is written nowhere in this platform, and it omitted
 * `AVAILABLE`, `THROTTLED` and `UNKNOWN`, which are three of the six that are.
 * `| string` made the mistake invisible -- the union widens to `string`, so
 * nothing ever failed to compile -- and it is kept, deliberately, for the same
 * reason `park_reason` keeps it: a value the server adds must still decode and
 * render as itself rather than crash the row. `check-contract-parity.sh` now
 * asserts `ProviderStateName` against the frozen enum, so there is one
 * restatement left and something watching it.
 */
export interface QuotaState {
  provider: string
  tenant_id: string
  state: ProviderStateName | string
  updated_at: string
  configured_hard_max: number
  adaptive_target: number | null
  quota_derived_limit: number | null
  requests_remaining: number | null
  tokens_remaining: number | null
  reset_at: string | null
  cooldown_until: string | null
  last_429_at: string | null
  retry_after_seconds: number | null
  success_count: number
  rate_limit_count: number
  effective_limit: number
}

/**
 * `swarm_common.states`. Twelve states, and the groupings below are the ones
 * that carry meaning rather than being pretty categories.
 */
export type TaskState =
  | 'SUBMITTED' | 'QUEUED' | 'PARKED' | 'READY' | 'LEASED' | 'DISPATCHED'
  | 'STARTING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'DEAD_LETTERED'

/**
 * Three of the twelve are never written to a task document. `service.py:122`
 * walks the real state machine but stores only the end state, creating tasks
 * directly at READY or PARKED; QUEUED is the creation state of a WORKFLOW, not
 * a task; and `finish()` is only ever called with SUCCEEDED / FAILED /
 * CANCELLED, so DEAD_LETTERED is an enum member, two membership tests and an
 * unreachable branch.
 *
 * So these must never appear in a filter, a legend or a histogram. A bucket
 * that cannot fill reads as "nothing is broken" rather than "this cannot
 * happen", which is the wrong lesson. DEAD_LETTERED must still be DECODED --
 * the scheduler lists it in _FAILED_PARENT_STATES -- but decoding a string is
 * not the same as offering it as a choice.
 */
export const NEVER_WRITTEN: ReadonlySet<TaskState> = new Set<TaskState>([
  'SUBMITTED', 'QUEUED', 'DEAD_LETTERED',
])

/** The nine a task document can actually hold. Filters and legends use this. */
export const REAL_STATES: readonly TaskState[] = [
  'READY', 'PARKED', 'LEASED', 'DISPATCHED', 'STARTING', 'RUNNING',
  'SUCCEEDED', 'FAILED', 'CANCELLED',
]

/**
 * CONTRACT.md invariant 1 and 3: only these four consume capacity. QUEUED,
 * PARKED and READY cost nothing. A UI that colours "in flight" by intuition
 * rather than by this set teaches the wrong mental model of what costs money.
 */
export const CONCURRENCY_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  'LEASED', 'DISPATCHED', 'STARTING', 'RUNNING',
])

export const TERMINAL_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED',
])

export type Tone = 'ok' | 'bad' | 'live' | 'wait'

export function stateTone(state: TaskState): Tone {
  if (state === 'SUCCEEDED') return 'ok'
  if (state === 'FAILED' || state === 'DEAD_LETTERED') return 'bad'
  if (CONCURRENCY_STATES.has(state)) return 'live'
  return 'wait'
}

/**
 * Colour is never the only signal. Roughly 8% of male viewers cannot separate
 * PARKED amber from FAILED red, so every chip carries a glyph and the state
 * word as well.
 */
export function stateGlyph(state: TaskState): string {
  switch (state) {
    case 'SUCCEEDED': return '\u2713'
    case 'FAILED': return '\u2717'
    case 'CANCELLED': return '\u2300'
    case 'PARKED': return '\u23f8'
    case 'READY': return '\u25cb'
    default: return '\u25cf'
  }
}

/**
 * `models.py:175` -- `list[dict[str, Any]]`, NOT a list of strings. This file
 * used to say `string[]`, which would have rendered "[object Object]" the
 * first time a blocked task appeared.
 *
 * Two unrelated writers produce it and the shapes differ:
 *
 *  - Admission denial (`admission.py:97-114`) always writes all four keys, on
 *    both the at-limit and the paused-pool branch:
 *      { pool, reason, limit, active }
 *  - A worker park (`agent_worker/control.py:595-602`) writes
 *      { reason, ...detail }
 *    where `detail` is arbitrary. On the interrupted path it is the ENTIRE
 *    artifact/log/checkpoint summary, so an entry can legitimately contain an
 *    `artifacts` array.
 *
 * So: key off `reason`, treat everything else as optional enrichment, and
 * never render the entry as JSON -- that is how a paragraph of GCS URIs ends
 * up inside a table cell.
 */
export interface BlockedEntry {
  reason: string
  pool?: string
  limit?: number
  active?: number
  [key: string]: unknown
}

/** `ParkReason`, states.py:125-137. All eight are really written. */
export type ParkReason =
  | 'PROVIDER_QUOTA_EXHAUSTED' | 'PROVIDER_COOLDOWN' | 'PROVIDER_OUTAGE'
  | 'SCHEDULED_RETRY' | 'DEPENDENCY_INCOMPLETE' | 'MANUAL_PAUSE'
  | 'BUDGET_EXHAUSTED' | 'CREDENTIAL_MISSING'

/**
 * The same eight as a value, so a test can compare the list against
 * `swarm_common.states.ParkReason` and the three sets below can be checked for
 * covering it. A union type erases at build time and can be checked against
 * nothing.
 */
export const PARK_REASONS = [
  // ONE PER LINE, and not for taste. `SCHEDULED_RETRY` and `MANUAL_PAUSE` are
  // spelled identically in `BlockedReason`, a different enum, and
  // `test_no_screen_declares_its_own_grouping_of_reasons` reads any LINE
  // carrying two of those names as a screen inventing its own blocker
  // grouping. Wrapping these three declarations one-per-line keeps that check
  // able to do its job instead of teaching it about a second enum.
  'PROVIDER_QUOTA_EXHAUSTED',
  'PROVIDER_COOLDOWN',
  'PROVIDER_OUTAGE',
  'SCHEDULED_RETRY',
  'DEPENDENCY_INCOMPLETE',
  'MANUAL_PAUSE',
  'BUDGET_EXHAUSTED',
  'CREDENTIAL_MISSING',
] as const

/**
 * WHAT ENDS A PARK. The three sets below partition `PARK_REASONS`, and the
 * partition is the only thing that decides how loudly a parked step is
 * reported.
 *
 * A PARKED task holds NO capacity -- CONTRACT invariant 1 gives demand to
 * LEASED, DISPATCHED, STARTING and RUNNING only -- so none of this is a
 * capacity problem and none of the copy derived from it may say it is. Parking
 * is how this platform declines to pay for a wait. What a reader needs to know
 * is not "how much is this costing" (nothing) but "will it come back by
 * itself", and that has exactly three answers.
 */

/**
 * Nothing will clear these. No timer runs out, no provider recovers: a person
 * registers a key, raises a budget or resumes a pool, or the work waits
 * forever. `next_eligible_at` is null on all three, which is the field-level
 * form of the same fact.
 */
export const PARK_NEEDS_A_PERSON: ReadonlySet<string> = new Set<ParkReason>([
  'CREDENTIAL_MISSING',
  'BUDGET_EXHAUSTED',
  'MANUAL_PAUSE',
])

/**
 * These end on their own -- a quota window resets, a cooldown expires, a
 * provider comes back, a retry falls due. `next_eligible_at` carries when,
 * where the writer knew it. Worth watching, not worth waking anyone.
 *
 * PROVIDER_OUTAGE is here rather than above deliberately. An outage is not
 * something an operator of THIS platform can fix, and moving it into the
 * act-now group would send someone to a screen with no control on it.
 */
export const PARK_CLEARS_ITSELF: ReadonlySet<string> = new Set<ParkReason>([
  'PROVIDER_QUOTA_EXHAUSTED',
  'PROVIDER_COOLDOWN',
  'PROVIDER_OUTAGE',
  'SCHEDULED_RETRY',
])

/**
 * The ordinary state of a workflow step that is waiting its turn, and the one
 * reason that is NOT a problem on its own.
 *
 * A three-step chain has two steps parked like this for its whole life and
 * nothing is wrong. Raising it would mean every healthy workflow lit the
 * attention panel, which trains a reader to ignore the panel -- the exact
 * failure the panel exists to avoid. It becomes interesting only when the
 * workflow holding it has stopped moving, and that is `workflowCheck`'s
 * question, asked once, over the workflow rather than over each step.
 */
export const PARK_WAITS_ON_A_STEP: ReadonlySet<string> = new Set<ParkReason>([
  'DEPENDENCY_INCOMPLETE',
])

/**
 * Copy for every reason that is ever actually written -- the seven from
 * admission plus the eight ParkReasons.
 *
 * `BlockedReason.BUDGET_LIMIT`, `QUOTA_EXHAUSTED`, `COOLDOWN`, `DEPENDENCY`
 * and `SCHEDULED_RETRY` are members of the enum that nothing ever writes as a
 * blocker, so they are deliberately absent: a legend promising them describes
 * a platform that does not exist.
 *
 * The distinction worth preserving is whose problem it is. TENANT_LIMIT means
 * you are at your own ceiling and only an admin can move it; the global and
 * resource-class limits mean the platform is busy and waiting is the answer.
 * That is why the field is surfaced verbatim rather than collapsed to "busy".
 */
export const REASON_COPY: Readonly<Record<string, string>> = {
  GLOBAL_CONCURRENCY_LIMIT: 'The platform is at its overall limit. Waiting is the answer.',
  TENANT_LIMIT: 'Your tenant is at its own limit. Only an admin can raise it.',
  PROVIDER_CONCURRENCY_LIMIT: 'This provider is at its concurrency limit.',
  RESOURCE_CLASS_LIMIT: 'This resource class is busy platform-wide.',
  RUNNER_LIMIT: 'This runner profile is at its limit.',
  BACKEND_LIMIT: 'This backend is at its limit.',
  MANUAL_PAUSE: 'An operator paused this pool. It will not admit work until resumed.',
  PROVIDER_QUOTA_EXHAUSTED: 'The provider quota is spent. Parked until it resets.',
  PROVIDER_COOLDOWN: 'Backing off after provider errors.',
  PROVIDER_OUTAGE: 'The provider is unavailable.',
  SCHEDULED_RETRY: 'Waiting for a scheduled retry.',
  DEPENDENCY_INCOMPLETE: 'Waiting on an earlier step in its workflow.',
  BUDGET_EXHAUSTED: 'The budget for this work is spent.',
  CREDENTIAL_MISSING: 'No provider key is registered for this tenant.',
}

/**
 * Copy for a reason, falling back to the RAW STRING rather than blanking the
 * cell. `codec.blocked_reason_values()` exists but is wired to no route, so
 * this table is shipped client-side and will go stale; printing the unknown
 * value is what makes that visible instead of silent.
 */
export function reasonCopy(reason: string): string {
  return REASON_COPY[reason] ?? reason
}

/** The one-line "why is this not running" for a task, or null if it is. */
export function whyNotRunning(task: Task): string | null {
  const first = task.blocked_by?.[0]
  if (first?.reason) {
    const where = first.pool ? ` (${first.pool})` : ''
    const at =
      typeof first.limit === 'number' && typeof first.active === 'number'
        ? ` -- ${first.active} of ${first.limit} in use`
        : ''
    return `${reasonCopy(first.reason)}${where}${at}`
  }
  if (task.park_reason) return reasonCopy(task.park_reason)
  return null
}

/**
 * `GET /v1/tasks`, routes/tasks.py:64-95.
 *
 * The key is `next_page_token`. This file said `next_cursor`, which the API
 * has never sent, so paging could never have advanced past the first page.
 *
 * `tenant_id` comes back too and is worth keeping: every row is the caller's
 * own tenant, so the scope belongs in the page header once rather than in a
 * column on every row.
 */
export interface TaskPage {
  tasks: Task[]
  next_page_token?: string | null
  tenant_id?: string
}

/**
 * The frozen catalogue, profiles.py:77-81. Bundled client-side because it is
 * frozen and three entries long; the alternative is a request per render to
 * learn something that cannot change without a contract change.
 */
export const RESOURCE_UNITS: Readonly<Record<string, number>> = {
  standard: 1,
  browser: 2,
  large: 4,
}

/**
 * Column 3, "Why". First match wins.
 *
 * The CANCELLED cascade case is the subtle one. When a workflow parent fails,
 * the scheduler cancels the child (scheduler/loop.py:438-442). Do not
 * string-match its message -- compute it. In this table the parents' states
 * are not loaded, only `depends_on` ids, so the discriminator that works with
 * what the row actually has is `cancel_requested`: the scheduler's cascade
 * writes state, park_reason, blocked_by, completed_at and last_error and
 * NEVER sets cancel_requested (scheduler/store.py:303-323), while every human
 * cancel does, including a whole-workflow cancel which fans out through
 * request_cancel per step. That survives an upstream copy change.
 */
export function whyAgent(task: Task): string {
  if (task.state === 'PARKED') {
    const base = task.park_reason ? reasonCopy(task.park_reason) : 'Parked.'
    return task.next_eligible_at ? `${base} Eligible again ${task.next_eligible_at}.` : base
  }
  if (task.state === 'READY' && task.blocked_by?.length) {
    const b = task.blocked_by[0]
    if (!b) return ''
    // Only show the fraction when BOTH keys are present. An admission blocker
    // always carries them; a worker park's arbitrary detail may not.
    const at =
      typeof b.active === 'number' && typeof b.limit === 'number'
        ? ` (${b.active}/${b.limit})`
        : ''
    return `${reasonCopy(b.reason)}${at}`
  }
  if (task.state === 'FAILED') return task.last_error ?? 'Failed.'
  if (task.state === 'CANCELLED') {
    if (!task.cancel_requested && task.depends_on?.length) {
      return 'An upstream step did not succeed.'
    }
    return 'Cancelled by request.'
  }
  return ''
}

/**
 * Column 6, "Elapsed".
 *
 * `started_at` is written on DISPATCHED -> STARTING, so a LEASED or
 * DISPATCHED task legitimately has none. That must read "queued 4m", never
 * "0s" and never "Invalid Date" -- and `completed_at` can be null on a row
 * that went terminal between two polls, so the terminal branch cannot assume
 * it.
 */
export function elapsed(task: Task, now: number): { text: string; ticking: boolean } {
  const ms = (v: string | null) => (v ? new Date(v).getTime() : NaN)
  const created = ms(task.created_at)
  const started = ms(task.started_at)
  const completed = ms(task.completed_at)

  if (Number.isFinite(completed)) {
    const from = Number.isFinite(started) ? started : created
    if (!Number.isFinite(from)) return { text: '\u2014', ticking: false }
    return { text: duration(completed - from), ticking: false }
  }
  if (Number.isFinite(started)) return { text: duration(now - started), ticking: true }
  if (Number.isFinite(created)) return { text: `queued ${duration(now - created)}`, ticking: true }
  return { text: '\u2014', ticking: false }
}

function duration(msSpan: number): string {
  const s = Math.max(0, Math.round(msSpan / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/**
 * A rollup over the tasks ON THE CURRENT PAGE, for the "group by workflow"
 * header on Agents. NOT a workflow's state.
 *
 * THE CLAIM THAT USED TO BE HERE IS NO LONGER TRUE, which is worth saying
 * plainly rather than quietly deleting: this comment said `workflow.state` was
 * written once as QUEUED and never updated, so rendering it would label every
 * finished workflow "queued" forever. That was correct when it was written and
 * is now fixed. `GET /v1/workflows` derives the state from the steps on every
 * read, serves it as `state`, and writes the stored copy back
 * (apps/swarm-api/swarm_api/rollup.py). `Workflow.state` is now the value to
 * render.
 *
 * This function survives because it answers a DIFFERENT question: Agents groups
 * whatever tasks the current page happens to contain, which is not the same set
 * as a workflow's steps, and the heading says "in this page" for that reason.
 * Its vocabulary deliberately does NOT match the server's -- it collapses
 * CANCELLED into 'failed' and has no DEAD_LETTERED case -- so do not reach for
 * it to describe a workflow. If a workflow's state is wanted on that screen, the
 * workflow route serves one.
 */
export function rollupState(states: TaskState[]): 'running' | 'succeeded' | 'failed' | 'waiting' {
  if (states.some((s) => CONCURRENCY_STATES.has(s))) return 'running'
  if (states.some((s) => s === 'FAILED' || s === 'CANCELLED')) return 'failed'
  if (states.length > 0 && states.every((s) => s === 'SUCCEEDED')) return 'succeeded'
  return 'waiting'
}

/**
 * The three parts of a workflow's state as `swarm_api.rollup` computes them.
 *
 * DERIVED SERVER-SIDE AND ONLY READ HERE. Nothing in this file may re-implement
 * the precedence rule: `check-contract-parity.sh` does not cover TypeScript, so
 * a copy would drift in silence -- which is the failure this whole feature was
 * written to stop happening to `workflow.state` itself.
 */
export interface WorkflowRollupCounts {
  /** TaskState value -> count, plus `unstarted` and `unreadable`. */
  [key: string]: number
}

export interface WorkflowRollup {
  /** A TaskState value, or 'UNKNOWN' when a step could not be read. */
  state: string
  /** False when any step's state could not be established. */
  complete: boolean
  reason: string
  counts: WorkflowRollupCounts
  unreadable_steps: string[]
  unstarted_steps: string[]
  steps_read: number
}

/**
 * The comparison between the stored copy and the derived one.
 *
 * `agrees` is THREE-VALUED and the third value is the point: null means the two
 * were not compared, because the derivation was incomplete. Rendering that as a
 * disagreement would manufacture a finding out of a failed read -- the same
 * caution the accounting-drift panel in Holders.tsx carries.
 */
export interface WorkflowDrift {
  stored: string
  derived: string
  agrees: boolean | null
  reason: string
  steps_read: number
  unreadable_steps: string[]
  /** The server wrote the derived value back. `agrees` stays as observed. */
  repaired: boolean
}

/** `workflow_to_api`, codec.py. Every field it actually sends. */
export interface Workflow {
  workflow_id: string
  /**
   * The DERIVED state -- what the steps say, not what the document holds. Safe
   * to render directly; it is 'UNKNOWN' when a step could not be read, which is
   * why this is `string` rather than `TaskState`.
   */
  state: string
  tenant_id: string
  /** The value in Firestore. Present so the cache can be audited. */
  stored_state: string
  /** 'derived' on a read route, 'stored' on the create response. */
  state_source: 'derived' | 'stored'
  /** Absent only where `state_source` is 'stored'. */
  rollup?: WorkflowRollup
  drift?: WorkflowDrift
  created_at: string
  /**
   * WHEN THE DOCUMENT LAST CHANGED, which on a workflow means WHEN ITS DERIVED
   * STATE LAST CHANGED -- and that is a stronger fact than it looks.
   *
   * Only two writers touch it: `Store.set_workflow_state` (store.py:733-737),
   * which the rollup calls ONLY when the derived value disagrees with the
   * stored one, and `cancel_workflow`. `rollup._persist` refuses to write a
   * value that already agrees precisely so that "`updated_at` keeps meaning
   * 'the document changed' rather than 'something looked at it'"
   * (rollup.py:602-605).
   *
   * So this is a progress timestamp, and it is the only one on this API that
   * is. A STEP TASK's `updated_at` is not: `scheduler/store.py:192-201` rewrites
   * `blocked_by` with a fresh `updated_at` on every pass in which a READY task
   * was not admitted, so a task that has been stuck at the front of a full pool
   * for an hour looks like it changed a minute ago. Anything asking "has this
   * moved" must ask the workflow, never the step.
   */
  updated_at: string
  submitted_by: string | null
  priority: number
  on_step_failure: string
  cancel_requested: boolean
  steps: WorkflowStep[]
}

/**
 * `GET /v1/workflows`, routes/workflows.py:64-84.
 *
 * `rollup_report` is the page's own provenance and is the reason this is a
 * declared shape rather than an inline `{ workflows }`. A page whose step-read
 * budget ran out carries rows that read UNKNOWN because the route stopped
 * reading, not because anything is wrong with those workflows, and a screen
 * that cannot tell those apart is this repository's defining bug.
 */
export interface WorkflowPage {
  workflows: Workflow[]
  next_page_token?: string | null
  tenant_id?: string
  rollup_report?: WorkflowRollupReport
}

/** `SweepReport.to_api`, rollup.py:463-478. */
export interface WorkflowRollupReport {
  examined: number
  written: number
  agreed: number
  disagreed: number
  unknown: number
  /** The LIST was cut short, so these counts are not a census of the tenant. */
  truncated: boolean
  step_reads: number
  /** The route stopped reading step tasks. Rows below it read UNKNOWN. */
  step_read_budget_exhausted: boolean
}

/**
 * codec.py:354-365. NOTE WHAT IS NOT HERE: there is no `state`.
 *
 * This file used to declare one, and the DAG view coloured its nodes from it.
 * The fixtures supplied it, so it looked right in development and would have
 * rendered every node grey and "unknown" against the real API -- the exact
 * failure docs/web-ui warns about, a field no live code path writes.
 *
 * A step's state is DERIVED: join `task_id` to the task documents that
 * `GET /v1/workflows/{id}` returns alongside the workflow
 * (routes/workflows.py:50-57), or, for the list screen, to a task page. Use
 * `stepState()` below so the join is in one place and its failure is visible.
 */
export interface WorkflowStep {
  step_id: string
  runner_profile: string
  resource_class: string
  /** The DAG edges. Real ones -- this is a tree, not a star. */
  depends_on: string[]
  /**
   * A MAP, upstream step_id -> artifact filename to stage into this step's
   * workspace. `models.WorkflowStep.input_from` is `dict[str, str]` and
   * `codec.workflow_to_api` serves it as it stands, so this was declared
   * `string | null` against an object that is never a string and never null --
   * and the fixtures supplied a string, so it looked right in development and
   * would have been wrong against every real response. A step may stage from
   * SEVERAL upstreams, which the old type could not express at all.
   */
  input_from: Record<string, string>
  timeout_seconds?: number | null
  task_id?: string | null
  input?: unknown
}

/**
 * The derived state of a step, and the three ways it can be absent -- which
 * are not the same thing and must not render alike:
 *
 *  - `unstarted`: the step has no task_id. The workflow has not reached it.
 *  - `unknown`:   it has a task_id we have no task document for. Either the
 *                 task page did not include it, or the fetch failed. The UI
 *                 must SAY the state could not be read, never imply idleness.
 *  - a TaskState: the join succeeded.
 */
export type StepState =
  | { kind: 'unstarted' }
  | { kind: 'unknown'; taskId: string }
  | { kind: 'state'; state: TaskState; task: Task }

export function stepState(
  step: WorkflowStep,
  taskById: ReadonlyMap<string, Task> | null,
): StepState {
  if (!step.task_id) return { kind: 'unstarted' }
  const task = taskById?.get(step.task_id)
  if (!task) return { kind: 'unknown', taskId: step.task_id }
  return { kind: 'state', state: task.state, task }
}

// ---------------------------------------------------------------------------
// The account pool  (Settings -> Accounts)
// ---------------------------------------------------------------------------
// Shapes returned by `account_to_api` in
// apps/quota-broker/quota_broker/main.py, which swarm-api proxies verbatim.
//
// NO KEY MATERIAL APPEARS HERE, and there is deliberately no field for its
// length either. `Credential.redacted()` exposes `access_token_len`, which is
// fine in a debug log and is not fine in a browser: a length is a real hint
// about a secret and nothing on this screen needs it. If a field like that
// ever turns up in the payload, it does not get a home in this file.

/** `AccountState` in quota_broker/accounts.py. Four members, no others. */
export type AccountStateName = 'AVAILABLE' | 'PAUSED' | 'DRAINING' | 'REAUTH_REQUIRED'

/**
 * One rate-limit window as the provider reported it.
 *
 * `utilization` is 0-1, NOT a percentage -- 0.87 is 87%. Multiplying is this
 * file's job precisely once, in `readingOf`, so no component can forget.
 */
export interface AccountWindow {
  utilization: number
  resets_at: string
  /**
   * The server's own answer to "has this window already passed its reset",
   * computed at response time. Trusted rather than recomputed here: the
   * browser's clock is not the platform's, and a reading that flips between
   * reset and not-reset depending on whose clock is read is worse than either.
   */
  reset: boolean
}

export interface Account {
  /** `<owner_tenant>:<label>`. Opaque; never split for display. */
  account_id: string
  owner_tenant: string
  label: string
  provider: string
  /** Widened to `string`: an unknown state must render as unknown, not crash. */
  state: AccountStateName | string
  /** Why it is in that state, written for the human who has to act on it. */
  reason: string
  /** Tenants the owner has explicitly lent this account to. Empty is the default. */
  lend_to: string[]
  /** Agents currently holding it. Advisory -- the lease is authoritative. */
  assigned: number
  /**
   * Keyed by the PROVIDER's window names, not a fixed pair. accounts.py:143
   * says so explicitly: "the windows are the provider's to define, and a new
   * one appearing must not need a schema change to be recorded". So this
   * screen reads `five_hour` and `seven_day` by name for its two columns and
   * lists anything else it finds rather than dropping it on the floor.
   */
  windows: Record<string, AccountWindow>
  /** When a reading last arrived. NULL MEANS NONE HAS, which is not zero. */
  observed_at: string | null
  /**
   * True when no reading is recent enough to trust (DEFAULT_STALE_AFTER, 30
   * minutes). Computed server-side because a reading's age is what decides
   * whether to believe it, and the server owns the clock.
   */
  stale: boolean
}

/** `GET /v1/accounts`. `tenant_id` is the scope the server actually applied. */
export interface AccountsPage {
  accounts: Account[]
  /** null when a platform caller asked for every tenant. */
  tenant_id: string | null
}

/** `RefreshOutcome.as_dict()` in quota_broker/credentials.py. */
export interface RefreshResult {
  tenant_id: string
  provider: string
  /** THE ONLY FIELD THAT SAYS IT WORKED. The HTTP status does not. */
  refreshed: boolean
  /** One of the eleven reasons in credentials.py. Never rendered raw. */
  reason: string
  expires_at: string | null
}

/** `POST /v1/accounts/{id}/refresh`. A 200 carries a FAILED refresh too. */
export interface RefreshResponse {
  refresh: RefreshResult
  account: Account
}

export const FIVE_HOUR = 'five_hour'
export const SEVEN_DAY = 'seven_day'

/**
 * Colour for a state chip.
 *
 * Returns its own union rather than `Tone`, because two of these are not tones
 * the rest of the app has: PAUSED is deliberate and gets the purple every
 * other paused thing here gets, and an unrecognised state gets grey rather
 * than falling through to a colour that would assert something.
 */
export type AccountTone = 'ok' | 'paused' | 'wait' | 'bad' | 'unknown'

export function accountTone(state: string): AccountTone {
  switch (state) {
    case 'AVAILABLE': return 'ok'
    case 'PAUSED': return 'paused'
    case 'DRAINING': return 'wait'
    case 'REAUTH_REQUIRED': return 'bad'
    default: return 'unknown'
  }
}

/** The one state only a person can clear. The sweep stops trying on it. */
export function needsAHuman(a: Account): boolean {
  return a.state === 'REAUTH_REQUIRED'
}

/**
 * What is actually known about one window, as five cases that must not render
 * alike.
 *
 * This is the whole screen in one function. A missing reading is not zero; a
 * stale reading is not a current one; a window that has already reset is
 * describing a window that no longer exists. `cs status` marks the last two
 * with `~` for exactly this reason, and a figure printed without that mark is
 * a claim that it is current.
 */
export type AccountReading =
  /** No reading has EVER arrived for this account. Not zero -- unmeasured. */
  | { kind: 'never' }
  /** Readings exist, but not for this window. The provider did not report it. */
  | { kind: 'absent' }
  /** The window passed its reset, so the figure describes a window that refilled. */
  | { kind: 'reset'; pct: number; resetsAt: string }
  /** A real figure, too old to trust. Shown, marked, never presented as current. */
  | { kind: 'stale'; pct: number; resetsAt: string; observedAt: string }
  /** Measured, recent, and safe to read as a fact. */
  | { kind: 'live'; pct: number; resetsAt: string; observedAt: string }

export function readingOf(a: Account, key: string): AccountReading {
  if (a.observed_at === null) return { kind: 'never' }
  const w = a.windows[key]
  if (!w || typeof w.utilization !== 'number' || !Number.isFinite(w.utilization)) {
    return { kind: 'absent' }
  }
  const pct = Math.max(0, Math.min(100, w.utilization * 100))
  // `reset` outranks `stale`: a fresh reading of a window that has since
  // refilled is still describing the window before it.
  if (w.reset) return { kind: 'reset', pct, resetsAt: w.resets_at }
  if (a.stale) return { kind: 'stale', pct, resetsAt: w.resets_at, observedAt: a.observed_at }
  return { kind: 'live', pct, resetsAt: w.resets_at, observedAt: a.observed_at }
}

/** True for the two cases `cs status` prefixes with `~`. */
export function isProjected(r: AccountReading): boolean {
  return r.kind === 'stale' || r.kind === 'reset'
}

/**
 * The window that will actually stop you, which is what CLEARS is about.
 *
 * The BINDING window, not the five-hour and not an average: an account at 5%
 * on its five-hour and 90% on its weekly is stopped by the weekly, and
 * averaging them to 47% would send agents at an account that is about to
 * refuse. `Account._binding_remaining` in accounts.py picks the same way, and
 * treats a window past its reset as full again -- so a window that has reset
 * cannot be the binding one while another still has room.
 *
 * Returns null when there is nothing to pick from, which is a real answer:
 * CLEARS then renders as an em dash rather than a time nobody measured.
 */
export function bindingWindow(a: Account): { key: string; window: AccountWindow } | null {
  let best: { key: string; window: AccountWindow } | null = null
  let leastRemaining = Infinity
  for (const [key, w] of Object.entries(a.windows)) {
    if (!w || typeof w.utilization !== 'number' || !Number.isFinite(w.utilization)) continue
    const remaining = w.reset ? 1 : Math.max(0, 1 - w.utilization)
    if (remaining < leastRemaining) {
      leastRemaining = remaining
      best = { key, window: w }
    }
  }
  return best
}

/**
 * A duration the way somebody reads a clock, not the way a computer counts it.
 *
 * Ported field-for-field from `shortDur` in claudeswitch's
 * internal/render/status.go, including the zero-padded minutes, because this
 * column and that one are meant to be the same column. "81h49m" is arithmetic;
 * "3d 10h" is an answer.
 */
export function humaniseUntil(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  if (ms <= 0) return 'now'
  const s = Math.floor(ms / 1000)
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`
  if (h >= 1) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  if (m >= 1) return `${m}m`
  return `${s}s`
}

/** Time until an ISO instant, humanised. Null in, em dash out. */
export function clearsIn(iso: string | null | undefined, now: number): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  return humaniseUntil(t - now)
}

/**
 * Time SINCE an instant, humanised. The past-facing twin of `humaniseUntil`.
 *
 * It lived in Shell.tsx, which is a components file, and moved here when
 * `checks.ts` needed it: the derived-checks layer is pure by construction --
 * no React, no DOM -- and an import from a `.tsx` module would have put a
 * renderer in its dependency graph. Shell.tsx re-exports it, so every existing
 * `import { timeAgo } from './Shell'` is unchanged.
 *
 * It floors at zero, so it must NEVER be pointed at a future instant: a reset
 * two hours away renders "just now", which is the opposite of the truth. That
 * is what `clearsIn` above is for.
 */
export function timeAgo(when: Date | string | number, now: number = Date.now()): string {
  const t =
    typeof when === 'number'
      ? when
      : typeof when === 'string'
        ? new Date(when).getTime()
        : when.getTime()
  if (!Number.isFinite(t)) return 'at an unknown time'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

/**
 * The five-cell bar, as `miniBar` in claudeswitch's internal/render/width.go
 * draws it: round to the nearest fifth, clamp, `▰` filled and `▱`
 * empty.
 *
 * Returned as a count rather than a string so the component can draw real
 * elements -- a screen reader hearing five geometric-shape glyphs learns
 * nothing, and the percentage beside it is the accessible version of the
 * same fact.
 */
export const BAR_CELLS = 5

export function barFilled(pct: number): number {
  const filled = Math.round((pct / 100) * BAR_CELLS)
  return Math.max(0, Math.min(BAR_CELLS, filled))
}

// ---------------------------------------------------------------------------
// Adding an account: the two-step sign-in
// ---------------------------------------------------------------------------
// Shapes returned by `begin_account_authorization` and
// `finish_account_authorization` in apps/quota-broker/quota_broker/main.py.
//
// WHY A PERSON STILL PASTES SOMETHING, since "just a button" is the obvious
// ask. Anthropic's OAuth client accepts exactly one redirect target -- its own
// callback page, which DISPLAYS a code. A third-party application cannot
// register `https://swarm.saga.xyz/callback`, so the browser cannot be
// redirected back here and the code on that page is what closes the loop. One
// short string replaces a keychain item; that is the whole distance this can
// travel, and the screen says so rather than leaving it looking like an
// oversight.
//
// NO KEY MATERIAL APPEARS HERE EITHER. The PKCE verifier is held server-side
// keyed by `state` and never reaches the browser -- a verifier the client holds
// is a PKCE flow that proves nothing -- so there is no field for it and there
// must not be one.

/** What `POST /v1/accounts/authorize` answers with. */
export interface AccountAuthorization {
  /** The page a person opens to sign in. Always the provider's own host. */
  authorize_url: string
  /**
   * What identifies this sign-in to the server. Sent back with the code.
   *
   * Also rendered on the callback page after a `#`, which is why the paste
   * field accepts the whole thing: the server splits it and refuses a paste
   * whose state belongs to a DIFFERENT sign-in rather than guessing between
   * two open tabs.
   */
  state: string
  /**
   * How long the server holds the pending sign-in.
   *
   * NULL WHEN THE SERVER DID NOT SAY. It is not defaulted to a plausible
   * fifteen minutes: a countdown to a deadline nobody reported is a number
   * invented in the browser, and this screen's whole discipline is that a
   * figure on it was measured somewhere.
   */
  expires_in_seconds: number | null
}

/** What `POST /v1/accounts/exchange` answers with on 201. No key material. */
export interface AccountExchangeResponse {
  account: Account
  /**
   * When the ACCESS half expires -- hours, not days. The pair outlives it:
   * the broker's sweep exchanges the refresh half for a successor
   * indefinitely, which is what makes "you sign in once" true rather than
   * aspirational. The screen has to say that, because an expiry in eight
   * hours otherwise reads as "do this again tonight".
   */
  expires_at: string | null
  note?: string
}

/**
 * The host the provider will send the person to, taken from the URL the SERVER
 * built rather than from a constant here.
 *
 * `OAUTH_REDIRECT_URI` lives in quota_broker/oauth.py and nothing checks that a
 * TypeScript copy of it still matches -- `check-contract-parity.sh` covers the
 * shell and jq restatements of the frozen contract, not this file. So the copy
 * that tells somebody which page will show them a code reads the value out of
 * the authorize URL itself, and says "the page Claude sends you to" when it
 * cannot. A wrong host name in that sentence sends a person hunting a page
 * they will never see.
 */
export function callbackHostOf(authorizeUrl: string): string | null {
  try {
    const redirect = new URL(authorizeUrl).searchParams.get('redirect_uri')
    return redirect ? new URL(redirect).host : null
  } catch {
    return null
  }
}

/**
 * A HINT ABOUT A PASTE, never a gate on one.
 *
 * The server is the only authority on what a code is: it splits `<code>#<state>`
 * itself, and `split_pasted_code` is the one implementation of that rule. A
 * second one here would disagree with it the first time the callback page
 * changed shape, and it would disagree by REFUSING something the platform
 * would have accepted -- which is the expensive direction to be wrong in.
 *
 * So this returns a sentence to show BESIDE the field, and the submit button
 * stays live whatever it says. It only recognises the two pastes that are
 * definitely not a code: a URL (the address bar instead of the page) and a
 * query string (the part after the `?`). Everything else returns null.
 */
export function pastedCodeHint(pasted: string): string | null {
  const v = pasted.trim()
  if (v === '') return null
  if (/^https?:\/\//i.test(v)) {
    return 'That looks like a URL rather than a code. The code is the short string the callback page prints on the page itself, not the address bar. Sending it anyway is safe — the platform will say what it made of it.'
  }
  if (/^[?&]?code=/i.test(v)) {
    return 'That looks like a query string. The code is the value, without the `code=` in front of it. Sending it anyway is safe — the platform will say what it made of it.'
  }
  return null
}

// --------------------------------------------------------------------------
// Checkpoint and log inspection
//
// `GET /v1/tasks/{id}/checkpoints` and `GET /v1/tasks/{id}/logs`, added when
// the server-side seam for them was built. Both endpoints answer in THREE
// states rather than two, and these types keep that: a field that could not be
// read is `null` and carries a `_detail` beside it, never a zero and never an
// empty string. A screen that renders `content ?? ''` shows nothing rather
// than an empty log that reads as a silent agent.
// --------------------------------------------------------------------------

// NOTE the names. `CheckpointRow` is already taken, by the type the UI built
// to work around NOT having this route: it is assembled from
// `checkpoint_completed` EVENTS, and its own comment says "CONTENTS ARE NOT
// RECORDED ANYWHERE ... a file listing would need a new route and a manifest
// read out of GCS". That route now exists, and `CheckpointRecord` below is
// what it serves -- named after the worker's own `CheckpointRecord` dataclass,
// which is what the manifest is a serialisation of.

/** Was the manifest -- the worker's commit marker -- there and readable? */
export type ManifestStatus = 'present' | 'absent' | 'unreadable'

export interface CheckpointFile {
  name: string
  key: string
  bytes: number
}

export interface CheckpointRecord {
  checkpoint_id: string
  attempt_id: string
  /** False means NO ATTEMPT DOCUMENT was found, not that the attempt never ran. */
  attempt_known: boolean
  attempt_created_at: string | null
  attempt_completed_at: string | null
  prefix: string
  uri: string
  /** True when `task.latest_checkpoint` names exactly this checkpoint. */
  is_latest_pointer: boolean
  /** What is in the bucket under this prefix, from the listing -- no download. */
  objects: CheckpointFile[]
  stored_bytes: number
  manifest: ManifestStatus
  manifest_detail: string | null
  created_at: string | null
  seq: number | null
  generation: number | null
  label: string | null
  archive_bytes: number | null
  archive_sha256: string | null
  /** Files inside the archive, from the manifest. Null when it was not readable. */
  file_count: number | null
  /**
   * Would a retry restore from this? NULL, not false, when the manifest could
   * not be read -- "cannot resume" and "cannot tell" are different sentences.
   */
  resumable: boolean | null
  resumable_detail: string | null
}

/** What `task.latest_checkpoint` currently names. `outside_this_task` is a
 *  finding: a resuming worker would ignore such a pointer entirely. */
export interface LatestCheckpointPointer {
  pointer: string | null
  status: 'unset' | 'present' | 'missing' | 'outside_this_task'
  checkpoint_id: string | null
  detail?: string
}

export interface CheckpointsPage {
  task_id: string
  tenant_id: string
  prefix: string
  checkpoints: CheckpointRecord[]
  count: number
  total_found: number
  next_page_token: string | null
  /** The listing SUCCEEDED. This is what makes an empty array an answer. */
  listed: boolean
  /** The scan limit cut the prefix short. Never silent. */
  truncated: boolean
  latest_checkpoint: LatestCheckpointPointer
}

/** Which object was served: the completed record, or the live tail. */
export type LogSource = 'final' | 'live'
export type LogStatus = 'ok' | 'absent' | 'unreadable'

export interface LogStream {
  stream: 'stdout' | 'stderr'
  source: LogSource | null
  status: LogStatus
  detail: string | null
  key: string | null
  uri: string | null
  /** Null for absent and unreadable. NEVER `''` -- that is a real, empty log. */
  content: string | null
  /** Null when nothing could be read. Never 0, which is a real, empty object. */
  total_bytes: number | null
  offset: number
  returned_bytes: number
  next_offset: number | null
  truncated: boolean
  /** True when read-time redaction actually replaced something in this window. */
  redacted: boolean
  redaction_count: number
  /** From the `#swarm-tail` header, for a live window: where it sits in the
   *  stream, so a reader can tell a gap from a continuation. */
  tail_window: { object_offset: number; stream_size: number } | null
}

export interface TaskLogs {
  task_id: string
  tenant_id: string
  attempt_id: string | null
  attempt: {
    status: 'latest' | 'requested' | 'unknown_attempt' | 'no_attempt_yet'
    known: boolean
    generation: number | null
    created_at: string | null
    completed_at: string | null
    exit_code: number | null
  }
  streams: LogStream[]
  prefix: string
  /** Stated by the server rather than assumed here. A deployment where
   *  redaction somehow stopped would otherwise look identical to a working one. */
  redaction: { applied_at_read_time: boolean; rules: number }
}

// ---------------------------------------------------------------------------
// Artifact CONTENT -- `GET /v1/tasks/{id}/artifacts/content?name=`
// ---------------------------------------------------------------------------

/**
 * Four answers, and the fourth is the one a viewer keeps getting wrong.
 *
 *  - `ok`         the object was read. `content` is a string, possibly `''`,
 *                 which is a real empty artifact and not a failure.
 *  - `absent`     the manifest lists it and the object is not in the bucket.
 *  - `unreadable` the bucket could not be read. Nothing may be concluded.
 *  - `binary`     it is not text, so the server deliberately served no bytes.
 *                 NOT an error, and not an empty document either: rendering it
 *                 as either turns "this file is a tarball" into "this agent
 *                 produced nothing".
 */
export type ArtifactContentStatus = 'ok' | 'absent' | 'unreadable' | 'binary'

export interface ArtifactContent {
  task_id: string
  tenant_id: string
  attempt_id: string
  artifact: { name: string | null; bytes: number | null; uri: string | null }
  status: ArtifactContentStatus
  /** Why, in a sentence, whenever the status alone does not carry it. */
  detail: string | null
  key: string | null
  uri: string | null
  /** Null for every non-`ok` status. NEVER `''` -- that is a real empty file. */
  content: string | null
  total_bytes: number | null
  offset: number
  returned_bytes: number
  /** Where the next window starts. Null means this window reached the end. */
  next_offset: number | null
  /** True when bytes were withheld by the cap. The viewer must SAY so. */
  truncated: boolean
  redacted: boolean
  redaction_count: number
  redaction: { applied_at_read_time: boolean; rules: number }
}

/**
 * How a viewer presents one artifact, decided from its NAME alone.
 *
 * Deliberately not from a server-supplied content type: nothing in the upload
 * path sets one (`lifecycle._upload_outputs` passes `content_type` for the two
 * log objects and for nothing else), so a viewer that branched on it would be
 * branching on `undefined` for every artifact a run actually produces.
 */
export type ArtifactKind = 'markdown' | 'transcript' | 'text'

export function artifactKind(name: string): ArtifactKind {
  const lower = name.toLowerCase()
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown'
  // `claude-transcript.json` and `codex-transcript.json` -- `spec.transcript_name`
  // in the two cliagent runners. Matched on the suffix rather than on either
  // exact name, so a third runner's transcript renders as a transcript too.
  if (lower.endsWith('transcript.json')) return 'transcript'
  return 'text'
}

/**
 * WHICH STATES A STOP CONTROL MAY BE OFFERED ON.
 *
 * `Store.request_cancel` raises a 409 for the four terminal states and accepts
 * everything else, so this is that rule stated once on the client -- a button
 * that 409s is a button that should not have been drawn.
 *
 * It is NOT `CONCURRENCY_STATES`: a QUEUED or PARKED task is perfectly
 * cancellable and cancelling it is free, which is exactly the case where an
 * operator most wants the control.
 */
export function canBeStopped(task: { state: TaskState; cancel_requested?: boolean }): boolean {
  return !TERMINAL_STATES.has(task.state) && task.cancel_requested !== true
}

/**
 * Whether stopping this task will end an ATTEMPT that is already under way.
 *
 * The distinction the confirmation turns on. A task holding capacity has a
 * live container: `request_cancel` only sets a flag, the worker acts on it at
 * its next heartbeat, and `lifecycle` checkpoints and uploads before it exits
 * -- so the work is kept. A task in QUEUED/READY/PARKED has no attempt at all,
 * goes straight to CANCELLED, and has nothing to keep.
 */
export function stoppingEndsALiveAttempt(task: { state: TaskState }): boolean {
  return CONCURRENCY_STATES.has(task.state)
}
