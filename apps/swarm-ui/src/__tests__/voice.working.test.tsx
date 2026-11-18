// VOICE IT'S MAIN VIEW: THE WORKING CARD, THE CREW STRIP AND THE NAVIGATE
// CARD (docs/design/voice-it.md §3.12.3, §3.12.5, §3.12.6; the owner's pick
// of 12 November 2026, "Crew strip"), on the live path (USE_FIXTURES off)
// against a fake EventSource and stubbed reads and writes.
//
//   * the stream's event list carries work.delegation, work.step and
//     roster.update, so every reader of the one EventSource hears them;
//   * the working card is one line collapsed; the delegation's end (its next
//     output) removes it and its steps; 51 steps show 50 and "and 1 earlier";
//     two delegations follow the oldest with "and 1 more";
//   * a step draws its phrase and its ids, nothing else it carries;
//   * a roster.update moves only its rows;
//   * an offer accepted by a click navigates once; a history card never does;
//     No dismisses.
//
// BREAK IT: leave a work event out of STREAM_EVENTS, keep a delegation's steps
// after its end, draw a step's other fields, replace the whole roster on an
// update, navigate on an accepted card that arrived accepted, or navigate
// again on the accepted card's own upsert.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    get USE_FIXTURES() {
      return false
    },
  }
})

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { applyStreamEvent, CardList, EMPTY_LIST, readSessionStream, STREAM_EVENTS, type CardListState } from '../voice/CardList'
import { WorkingCard } from '../voice/WorkingCard'

const SESSION = 'vs_0001'
const PROJECT = 'vp_0001'

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeEventSource.all.push(this)
  }
  close(): void {
    this.readyState = 2
  }
  frame(type: string, seq: number, data: Record<string, unknown>): void {
    act(() => {
      this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ seq, type, ...data }), lastEventId: String(seq) }))
    })
  }
}

const stream = (): FakeEventSource => {
  const last = FakeEventSource.all[FakeEventSource.all.length - 1]
  if (last === undefined) throw new Error('no stream opened')
  return last
}

const STARTED_AT = new Date(Date.now() - 42_000).toISOString()

/** The main view's caption band as Talk draws it: the holder's list state, read from the one stream. */
function Band() {
  const [s, setS] = useState<CardListState>(EMPTY_LIST)
  useEffect(() => readSessionStream(SESSION, 0, { types: STREAM_EVENTS, onEvent: (ev) => setS((x) => applyStreamEvent(x, ev)) }), [])
  return <WorkingCard work={s.work ?? []} />
}

let seq = 0
const next = () => (seq += 1)
const delegation = (id: string, state: string) => stream().frame('work.delegation', next(), { delegation_id: id, state, at: STARTED_AT })
const step = (id: string, phrase: string, ids: Record<string, string> = {}, extra: Record<string, unknown> = {}) =>
  stream().frame('work.step', next(), { delegation_id: id, phrase, method: 'GET', class: 'read', ids, at: STARTED_AT, ...extra })

const head = () => screen.getByRole('button', { expanded: false })
const expand = () => fireEvent.click(screen.getByRole('button', { expanded: false }))

/** A runtime-built secret-shaped value, never a literal. */
const runtimeHex = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, '0')).join('')

describe("Voice it's working card", () => {
  beforeEach(() => {
    seq = 0
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("lists the work events among the stream's events", () => {
    expect(STREAM_EVENTS).toEqual(expect.arrayContaining(['work.delegation', 'work.step', 'roster.update']))
  })

  it('takes a list state built without work or roster, as callers before it wrote one', () => {
    const before: CardListState = { lastSeq: 0, cards: [] }
    const s = applyStreamEvent(before, { type: 'work.delegation', seq: 1, data: { delegation_id: 'dl_1', state: 'started', at: STARTED_AT } })
    expect(s.work?.map((d) => d.delegation_id)).toEqual(['dl_1'])
    expect(s.roster?.map((r) => r.id)).toEqual(['dl_1'])
  })

  it('is one line collapsed: the live dot, what it does now and the elapsed time', () => {
    const { container } = render(<Band />)
    delegation('dl_1', 'started')
    step('dl_1', 'Reading running agents')
    step('dl_1', "Reading agent {task_id}'s events", { task_id: 'task-0042' })
    const card = container.querySelector('.vtc-work')
    expect(card).not.toBeNull()
    expect(card?.querySelectorAll('.vtc-work-head')).toHaveLength(1)
    expect(card?.querySelector('ol')).toBeNull()
    expect(card?.querySelector('.vtc-work-live')).not.toBeNull()
    expect(head().textContent).toMatch(/Reading agent task-0042's events/)
    expect(head().textContent).toMatch(/0:4\d/)
    expect(head().textContent).not.toMatch(/Reading running agents/)
  })

  it('is replaced by the next output, and its steps are not kept', () => {
    const { container } = render(<Band />)
    delegation('dl_1', 'started')
    step('dl_1', 'Reading running agents')
    expand()
    expect(screen.getByRole('list', { name: 'Steps' }).textContent).toMatch(/Reading running agents/)
    delegation('dl_1', 'done')
    expect(container.querySelector('.vtc-work')).toBeNull()
    expect(container.textContent).not.toMatch(/Reading running agents/)
    delegation('dl_2', 'started')
    expect(container.textContent).not.toMatch(/Reading running agents/)
  })

  it('shows at most 50 steps, the older folded into "and N earlier"', () => {
    render(<Band />)
    delegation('dl_1', 'started')
    for (let i = 1; i <= 51; i += 1) step('dl_1', 'Reading repository {repo_id}', { repo_id: `repo-${i}` })
    expand()
    const list = screen.getByRole('list', { name: 'Steps' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(51)
    expect(rows[0]?.textContent).toBe('and 1 earlier')
    expect(list.querySelectorAll('.vtc-step-text')).toHaveLength(50)
    expect(list.textContent).not.toMatch(/repo-1\b/)
    expect(rows[50]?.textContent).toMatch(/repo-51/)
  })

  it('follows the oldest of two delegations and says "and 1 more"', () => {
    render(<Band />)
    delegation('dl_old', 'started')
    delegation('dl_new', 'started')
    step('dl_old', 'Reading repository {repo_id}', { repo_id: 'repo-old' })
    step('dl_new', 'Reading repository {repo_id}', { repo_id: 'repo-new' })
    expect(head().textContent).toMatch(/repo-old/)
    expect(head().textContent).not.toMatch(/repo-new/)
    expect(head().textContent).toMatch(/and 1 more/)
    delegation('dl_old', 'done')
    expect(head().textContent).toMatch(/repo-new/)
    expect(head().textContent).not.toMatch(/more/)
  })

  it("draws a step's phrase and ids and nothing else it carries", () => {
    const { container } = render(<Band />)
    const leaked = runtimeHex()
    delegation('dl_1', 'started')
    step('dl_1', 'Reading repository {repo_id}', { repo_id: 'repo-7' }, { query: `key=${leaked}`, body: `body ${leaked}`, text: `said ${leaked}`, method: 'GET' })
    expand()
    expect(container.textContent).toMatch(/Reading repository repo-7/)
    expect(container.textContent).not.toContain(leaked)
    expect(container.textContent).not.toMatch(/GET|read\b/)
  })
})

const json = <T,>(data: T) => ({ status: 'ok', data, fetchedAt: Date.now() })

describe("Voice it's crew strip and Navigate card", () => {
  beforeEach(() => {
    seq = 0
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    fetchMock.read.mockImplementation(async (target: { url: string }) =>
      target.url === `/v1/voice/projects/${PROJECT}/roster`
        ? json({
            project_id: PROJECT,
            rows: [
              { kind: 'workflow', id: 'wf-0001', title: 'Landing page versions', state: 'running', done: 1, total: 3 },
              { kind: 'agent', id: 'task-0002', title: 'Review the copy', state: 'queued', done: null, total: null },
            ],
          })
        : { status: 'empty', fetchedAt: Date.now() },
    )
    fetchMock.write.mockImplementation(async () => json({ card: null }))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })

  const settle = () => act(async () => {})

  it('reads the roster once and a roster.update moves only its rows', async () => {
    render(<CardList sessionId={SESSION} projectId={PROJECT} />)
    await settle()
    const rosterReads = fetchMock.read.mock.calls.filter(([t]) => String((t as { url: string }).url).endsWith('/roster'))
    expect(rosterReads).toHaveLength(1)
    const strip = screen.getByRole('region', { name: 'Agents' })
    expect(within(strip).getByRole('button', { name: /Landing page versions, Workflow, Working/ })).toBeTruthy()
    expect(within(strip).getByRole('progressbar', { name: 'Landing page versions progress' }).getAttribute('aria-valuenow')).toBe('33')
    expect(within(strip).getByRole('button', { name: /Review the copy, Agent, Queued/ })).toBeTruthy()

    stream().frame('roster.update', next(), { rows: [{ kind: 'workflow', id: 'wf-0001', state: 'waiting', done: 2, total: 3 }] })
    expect(within(strip).getByRole('button', { name: /Landing page versions, Workflow, Waiting on you/ })).toBeTruthy()
    expect(within(strip).getByRole('progressbar', { name: 'Landing page versions progress' }).getAttribute('aria-valuenow')).toBe('67')
    expect(within(strip).getByRole('button', { name: /Review the copy, Agent, Queued/ })).toBeTruthy()
    expect(within(strip).getAllByRole('listitem')).toHaveLength(2)
    expect(fetchMock.read.mock.calls.filter(([t]) => String((t as { url: string }).url).endsWith('/roster'))).toHaveLength(1)
  })

  const offer = (state: string, version = 1) => ({
    card: {
      card_id: 'vc_nav_1',
      kind: 'navigate',
      project_id: PROJECT,
      session_id: SESSION,
      version,
      state,
      title: 'Running agents',
      created_by: 'planner',
      references: null,
      summary: null,
      at: STARTED_AT,
      body: { destination: 'work/running', object: null, why: 'Three live, one waiting', accepted_by: state === 'accepted' ? 'person' : null },
    },
  })

  it('opens an offer on a click through the accept route, and navigates once', async () => {
    const go = vi.fn()
    render(<CardList sessionId={SESSION} navigate={go} />)
    await settle()
    stream().frame('card.upsert', next(), offer('proposed'))
    expect(go).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    await settle()
    const accepts = fetchMock.write.mock.calls.filter(([t]) => String((t as { url: string }).url) === `/v1/voice/sessions/${SESSION}/cards/vc_nav_1/accept`)
    expect(accepts).toHaveLength(1)
    expect(accepts[0]?.[1]).toBe('POST')
    expect(go).toHaveBeenCalledTimes(1)
    expect(go).toHaveBeenCalledWith('/agents')
    // The accepted card's own upsert, and a done one after it, move nothing.
    stream().frame('card.upsert', next(), offer('accepted', 2))
    stream().frame('card.upsert', next(), offer('done', 3))
    await settle()
    expect(go).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Open' })).toBeNull()
  })

  it('never navigates from a history card', async () => {
    const go = vi.fn()
    render(<CardList sessionId={SESSION} navigate={go} />)
    await settle()
    stream().frame('card.upsert', next(), offer('accepted', 2))
    await settle()
    expect(screen.getByText('Running agents')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open' })).toBeNull()
    expect(go).not.toHaveBeenCalled()
    expect(fetchMock.write).not.toHaveBeenCalled()
  })

  it('dismisses the offer on No', async () => {
    const go = vi.fn()
    const { container } = render(<CardList sessionId={SESSION} navigate={go} />)
    await settle()
    stream().frame('card.upsert', next(), offer('proposed'))
    fireEvent.click(screen.getByRole('button', { name: 'No' }))
    await settle()
    expect(screen.queryByRole('button', { name: 'Open' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'No' })).toBeNull()
    expect(container.querySelector('[data-kind="navigate"]')?.getAttribute('data-state')).toBe('dismissed')
    expect(go).not.toHaveBeenCalled()
    expect(fetchMock.write).not.toHaveBeenCalled()
  })
})
