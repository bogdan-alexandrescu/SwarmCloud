// QA1-FIX-28, THE TIMELINE'S LANES PAGE (part of #2596, the console visual QA
// pass 1 epic, 2026-10-28):
//
//   runs-submit-10  the headline '91 % success, 272 of 300 decided Oct 6 – Oct
//          19' sat above lanes drawn over another span; the window followed
//          the figures, so they read as the lanes'. It now leads them.
//   runs-submit-11  at 390 '91 %' broke over two lines, '91' above '%'.
//   runs-submit-33 / chrome-C17  the fenced and parks cells said 'events not
//          read on 24 lanes' while the reads were still out, and once settled
//          printed two dashes, 'fenced — events not read on 332 lanes · parks
//          — events not read on 332 lanes'. Loading now says counting, and a
//          settled partial read is one cell that says how to get the rest.
//   runs-submit-35  a workflow's span was an outline with no bottom edge, and
//          its '4 of 5 steps have attempts…' line was cut at 390.
//   runs-submit-36  'Filters · 0' and 'Key' open looked the same as closed,
//          and Escape did not fold them.
//   runs-submit-37  'Profile all 0': the count of the profiles seen read as
//          zero profiles. 'all' carries no count; a narrowed filter names its picks.
//   runs-submit-38  the lane panel printed raw event kinds and 'gen 1' on every
//          row, and named the task and its workflow by id.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes } from '../outcomes'
import type { WorkflowRead } from '../api'
import type { AttemptRow, AttemptsPage, Task, TaskEvent, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = <T,>(httpStatus: number, message: string): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message } as never,
})
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

const TASK = {
  id: 'task_solo', tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
  priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
  submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
  workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
  current_generation: 1, current_lease_id: null,
} as unknown as Task
const STEP = { ...TASK, current_generation: 2, attempt_count: 2, id: 'task_fan_0', step_id: 'fan_0', workflow_id: 'wf_5e5ad3b6f7da4299a839', metadata: { unit: 'Audit the operator scripts' } } as unknown as Task
const attempt = (taskId: string) =>
  ({
    attempt_id: `att_${taskId}_1`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: 'l1',
    backend: 'cloud_run', execution_name: null, created_at: ago(280), started_at: ago(279), completed_at: ago(100), exit_code: 0,
    error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
    output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
  }) as unknown as AttemptRow
const page = (attempts: AttemptRow[]) =>
  ({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: null, coverage: { scope: 'page' } }) as AttemptsPage
const ev = (taskId: string, type: string, min: number, generation: number | null = 1): TaskEvent => ({
  event_id: `${taskId}_${type}_${min}`, task_id: taskId, type, at: ago(min), attempt_id: `att_${taskId}_1`, lease_id: 'l1', generation, detail: null,
})
const WF = {
  workflow: { workflow_id: STEP.workflow_id, tenant_id: 'eng', state: 'SUCCEEDED', steps: [{ step_id: 'fan_0', task_id: STEP.id, depends_on: [] }] },
  tasks: [STEP],
} as unknown as WorkflowRead

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadTaskEventsPage.mockImplementation(async () => ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [TASK] } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockImplementation(async (id: string) => (id === STEP.workflow_id ? ok(WF) : failed(404, `no ${id}`)))
  // A ledger over the 24 h span: since QA2-FIX-05 (runs2-04) one over
  // another window shows no figures.
  api.loadOutcomes.mockResolvedValue(ok({ ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: ago(24 * 60), until: ago(0), generated_at: ago(0) }))
  api.loadAttemptsPage.mockResolvedValue(ok(page([attempt('task_solo')])))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const strip = () => document.querySelector('.tl-strip') as HTMLElement
const cells = () => [...document.querySelectorAll('.tl-strip1 > .tl-x')]
const cell = (re: RegExp) => cells().find((el) => re.test(text(el))) ?? null
const summary = (label: string) => {
  const box = within(document.body).getByRole('group', { name: 'Timeline span and filters' })
  return text([...box.querySelectorAll<HTMLElement>('details.tl-pick > summary')].find((el) => el.textContent?.startsWith(label)) ?? null)
}

describe('runs-submit-10: the headline names its window before its figures', () => {
  it('shows no figure over a ledger outside the span, and the figure first over one that matches', async () => {
    api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = cell(/not this span, read for/)
      expect(el).not.toBeNull()
      return el!
    })
    // MUTATION: print the ledger's figures over another window and the line
    // reads '91 % success' as the span's (superseded by QA2-FIX-05, runs2-04).
    expect(text(head)).toBe('outcomes — not this span, read for Sep 12 – Sep 25')
    cleanup()

    const same: Outcomes = { ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: ago(24 * 60), until: ago(0), generated_at: ago(0) }
    api.loadOutcomes.mockResolvedValue(ok(same))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const plain = await waitFor(() => {
      const el = cell(/% success/)
      expect(el).not.toBeNull()
      return el!
    })
    // MUTATION: lead with 'Outcomes' whatever the window, and the span's own figures read as another's.
    expect(text(plain)).toMatch(/^\d+ % success/)
  })
})

describe('runs-submit-11: a figure and its unit never wrap apart', () => {
  it('keeps the headline figure on one line and lets the sentence wrap under it at 390', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = cell(/% success/)
      expect(el).not.toBeNull()
      return el!
    })
    const fig = [...head.querySelectorAll('b')].find((b) => /%$/.test(text(b)))!
    // MUTATION: drop `white-space: nowrap` from the strip's figures and '91' sits above '%'.
    expect(painted(fig, 'white-space', PHONE)).toBe('nowrap')
    // MUTATION: drop the cell's `flex-wrap: wrap` and the sentence squeezes into a column beside the figure.
    expect(painted(head, 'flex-wrap', PHONE)).toBe('wrap')
  })
})

describe('runs-submit-33 / chrome-C17: loading says counting; a partial read is one cell that says how to get the rest', () => {
  it('says counting while the attempts read is out, in one cell', async () => {
    api.loadAttemptsPage.mockImplementation(() => new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(text(strip())).toMatch(/% success/))
    // MUTATION: keep 'fenced — reading the lanes' and 'parks — reading the lanes', two dashes that read as a failure.
    await waitFor(() => expect(cell(/fenced/)).not.toBeNull())
    expect(text(cell(/fenced/))).toBe('fenced and parks — counting…')
    expect(text(strip())).not.toMatch(/reading the lanes/)
  })

  it('says counting, not “events not read”, while a lane’s events read is out', async () => {
    api.loadTaskEventsPage.mockImplementation(() => new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(cell(/fenced/)?.getAttribute('aria-busy')).toBe('true'))
    // MUTATION: say 'events not read on 1 lane' while the read is out, and loading reads as a failure.
    expect(text(strip())).not.toMatch(/events not read/)
    expect(text(cell(/fenced/))).toBe('fenced and parks — counting…')
  })

  it('once the reads settle short, says it in one cell, of how many, and how to read the rest', async () => {
    api.loadTaskEventsPage.mockResolvedValue(failed(500, 'boom'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(text(strip())).toMatch(/not counted/))
    // MUTATION: print a fenced cell and a parks cell each 'not counted on N lanes' and this is red: two dashes.
    expect(cells().filter((c) => /not counted/.test(text(c)))).toHaveLength(1)
    expect(document.querySelectorAll('.tl-strip1 .tl-dash')).toHaveLength(1)
    // MUTATION: drop 'of M' or the way to read the rest, and the reader cannot tell a short read from a broken page.
    // QA2-FIX-05 (runs2-15): 'not counted', not 'events not read'.
    expect(text(cell(/not counted/))).toBe('fenced and parks — not counted on 1 of 1 lane · open a lane to count')
  })
})

describe('runs-submit-35: a workflow’s span is a closed bar, and its summary wraps at 390', () => {
  it('draws the span with four edges and lets the group’s note wrap on a phone', () => {
    document.body.innerHTML =
      '<div class="tl-row is-group"><div class="tl-lab"><small>4 of 5 steps have attempts in this span · running</small></div><div class="tl-trk"><span class="tl-wf-span"></span></div></div>'
    const span = document.querySelector('.tl-wf-span')!
    // MUTATION: put back `border-bottom: 0` and the span is an open bracket again.
    expect(painted(span, 'border-bottom', WIDE)).toBeNull()
    expect(painted(span, 'border', WIDE)).toMatch(/solid/)
    // MUTATION: leave the 4px 4px 0 0 radius and the bottom corners are square under a rounded top.
    expect(painted(span, 'border-radius', WIDE)).toBe('4px')
    // MUTATION: drop border-box and a span reaching now pushes its right edge past the track's clip.
    expect(painted(span, 'box-sizing', WIDE)).toBe('border-box')
    const note = document.querySelector('.tl-lab small')!
    // MUTATION: leave the group note on `nowrap` at 390 and '4 of 5 steps have attempts in this span · ru…' is cut.
    expect(painted(note, 'white-space', PHONE)).toBe('normal')
    // The desktop row keeps its one line, with the whole note in its title.
    expect(painted(note, 'white-space', WIDE)).toBe('nowrap')
    document.body.innerHTML = ''
  })
})

describe('runs-submit-36: an open fold looks pressed, and Escape folds it', () => {
  it('styles an expanded Filters and Key toggle apart from a closed one', () => {
    for (const cls of ['tl-sheet-toggle', 'tl-key-toggle']) {
      document.body.innerHTML = `<button class="${cls}" aria-expanded="false"></button><button class="${cls}" aria-expanded="true"></button>`
      const [closed, open] = [...document.querySelectorAll('button')]
      // MUTATION: drop the `[aria-expanded='true']` rule and open looks the same as closed.
      expect(painted(open!, 'background', PHONE), cls).not.toBe(painted(closed!, 'background', PHONE))
      expect(painted(open!, 'color', PHONE), cls).toBe('var(--info)')
    }
    document.body.innerHTML = ''
  })

  it('folds the open sheet and the key on Escape, and hands focus back to its toggle', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // The toggles are a phone's only (display: none on a wide screen), so they are found by class.
    for (const cls of ['tl-sheet-toggle', 'tl-key-toggle']) {
      const toggle = await waitFor(() => {
        const el = document.querySelector<HTMLButtonElement>(`.${cls}`)
        expect(el).not.toBeNull()
        return el!
      })
      fireEvent.click(toggle)
      expect(toggle.getAttribute('aria-expanded')).toBe('true')
      fireEvent.keyDown(document.body, { key: 'Escape' })
      // MUTATION: give the folds no Escape layer and they stay open.
      expect(toggle.getAttribute('aria-expanded')).toBe('false')
      // MUTATION: close without focusing the toggle and focus is lost to the page.
      expect(document.activeElement).toBe(toggle)
    }
  })
})

describe('runs-submit-37: an unnarrowed filter says “all”, with no count', () => {
  it('reads “Profile all” and “State all” once the lanes are read, and names its picks when narrowed', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(text(strip())).toMatch(/0 generations fenced/))
    // MUTATION: summarise as `all ${options.length}` and this reads 'Profile all 1': a count that reads as profiles.
    expect(summary('Profile')).toBe('Profile all')
    expect(summary('State')).toBe('State all')
    cleanup()
    render(<TimelineLanesScreen view="profile=claude-code" onView={() => {}} />)
    await waitFor(() => expect(summary('Profile')).toBe('Profile claude-code'))
  })
})

describe('runs-submit-38: the lane panel says events in words, its generation once, and names the task and workflow', () => {
  it('words the kinds, prints gen only where it differs, and names the workflow', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [STEP] } as TaskPage))
    api.loadAttemptsPage.mockResolvedValue(ok(page([attempt(STEP.id)])))
    api.loadTaskEventsPage.mockResolvedValue(
      ok({ events: [ev(STEP.id, 'checkpoint_completed', 150, 2), ev(STEP.id, 'lease_acquired', 160, 2), ev(STEP.id, 'failed', 200, 1)], next_page_token: null }),
    )
    // A lane opens through the address (`lane=`), as a click on its name asks.
    render(<TimelineLanesScreen view={`lane=${STEP.id}`} onView={() => {}} />)
    const panel = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-detail')
      expect(el?.querySelectorAll('.tl-evr').length).toBe(3)
      return el!
    })
    const rows = [...panel.querySelectorAll('.tl-evr')].map(text)
    // MUTATION: print `eventKind(e)` as it came and these read 'checkpoint_completed', 'lease_acquired'.
    expect(rows.join(' | ')).toMatch(/checkpoint completed/)
    expect(rows.join(' | ')).toMatch(/lease acquired/)
    expect(rows.join(' | ')).not.toMatch(/_/)
    // MUTATION: print 'gen N' on every row and the current generation repeats on each.
    expect(rows.filter((r) => /gen 2/.test(r))).toHaveLength(0)
    expect(rows.filter((r) => /gen 1/.test(r))).toHaveLength(1)
    const sub = text(panel.querySelector('.tl-card-head + .tl-q'))
    // The generation once, in the header line.
    expect(sub).toMatch(/generation 2/)
    // MUTATION: print `lane.taskId` and `t.workflow_id` in the line and the panel names them by id.
    expect(sub).not.toMatch(/task_fan_0|wf_5e5ad3b6f7da4299a839/)
    expect(sub).toMatch(/workflow Audit the operator scripts/)
  })
})
