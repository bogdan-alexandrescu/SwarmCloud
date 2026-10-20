// LIVE QA, WORK › AGENTS, THE AGENT (2026-10-11, swarm.example.com).
//
// WA-3 (S2): a failed merge that ran twice -- parked for CI once, then failed
// -- gave four attempt numbers: `attempt 1 of 10` (header), `after 1 of 10
// attempts` (failure card), `2 of 10 · task counts 1` (Attempt card) and
// `Attempts 1` (tab), beside an Attempts tab listing two. `attempt_count` is
// what is charged against the cap; the attempt documents are the runs. Every
// place that says how many times it ran now says 2.
//
// WA-6 (S2): a task stopped from the console was drawn as an error the agent
// wrote: `Error written by agent, at finish`, in the red error box. It now
// says who asked for the stop, and the recorded text is a note.
//
// WA-12 (S3): `2of 10` on the Attempt card. WA-14 (S3): Copy link said
// nothing. WA-16 (S3): Home/End did nothing on the splitter. WA-17 (S3): the
// overlay was a dialog without `aria-modal`. WA-21 (S3): the detail's tab
// links were `#work/…` hashes.
//
// MUTATIONS: read `task.attempt_count` again in the header, the tab or the
// failure card (WA-3 red); draw a requested cancel through `errorWriter`
// (WA-6 red); put the space back inside `<small>` (WA-12 red); drop the Home
// or End branch (WA-16 red); drop the `aria-modal` effect (WA-17 red).

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent, TaskPage } from '../types'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Every read this file does not answer never answers, so nothing it does
  // not name can land and change what is asserted.
  const quiet = Object.fromEntries(
    Object.entries(actual)
      .filter(([k, v]) => k.startsWith('load') && typeof v === 'function')
      .map(([k]) => [k, () => new Promise(() => {})]),
  )
  return { ...actual, ...quiet, ...api }
})

const { AgentDetailScreen, cancelRequester, timesRun } = await import('../AgentDetail')
const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap, useListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef0789'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

/** The failed merge of WA-3: charged once, ran twice (a CI park, then the failure). */
function merge(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    runner_profile: 'merge',
    step_id: 'merge',
    attempt_count: 1,
    max_attempts: 10,
    current_generation: 2,
    started_at: at(1),
    completed_at: at(20),
    end_cause: 'runner_error',
    last_error: 'merge failed: the branch could not be fast-forwarded',
    ...over,
  } as Partial<Task>)
}

const RUNS: AttemptRow[] = [
  attempt(2, { task_id: ID, created_at: at(12), started_at: at(13), completed_at: at(20), exit_code: 1 }),
  attempt(1, { task_id: ID, created_at: at(0), started_at: at(1), completed_at: at(10), exit_code: 75 }),
]

function detailRun(t: Task, attempts: AttemptRow[] | null, events: TaskEvent[] = []): Result<AgentRun> {
  return ok({ task: t, events, eventsDetail: null, attempts, attemptsDetail: null, classes: null, classesDetail: null, classesRouteMissing: false })
}

async function details(t: Task, attempts: AttemptRow[] | null, events: TaskEvent[] = []): Promise<HTMLElement> {
  api.loadAgentRun.mockResolvedValue(detailRun(t, attempts, events))
  const { container } = render(<AgentDetailScreen taskId={t.id} onClose={() => {}} />)
  await waitFor(() => expect(container.querySelector('.dt-card[data-lead]')).not.toBeNull())
  return container as HTMLElement
}

function Split({ pane = 'detail' as const }: { pane?: 'detail' }) {
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/recent" go={() => {}} base={`work/task/${ID}`} />
    </div>
  )
}

function tab(name: string): HTMLElement {
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  return within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)!
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
})

describe('WA-3: one answer to "how many times did it run"', () => {
  it('is the attempt documents when they were read, and the counter when they were not', () => {
    expect(timesRun({ attempt_count: 1 }, RUNS)).toBe(2)
    expect(timesRun({ attempt_count: 1 }, null)).toBe(1)
    // Fewer documents than the counter is a hole in the record, not fewer runs.
    expect(timesRun({ attempt_count: 3 }, RUNS)).toBe(3)
  })

  it('says 2 in the failure card and on the Attempt card, and what the cap was charged', async () => {
    const el = await details(merge(), RUNS)
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    expect(card.textContent).toMatch(/after 2 attempts/)
    expect(card.textContent).toMatch(/1 of 10 counted against the cap/)
    expect(card.textContent).not.toMatch(/after 1 of 10/)
    const cell = [...el.querySelectorAll<HTMLElement>('.dt-sc')].find((c) => c.querySelector('.dt-sc-l')?.textContent === 'Attempt')!
    expect(cell.querySelector('.dt-sc-v')!.textContent).toBe('2 of 10')
    // A CI park is not a missing document: the cell is not drawn as a failed read.
    expect(cell.className).not.toContain('is-unread')
  })

  it('says 2 in the split header and on the Attempts tab', async () => {
    api.loadTask.mockResolvedValue(ok(merge()))
    api.loadAttempts.mockResolvedValue(ok({ attempts: RUNS }))
    render(<Split />)
    await waitFor(() => expect(document.querySelector('.ag-head-hl')?.textContent).toContain('attempt 2 of 10'))
    await waitFor(() => expect(tab('Attempts').textContent).toMatch(/Attempts\s*2/))
  })

  it('falls back to the task’s counter when the attempt read fails', async () => {
    api.loadTask.mockResolvedValue(ok(merge()))
    api.loadAttempts.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } })
    render(<Split />)
    await waitFor(() => expect(document.querySelector('.ag-head-hl')?.textContent).toContain('attempt 1 of 10'))
  })
})

describe('WA-12: the Attempt card puts a space between the figure and its unit', () => {
  it('reads `1 of 3`, not `1of 3`', async () => {
    const t = merge({ attempt_count: 1, max_attempts: 3 })
    const el = await details(t, [RUNS[1]!])
    const cell = [...el.querySelectorAll<HTMLElement>('.dt-sc')].find((c) => c.querySelector('.dt-sc-l')?.textContent === 'Attempt')!
    expect(cell.querySelector('.dt-sc-v')!.textContent).toBe('1 of 3')
    // THE SPACE IS NOT THE UNIT'S: `.dt-sc-v > small` is an inline-block,
    // which drops its own leading space when laid out, and jsdom lays nothing
    // out -- so the test asks where the space is, not only that it is there.
    expect(cell.querySelector('.dt-sc-v > small')!.textContent).toBe('of 3')
  })
})

describe('WA-6: a stop asked for from the console is not an error the agent wrote', () => {
  const cancelled = (over: Partial<Task> = {}) =>
    merge({
      state: 'CANCELLED',
      cancel_requested: true,
      end_cause: 'cancel_requested',
      last_error: 'it was cancelled on request',
      ...over,
    } as Partial<Task>)
  const asked = [ev('cancel_requested', at(15), null, { requested_by: 'ada@acme.test', from_state: 'RUNNING' })]

  it('names who asked, from the request’s own event', () => {
    expect(cancelRequester({ events: asked })).toBe('ada@acme.test')
    // The legacy spelling: a `cancelled` event whose phase is the request.
    expect(cancelRequester({ events: [ev('cancelled', at(15), null, { phase: 'cancel_requested', requested_by: 'bo@acme.test' })] })).toBe('bo@acme.test')
    expect(cancelRequester({ events: [] })).toBeNull()
    expect(cancelRequester({ events: null })).toBeNull()
  })

  it('draws the cancel with who asked and no error box', async () => {
    const el = await details(cancelled(), [RUNS[0]!], asked)
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="cancelled"]')!
    expect(card, 'a requested cancel led as a failure').not.toBeNull()
    expect(card.className).toContain('is-neu')
    expect(card.textContent).toContain('Stop requested by ada@acme.test')
    expect(card.textContent).not.toMatch(/Error written by/)
    expect(card.querySelector('.dt-err'), 'the cancel is drawn in the error box').toBeNull()
    // The recorded text is still there, as a note.
    expect(card.textContent).toContain('it was cancelled on request')
  })

  it('says who asked is unknown when no request event was read, never "agent"', async () => {
    const el = await details(cancelled(), [RUNS[0]!], [])
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="cancelled"]')!
    expect(card.textContent).toMatch(/who asked is not on the events read/)
    expect(card.textContent).not.toMatch(/agent, at finish/)
  })

  it('still leads a real failure with its error and its writer (control)', async () => {
    const el = await details(merge(), RUNS)
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    expect(card.textContent).toMatch(/Error written by/)
    expect(card.querySelector('.dt-err')).not.toBeNull()
  })
})

describe('WA-21: the detail’s links are the pages’ own paths', () => {
  // The failure card's own `Attempts ›` is gone (QA-115: two links, one
  // place); the Progress card's is the failed agent's link to the tab now.
  it('links a failed agent’s Attempts tab to the agent’s path, under the tab its state is in', async () => {
    const el = await details(merge(), RUNS)
    const more = [...el.querySelectorAll<HTMLAnchorElement>('.dt-card a.dt-more')].find((a) => a.textContent?.startsWith('Attempts'))!
    expect(more.getAttribute('href')).toBe(`/agents/recent/${ID}/attempts`)
  })
})

describe('the split header and its splitter', () => {
  it('WA-14: answers a Copy link press on the button and in a live region that was already there', async () => {
    api.loadTask.mockResolvedValue(ok(merge()))
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      render(<Split />)
      const region = await waitFor(() => {
        const r = document.querySelector<HTMLElement>('.ag-head [role="status"][aria-live="polite"]')
        expect(r, 'no live region before the press').not.toBeNull()
        return r!
      })
      expect(region.textContent).toBe('')
      fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
      expect(region.textContent).toBe('copied')
      expect(writeText).toHaveBeenCalledWith(window.location.href)
    } finally {
      delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('WA-14: says a clipboard that throws could not copy', async () => {
    api.loadTask.mockResolvedValue(ok(merge()))
    const writeText = vi.fn(() => {
      throw new Error('denied')
    })
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      render(<Split />)
      await waitFor(() => expect(screen.getByRole('button', { name: 'Copy link' })).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Copy failed' })).toBeTruthy())
    } finally {
      delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('WA-16: moves the splitter to its ends on Home and End', async () => {
    api.loadTask.mockResolvedValue(ok(merge()))
    let snap = ''
    function Probe() {
      snap = useListSnap()
      return null
    }
    render(
      <>
        <Split />
        <Probe />
      </>,
    )
    const grip = await waitFor(() => screen.getByRole('separator', { name: 'Resize the agent list' }))
    const max = grip.getAttribute('aria-valuemax')
    await act(async () => {
      fireEvent.keyDown(grip, { key: 'End' })
    })
    expect(grip.getAttribute('aria-valuenow')).toBe(max)
    const atEnd = snap
    await act(async () => {
      fireEvent.keyDown(grip, { key: 'Home' })
    })
    expect(grip.getAttribute('aria-valuenow')).toBe(grip.getAttribute('aria-valuemin'))
    expect(snap).not.toBe(atEnd)
  })

  it('WA-17: is aria-modal where it covers the list', async () => {
    // jsdom computes no `position: sticky`, which is how the split tells a
    // column beside the list from an overlay (`isOverlay`): here it is the overlay.
    api.loadTask.mockResolvedValue(ok(merge()))
    render(<Split />)
    const dialog = await waitFor(() => screen.getByRole('dialog'))
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })
})
