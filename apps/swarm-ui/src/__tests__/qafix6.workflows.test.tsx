// QAFIX6-WORKFLOWS (part of #1680; visual QA run 6, wf_31aca00c977e4d37a935,
// the fixture build of main at d3cdbd62, 2026-10-23, 1440 and 390 px, light
// and dark, and the boxes earlier runs left open). Each block names its item,
// its run-6 finding and its #1680 comment:
//
//   a  F1                        the expanded stage 'covered' cold-start and
//                                fencing: the arrival scroll centred the
//                                picked card and slid the first two under the
//                                sticky rail, cutting the band's red line to
//                                'was interrupted: …', which named no step.
//   b  F2, 6298221306            'checkpoints 2' (a count named like the
//                                step), the exit said twice, the failure
//                                sentence three times, 'exit 0' beside 'why
//                                it failed'.
//   c  F3                        'claude-code ×6' and 'Steps done 4/6' said
//                                neither what the 6 counts nor what became of
//                                the other two steps.
//   d  F4, 6298221770            three routes to 'changes', none naming the
//                                step.
//   e  F5, 6343766227            a finished step's Changes said 'yet', '0+'
//                                and 'not measured not collected'.
//   f  6343767840                unknown, loading and zero cost.
//   g  6343794378                the policy chip read as what happened.
//   i  6283740207 (QA-120)       the failed step on arrival.
//
// Every assertion names the mutation that turns it red.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import type { WorkflowView } from '../stepviews'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { attempt } from './runfixture'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, WorkflowCard } = await import('../Workflows')
const { WorkflowChangesTab } = await import('../WorkflowChanges')

const T0 = Date.parse('2026-10-23T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: iso(-500),
    completed_at: iso(-100),
    submitted_by: 'qa@example.com',
    attempt_count: 3,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_fan',
    step_id: null,
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
    current_generation: 3,
    current_lease_id: null,
    ...over,
  }
}

const ERROR = 'exit status 2: pytest was interrupted: 1 error during collection'
const FAN = ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero']

/** The fixture's fan-in: five steps, `checkpoints` failed, then `synthesis` on all five, cancelled. */
function fan(over: (id: string) => Partial<Task> = () => ({})): { w: Workflow; tasks: Map<string, Task> } {
  const steps = [...FAN.map((id) => step(id, [], { task_id: `t_${id}` })), step('synthesis', FAN, { task_id: 't_synthesis' })]
  const tasks = new Map<string, Task>([
    ...FAN.map((id): [string, Task] => [
      `t_${id}`,
      task(`t_${id}`, id === 'checkpoints' ? 'FAILED' : 'SUCCEEDED', {
        step_id: id,
        last_error: id === 'checkpoints' ? ERROR : null,
        ...over(id),
      }),
    ]),
    ['t_synthesis', task('t_synthesis', 'CANCELLED', { step_id: 'synthesis', started_at: null, attempt_count: 0, current_generation: 0 })],
  ])
  const w: Workflow = {
    workflow_id: 'wf_fan',
    state: 'FAILED',
    tenant_id: 'eng',
    stored_state: 'FAILED',
    state_source: 'derived',
    rollup: {
      state: 'FAILED',
      complete: true,
      reason: 'terminal_worst_first',
      counts: { SUCCEEDED: 4, FAILED: 1, CANCELLED: 1 },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: 6,
    },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'qa@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
  return { w, tasks }
}

function serve(f: { w: Workflow; tasks: Map<string, Task> }): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [f.w], taskById: f.tasks, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

/** The run-6 fixture's attempts: three, each exiting 2 with the step's error. */
function attemptsExiting(code: number, error: string | null): void {
  api.loadAttempts.mockResolvedValue({
    status: 'ok',
    fetchedAt: T0,
    data: {
      attempts: [
        attempt(1, { exit_code: code, error }),
        attempt(2, { exit_code: code, error }),
        attempt(3, { exit_code: code, error, cost_usd: 0.253, checkpoints: ['ckpt-00001', 'ckpt-00002'] }),
      ],
    },
  })
}

const hosts: HTMLElement[] = []
const restore: (() => void)[] = []

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  serve(fan())
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  attemptsExiting(2, ERROR)
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: vi.fn(), configurable: true, writable: true })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  atWidth(realWidth)
  for (const h of hosts.splice(0)) h.remove()
  for (const r of restore.splice(0).reverse()) r()
})

/** Shadow a layout property on HTMLElement for this test only. */
function stubBox(prop: 'clientWidth' | 'scrollWidth' | 'offsetWidth', of: (el: HTMLElement) => number): void {
  Object.defineProperty(HTMLElement.prototype, prop, {
    configurable: true,
    get(this: HTMLElement) {
      return of(this)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop]
  })
}

/** jsdom's `scrollLeft` ignores writes; this one keeps them. */
function keepScroll(): void {
  const at = new WeakMap<HTMLElement, number>()
  Object.defineProperty(HTMLElement.prototype, 'scrollLeft', {
    configurable: true,
    get(this: HTMLElement) {
      return at.get(this) ?? 0
    },
    set(this: HTMLElement, v: number) {
      at.set(this, v)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollLeft
  })
}

/**
 * An IntersectionObserver double: every element is in view except the failure
 * banner, which is in view or not as `bannerInView` says.
 */
function observeBanner(bannerInView: boolean): void {
  class IO {
    constructor(private readonly cb: IntersectionObserverCallback) {}
    observe(el: Element) {
      const isIntersecting = el.classList.contains('wfp-fail-lead') ? bannerInView : true
      queueMicrotask(() => this.cb([{ target: el, isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver))
    }
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return []
    }
  }
  const before = (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver
  ;(globalThis as { IntersectionObserver?: unknown }).IntersectionObserver = IO
  restore.push(() => {
    ;(globalThis as { IntersectionObserver?: unknown }).IntersectionObserver = before
  })
}

async function page(): Promise<void> {
  render(<WorkflowsScreen view="wf=wf_fan" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

const panel = () => document.querySelector<HTMLElement>('.wf-panel')
const flat = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ')
const count = (hay: string, needle: string) => hay.split(needle).length - 1
const WIDE: CascadeEnv = { width: 1440 }

const realWidth = window.innerWidth
function atWidth(px: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

/**
 * THE CANVAS, RENDERED DIRECTLY (the Graph Work redesign, part of #2688): the
 * page no longer draws `WorkflowCard`'s graph or picks a step on arrival, but
 * the card itself still carries the arrival-pick behaviour run 6 found bugs
 * in, so these cases render it the way the old workflow page used to --
 * `page`, with the failed step already picked -- rather than through
 * `WorkflowsScreen`, which draws the stage grid and the inspector instead.
 */
function PageCard({
  initialPicked = 'checkpoints' as string | null,
  onView,
}: { initialPicked?: string | null; onView?: (id: string, v: WorkflowView) => void } = {}) {
  const f = fan()
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [picked, setPicked] = useState<string | null>(initialPicked)
  return (
    <WorkflowCard
      workflow={f.w}
      taskById={f.tasks}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      picked={picked}
      onPick={(_id, stepId) => setPicked((p) => (p === stepId ? null : stepId))}
      view="graph"
      onView={onView}
      page
      reload={() => {}}
    />
  )
}

async function openPanel(): Promise<HTMLElement> {
  render(<PageCard />)
  await waitFor(() => expect(panel()?.querySelector('.wf-inspect-id')?.textContent).toBe('checkpoints'))
  // The attempt facts land after the panel.
  await waitFor(() => expect(panel()!.querySelector('.wf-inspect-facts')).toBeTruthy())
  return panel()!
}

/** The real page, the checkpoints chip picked from the grid: what the Graph
 *  tab shows once a reader opens a step (there is no arrival pick any more). */
async function openOnPage(): Promise<HTMLElement> {
  atWidth(1440)
  await page()
  const chip = await waitFor(() => {
    const b = document.querySelector<HTMLButtonElement>('.sg [data-step="checkpoints"]')
    expect(b, 'no checkpoints chip on the stage grid').toBeTruthy()
    return b!
  })
  fireEvent.click(chip)
  const dock = await waitFor(() => {
    const d = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Step checkpoints"]')
    expect(d, 'the inspector did not open on checkpoints').toBeTruthy()
    return d!
  })
  await waitFor(() => expect(dock.querySelector('.wf-inspect-facts')).toBeTruthy())
  return dock
}

// ---------------------------------------------------------------------------
// a. F1: the expanded stage covers no card, and its red line names its step
// ---------------------------------------------------------------------------

describe('a. F1: the expanded stage', () => {
  it('holds no step card, sits above its cards, and begins its error line with the step’s name', async () => {
    // F1 is a canvas behaviour, and the canvas is no longer drawn by the page
    // (graph-workflow-rest: the Graph tab is the stage grid); `WorkflowCard`
    // still carries it, rendered the way the old workflow page used to.
    render(<PageCard />)
    await waitFor(() => expect(document.querySelector('.wfp-fail-lead')).toBeTruthy())
    // The page's own pick leaves its stage folded (visual QA pass 2,
    // overview-agents-63, #2694); the failure line's control opens it.
    const go = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')
      expect(b, 'the failure line has no control to open its step').toBeTruthy()
      return b!
    })
    act(() => {
      fireEvent.click(go)
    })
    const band = await waitFor(() => {
      const b = document.querySelector<HTMLElement>('.wf-canvas .wf-band.is-opened')
      expect(b, 'the failed step’s stage did not open when the reader asked').toBeTruthy()
      return b!
    })
    expect(band.querySelector('.node-slot, .node'), 'a step card is inside the band').toBeNull()
    // Its own row: the band ends above every card of its stage.
    const bottom = Number.parseFloat(band.style.top) + Number.parseFloat(band.style.height)
    const cards = [...document.querySelectorAll<HTMLElement>('.wf-canvas .wf-stage .node-slot')]
    expect(cards.length).toBe(FAN.length)
    for (const c of cards) expect(Number.parseFloat(c.style.top)).toBeGreaterThan(bottom)
    // MUTATION: print `lead.first.text` bare in `stageCause` -- the band's
    // line reads 'exit status 2: …' and, cut, names no step.
    const lines = [...band.querySelectorAll<HTMLElement>('.wf-band-cause')]
    expect(lines.length).toBeGreaterThan(0)
    for (const l of lines) expect(l.textContent).toMatch(new RegExp(`^(${FAN.join('|')}): `))
  })

  it('opens on the stage’s first card, not with cold-start and fencing under the rail', async () => {
    // The 1440 page with the card docked: a ~770px wrapper, the 72px rail.
    keepScroll()
    stubBox('clientWidth', (el) => (el.classList.contains('wf-canvas-wrap') ? 770 : 0))
    stubBox('scrollWidth', (el) => (el.classList.contains('wf-canvas-wrap') ? 72 + Number.parseFloat(el.querySelector<HTMLElement>('.wf-canvas')?.style.width ?? '0') : 0))
    stubBox('offsetWidth', (el) => (el.classList.contains('wf-levels') ? 72 : 0))
    render(<PageCard />)
    await waitFor(() => expect(document.querySelector('.wfp-fail-lead')).toBeTruthy())
    // The reader's open, not the arrival, brings the stage round (overview-agents-63).
    const go = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')
      expect(b, 'the failure line has no control to open its step').toBeTruthy()
      return b!
    })
    act(() => {
      fireEvent.click(go)
    })
    const wrap = await waitFor(() => {
      const w = document.querySelector<HTMLElement>('.wf-canvas-wrap')
      expect(w?.querySelector('.wf-band.is-opened')).toBeTruthy()
      return w!
    })
    const left = wrap.scrollLeft
    const cards = [...wrap.querySelectorAll<HTMLElement>('.wf-stage .node-slot')]
    // MUTATION: centre the picked card again in the arrival scroll -- the
    // wrapper opens at ~228px and cold-start (x 10) and fencing (x 176) are
    // under the rail.
    for (const c of cards) {
      expect(Number.parseFloat(c.style.left) - left, `${c.textContent?.slice(0, 12)} starts under the rail`).toBeGreaterThanOrEqual(0)
    }
    // And the picked card is whole in view.
    const picked = cards.find((c) => c.querySelector('[data-step="checkpoints"]'))!
    const right = 72 + Number.parseFloat(picked.style.left) + Number.parseFloat(picked.style.width) - left
    expect(right).toBeLessThanOrEqual(770)
  })
})

// ---------------------------------------------------------------------------
// b. F2, 6298221306: the inspector's words
// ---------------------------------------------------------------------------

describe('b. F2: the inspector says each thing once, in words', () => {
  it('labels the count "saved checkpoints" and leaves no bare ckpts or gen', async () => {
    const p = await openPanel()
    const labels = [...p.querySelectorAll('.wf-inspect-facts .ctl-fact > b')].map((b) => b.textContent)
    // MUTATION: label `ckpts` 'checkpoints' in `FACT_LABEL` -- beside the
    // step called checkpoints, 'checkpoints 2' reads as the step.
    expect(labels).toContain('saved checkpoints')
    expect(flat(p)).not.toMatch(/\bckpts\b|\bgen\b/)
  })

  it('states the exit once: no "error exit status 2" after "exit code 2"', async () => {
    const p = await openPanel()
    const labels = [...p.querySelectorAll('.wf-inspect-facts .ctl-fact > b')].map((b) => b.textContent)
    // MUTATION: keep the `error` fact when its line is the exit status the
    // exit code already states.
    expect(labels).not.toContain('error')
    expect(count(flat(p.querySelector('.wf-inspect-facts')), 'exit status 2')).toBe(0)
    expect(flat(p.querySelector('.wf-inspect-facts'))).toMatch(/exit code ?2/)
  })

  it('prints the failure sentence once outside the banner while the banner is in view', async () => {
    observeBanner(true)
    const p = await openPanel()
    const banner = document.querySelector<HTMLElement>('.wfp-fail-lead')!
    expect(count(flat(banner), ERROR)).toBe(1)
    // MUTATION: drop the `leadInView` branch in `InspectorSlot` -- the
    // banner and the card both print the whole sentence.
    await waitFor(() => expect(count(flat(p), ERROR)).toBe(0))
    expect(within(p).getByRole('heading', { level: 4, name: 'Why it failed' })).toBeTruthy()
  })

  it('prints it in the inspector, once, when the banner is off screen', async () => {
    observeBanner(false)
    const p = await openPanel()
    // MUTATION: suppress the cause whenever a banner exists, seen or not.
    await waitFor(() => expect(count(flat(p), ERROR)).toBe(1))
  })

  it('never draws "exit 0" beside "why it failed"', async () => {
    attemptsExiting(0, null)
    const p = await openPanel()
    expect(within(p).getByRole('heading', { level: 4, name: 'Why it failed' })).toBeTruthy()
    // MUTATION: print the exit fact as-is for a failed step -- 'exit code 0'
    // reads as the step having succeeded.
    expect(flat(p)).not.toMatch(/exit (code ?)?0\b/)
    expect(flat(p.querySelector('.wf-inspect-facts'))).toMatch(/agent process ?ended cleanly · the step failed on what it reported/)
  })
})

// ---------------------------------------------------------------------------
// c. F3: the list row says what it counts
// ---------------------------------------------------------------------------

describe('c. F3: the list row', () => {
  it('reads "claude-code · 6 steps" and "4/6 · 1 failed · 1 cancelled"', async () => {
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('.wfg-table tr[data-workflow="wf_fan"]')
      expect(r).toBeTruthy()
      return r!
    })
    // Runners and Steps done moved from the row into the inspector's summary
    // the row opens (the redesign, frame graph-workflows-rest).
    fireEvent.click(row.querySelector<HTMLElement>('[data-col="state"]')!)
    const sum = document.querySelector<HTMLElement>('aside[aria-label="Workflow inspector"] .wfg-sum')!
    expect(sum).toBeTruthy()
    // MUTATION: print `×{m.count}` again in `Mix`.
    const chip = sum.querySelector<HTMLElement>('.wfg-sum-dl .c-chip')!
    expect(flat(chip)).toBe('claude-code · 6 steps')
    // MUTATION: drop the outcomes from `StepsDone` -- the summary reads '4/6'.
    expect(flat(sum.querySelector('.wfg-sum-row')).trim()).toBe('4/6 · 1 failed · 1 cancelled')
  })
})

// ---------------------------------------------------------------------------
// d. F4, 6298221770: one control leads to the step's changes, and names it
// ---------------------------------------------------------------------------

describe('d. F4: one name per scope', () => {
  it('has exactly one control naming the step and its changes, and the banner’s button no longer says changes', async () => {
    // The page itself has no Changes tab inside a bare WorkflowCard -- that
    // chrome is `WfTabs`, drawn by the page around the card -- so "the tab
    // stays the workflow's" is read off `onView` instead: opening the step's
    // card from the banner must never ask the page to change its view.
    const onView = vi.fn()
    render(<PageCard onView={onView} />)
    await waitFor(() => expect(panel()?.querySelector('.wf-inspect-id')?.textContent).toBe('checkpoints'))
    const named = [...screen.getAllByRole('link'), ...screen.getAllByRole('button')]
      .map((el) => el.getAttribute('aria-label') ?? el.textContent ?? '')
      .filter((n) => n.includes('checkpoints') && /changes/i.test(n))
    // MUTATION: put back 'This step’s changes in this workflow ›' -- no
    // control names the step.
    expect(named).toHaveLength(1)
    const lead = screen.getByRole('region', { name: 'Why step checkpoints failed' })
    // MUTATION: keep ', changes' on the banner's button while the card is open.
    expect(within(lead).getByRole('button').textContent).not.toMatch(/changes/i)
    // The tab stays the workflow's.
    expect(onView).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// e. F5, 6343766227: a finished step's empty Changes
// ---------------------------------------------------------------------------

function changes(stepId: string | null) {
  const f = fan()
  render(<WorkflowChangesTab workflow={f.w} taskById={f.tasks} at={{ step: stepId, file: null }} onAt={() => {}} />)
}

describe('e. F5: a finished step’s empty Changes', () => {
  it('says the failed step’s file list was not recorded: no "yet", no "0+", one column', async () => {
    changes('checkpoints')
    const table = await screen.findByRole('table', { name: 'Files by step' })
    await waitFor(() => expect(flat(document.querySelector('.chg-mx'))).toMatch(/file list not recorded/))
    const text = flat(document.querySelector('.chg-mx'))
    // MUTATION: put back 'no file known yet' for a step that has ended.
    expect(text).not.toMatch(/\byet\b/)
    // MUTATION: print `${n}+` when no step's list is known.
    expect(text).not.toMatch(/(^|\D)0\+/)
    // No '0 files' total with no list known, since QA1-FIX-29 (overview-agents-76).
    expect(text).not.toMatch(/(^|\D)0 files/)
    expect(text).toContain('File lists not collected for any step')
    // Still one column for the selected step (QAFIX5, L6).
    expect(within(table).getAllByRole('columnheader').map((th) => th.getAttribute('data-step') ?? 'File')).toEqual(['File', 'checkpoints'])
  })

  it('says "not measured" and "not collected" as two labelled facts, and "the files of checkpoints"', async () => {
    changes('checkpoints')
    const th = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('th[data-step="checkpoints"]')
      expect(el?.querySelectorAll('.chg-col-fact').length).toBe(2)
      return el!
    })
    const facts = [...th.querySelectorAll<HTMLElement>('.chg-col-fact')].map(flat)
    // MUTATION: run the chip and the word together again in `ColumnSay`.
    expect(facts[0]).toMatch(/^patch not measured$/)
    expect(facts[1]).toMatch(/^file list not collected$/)
    const note = screen.getByRole('note', { name: 'What this page shows' })
    // MUTATION: put back `Only ${label}’s files`.
    expect(note.textContent).toMatch(/Only the files of checkpoints/)
    expect(note.textContent).not.toMatch(/checkpoints’s/)
  })
})

// ---------------------------------------------------------------------------
// f. 6343767840: unknown, loading and zero cost
// ---------------------------------------------------------------------------

describe('f. the head’s cost: reading, then not reported, then the figure', () => {
  // THE COST FIGURE IS NO LONGER A DEDICATED `.wfp-cost` CHIP (the redesign):
  // the head's line holds it as one of three `.wfp-fig`s (steps, spend,
  // age), so the cost is picked out by what it says rather than its class.
  const headCost = () => {
    const figs = [...document.querySelectorAll<HTMLElement>('.wfp-head .wfp-fig')]
    return flat(figs.find((el) => /reading|not reported|\$/.test(el.textContent ?? '')) ?? null)
  }

  it('says "reading" while the read is in flight', async () => {
    api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
    await page()
    // MUTATION: drop the `reading` branch in `WorkflowHead`.
    expect(headCost()).toMatch(/reading/)
    expect(headCost()).not.toMatch(/not reported|\$/)
  })

  it('says "not reported" once the read has answered without a cost', async () => {
    await page()
    await waitFor(() => expect(headCost()).toMatch(/not reported/))
    expect(headCost()).not.toMatch(/reading|\$0\.00/)
  })

  it('prints the figure once a step has reported one, and labels the panel’s cost as the step’s own', async () => {
    serve(fan((id) => (id === 'cold-start' ? { result_summary: { runner: { usage: { total_cost_usd: 1.21 } } } } : {})))
    // NO ARRIVAL PICK ON THE PAGE any more: the reader opens checkpoints
    // themselves, from the stage grid, to see its own attempt's cost.
    const dock = await openOnPage()
    await waitFor(() => expect(headCost()).toMatch(/\$1\.21/))
    const p = dock.querySelector<HTMLElement>('.wf-inspect-facts')!
    // MUTATION: drop the `cost` hint in `FACT_HINT` -- '$0.253' beside the
    // head's '$1.21' reads as a second figure for the workflow.
    expect(flat(p)).toMatch(/this attempt’s cost ?\$0\.253 · this step’s own, not the workflow’s/)
  })
})

// ---------------------------------------------------------------------------
// g. 6343794378: the policy chip
// ---------------------------------------------------------------------------

describe('g. the policy chip reads as configuration', () => {
  it('carries a Policy label and is not the state chip’s sibling', async () => {
    // THE POLICY IS NO LONGER A CHIP IN THE HEAD (the redesign): it reads as
    // configuration in the Overview, behind its own "On a failed step" row
    // and a Policy toggle -- nowhere beside the state mark any more.
    atWidth(1440)
    await page()
    const head = document.querySelector<HTMLElement>('.wfp-head')!
    // OVERVIEW-AGENTS-66 (visual QA pass 1, part of #2596): 'on failure: fail
    // the workflow' was jargon. MUTATION: put the old chip back in the head
    // and this finds it beside the state mark.
    expect(within(head).queryByText(/if a step fails/i)).toBeNull()
    const ov = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"]')!
    const row = within(ov).getByText('On a failed step')
    expect(row.nextElementSibling?.textContent).toBe('stop the workflow')
    const details = within(ov).getByText('Policy').closest('details')!
    // MUTATION: drop the "configuration, not an outcome" sentence -- the
    // policy reads as what happened again, exactly the bug this guards.
    expect(flat(details)).toMatch(/A failed step stops the workflow: no step after it starts\. This is what the workflow was submitted with: configuration, not an outcome\./)
  })

  it('says a workflow submitted to continue keeps going when a step fails', async () => {
    atWidth(1440)
    const f = fan()
    serve({ ...f, w: { ...f.w, on_step_failure: 'CONTINUE' } })
    await page()
    const ov = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"]')!
    // MUTATION: swap the two branches and "On a failed step" reads 'stop the workflow'.
    expect(within(ov).getByText('On a failed step').nextElementSibling?.textContent).toBe('keep going')
  })
})

// ---------------------------------------------------------------------------
// i. QA-120 (6283740207): the failed step on arrival
// ---------------------------------------------------------------------------

describe('i. QA-120: the failed step on arrival', () => {
  it('opens its card on arrival, and a click on the selected card keeps it open', async () => {
    await openPanel()
    // Its stage is folded on arrival (overview-agents-63): the selected step
    // is the band's pressed chip.
    const chip = [...document.querySelectorAll<HTMLButtonElement>('.wf-band-chips button')].find((b) => b.textContent?.includes('checkpoints'))!
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    act(() => {
      fireEvent.click(chip)
    })
    // MUTATION: drop the open-first branch in `pickStep` -- the first click
    // on the arrival pick closes its card.
    expect(panel(), 'the click closed the card').not.toBeNull()
  })

  it('cuts the card’s reason at a word: one clamped line that wraps at spaces', () => {
    const note = build('div.wf-canvas div.node-slot button.node div.node-note', hosts)
    // MUTATION: drop the `.wf-canvas .node-note` clamp in workflows.css --
    // `nowrap` + ellipsis cuts 'pytest coll…' mid-word.
    expect(painted(note, 'white-space', WIDE)).toBe('normal')
    expect(painted(note, '-webkit-line-clamp', WIDE)).toBe('1')
    expect(painted(note, 'display', WIDE)).toBe('-webkit-box')
  })
})
