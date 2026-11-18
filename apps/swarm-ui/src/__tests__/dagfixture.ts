// THE GRAPH-RENDERING FIXTURE (docs/design/graph-rendering.md §2.4), moved
// out of the GFY lane's prototype (`src/proto/fixtures.ts` on that branch) so
// lane GR1's targets are held against the same 48 steps the design measured:
// 129 crossings drawn in listing order, 30 after the barycentric pass.
//
// Built from the console's own types, so the renderer draws exactly what a
// workflow read would hand it. Every value here is a fixture.

import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

export const T0 = Date.parse('2026-10-08T12:00:00.000Z')
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString()

function step(step_id: string, depends_on: string[]): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null }
}

export function fixtureTask(
  id: string,
  step_id: string,
  state: TaskState,
  workflow_id: string,
  times: { started_at: string | null; completed_at: string | null },
): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-7_200_000),
    updated_at: iso(-60_000),
    started_at: times.started_at,
    completed_at: times.completed_at,
    submitted_by: 'operator@swarm.example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
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
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
  }
}

const AREAS = ['api', 'ui', 'worker', 'infra'] as const

/**
 * A workflow (48 steps at the default three rounds) in the shape the owner's
 * lanes take: one plan, a spec per area, `rounds` implementers per spec, a test
 * per implementer, a security scan over every implementer, a review per area,
 * docs, one integrator, a wide e2e stage, two gates and a release that also
 * reads the plan (a skip-level edge).
 *
 * The implementers are LISTED ROUND BY ROUND (api-1, ui-1, worker-1, infra-1,
 * api-2, ...), which is how a composer adds them and is what made the
 * listing-order layout cross its edges.
 */
export function bigSteps(rounds = 3): WorkflowStep[] {
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

/**
 * The workflow around `steps`, with one task per step that `run` gives a
 * state and times to; a step `run` answers null for has no task (unstarted).
 */
export function bigWorkflow(
  steps: WorkflowStep[],
  run: (s: WorkflowStep) => { state: TaskState; started_at: string | null; completed_at: string | null } | null,
): { workflow: Workflow; taskById: Map<string, Task> } {
  const workflow_id = `wf_fixture_graph${steps.length}`
  const taskById = new Map<string, Task>()
  const drawn = steps.map((s) => {
    const r = run(s)
    if (r === null) return { ...s, task_id: null }
    const id = `task_${s.step_id.replace(/-/g, '_')}`
    taskById.set(id, fixtureTask(id, s.step_id, r.state, workflow_id, r))
    return { ...s, task_id: id }
  })
  const done = [...taskById.values()].every((t) => t.state === 'SUCCEEDED') && taskById.size === steps.length
  const workflow: Workflow = {
    workflow_id,
    state: done ? 'SUCCEEDED' : 'RUNNING',
    tenant_id: 'eng',
    stored_state: done ? 'SUCCEEDED' : 'RUNNING',
    // Stored, so no rollup is invented for a fixture: the graph reads the steps.
    state_source: 'stored',
    created_at: iso(-7_200_000),
    updated_at: iso(-60_000),
    submitted_by: 'operator@swarm.example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps: drawn,
  }
  return { workflow, taskById }
}

/** A finished step that ran `seconds`, ending `endsAgoMs` before T0. */
export function ran(seconds: number, endsAgoMs = 60_000): { state: TaskState; started_at: string; completed_at: string } {
  return { state: 'SUCCEEDED', started_at: iso(-endsAgoMs - seconds * 1000), completed_at: iso(-endsAgoMs) }
}

/** A step running since `secondsAgo` before T0: elapsed, not a duration. */
export function running(secondsAgo: number): { state: TaskState; started_at: string; completed_at: null } {
  return { state: 'RUNNING', started_at: iso(-secondsAgo * 1000), completed_at: null }
}
