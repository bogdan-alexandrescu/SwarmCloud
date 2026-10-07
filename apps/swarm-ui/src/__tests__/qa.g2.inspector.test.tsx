/**
 * THE AGENT INSPECTOR, FROM THE QA PASS ON DEPLOYED DEV (2026-10-07, group G2).
 *
 *   G2-01  At 390x844 the header (y 0-373) and the sticky stat strip (h 282)
 *          left about 150px of the screen to scroll.
 *   G2-03  One running task, four elapsed figures: header, Elapsed tile, the
 *          Now card's `run` chip and the unlabelled Progress total.
 *   G2-08  A parked task's heartbeat said `queued · not dispatched`.
 *   G2-09  Staged inputs headed a size `Arrived`.
 *   G2-10  The Outcome, Input and strategy `?` opened Submit-side or unrelated
 *          topics.
 *   G2-11  `Peak memory … at exit` read as the value at exit.
 *   G2-12  After success the pointer's reclaimed checkpoint was an amber
 *          warning (the badge's `0 of 3` is qa.u11a.agent.test.tsx's D21).
 *   G2-13  A parked task's tile cut `waiting 19…`.
 *   G2-14  The Progress total wrapped to `19m / 15s+`.
 *   G2-20  The phone header began lines with orphan middots.
 *   G2-23  Escape on the split (re-verified: already on main).
 *   G2-33  The Outputs file list was stacked cards at 1440.
 *   G2-34  The Inputs prompt was a second scroller that took the wheel.
 *
 * jsdom has no layout engine, so the layout findings are asserted through
 * the shipped sheets' cascade (`painted`) and the structure that produces
 * them, as details.v3.test.tsx does.
 *
 * MUTATIONS, one per block: make the strip sticky at any pane under 640px
 * again, or drop the phone column's `overflow-y: auto`; drop `SplitClock`
 * from `Run`, or the `liveRun` branch from the run chip; put `queued` back
 * as the not-dispatched word; head the staged column `Arrived`; point a card
 * back at `attempt-documents`, `input-is-opaque` or `dispatch-strategies`;
 * put `at exit` back alone; make the finished pointer line `untrusted`;
 * print `waiting 19m` as the value; put the 64px column back; put the
 * separator back on `::before`; drop `is-roomy`; give the prompt its 40vh
 * scroller back. Each turns a case red.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { revealTop } from '../ArtifactViewer'
import { HELP } from '../help'
import type { AgentRun, ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { CheckpointsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { Run, SplitClock } = await import('../AgentDetail')
const { livenessOf } = await import('../Liveness')
const { StagedInputs } = await import('../StagedInputs')
const { RunFiles } = await import('../RunFiles')
const { PromptText } = await import('../Artifacts')
const { resetListSnap } = await import('../listSnap')
const { formatDuration } = await import('../types')

const ID = 'task_0123456789abcdef0123'
const MIN = 60_000
const PHONE: CascadeEnv = { width: 390, height: 844, container: 360 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

/** An instant `m` minutes before `from`. */
function ago(m: number, from = Date.now()): string {
  return new Date(from - m * MIN).toISOString()
}

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    created_at: ago(15),
    started_at: ago(14),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
    workflow_id: 'wf_a',
    step_id: 'refactor-backoff',
    account: null,
    ...over,
  } as Partial<Task>)
}

function runOf(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: running(),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

const hosts: HTMLElement[] = []
function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

/** What a sighted reader sees: the text without a help card's hidden description or its `?`. */
function seen(el: Element | null): string {
  if (el === null) return ''
  const c = el.cloneNode(true) as Element
  for (const n of c.querySelectorAll('[data-help-description], button[aria-expanded], [role="tooltip"]')) n.remove()
  return (c.textContent ?? '').replace(/\s+/g, ' ').trim()
}

function tile(el: HTMLElement, label: RegExp): { label: string; value: string; sub: string } {
  const cell = [...el.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].find((c) => label.test(seen(c.querySelector('.dt-sc-l'))))
  expect(cell, `no ${label} tile`).toBeTruthy()
  return { label: seen(cell!.querySelector('.dt-sc-l')), value: seen(cell!.querySelector('.dt-sc-v')), sub: seen(cell!.querySelector('.dt-sc-s')) }
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

function Routed({ start, onGo }: { start: Pane; onGo?: (to: string) => void }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    onGo?.(to)
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

async function split(start: Pane, t: Task, onGo?: (to: string) => void): Promise<HTMLElement> {
  api.loadTask.mockResolvedValue(ok(t))
  render(<Routed start={start} onGo={onGo} />)
  await waitFor(() => expect(document.querySelector('.ag-head-facts > li')).not.toBeNull())
  return document.querySelector<HTMLElement>('.ag-split')!
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  delete document.documentElement.dataset.agentList
})

describe('G2-01: on a phone the inspector scrolls, and the strip sticks only where there is room', () => {
  it('sticks the strip in a 480-639px pane on a 700px-tall window, and nowhere smaller', () => {
    const strip = host(runOf()).querySelector<HTMLElement>('.dt-strip')!
    expect(painted(strip, 'position', { width: 1440, height: 900, container: 600 })).toBe('sticky')
    expect(painted(strip, 'top', { width: 1440, height: 900, container: 600 })).toBe('0')
    // The phone: a 360px pane on a 390x844 screen. It scrolls with the rest.
    expect(painted(strip, 'position', PHONE) ?? 'static', 'the strip sticks on a phone').toBe('static')
    // A short window: no room to hold 282px of figures in view.
    expect(painted(strip, 'position', { width: 1440, height: 600, container: 600 }) ?? 'static').toBe('static')
    // Two columns from 640px, as before: nothing sticks there.
    expect(painted(strip, 'position', { width: 1440, height: 900, container: 700 }) ?? 'static').toBe('static')
  })

  it('makes the phone column the one scroller, so the header scrolls away and the tabs stay', async () => {
    const el = await split('attempts', running())
    const pane = el.querySelector<HTMLElement>(':scope > .ag-split-pane')!
    const tabs = el.querySelector<HTMLElement>(':scope > .ag-tabs-edge')!
    const head = el.querySelector<HTMLElement>(':scope > .ag-head')!
    expect(painted(el, ['overflow-y', 'overflow'], PHONE), 'the phone column does not scroll').toBe('auto')
    expect(painted(pane, ['overflow-y', 'overflow'], PHONE) ?? 'visible', 'the pane is a second scroller').not.toMatch(/auto|scroll/)
    expect(painted(head, 'position', PHONE) ?? 'static', 'the header is held in view').toBe('static')
    expect(painted(tabs, 'position', PHONE)).toBe('sticky')
    expect(painted(tabs, 'top', PHONE)).toBe('0')
    // At 1440 the pane is still the one scroller (details.v3.test.tsx).
    const wide: CascadeEnv = { width: 1440, container: 700 }
    expect(painted(el, ['overflow-y', 'overflow'], wide)).toBe('hidden')
    expect(painted(pane, ['overflow-y', 'overflow'], wide)).toBe('auto')
  })

  it('leaves the Logs tab its own scroller on a phone, under a column that does not move', async () => {
    const el = await split('logs', running())
    expect(el.classList.contains('on-logs')).toBe(true)
    expect(painted(el, ['overflow-y', 'overflow'], PHONE)).toBe('hidden')
  })
})

describe('G2-01: a file opened on a phone is brought into view by the column that scrolls', () => {
  // The phone column scrolls and the pane is `overflow: visible`: moving the
  // pane's scrollTop would do nothing, and the viewer would open below the fold.
  function column(paneOverflow: string): { split: HTMLElement; pane: HTMLElement; tabs: HTMLElement; viewer: HTMLElement } {
    const { container } = render(
      <div className="ag-split" style={{ overflowY: 'auto' }}>
        <div className="ag-tabs-edge" style={{ position: 'sticky' }} />
        <div className="ag-split-pane" style={{ overflowY: paneOverflow as 'auto' }}>
          <div className="art-viewer" />
        </div>
      </div>,
    )
    const split = container.querySelector<HTMLElement>('.ag-split')!
    const pane = container.querySelector<HTMLElement>('.ag-split-pane')!
    const tabs = container.querySelector<HTMLElement>('.ag-tabs-edge')!
    const viewer = container.querySelector<HTMLElement>('.art-viewer')!
    split.getBoundingClientRect = () => ({ top: 0 }) as DOMRect
    pane.getBoundingClientRect = () => ({ top: 400 }) as DOMRect
    tabs.getBoundingClientRect = () => ({ top: 0, height: 88 }) as DOMRect
    viewer.getBoundingClientRect = () => ({ top: 900 }) as DOMRect
    split.scrollTop = 0
    pane.scrollTop = 0
    return { split, pane, tabs, viewer }
  }

  it('scrolls the column, less the sticky tabs, when the pane does not scroll', () => {
    const { split, pane, viewer } = column('visible')
    revealTop(viewer)
    expect(split.scrollTop, 'the phone column was not scrolled to the viewer').toBe(900 - 0 - 88 - 8)
    expect(pane.scrollTop).toBe(0)
  })

  it('still scrolls only the pane where the pane is the scroller', () => {
    const { split, pane, viewer } = column('auto')
    revealTop(viewer)
    expect(pane.scrollTop).toBe(900 - 400 - 8)
    expect(split.scrollTop, 'the column moved under a pane that scrolls').toBe(0)
  })
})

describe('G2-03: one running task, one elapsed figure', () => {
  // A heartbeat two minutes old: the run chip used to stop there (`run 12m`)
  // while the tile ticked on to 14m.
  function live(t0: number): AgentRun {
    const t = running({ created_at: ago(15, t0), started_at: ago(14, t0) })
    return runOf({
      task: t,
      attempts: [attempt(1, { attempt_id: 'att_1', created_at: ago(14, t0), started_at: ago(14, t0), completed_at: null, exit_code: null })],
      events: [ev('lease_acquired', ago(15, t0), 'att_1'), ev('running', ago(14, t0), 'att_1'), ev('heartbeat', ago(2, t0), 'att_1')],
    })
  }

  it('draws the tile, the run chip and the Progress run at the split’s one instant', () => {
    const t0 = Date.now()
    const r = live(t0)
    // The pane's own read is ten minutes old: alone, it would cap the clock.
    const { container } = render(
      <SplitClock.Provider value={t0}>
        <Run run={r} headed reading={{ fetchedAt: t0 - 10 * MIN, pollMs: 10_000 }} />
      </SplitClock.Provider>,
    )
    const el = container as HTMLElement
    const want = formatDuration(14 * MIN)
    expect(tile(el, /^Elapsed/).value).toBe(want)
    expect(el.querySelector('.dt-phase.is-cur')?.textContent).toBe(`run ${want}`)
    // Progress: queue 0 + cold 1m + run 14m, measured to now, so no `+`.
    const rows = [...el.querySelectorAll<HTMLElement>('.dt-progress .dt-phrow:not(.is-head)')]
    expect(rows.at(-1)!.querySelector('b')?.textContent).toBe(formatDuration(15 * MIN))
  })

  it('labels the Progress total as including the queue', () => {
    const el = host(live(Date.now()))
    const headRow = el.querySelector('.dt-progress .dt-phrow.is-head')
    expect(headRow, 'the Progress total is unlabelled').not.toBeNull()
    expect(seen(headRow)).toBe('total incl. queue')
  })

  it('prints the header and the Elapsed tile at the same instant in the split', async () => {
    const t = running()
    api.loadAgentRun.mockResolvedValue(ok(runOf({ task: t })))
    await split('attempts', t)
    const list = screen.getByRole('tablist', { name: 'Agent panes' })
    fireEvent.click(
      within(list)
        .getAllByRole('tab')
        .find((b) => b.querySelector('.c-tab-label')?.textContent === 'Details')!,
    )
    const strip = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.ag-split .dt-strip')
      expect(s).not.toBeNull()
      return s!
    })
    const header = document.querySelector('.ag-head-hl b')?.textContent
    expect(header).toMatch(/^1[34]m \d+s$/)
    expect(tile(strip.parentElement!, /^Elapsed/).value).toBe(header)
  })
})

describe('G2-08: the heartbeat names the state it is in', () => {
  it('says `parked`, `ready` or `queued` with `not dispatched`, never `queued` for all three', () => {
    for (const state of ['PARKED', 'READY', 'QUEUED'] as const) {
      const l = livenessOf(running({ state, started_at: null }), [], Date.now())
      expect(l.kind).toBe('not-started')
      expect(`${l.word} · ${l.copy}`).toBe(`${state.toLowerCase()} · not dispatched`)
    }
  })
})

describe('G2-09: the staged-inputs column is headed by what it holds', () => {
  it('heads a size `Size`, as the Artifacts tab does', () => {
    const t = running({
      metadata: { input_from: { t_plan: 'plan.md' } },
      result_summary: { staged_inputs: [{ task_id: 't_plan', filename: 'plan.md', path: 'plan.md', bytes: 17 * 1024 }] },
    })
    render(<StagedInputs task={t} />)
    const heads = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(heads).toEqual(['File', 'From', 'Size'])
    expect(screen.queryByText('Arrived')).toBeNull()
    const cell = screen.getByRole('rowheader', { name: 'plan.md' }).closest('tr')!.querySelector('td[data-label="Size"]')
    expect(cell?.textContent).toContain('17 KiB')
  })
})

describe('G2-10: each card’s `?` opens a topic about what it shows', () => {
  const helpOf = (card: Element | null) =>
    [...(card?.querySelectorAll('.dt-card-head button[aria-label^="Help: "]') ?? [])].map((b) => b.getAttribute('aria-label'))

  it('Outcome opens `outcome-explained`, Input opens `input-as-submitted`', () => {
    const t = running({ state: 'SUCCEEDED', completed_at: ago(1), result_summary: { artifacts: [] } })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1) })] }))
    expect(helpOf(el.querySelector('.dt-now[data-lead="outcome"]'))).toEqual([`Help: ${HELP['outcome-explained'].title}`])
    expect(helpOf(el.querySelector('.dt-input'))).toEqual([`Help: ${HELP['input-as-submitted'].title}`])
  })

  it('the strategy chips open `strategy-on-a-task`', async () => {
    await split('attempts', running({ dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] } }))
    const chips = document.querySelector('.ag-head-dispatch')!
    expect(chips.querySelector('button[aria-label^="Help: "]')?.getAttribute('aria-label')).toBe(`Help: ${HELP['strategy-on-a-task'].title}`)
  })

  it('the read-side topics speak of no form and no typing', () => {
    for (const id of ['outcome-explained', 'input-as-submitted', 'strategy-on-a-task'] as const) {
      const text = [HELP[id].short, ...HELP[id].long].join(' ')
      expect(text, id).not.toMatch(/\bform\b|you type|on the option/i)
    }
  })
})

describe('G2-11: the memory tile says its figure is a peak', () => {
  it('reads `of 8 GiB · peak, written at exit` on a finished attempt', () => {
    const t = running({ state: 'SUCCEEDED', completed_at: ago(1) })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1), peak_rss_bytes: 28 * 1024 ** 2 })] }))
    const mem = tile(el, /^Peak memory/)
    expect(mem.sub).toBe('of 8 GiB · peak, written at exit')
  })
})

describe('G2-12: a checkpoint reclaimed after the task finished is not a warning', () => {
  const PREFIX = `tenants/eng/tasks/${ID}/attempts/`
  const POINTER = `gs://swarm-artifacts/${PREFIX}att_1/checkpoints/ckpt-00003/`
  function page(): CheckpointsPage {
    return {
      task_id: ID,
      tenant_id: 'eng',
      prefix: PREFIX,
      checkpoints: [],
      count: 0,
      total_found: 0,
      next_page_token: null,
      listed: true,
      truncated: false,
      latest_checkpoint: { pointer: POINTER, status: 'missing', checkpoint_id: 'ckpt-00003' },
    } as CheckpointsPage
  }
  async function finding(t: Task): Promise<HTMLElement> {
    api.loadCheckpoints.mockResolvedValue(ok(page()))
    const { container } = render(<RunFiles task={t} attempts={[attempt(1, { checkpoints: ['ckpt-00001', 'ckpt-00002', 'ckpt-00003'] })]} />)
    return waitFor(() => {
      const p = [...container.querySelectorAll<HTMLElement>('p.rollup')].find((x) => x.textContent?.includes(POINTER))
      expect(p).toBeTruthy()
      return p!
    })
  }

  it('says `reclaimed after the task finished`, in the neutral tone, on a succeeded task', async () => {
    const p = await finding(running({ state: 'SUCCEEDED', completed_at: ago(1), latest_checkpoint: POINTER }))
    expect(p.classList.contains('untrusted'), 'a finished task’s retention reads as a fault').toBe(false)
    expect(p.textContent).toMatch(/Reclaimed after the task finished\.$/)
  })

  it('keeps the amber line while the task can still run', async () => {
    const p = await finding(running({ state: 'PARKED', latest_checkpoint: POINTER }))
    expect(p.classList.contains('untrusted')).toBe(true)
  })
})

describe('G2-13: a wait is named by the label, so its figure is never cut', () => {
  it('a never-started parked task: label `Waiting`, value the duration alone', () => {
    const t = running({ state: 'PARKED', started_at: null, created_at: ago(19), attempt_count: 0 })
    const el = host(runOf({ task: t, attempts: [] }))
    const w = tile(el, /^Waiting/)
    expect(w.label).toBe('Waiting')
    expect(w.value).toMatch(/^19m \d+s$/)
  })
})

describe('G2-14: the Progress total never wraps', () => {
  it('sizes its last column to the figure and keeps the figure on one line', () => {
    const el = host(runOf({ attempts: [attempt(1, { created_at: ago(20), started_at: ago(19), completed_at: ago(1) })] }))
    const row = el.querySelector<HTMLElement>('.dt-progress .dt-phrow:not(.is-head)')!
    const cols = painted(row, 'grid-template-columns', { width: 1440, container: 700 })!
    expect(cols.trim().split(/\s+(?![^(]*\))/).at(-1)).toMatch(/max-content/)
    expect(painted(row.querySelector('b')!, 'white-space', { width: 1440, container: 700 })).toBe('nowrap')
  })
})

describe('G2-20: a wrapped line of the header never starts with a middot', () => {
  it('puts each separator after its fact, never before one', async () => {
    const t = running({
      account: { status: 'assigned', account_id: 'eng:team', provider: 'anthropic', attempt_id: 'att_1', generation: 1, swapped_from: null, swaps: [] },
    } as Partial<Task>)
    await split('attempts', t)
    const facts = [...document.querySelectorAll<HTMLElement>('.ag-head-facts > li:not(.is-nodot)')]
    expect(facts.length).toBeGreaterThanOrEqual(3)
    const env: CascadeEnv = PHONE
    facts.forEach((li, i) => {
      expect(painted(li, 'content', env, 'before') ?? 'none', `fact ${i} opens with a separator`).toBe('none')
      const after = painted(li, 'content', env, 'after')
      if (i < facts.length - 1) expect(after, `fact ${i} has no separator after it`).toBe('"·"')
      else expect(after ?? 'none', 'the last fact trails a separator').toBe('none')
    })
    // The `?` is held to the last dispatch chip, so it never sits alone.
    const last = document.querySelector('.ag-head-dispatch .ag-head-dlast')!
    expect(last.querySelector('.ag-head-dchip')).not.toBeNull()
    expect(last.querySelector('button[aria-label^="Help: "]')).not.toBeNull()
    expect(painted(last, 'white-space', env)).toBe('nowrap')
  })
})

describe('G2-23: Escape on the split closes it (re-verified on main, AgentSplit.tsx onKeyDown)', () => {
  it('returns to the list the split was opened from', async () => {
    const go = vi.fn()
    const el = await split('attempts', running(), go)
    el.focus()
    fireEvent.keyDown(el, { key: 'Escape' })
    expect(go).toHaveBeenCalledWith('work/running/live')
  })
})

describe('G2-33: the Outputs file list is a table where the pane has room', () => {
  function table(): HTMLElement {
    const el = document.createElement('div')
    el.className = 'ag-split-pane'
    el.innerHTML =
      '<div class="ctl-table is-stacked is-roomy"><table role="table"><thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">File</th><th scope="col" class="is-num">Size</th><th scope="col">Get</th></tr></thead>' +
      '<tbody role="rowgroup"><tr role="row"><th role="rowheader" scope="row">out.md</th><td role="cell" data-label="Size" class="is-num">2 KiB</td><td role="cell" data-label="Get">view</td></tr></tbody></table></div>'
    document.body.appendChild(el)
    hosts.push(el)
    return el
  }

  it('is a table in a 700px pane at 1440 and stacked records under 480px', () => {
    const el = table()
    const t = el.querySelector('table')!
    const td = el.querySelector('td')!
    const wide: CascadeEnv = { width: 1440, container: 700 }
    expect(painted(t, 'display', wide)).toBe('table')
    expect(painted(td, 'display', wide)).toBe('table-cell')
    expect(painted(td, 'content', wide, 'before') ?? 'none').toBe('none')
    expect(painted(el.querySelector('thead')!, 'clip-path', wide) ?? 'none').toBe('none')
    const narrow: CascadeEnv = { width: 1440, container: 400 }
    expect(painted(t, 'display', narrow)).toBe('block')
  })

  it('is what the Artifacts tab’s file list carries', () => {
    const src = readFileSync(join(__dirname, '..', 'Artifacts.tsx'), 'utf8')
    const at = src.indexOf('scope="col">Get</th>')
    expect(at).toBeGreaterThan(0)
    const open = src.lastIndexOf('<div className="ctl-table', at)
    expect(src.slice(open, src.indexOf('>', open))).toContain('is-roomy')
  })
})

describe('G2-34: the Inputs prompt is not a second scroller', () => {
  it('clamps a long prompt with `Show all`, and scrolls inside nothing', () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join('\n')
    const { container } = render(
      <div className="ag-split-pane">
        <PromptText text={long} />
      </div>,
    )
    const pre = container.querySelector<HTMLElement>('pre.arts-prompt')!
    const env: CascadeEnv = { width: 1440, container: 700 }
    expect(pre.classList.contains('is-clamped')).toBe(true)
    expect(painted(pre, ['overflow-y', 'overflow'], env) ?? 'visible').not.toMatch(/auto|scroll/)
    expect(painted(pre, 'max-height', env) ?? 'none').toBe('none')
    const more = within(container as HTMLElement).getByRole('button', { name: 'Show all' })
    fireEvent.click(more)
    expect(pre.classList.contains('is-all')).toBe(true)
    expect(painted(pre, ['overflow-y', 'overflow'], env) ?? 'visible').not.toMatch(/auto|scroll/)
  })

  it('offers no `Show all` for a prompt that fits', () => {
    const { container } = render(<PromptText text="Fix the flaky test." />)
    expect(within(container as HTMLElement).queryByRole('button', { name: 'Show all' })).toBeNull()
  })
})
