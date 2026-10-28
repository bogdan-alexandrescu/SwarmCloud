/**
 * QA1-FIX-05: the agent Details findings of the 2026-10-28 visual QA pass 1
 * (part of #2596 and #2391), each pinned where AgentDetail.tsx draws it.
 *
 *   overview-agents-26  the line above the Now card said `Building` on an
 *                       agent running for days, a failed one and a ready one.
 *   overview-agents-23  a silent worker's sentence was drawn twice: by `Why`
 *                       above the Now card and by the Now card itself.
 *   overview-agents-58  an events read that answered with none was drawn in
 *                       red as `This is a failed read`.
 *   overview-agents-29  `newest first · one page, cap unknown`, `masked 0`
 *                       and `no prompt key` on the glass.
 *   overview-agents-43  the attempt card said `not recorded · the worker did
 *                       not write it` while Input named the model asked for.
 *   overview-agents-28  Input said `timeout 3600s` beside a run of days, and
 *                       nothing reconciled the two.
 *   overview-agents-25  the Resources row opened onto a card titled
 *                       `Resources` again.
 *
 * Each case carries the MUTATION that turns it red.
 */
import { render, waitFor } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import type { Task, TaskInputCopy, TaskState } from '../types'
import { attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, AttemptModel, buildingWord, overTimeout } = await import('../AgentDetail')

const MIN = 60_000
const HOUR = 60 * MIN

const CLASSES = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

function ago(ms: number): string {
  return new Date(Date.now() - ms).toISOString()
}

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: 'task_qa1fix05running',
    state: 'RUNNING',
    created_at: ago(13 * MIN),
    started_at: ago(12 * MIN),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
    model: 'claude-opus-5',
    ...over,
  })
}

function runOf(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: running(),
    events: [ev('running', ago(12 * MIN), 'att_1'), ev('heartbeat', ago(MIN), 'att_1')],
    eventsDetail: null,
    attempts: [attempt(1, { backend: 'GKE_JOB', created_at: ago(13 * MIN), started_at: ago(12 * MIN), completed_at: null, exit_code: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el.querySelector<HTMLElement>('.dt')!
}

function text(el: Element | null | undefined): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

function copyOf(prompt: string | null, count = 0): TaskInputCopy {
  return {
    task_id: 'task_qa1fix05running',
    tenant_id: 'acme',
    read_at: new Date().toISOString(),
    prompt_key: prompt === null ? 'missing' : 'string',
    prompt: prompt === null ? null : { text: prompt, redaction_count: count },
    rest: null,
    full: { text: JSON.stringify({ prompt }, null, 2), redaction_count: count },
    redacted: count > 0,
    redaction_count: count,
    redaction: { applied_at_read_time: true, rules: 11 },
  } as TaskInputCopy
}

describe('overview-agents-26: the line’s word follows the state', () => {
  it('says Running, Starting, Waiting, Finished, Failed or Cancelled, never Building', () => {
    // MUTATION: return 'Building' from `buildingWord` -- every state reads `Building` again.
    const words: [TaskState, string][] = [
      ['RUNNING', 'Running'],
      ['LEASED', 'Starting'],
      ['DISPATCHED', 'Starting'],
      ['STARTING', 'Starting'],
      ['SUBMITTED', 'Waiting'],
      ['QUEUED', 'Waiting'],
      ['PARKED', 'Waiting'],
      ['READY', 'Waiting'],
      ['SUCCEEDED', 'Finished'],
      ['FAILED', 'Failed'],
      ['DEAD_LETTERED', 'Failed'],
      ['CANCELLED', 'Cancelled'],
    ]
    for (const [state, word] of words) expect(buildingWord(state), state).toBe(word)
  })

  it('draws the state’s word on a ready agent’s page, not Building', () => {
    const t = running({ state: 'READY', started_at: null, current_lease_id: null })
    // MUTATION: draw the literal `Building` in `DtBuilding` again.
    expect(text(host(runOf({ task: t, events: [], attempts: [] })).querySelector('.dt-building-l'))).toBe('Waiting')
  })
})

describe('overview-agents-23: a silent worker is said once, in the Now card', () => {
  it('draws the silent sentence once, inside the Now card', () => {
    const quiet = [ev('running', ago(40 * MIN), 'att_1'), ev('heartbeat', ago(20 * MIN), 'att_1')]
    const dt = host(runOf({ events: quiet }))
    const said = [...dt.querySelectorAll('.why-full')].filter((p) => /No event for 20m/.test(p.textContent ?? ''))
    // MUTATION: put `return <SilentWorker …/>` back in `Why` -- the sentence is drawn twice, above the card and in it.
    expect(said).toHaveLength(1)
    expect(said[0]!.closest('.dt-now'), 'the one sentence is not the Now card’s').not.toBeNull()
  })
})

describe('overview-agents-58: an events read that answered with none is not called a failed read', () => {
  it('says the history is missing from the answer, with a partial mark, out of the error ink', () => {
    const t = running({ state: 'READY', started_at: null, current_lease_id: null })
    const dt = host(runOf({ task: t, events: [], attempts: [] }))
    const progress = dt.querySelector<HTMLElement>('.dt-progress')!
    // MUTATION: put back `0 events · an agent always has one … This is a failed read.` in `is-err`.
    expect(progress.textContent).not.toMatch(/failed read/)
    const none = progress.querySelector('[data-events="none-returned"]')
    expect(none, 'no line for the empty answer').not.toBeNull()
    expect(none!.classList.contains('is-err'), 'an answer is drawn as a failed read').toBe(false)
    expect(none!.querySelector('.ctl-mark')).not.toBeNull()
    expect(text(none)).toMatch(/no event returned/)
  })

  it('keeps the failed-read card for a read that did fail', () => {
    const dt = host(runOf({ events: null }))
    expect(text(dt.querySelector('.dt-progress .dt-empty.is-err'))).toMatch(/The event history could not be read/)
  })
})

describe('overview-agents-29: plain words on Progress and Input', () => {
  it('says what the events list is without `one page, cap unknown`', () => {
    const dt = host(runOf())
    // MUTATION: put back `newest first · one page, cap unknown`.
    expect(dt.querySelector('.dt-progress')!.textContent).not.toMatch(/cap unknown/)
    expect(dt.querySelector('.dt-progress')!.textContent).toMatch(/newest first · the first page of events/)
  })

  it('says `nothing masked` for a clean prompt, and keeps the count above zero', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', data: copyOf('Keep three versions.'), fetchedAt: Date.now() })
    const { container, unmount } = render(<Run run={runOf()} />)
    const input = () => container.querySelector<HTMLElement>('.dt-input')!
    await waitFor(() => expect(input().textContent).toContain('Keep three versions.'))
    // MUTATION: drop `plainZero` from Input's `MaskedNote` -- the head reads `masked 0`.
    expect(input().querySelector('.dt-card-head')!.textContent).toMatch(/nothing masked/)
    expect(input().querySelector('.dt-card-head')!.textContent).not.toMatch(/masked\s*0/)
    unmount()

    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', data: copyOf('Keep ******** safe.', 2), fetchedAt: Date.now() })
    const again = render(<Run run={runOf()} />)
    await waitFor(() => expect(again.container.querySelector('.dt-input')!.textContent).toContain('Keep ******** safe.'))
    expect(again.container.querySelector('.dt-input .art-masked')?.textContent).toBe('2')
    again.unmount()
  })

  it('says `no prompt`, not `no prompt key`', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', data: copyOf(null), fetchedAt: Date.now() })
    const { container, unmount } = render(<Run run={runOf()} />)
    await waitFor(() => expect(container.querySelector('.dt-input .att-none')).not.toBeNull())
    // MUTATION: put back `no prompt key`.
    const none = text(container.querySelector('.dt-input .att-none'))
    expect(none).toMatch(/no prompt$/)
    expect(none).not.toMatch(/prompt key/)
    unmount()
  })
})

describe('overview-agents-43: an attempt with no model record shows the one requested, as requested', () => {
  function fact(t: Task, model: string | null): HTMLElement {
    const el = document.createElement('ul')
    el.innerHTML = renderToStaticMarkup(<AttemptModel a={attempt(1, { model })} task={t} isLatest />)
    document.body.appendChild(el)
    hosts.push(el)
    return el.querySelector<HTMLElement>('.ctl-fact')!
  }

  it('names the task’s model, labelled `requested`, when the worker wrote none', () => {
    const f = fact(running(), null)
    // MUTATION: drop the `requested` branch in `AttemptModel` -- the card reads `not recorded · the worker did not write it`.
    expect(text(f)).toBe('modelclaude-opus-5 requested')
    expect(text(f)).not.toMatch(/not recorded|did not write/)
    expect(f.classList.contains('is-absent')).toBe(false)
    expect(f.querySelector('.att-model-why')!.getAttribute('title')).toMatch(/not a record of what ran/)
  })

  // A CONTROL, green before and after: the fallback reaches no other case.
  it('keeps the recorded model when there is one, and the absence when nothing was asked for', () => {
    expect(text(fact(running(), 'claude-sonnet-5'))).toBe('modelclaude-sonnet-5')
    const none = fact(running({ model: null }), null)
    expect(none.classList.contains('is-absent')).toBe(true)
    expect(text(none.querySelector('.att-model-why'))).toBe('the worker did not write it')
    // A runner that starts no model is not given the task's.
    expect(text(fact(running({ runner_profile: 'mock' }), null).querySelector('.att-model-why'))).toBe('no model')
  })
})

describe('overview-agents-28: a run past its timeout says so beside the timeout', () => {
  it('flags a run of days against a one-hour timeout, in the run figure the page draws', () => {
    const t = running({ timeout_seconds: 3600, started_at: ago(213 * HOUR) })
    const dt = host(runOf({ task: t }))
    const row = [...dt.querySelectorAll<HTMLElement>('.dt-ifacts .ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'timeout')!
    const over = row.querySelector('[data-over="timeout"]')
    // MUTATION: drop the `overTimeout` line from Input -- `timeout 3600s` stands beside a nine-day run unexplained.
    expect(over, 'no flag on a run past its timeout').not.toBeNull()
    expect(text(row)).toMatch(/^timeout3600s · running 8d 21h, past it$/)
  })

  it('says nothing within the timeout, with no timeout, or once the run has ended', () => {
    const now = Date.now()
    expect(overTimeout(running({ timeout_seconds: 3600 }), now)).toBeNull()
    expect(overTimeout(running({ timeout_seconds: null, started_at: ago(5 * HOUR) }), now)).toBeNull()
    const ended = running({ state: 'SUCCEEDED', timeout_seconds: 3600, started_at: ago(5 * HOUR), completed_at: ago(HOUR) })
    expect(overTimeout(ended, now)).toBeNull()
    expect(overTimeout(running({ timeout_seconds: 3600, started_at: ago(2 * HOUR) }), now)?.past).toMatch(/^1h/)
  })
})

describe('overview-agents-25: one Resources title', () => {
  it('opens the Resources row onto a card with no second `Resources` title', () => {
    const dt = host(runOf())
    const fold = dt.querySelector('details.dt-res')!
    expect(text(fold.querySelector(':scope > summary .dt-res-l'))).toBe('Resources')
    // MUTATION: give `DtResources` its `DtCardHead title="Resources"` back -- the fold opens on the same word.
    const titles = [...fold.querySelectorAll('h2, h3, .dt-res-l')].filter((h) => text(h) === 'Resources')
    expect(titles).toHaveLength(1)
    // What the head carried besides the word is still there.
    expect(text(fold.querySelector('.dt-resources .dt-card-head > h2'))).toBe('Against each limit')
    expect(fold.querySelector('.dt-resources .dt-card-head .dt-card-r [aria-label^="Help: "]')).not.toBeNull()
  })
})
