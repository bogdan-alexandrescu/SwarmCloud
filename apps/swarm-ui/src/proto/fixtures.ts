// PROTOTYPE FIXTURES for the graph-rendering lane (docs/design/graph-rendering.md).
//
// NOT IMPORTED BY THE APP. Nothing under src/proto/ is reachable from main.tsx
// or App.tsx; the preview page (src/proto/preview.html) is served only by the
// Vite dev server, and `vite build` (whose one input is index.html) never sees
// it. Every value here is a labelled fixture: the preview says so on screen, so
// no screenshot taken from it can be mistaken for a live run or a real index.
//
// Built from the console's own types (types.ts, RepoGraphData.ts) so the
// prototype renderers and today's renderers draw exactly the same data.

import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const T0 = Date.parse('2026-10-08T12:00:00.000Z')
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString()

function step(step_id: string, depends_on: string[]): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null }
}

function task(id: string, step_id: string, state: TaskState, workflow_id: string): Task {
  const started = state === 'QUEUED' || state === 'PARKED' ? null : iso(-1_800_000)
  const done = state === 'SUCCEEDED' || state === 'FAILED'
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-3_600_000),
    updated_at: iso(-60_000),
    started_at: started,
    completed_at: done ? iso(-600_000) : null,
    submitted_by: 'operator@swarm.example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: state === 'PARKED' ? 'provider_quota' : null,
    blocked_by: null,
    workflow_id,
    step_id,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: state === 'FAILED' ? 'exit status 1' : null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
  }
}

const AREAS = ['api', 'ui', 'worker', 'infra'] as const

/**
 * A workflow (48 steps at the default three rounds) in the shape the owner's
 * lanes actually take: one plan, a spec per area, `rounds` implementers per
 * spec, a test per implementer, a security scan over every implementer, a
 * review per area, docs, one integrator, a wide e2e stage, two gates and a
 * release that also reads the plan (a skip-level edge).
 *
 * The implementers are LISTED ROUND BY ROUND (api-1, ui-1, worker-1, infra-1,
 * api-2, ...), which is how a composer adds them and is what makes today's
 * order-of-listing layout cross its edges.
 */
export function protoSteps(rounds = 3): WorkflowStep[] {
  const ks = Array.from({ length: rounds }, (_, i) => i + 1)
  const steps: WorkflowStep[] = [step('plan', [])]
  for (const a of AREAS) steps.push(step(`spec-${a}`, ['plan']))
  for (const k of ks) for (const a of AREAS) steps.push(step(`impl-${a}-${k}`, [`spec-${a}`]))
  for (const k of ks) for (const a of AREAS) steps.push(step(`test-${a}-${k}`, [`impl-${a}-${k}`]))
  steps.push(step('security-scan', AREAS.flatMap((a) => ks.map((k) => `impl-${a}-${k}`))))
  // Reviews read their area's tests, listed in reverse area order.
  for (const a of [...AREAS].reverse()) steps.push(step(`review-${a}`, ks.map((k) => `test-${a}-${k}`)))
  for (const a of AREAS) steps.push(step(`docs-${a}`, [`review-${a}`, `spec-${a}`]))
  steps.push(step('integrate', [...AREAS.map((a) => `review-${a}`), ...AREAS.map((a) => `docs-${a}`), 'security-scan']))
  for (let k = 1; k <= 6; k++) steps.push(step(`e2e-${k}`, ['integrate']))
  steps.push(step('canary', ['e2e-1', 'e2e-3', 'e2e-5']))
  steps.push(step('perf', ['e2e-2', 'e2e-4', 'e2e-6']))
  steps.push(step('release', ['canary', 'perf', 'plan']))
  return steps
}

/** States a running run would have at one instant; a step whose parents have not all succeeded has no task. */
const SCRIPT: Record<string, TaskState> = {
  'impl-ui-3': 'RUNNING',
  'impl-infra-2': 'RUNNING',
  'impl-worker-3': 'LEASED',
  'test-ui-1': 'RUNNING',
  'test-ui-2': 'FAILED',
  'test-worker-1': 'PARKED',
  'test-infra-1': 'DISPATCHED',
}

/** `rounds` implementers per area: 3 gives the 48-step run the screenshots show, 22 gives 200 steps (past the server's 50-step cap, for measurement only). */
export function protoWorkflow(rounds = 3): { workflow: Workflow; taskById: Map<string, Task> } {
  const steps = protoSteps(rounds)
  const workflow_id = `wf_proto_graph${steps.length}`
  const state = new Map<string, TaskState>()
  const taskById = new Map<string, Task>()
  for (const s of steps) {
    const ready = s.depends_on.every((d) => state.get(d) === 'SUCCEEDED')
    if (!ready) continue
    const early = s.step_id === 'plan' || s.step_id.startsWith('spec-') || s.step_id.startsWith('impl-')
    const st: TaskState = SCRIPT[s.step_id] ?? (early || /^test-(api|worker-2|infra-2)/.test(s.step_id) ? 'SUCCEEDED' : 'QUEUED')
    state.set(s.step_id, st)
    const id = `task_${s.step_id.replace(/-/g, '_')}`
    s.task_id = id
    taskById.set(id, task(id, s.step_id, st, workflow_id))
  }
  const counts: Record<string, number> = { unstarted: steps.length - taskById.size }
  for (const t of taskById.values()) counts[t.state] = (counts[t.state] ?? 0) + 1
  const workflow: Workflow = {
    workflow_id,
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts,
      unreadable_steps: [],
      unstarted_steps: steps.filter((s) => !s.task_id).map((s) => s.step_id),
      steps_read: steps.length,
    },
    created_at: iso(-3_600_000),
    updated_at: iso(-60_000),
    submitted_by: 'operator@swarm.example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
  return { workflow, taskById }
}

/** mulberry32: a seeded generator, so every screenshot and measurement draws the same graph. */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const PACKAGES = ['src/api', 'src/core', 'src/db', 'src/workers', 'src/ui', 'lib/shared'] as const
const LEAVES = ['orders', 'money', 'tax', 'users', 'auth', 'queue', 'cache', 'routes', 'models', 'schema', 'jobs', 'views', 'forms', 'client']
/** Which packages a package's modules call: api → core → db, workers → core, ui → api, all → shared. */
const CALLS: Record<string, readonly string[]> = {
  'src/api': ['src/core', 'lib/shared'],
  'src/core': ['src/db', 'lib/shared'],
  'src/db': ['lib/shared'],
  'src/workers': ['src/core', 'src/db', 'lib/shared'],
  'src/ui': ['src/api', 'lib/shared'],
  'lib/shared': [],
}

/**
 * The raw body of `GET /v1/repositories/{id}/graph` for a repository of
 * `modules` modules, before `normModuleGraph`. At 48 it sits under COLLAPSE_AT
 * (60), so today's canvas draws every module; the scale measurements pass
 * hundreds or thousands.
 */
export function protoGraphBody(modules = 48, seed = 7): Record<string, unknown> {
  const r = rng(seed)
  const ids: string[] = []
  const pkgOf = new Map<string, string>()
  for (let i = 0; i < modules; i++) {
    const pkg = PACKAGES[i % PACKAGES.length]!
    const leaf = LEAVES[Math.floor(i / PACKAGES.length) % LEAVES.length]!
    const round = Math.floor(i / (PACKAGES.length * LEAVES.length))
    const id = `${pkg}/${leaf}${round === 0 ? '' : `_${round}`}.py`
    ids.push(id)
    pkgOf.set(id, pkg)
  }
  const byPkg = new Map<string, string[]>()
  for (const id of ids) byPkg.set(pkgOf.get(id)!, [...(byPkg.get(pkgOf.get(id)!) ?? []), id])
  const edges: Record<string, unknown>[] = []
  const seen = new Set<string>()
  for (const id of ids) {
    const own = byPkg.get(pkgOf.get(id)!)!
    const targets = CALLS[pkgOf.get(id)!]!
    const n = 1 + Math.floor(r() * 3)
    for (let k = 0; k < n; k++) {
      const pool = r() < 0.45 || targets.length === 0 ? own : byPkg.get(targets[Math.floor(r() * targets.length)]!)!
      const to = pool[Math.floor(r() * pool.length)]!
      const key = `${id}>${to}`
      if (to === id || seen.has(key)) continue
      seen.add(key)
      const weight = 1 + Math.floor(r() * 20)
      edges.push({ from: id, to, weight, kinds: { call: weight }, max_confidence: r() < 0.15 ? 0.3 : 0.95 })
    }
  }
  return {
    index_sha: 'a1b2c3d'.padEnd(40, '0'),
    head_sha: 'a1b2c3d'.padEnd(40, '0'),
    behind_by: 0,
    stale: false,
    freshness: { state: 'current' },
    cluster: 'module',
    modules: ids.map((id) => ({
      id,
      modules: 1,
      symbols: 4 + Math.floor(r() * 60),
      tests: Math.floor(r() * 4),
      hot_spot_changes: r() < 0.1 ? null : Math.floor(r() * 30),
      test_reach: r() < 0.1 ? null : Math.round(r() * 100) / 100,
      languages: ['python'],
    })),
    edges,
    counts: { files: modules * 3, symbols: modules * 30, edges: edges.length * 40 },
    truncated: [],
  }
}
