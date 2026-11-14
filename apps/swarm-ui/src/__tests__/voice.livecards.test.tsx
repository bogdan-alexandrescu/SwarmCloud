/**
 * Voice it's cards appear as they happen (owner, 12 November 2026): a card is
 * drawn on its first event and updated in place, and a Work card follows its
 * agent's or workflow's `roster.update` rows without a reload.
 */
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { applyStreamEvent, CardList, EMPTY_LIST, FIXTURE_CARDS, STREAM_EVENTS, type VoiceCard } from '../voice/CardList'

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
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ seq, type, ...data }), lastEventId: String(seq) }))
  }
  static get last(): FakeEventSource {
    const s = FakeEventSource.all[FakeEventSource.all.length - 1]
    if (s === undefined) throw new Error('no EventSource was opened')
    return s
  }
}

const WORK = FIXTURE_CARDS.find((c) => c.kind === 'work')!

function work(id: string, version: number, title: string, target: [string, string], extra: Partial<VoiceCard> = {}): VoiceCard {
  return {
    ...WORK,
    card_id: id,
    version,
    title,
    references: null,
    body: { target_kind: target[0], target_id: target[1], repository: 'example/repo', lanes: [], live_state: null },
    ...extra,
  }
}

function drawn(root: ParentNode): string[] {
  return Array.from(root.querySelectorAll('article')).map((a) => a.getAttribute('data-card-id') ?? '')
}

const SESSION = 'vs_live'

describe('cards appear as they happen', () => {
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('reads roster.update from the session stream', () => {
    expect(STREAM_EVENTS).toContain('roster.update')
  })

  it("draws a mock-up's card on its first frame and replaces it in place as frames two and three arrive", () => {
    const { container } = render(<CardList sessionId={SESSION} />)
    const es = FakeEventSource.last
    const names = ['Classic', 'Bold', 'Minimal']
    const mockup = (n: number): VoiceCard => ({
      ...WORK,
      kind: 'mockup',
      card_id: 'vc_m',
      version: n,
      state: 'proposed',
      title: 'The landing page',
      summary: `${n} of 3 frames`,
      references: null,
      body: { drawn_by: 'planner', viewport: 'desktop', height: null, frames: names.slice(0, n).map((name) => ({ name, html: `<h1>${name}</h1>`, blob_key: null })) },
    })
    act(() => es.frame('card.upsert', 1, { card: mockup(1) }))
    // Frame one is on screen before frame three is written.
    const first = screen.getByRole('article', { name: 'Mock-up: The landing page' })
    expect(first.getAttribute('data-version')).toBe('1')
    expect(first.textContent).toContain('1 of 3 frames')
    act(() => es.frame('card.upsert', 2, { card: mockup(2) }))
    act(() => es.frame('card.upsert', 3, { card: mockup(3) }))
    // Three updates of one card: drawn once, its latest version in place.
    expect(drawn(container)).toEqual(['vc_m'])
    const last = screen.getByRole('article', { name: 'Mock-up: The landing page' })
    expect(last.getAttribute('data-version')).toBe('3')
    expect(last.textContent).toContain('3 of 3 frames')
  })

  it('a later update of a card replaces it where it stands, between the others', () => {
    const { container } = render(<CardList sessionId={SESSION} />)
    const es = FakeEventSource.last
    act(() => es.frame('card.upsert', 1, { card: work('vc_a', 1, 'First', ['agent', 't-a']) }))
    act(() => es.frame('card.upsert', 2, { card: work('vc_b', 1, 'Second', ['agent', 't-b']) }))
    act(() => es.frame('card.upsert', 3, { card: work('vc_c', 1, 'Third', ['agent', 't-c']) }))
    act(() => es.frame('card.upsert', 4, { card: work('vc_b', 2, 'Second, again', ['agent', 't-b']) }))
    expect(drawn(container)).toEqual(['vc_a', 'vc_b', 'vc_c'])
    expect(screen.getByRole('article', { name: 'Work: Second, again' })).toBeTruthy()
    expect(screen.queryByRole('article', { name: 'Work: Second' })).toBeNull()
  })

  it("a roster.update changes only its Work card's state and steps, live, and draws nothing twice", () => {
    const { container } = render(<CardList sessionId={SESSION} />)
    const es = FakeEventSource.last
    act(() => es.frame('card.upsert', 1, { card: work('vc_wf', 1, 'The workflow', ['workflow', 'wf-1'], { state: 'acting' }) }))
    act(() => es.frame('card.upsert', 2, { card: work('vc_ag', 1, 'The agent', ['agent', 't-1'], { state: 'acting' }) }))
    const wf = () => screen.getByRole('article', { name: 'Work: The workflow' })
    const ag = () => screen.getByRole('article', { name: 'Work: The agent' })
    const agentBefore = ag().innerHTML

    act(() => es.frame('roster.update', 3, { rows: [{ kind: 'workflow', id: 'wf-1', state: 'running', done: 2, total: 5 }] }))
    expect(wf().getAttribute('data-live-state')).toBe('running')
    expect(within(wf()).getByText('2 of 5')).toBeTruthy()
    // Only its own card: the agent's card is drawn exactly as it was.
    expect(ag().innerHTML).toBe(agentBefore)
    expect(ag().getAttribute('data-live-state')).toBeNull()

    act(() => es.frame('roster.update', 4, { rows: [{ kind: 'workflow', id: 'wf-1', state: 'waiting', done: 3, total: 5 }] }))
    expect(wf().getAttribute('data-live-state')).toBe('waiting')
    expect(within(wf()).getByText('3 of 5')).toBeTruthy()
    expect(wf().textContent).toContain('waiting on you')
    expect(wf().getAttribute('data-state')).toBe('waiting')

    act(() => es.frame('roster.update', 5, { rows: [{ kind: 'agent', id: 't-1', state: 'failed', done: null, total: null }] }))
    expect(ag().getAttribute('data-live-state')).toBe('failed')
    expect(ag().getAttribute('data-state')).toBe('failed')
    expect(wf().getAttribute('data-live-state')).toBe('waiting')

    act(() => es.frame('roster.update', 6, { rows: [{ kind: 'workflow', id: 'wf-1', state: 'done', done: 5, total: 5 }] }))
    expect(wf().getAttribute('data-state')).toBe('done')
    expect(within(wf()).getByText('5 of 5')).toBeTruthy()
    // Nothing drawn twice, and a repeated sequence changes nothing.
    act(() => es.frame('roster.update', 6, { rows: [{ kind: 'workflow', id: 'wf-1', state: 'failed', done: 0, total: 5 }] }))
    expect(drawn(container)).toEqual(['vc_wf', 'vc_ag'])
    expect(wf().getAttribute('data-live-state')).toBe('done')
  })

  it('a roster row that arrives before its Work card is drawn is used when the card comes', () => {
    render(<CardList sessionId={SESSION} />)
    const es = FakeEventSource.last
    act(() => es.frame('roster.update', 1, { rows: [{ kind: 'agent', id: 't-9', state: 'running', done: null, total: null }] }))
    act(() => es.frame('card.upsert', 2, { card: work('vc_9', 1, 'Late card', ['agent', 't-9'], { state: 'proposed' }) }))
    const card = screen.getByRole('article', { name: 'Work: Late card' })
    expect(card.getAttribute('data-live-state')).toBe('running')
    expect(card.getAttribute('data-state')).toBe('acting')
  })

  it('keeps the reducer pure: a roster row is merged by kind and id, and a bad row is skipped', () => {
    const one = applyStreamEvent(EMPTY_LIST, { type: 'roster.update', seq: 1, data: { rows: [{ kind: 'workflow', id: 'wf-1', state: 'running', done: 1, total: 4 }] } })
    const two = applyStreamEvent(one, { type: 'roster.update', seq: 2, data: { rows: [{ kind: 'agent', id: 't-1', state: 'queued' }, { kind: 'workflow', id: 'wf-1', state: 'nonsense' }] } })
    const row = (s: typeof one, kind: string, id: string) => s.roster?.find((r) => r.kind === kind && r.id === id)
    expect(row(two, 'workflow', 'wf-1')?.state).toBe('running')
    expect(row(two, 'agent', 't-1')?.state).toBe('queued')
    // The rows it did not touch are the same objects, so their cards are not drawn again.
    expect(row(two, 'workflow', 'wf-1')).toBe(row(one, 'workflow', 'wf-1'))
    expect(two.cards).toBe(one.cards)
  })
})
