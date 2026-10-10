/**
 * #113, WHAT WAS LEFT AFTER THE LIST'S SKELETON (workflow.readability.test.tsx
 * pins the list's toolbar, head and rows, and the shared `.skeleton` bar).
 *
 *  - ONE WORKFLOW'S PAGE (`#work/workflows?wf=...`, the board) drew the
 *    generic full-width `SkeletonRows` while it read, then its head row -- the
 *    chips, Copy link and Cancel workflow -- and its view tabs appeared and
 *    pushed the body down. It now draws that head and those tabs during the
 *    load, the actions disabled, and a body card where the board's will be.
 *  - THE SHARED CARD SKELETON (`CardSkeleton`, every lazily read card) drew
 *    its bars as `.ctl-pending`, whose fill and sweep both sit on
 *    `--surface-2`: about 1.07:1 against the light card. The issue said it in
 *    as many words -- swapping the class alone will not fix contrast -- so the
 *    bar's own step changes, to the shared `.skeleton`'s.
 */
import STYLES from '../styles.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, Workflow, WorkflowStep } from '../types'
import { cascade } from './cssgate'
import { colour, contrast, resolveSheet, resolveVars, tokenTables, type RGBA } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')
const { CardSkeleton } = await import('../CardSkeleton')

const T0 = Date.parse('2026-10-01T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: iso(-300),
    completed_at: null,
    submitted_by: 'priya@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_one',
    step_id: 'plan',
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
    ...over,
  }
}

function fixture(): WorkflowBoard {
  const steps = [step('plan', [], { task_id: 't_plan' }), step('core', ['plan'])]
  const wf: Workflow = {
    workflow_id: 'wf_one',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'priya',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
  return { workflows: [wf], taskById: new Map([['t_plan', task('t_plan')]]), statesDetail: null }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockResolvedValue({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function Routed({ initial, onView }: { initial: string; onView?: (v: string) => void }) {
  const [view, setView] = useState(initial)
  return (
    <WorkflowsScreen
      view={view}
      onView={(v) => {
        onView?.(v)
        setView(v)
      }}
    />
  )
}

const tabLabels = (root: ParentNode) =>
  [...within(root as HTMLElement).getByRole('navigation', { name: 'Views of this workflow' }).querySelectorAll('.c-tab-label')].map(
    (s) => s.textContent,
  )

describe('#113: one workflow’s page draws its head and tabs while it reads', () => {
  it('draws the head row with its actions disabled, the view tabs and a body card, then the page in the same places', async () => {
    let land: (r: Result<WorkflowBoard>) => void = () => {}
    api.loadWorkflowBoard.mockReturnValue(new Promise<Result<WorkflowBoard>>((r) => (land = r)))
    render(<Routed initial="wf=wf_one&tab=table" />)

    const page = document.querySelector<HTMLElement>('.wfp.is-loading')
    expect(page, 'no page frame while loading').toBeTruthy()
    expect(page!.getAttribute('aria-busy')).toBe('true')
    // Not the generic full-width rows: the page's own geometry.
    expect(document.querySelectorAll('.skeleton')).toHaveLength(0)

    const order = [...page!.children].map((c) => c.className.split(' ')[0])
    expect(order).toEqual(['wfp-head', 'c-tabs', 'wfp-skel-body'])
    const head = page!.querySelector<HTMLElement>('.wfp-head')!
    const actions = within(head).getAllByRole('button')
    expect(actions.map((b) => b.textContent)).toEqual(['Copy link', 'Cancel workflow'])
    expect(actions.every((b) => (b as HTMLButtonElement).disabled)).toBe(true)
    // The chips are bars, not words: a skeleton answers nothing.
    const chips = head.querySelector('.wfp-chips')!
    expect(chips.querySelectorAll('.wfl-skel').length).toBeGreaterThan(0)
    expect(chips.textContent).toBe('')
    // The tabs are the address's, the current one already marked; no count
    // is drawn for a read that has not landed.
    expect(tabLabels(page!)).toEqual(['Graph', 'Table', 'Timeline', 'Changes'])
    expect(page!.querySelector('.c-tabs [aria-current="page"] .c-tab-label')!.textContent).toBe('Table')
    expect(page!.querySelector('.c-tabs em')).toBeNull()
    const body = page!.querySelector('.wfp-skel-body')
    expect(body, 'no body card where the board will be').toBeTruthy()
    expect(body!.getAttribute('aria-hidden')).toBe('true')
    // Not a `.wf-card`: everything that finds the loaded card keys on that.
    expect(document.querySelectorAll('.wf-card')).toHaveLength(0)
    expect(body!.querySelectorAll('.wfl-skel').length).toBeGreaterThan(0)

    land({ status: 'ok', data: fixture(), fetchedAt: T0 })
    await waitFor(() => expect(document.querySelector('.wfp.is-loading')).toBeNull())
    const loaded = document.querySelector<HTMLElement>('.wfp')!
    expect(loaded.className).toBe('wfp')
    expect([...loaded.children].map((c) => c.className.split(' ')[0]).slice(0, 2)).toEqual(['wfp-head', 'c-tabs'])
    expect(within(loaded.querySelector<HTMLElement>('.wfp-head')!).getByRole('button', { name: 'Copy link' })).not.toHaveProperty(
      'disabled',
      true,
    )
    expect(tabLabels(loaded)).toEqual(['Graph', 'Table', 'Timeline', 'Changes'])
  })

  it('lets a tab be chosen during the load: each view is an address, not a control the data drives', () => {
    api.loadWorkflowBoard.mockReturnValue(new Promise<Result<WorkflowBoard>>(() => {}))
    const views: string[] = []
    render(<Routed initial="wf=wf_one" onView={(v) => views.push(v)} />)
    fireEvent.click(screen.getByRole('link', { name: 'Timeline' }))
    expect(views.at(-1)).toMatch(/tab=timeline/)
    expect(document.querySelector('.wfp.is-loading .c-tabs [aria-current="page"] .c-tab-label')!.textContent).toBe('Timeline')
  })
})

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']
const SHEET_VARS = tokenTables(STYLES)
const SHEETS: Readonly<Record<Theme, string>> = {
  dark: resolveSheet(STYLES, 'dark'),
  light: resolveSheet(STYLES, 'light'),
}

function varColour(name: string, theme: Theme): RGBA {
  const c = colour(resolveVars(SHEET_VARS[theme].get(name) ?? '', SHEET_VARS[theme]))
  expect(c, `${name} does not resolve to a colour`).not.toBeNull()
  return c!
}

const at = (el: Element, prop: string, theme: Theme, reducedMotion = false) =>
  cascade(SHEETS[theme], el, prop, { width: 1440, theme, reducedMotion }).winner?.value ?? null

describe('#113: the shared card skeleton’s bars are visible in both themes', () => {
  it('draws its bars through the component, moving and static', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('reduce'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
    render(<CardSkeleton title="Retries" say="Retries is still being read." lines={[80, 60]} />)
    expect(document.querySelectorAll('.ol-skel-bar.is-static')).toHaveLength(2)
  })

  it.each(['ctl-pending', 'is-static'])('a `.ol-skel-bar.%s` clears 1.5:1 against the page and the card, as `.skeleton` does', (kind) => {
    render(
      <>
        <section className="ctl-card ol-card is-loading">
          <div className="ctl-card-body">
            <p className="ol-line ol-skel-line">
              <span className={`ol-skel-bar ${kind}`} />
            </p>
          </div>
        </section>
        <div className="skeleton ctl-skeleton-row" />
      </>,
    )
    const bar = document.querySelector('.ol-skel-bar')!
    const shared = document.querySelector('.skeleton')!
    for (const theme of THEMES) {
      const fill = colour(at(bar, 'background-color', theme) ?? '')
      expect(fill, `the card bar has no resolvable fill in ${theme}`).not.toBeNull()
      for (const ground of ['--bg', '--surface'] as const) {
        const ratio = contrast(fill!, varColour(ground, theme))
        expect(ratio, `${theme}: the ${kind} card bar against ${ground} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(1.5)
      }
      // `--ctl-pending` is an opaque gradient over `--surface-2`: left in
      // place it would paint over the fill above and undo it.
      const image = at(bar, 'background-image', theme) ?? 'none'
      if (kind === 'ctl-pending') {
        expect(image, `${theme}: the moving bar does not sweep the shared band`).toBe(at(shared, 'background-image', theme))
        expect(at(bar, 'animation', theme)).toMatch(/ctl-sweep/)
        expect(at(bar, 'animation', theme, true)).toBe('none')
      } else {
        expect(image).toBe('none')
      }
    }
  })
})

/**
 * #113, WHAT #740 LEFT: every other `.ctl-pending` loading bar. The artifact,
 * log, child, split and checkpoint panes draw `.art-loading-bar` and the
 * Activity ledger draws `.ol-pending`, each on `.ctl-pending`'s `--surface-2`
 * fill under an opaque `--ctl-pending` sweep of that same surface -- 1.21:1
 * in dark. They take the card bar's step, in one shared rule.
 */
const SOURCES = import.meta.glob<string>('../*.tsx', { query: '?raw', import: 'default', eager: true })

/** Every class drawn beside `ctl-pending` in a literal `className`, with the files that draw it. */
function pendingBars(): Map<string, string[]> {
  const found = new Map<string, string[]>()
  for (const [path, text] of Object.entries(SOURCES)) {
    for (const m of text.matchAll(/className="([^"]*)"/g)) {
      const classes = (m[1] ?? '').split(/\s+/)
      if (!classes.includes('ctl-pending')) continue
      for (const c of classes.filter((c) => c && c !== 'ctl-pending')) {
        const files = found.get(c) ?? []
        if (!files.includes(path)) files.push(path)
        found.set(c, files)
      }
    }
  }
  return found
}

/** Each loading bar, drawn in the parent it sits in on screen. */
const BARS = {
  'art-loading-bar': () => (
    <p className="art-loading">
      <span className="ctl-pending art-loading-bar" />
    </p>
  ),
  'ol-pending': () => (
    <div className="ol-body">
      <div className="ctl-pending ol-pending" aria-hidden="true" />
    </div>
  ),
} as const satisfies Readonly<Record<string, () => JSX.Element>>
type Bar = keyof typeof BARS

describe('#113: every `.ctl-pending` loading bar is visible in both themes', () => {
  it('the scan finds every loading bar the screens draw, and each one is measured below', () => {
    const found = pendingBars()
    expect([...found.keys()].sort()).toEqual(Object.keys(BARS).sort())
    expect(found.get('art-loading-bar')!.map((p) => p.replace('../', '')).sort()).toEqual([
      'AgentChildren.tsx',
      'AgentLogs.tsx',
      'AgentSplit.tsx',
      'ArtifactViewer.tsx',
      'Artifacts.tsx',
      'CheckpointBrowser.tsx',
      'WorkflowChanges.tsx',
    ])
    expect(found.get('ol-pending')).toEqual(['../Activity.tsx'])
  })

  it.each(Object.keys(BARS) as Bar[])('a `.%s` clears 1.5:1 against the page and the card, and sweeps the shared band', (name) => {
    render(
      <>
        {BARS[name]()}
        <div className="skeleton ctl-skeleton-row" />
      </>,
    )
    const bar = document.querySelector(`.${name}`)!
    const shared = document.querySelector('.skeleton')!
    for (const theme of THEMES) {
      const fill = colour(at(bar, 'background-color', theme) ?? '')
      expect(fill, `the ${name} bar has no resolvable fill in ${theme}`).not.toBeNull()
      for (const ground of ['--bg', '--surface'] as const) {
        const ratio = contrast(fill!, varColour(ground, theme))
        expect(ratio, `${theme}: the ${name} bar against ${ground} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(1.5)
      }
      expect(at(bar, 'background-image', theme), `${theme}: the ${name} bar does not sweep the shared band`).toBe(
        at(shared, 'background-image', theme),
      )
      expect(at(bar, 'animation', theme)).toMatch(/ctl-sweep/)
      expect(at(bar, 'animation', theme, true)).toBe('none')
    }
  })
})
