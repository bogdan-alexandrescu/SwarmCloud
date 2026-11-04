/**
 * A 40-STEP WORKFLOW'S PAGE READS ITS STEPS IN A BOUNDED NUMBER OF REQUESTS,
 * NOT ONE PER STEP (#2688, WORK-READS-SPEED; the owner's request of
 * 2026-11-02). Measured in the owner's browser on a 40-step workflow whose
 * steps all run claude-code-browser, 10 running: 61 requests on load, among
 * them 24 per-step `GET /v1/tasks/{id}/events` pages (the slowest 16.7 s,
 * 31.6 s in all) behind each waiting step's time, and 12 per-step
 * `GET /v1/tasks/{id}/attempts` reads behind each step's tokens and cost.
 *
 * The page now reads both through `GET /v1/tasks/digests`, ids batched. This
 * runs the REAL api.ts against a stubbed `fetch` -- the fixture build is
 * turned off, so every read is a request -- and counts them by route.
 *
 * MUTATIONS: put the per-step `loadTaskEventsPage` back in `useEnteredAt`;
 * put `loadAttempts` per step back in `loadWorkflowUsage`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import STYLES from '../styles.css?raw'
import type { WorkflowBoard } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

// Every read a request: the fixture build would answer in-process.
vi.hoisted(() => {
  vi.stubEnv('VITE_LIVE', '1')
})

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflow: vi.fn(),
  loadTasks: vi.fn(),
  loadOlderWorkflows: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, WorkflowCard, stageKey } = await import('../Workflows')
const { USE_FIXTURES } = await import('../api')
const { ownFromBoard } = await import('./workflowsById')

const T0 = Date.parse('2026-11-02T04:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

/** The 40 steps' states: 24 waits the page times, 10 running, 6 done. */
const STATES: TaskState[] = [
  ...Array<TaskState>(10).fill('PARKED'),
  ...Array<TaskState>(8).fill('LEASED'),
  ...Array<TaskState>(6).fill('DISPATCHED'),
  ...Array<TaskState>(10).fill('RUNNING'),
  ...Array<TaskState>(6).fill('SUCCEEDED'),
]

const taskId = (i: number) => `task_${(i + 1).toString(16).padStart(20, '0')}`

function task(i: number, state: TaskState): Task {
  return {
    id: taskId(i),
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-3600),
    updated_at: iso(-60),
    started_at: state === 'RUNNING' || state === 'SUCCEEDED' ? iso(-600) : null,
    completed_at: state === 'SUCCEEDED' ? iso(-100) : null,
    submitted_by: 'priya@example.com',
    attempt_count: state === 'RUNNING' || state === 'SUCCEEDED' ? 1 : 0,
    max_attempts: 3,
    park_reason: state === 'PARKED' ? 'QUOTA_EXHAUSTED' : null,
    blocked_by: null,
    workflow_id: 'wf_big',
    step_id: `s${i}`,
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
  } as Task
}

function step(i: number): WorkflowStep {
  // Four stages of ten, each stage after the one before.
  const stage = Math.floor(i / 10)
  return {
    step_id: `s${i}`,
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    depends_on: stage === 0 ? [] : [`s${(stage - 1) * 10}`],
    input_from: {},
    task_id: taskId(i),
  }
}

function board(): WorkflowBoard {
  const steps = STATES.map((_, i) => step(i))
  const wf: Workflow = {
    workflow_id: 'wf_big',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-3600),
    updated_at: iso(-60),
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  } as Workflow
  return {
    workflows: [wf],
    taskById: new Map(STATES.map((s, i) => [taskId(i), task(i, s)] as const)),
    statesDetail: null,
  }
}

const fetched: string[] = []

/** The digests route's answer for the ids asked: each wait entered 7m ago, each task one attempt. */
function digests(url: URL): Response {
  const ids = (url.searchParams.get('ids') ?? '').split(',').filter(Boolean)
  const include = (url.searchParams.get('include') ?? '').split(',')
  const rows = ids.map((id) => {
    const i = Number.parseInt(id.slice('task_'.length), 16) - 1
    const state = STATES[i]!
    return {
      task_id: id,
      state,
      ...(include.includes('entered') ? { entered_at: ['PARKED', 'LEASED', 'DISPATCHED'].includes(state) ? iso(-420) : null } : {}),
      ...(include.includes('usage')
        ? {
            usage_read: 'ok',
            usage: {
              attempts: 1, attempts_with_cost: 1, attempts_with_tokens: 1, checkpoints: 0, cost_usd: 0.25,
              input_tokens: 1000, output_tokens: 100, cache_read_input_tokens: null, cache_creation_input_tokens: null,
            },
          }
        : {}),
    }
  })
  return new Response(JSON.stringify({ tenant_id: 'eng', digests: rows }), { status: 200, headers: { 'content-type': 'application/json' } })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  fetched.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input instanceof Request ? input.url : input), 'http://console.test')
      fetched.push(url.pathname + url.search)
      if (url.pathname.endsWith('/v1/tasks/digests')) return digests(url)
      return new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } })
    }),
  )
  const b = board()
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: b, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflow.mockImplementation(ownFromBoard(async () => ({ status: 'ok', data: board(), fetchedAt: T0 })))
  api.loadTasks.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadOlderWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

const perStep = (kind: 'events' | 'attempts') => fetched.filter((u) => new RegExp(`/v1/tasks/[^/?]+/${kind}(\\?|$)`).test(u))

describe('a 40-step workflow page reads its steps in one batched read', () => {
  it('runs against the stubbed fetch, not the fixture build', () => {
    expect(USE_FIXTURES).toBe(false)
  })

  it('makes no per-step events or attempts read, and a bounded number of requests in all', async () => {
    render(<WorkflowsScreen view="wf=wf_big" />)
    // The graph tab draws the stage grid now; the old canvas is gone from the page.
    await waitFor(() => expect(document.querySelector('.sg')).toBeTruthy())
    // Let the page's step reads go out and settle.
    await waitFor(() => expect(fetched.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 300))

    expect(perStep('events').length, `one events page per waiting step:\n${perStep('events').join('\n')}`).toBe(0)
    // The ONE step whose inspector is open reads its own attempts
    // (`StepInspector`); no other step does.
    expect(perStep('attempts').length, `one attempts read per step:\n${perStep('attempts').join('\n')}`).toBeLessThanOrEqual(1)
    // Both of the page's step reads went out, batched: the waits and the usage.
    const batched = fetched.filter((u) => u.includes('/v1/tasks/digests'))
    expect(batched.some((u) => u.includes('include=entered'))).toBe(true)
    expect(batched.some((u) => u.includes('include=usage'))).toBe(true)
    expect(batched.length).toBeLessThanOrEqual(2)
    // Every request the page made names at most a handful of routes, never
    // one per step: 40 steps, and no route was asked for more than twice.
    const byRoute = new Map<string, number>()
    for (const u of fetched) {
      const shape = u.split('?')[0]!.replace(/task_[0-9a-f]+/g, '{id}')
      byRoute.set(shape, (byRoute.get(shape) ?? 0) + 1)
    }
    for (const [shape, n] of byRoute) expect(n, `${shape} was read ${n} times`).toBeLessThanOrEqual(2)
    expect(fetched.length, fetched.join('\n')).toBeLessThan(12)
  })

  it('asks for every waiting step at once, and only them, for their times', async () => {
    render(<WorkflowsScreen view="wf=wf_big" />)
    await waitFor(() => expect(fetched.some((u) => u.includes('include=entered'))).toBe(true))
    const asked = fetched.find((u) => u.includes('include=entered'))!
    const ids = new URL(asked, 'http://console.test').searchParams.get('ids')!.split(',')
    const waits = STATES.flatMap((s, i) => (['PARKED', 'LEASED', 'DISPATCHED'].includes(s) ? [taskId(i)] : []))
    expect([...ids].sort()).toEqual([...waits].sort())
  })
})

/**
 * THE GRAPH'S STEP INSPECTOR IS DRAWN ABOVE THE STAGE CARDS (#2688; the
 * owner's browser, 2026-11-02: the inspector was drawn under the stage
 * cards). The canvas paints four layers (styles.css, over `.wf-levels`): the
 * cards at auto, `.wf-band`, `.node-stop` at 2, and the sticky rail and a
 * focused card's full note at 3. The docked panel was `z-index: auto`, so
 * every numbered layer of the canvas painted over it wherever the two met.
 * Rendered with the real stylesheet: the panel's layer is above every layer
 * inside the canvas column, at the docked widths (1280-1512) and as the
 * phone's bottom sheet.
 */
describe('the Graph’s step inspector sits above the stage cards', () => {
  const z = (el: Element) => {
    const v = Number.parseInt(getComputedStyle(el).zIndex, 10)
    return Number.isFinite(v) ? v : 0
  }

  it('docked: the panel’s layer is above every layer the canvas draws', async () => {
    // This is a CANVAS behaviour (the `WorkflowCard`'s own panel beside its own
    // graph): the redesigned page no longer draws the canvas at all, so the
    // card is rendered directly, as the brief for the Graph Work redesign
    // instructs for anything that is a property of the canvas rather than of
    // the page's stage grid.
    const b = board()
    const wf = b.workflows[0]!
    render(
      <WorkflowCard
        workflow={wf}
        taskById={b.taskById}
        usage={{ kind: 'ready', usage: null }}
        // s0's stage (index 0) has to be expanded, or the card draws only a
        // band summary and no node to click.
        openStages={{ [stageKey(wf.workflow_id, 0)]: true }}
        onToggleStage={() => {}}
        reload={() => {}}
        page
      />,
    )
    fireEvent.click(document.querySelector('.node[data-step="s0"]')!)
    await waitFor(() => expect(document.querySelector('.wf-split.has-panel > .wf-panel .wf-inspect')).toBeTruthy())
    // The canvas's layers are read from the stylesheet's own z-index rules
    // that reach a canvas element, the highest of them: an upper bound on
    // what any element paints at. `getComputedStyle` on each of the canvas's
    // 415 elements walks the 600 KB sheet every time in jsdom, and ran this
    // case past its 30 s budget.
    const sheet = document.createElement('style')
    sheet.textContent = STYLES
    document.head.appendChild(sheet)
    const layers: { sel: string; z: number }[] = []
    const collect = (rules: CSSRuleList) => {
      for (const r of [...rules]) {
        if (r instanceof CSSStyleRule) {
          const v = Number.parseInt(r.style.getPropertyValue('z-index'), 10)
          if (Number.isFinite(v)) layers.push({ sel: r.selectorText, z: v })
        } else if ('cssRules' in r) collect((r as CSSGroupingRule).cssRules)
      }
    }
    collect(sheet.sheet!.cssRules)
    const reaches = (el: Element, sel: string) => {
      try {
        return el.matches(sel)
      } catch {
        return false
      }
    }
    try {
      const panel = document.querySelector('.wf-split.has-panel > .wf-panel')!
      expect(getComputedStyle(panel).position).toBe('sticky')
      const canvas = [...document.querySelectorAll('.wf-split.has-panel > .wf-split-main *')]
      expect(canvas.length).toBeGreaterThan(40)
      const drawn = layers.filter((l) => canvas.some((el) => reaches(el, l.sel)))
      const top = Math.max(0, ...drawn.map((l) => l.z), ...canvas.map((el) => Number.parseInt((el as HTMLElement).style?.getPropertyValue('z-index') ?? '', 10) || 0))
      expect(top, 'the canvas paints no numbered layer: the check would prove nothing').toBeGreaterThan(0)
      expect(z(panel), `the panel paints at ${z(panel)}, under the canvas's layer ${top}`).toBeGreaterThan(top)
    } finally {
      sheet.remove()
    }
  })

  it('on a phone: the bottom sheet is above every canvas layer too', () => {
    // jsdom does not apply media queries, so the phone rule is read from the sheet.
    const phone = STYLES.slice(STYLES.indexOf('@media (max-width: 699px) {\n  .wf-split.has-panel { display: block; }'))
    const sheet = phone.slice(0, phone.indexOf('\n}\n'))
    const zs = /\.wf-panel \{[^}]*z-index: (\d+)/.exec(sheet)
    expect(zs, 'no z-index on the phone sheet').not.toBeNull()
    const canvasLayers = [...STYLES.matchAll(/^\.(?:wf-levels|node-note-full|node-stop|wf-band)\b[^{]*\{[^}]*?z-index: (\d+)/gm)].map((m) => Number(m[1]))
    expect(canvasLayers.length).toBeGreaterThan(0)
    expect(Number(zs![1])).toBeGreaterThan(Math.max(...canvasLayers))
  })
})
