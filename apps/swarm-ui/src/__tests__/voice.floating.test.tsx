// VOICE IT FOLLOWS THE OWNER, AND THE SPINE MARKS WHAT WAITS
// (docs/design/voice-it.md §3.12.2, §3.12.4; voice/Floating.tsx).
//
//   * the floating view never draws a card's content: no title, summary or
//     body, only the kind of what is new;
//   * the spine's number counts only the items after `seen_seq`, and Talk on
//     screen moves `seen_seq` with a post to swarm-api;
//   * a seen card that still waits leaves a dot, gone once it stops waiting;
//   * Escape inside the view stops the session;
//   * Space and M do nothing unless the view has focus;
//   * folded and dragged, it stays so across a route change;
//   * a left corner starts at the page's left edge, clear of the side panel;
//   * nothing is drawn with no live session, nor on Talk.
//
// MUTATIONS: draw the card's title beside "New:"; count every item whatever
// `seen_seq` says; drop the dot once seen; bind Escape, Space or M on the
// window; keep the fold or the corner in component state; offset a left
// corner by the spine's width alone.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))
const held = vi.hoisted(() => ({ current: null as unknown }))

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
  write: fetchMock.write,
}))

vi.mock('../voice/session', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../voice/session')>()),
  useVoiceSession: () => {
    if (held.current === null) throw new Error('no holder')
    return held.current
  },
}))

import type { CardListState, VoiceCard } from '../voice/CardList'
import {
  applyWork,
  clearFollow,
  FloatingView,
  forgetPlace,
  currentMarker,
  noteCards,
  noteSeen,
  noteWork,
  PLACE_KEY,
  VOICE_SEEN,
  VoiceFloatLayer,
  VoiceMarker,
  workLine,
} from '../voice/Floating'
import { MIC_START, type MicState } from '../voice/mic'
import type { VoiceSession } from '../voice/session'
import { stageFeed } from '../voice/Stage'

const SESSION = 'vs_0001'
const SECRET_TITLE = 'Launch plan for example.com, owned by example-org'
const SECRET_SUMMARY = 'Move the storefront to example.com next week'
const SECRET_BODY = 'step one: rename the example-org bucket'

function card(id: string, kind: string, state: string, version = 1): VoiceCard {
  return {
    card_id: id,
    kind,
    project_id: 'vp_0001',
    session_id: SESSION,
    version,
    state: state as VoiceCard['state'],
    title: SECRET_TITLE,
    created_by: 'planner',
    references: null,
    summary: SECRET_SUMMARY,
    at: '2026-11-12T10:00:00Z',
    body: { steps: [SECRET_BODY] },
  }
}

function list(lastSeq: number, ...cards: VoiceCard[]): CardListState {
  return { lastSeq, cards }
}

interface Fake {
  session: VoiceSession
  end: ReturnType<typeof vi.fn>
  setMuted: ReturnType<typeof vi.fn>
  hold: ReturnType<typeof vi.fn>
  setMode: ReturnType<typeof vi.fn>
}

function fake(over: { phase?: VoiceSession['phase']; mic?: MicState; cards?: CardListState } = {}): Fake {
  const end = vi.fn(async () => undefined)
  const setMuted = vi.fn()
  const hold = vi.fn()
  const setMode = vi.fn()
  const session = {
    phase: over.phase ?? 'live',
    problem: null,
    problemTip: null,
    lines: [],
    debug: false,
    turn: null,
    voice: null,
    micStream: null,
    voicePlaying: false,
    sessionId: SESSION,
    streamId: over.phase === 'idle' ? null : SESSION,
    projectId: 'vp_0001',
    talkMode: 'live',
    boot: [],
    speechState: null,
    fallback: null,
    dismissFallback: vi.fn(),
    tierLine: null,
    onPlan: false,
    level: null,
    typed: null,
    draft: '',
    setDraft: vi.fn(),
    cards: over.cards ?? list(0),
    meter: null,
    feed: stageFeed(),
    mic: over.mic ?? MIC_START,
    setMuted,
    setMode,
    hold,
    start: vi.fn(async () => undefined),
    end,
    say: vi.fn(),
    quiet: vi.fn(),
    attach: vi.fn(() => () => undefined),
    navigate: vi.fn(),
  } as unknown as VoiceSession
  return { session, end, setMuted, hold, setMode }
}

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
  }
  close(): void {
    this.readyState = 2
  }
}

/** jsdom has no PointerEvent: without one a pointer's coordinates are lost and a drag lands nowhere. */
class FakePointerEvent extends MouseEvent {
  readonly pointerId: number
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init)
    this.pointerId = init.pointerId ?? 1
  }
}

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
  if (typeof window.PointerEvent !== 'function') vi.stubGlobal('PointerEvent', FakePointerEvent)
  window.sessionStorage.clear()
  forgetPlace()
  clearFollow()
  held.current = null
  fetchMock.write.mockReset()
  fetchMock.write.mockImplementation(async () => ({ status: 'ok', data: null, fetchedAt: Date.now() }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('the floating view says what is new, never what it says', () => {
  it('draws the kind of a new card and none of its title, summary or body', () => {
    noteCards(SESSION, list(4, card('c_plan', 'plan', 'proposed'), card('c_mock', 'mockup', 'proposed')))
    noteCards(SESSION, list(6, card('c_plan', 'plan', 'proposed'), card('c_mock', 'mockup', 'proposed'), card('c_review', 'review', 'waiting')))
    const f = fake()
    const { container } = render(<FloatingView held={f.session} go={vi.fn()} />)
    expect(screen.getByText('New: Review')).toBeTruthy()
    const markup = container.innerHTML
    for (const text of [SECRET_TITLE, SECRET_SUMMARY, SECRET_BODY, 'example.com', 'example-org']) expect(markup).not.toContain(text)
    expect(container.querySelector('.vtb-count')?.textContent).toBe('3')
  })

  it('draws the working line from the delegation feed, with no card content', () => {
    const now = Date.parse('2026-11-12T10:00:42Z')
    const started = { type: 'work.delegation', seq: 1, data: { delegation_id: 'd1', state: 'started', at: '2026-11-12T10:00:00Z' } }
    const step = { type: 'work.step', seq: 2, data: { delegation_id: 'd1', phrase: 'Reading running agents', at: '2026-11-12T10:00:05Z' } }
    let ds = applyWork([], started, now)
    ds = applyWork(ds, step, now)
    expect(workLine(ds, now)).toBe('Reading running agents · 0:42')
    ds = applyWork(ds, { type: 'work.delegation', seq: 3, data: { delegation_id: 'd2', state: 'started', at: '2026-11-12T10:00:30Z' } }, now)
    expect(workLine(ds, now)).toBe('Reading running agents · 0:42 · and 1 more')
    ds = applyWork(ds, { type: 'work.delegation', seq: 4, data: { delegation_id: 'd1', state: 'done' } }, now)
    expect(workLine(ds, now)).toBe('Working · 0:12')
    noteWork(SESSION, started, now)
    noteWork(SESSION, step, now)
    render(<FloatingView held={fake().session} go={vi.fn()} />)
    expect(screen.getByText(/^Reading running agents · \d+:\d\d$/)).toBeTruthy()
  })
})

describe('the spine marker', () => {
  it('counts only the items after seen_seq', () => {
    noteCards(SESSION, list(5, card('c_plan', 'plan', 'proposed')))
    noteSeen(SESSION, 5)
    expect(currentMarker()).toEqual({ n: 0, dot: true, kind: null })
    noteCards(SESSION, list(7, card('c_plan', 'plan', 'proposed'), card('c_mock', 'mockup', 'proposed')))
    noteCards(SESSION, list(9, card('c_plan', 'plan', 'proposed'), card('c_mock', 'mockup', 'proposed'), card('c_budget', 'budget', 'waiting')))
    expect(currentMarker()?.n).toBe(2)
    noteSeen(SESSION, 7)
    expect(currentMarker()?.n).toBe(1)
    render(<VoiceMarker />)
    expect(screen.getByLabelText(/1 new in Voice it: Budget/).textContent).toBe('1')
  })

  it('counts a work card moving between running states as progress, not an item', () => {
    noteCards(SESSION, list(3, card('c_work', 'work', 'acting')))
    expect(currentMarker()).toBeNull()
  })

  it('draws 9+ past nine', () => {
    const many = Array.from({ length: 12 }, (_, i) => card(`c_${i}`, 'review', 'waiting'))
    noteCards(SESSION, list(12, ...many))
    render(<VoiceMarker />)
    expect(screen.getByLabelText(/12 new in Voice it/).textContent).toBe('9+')
  })

  it('keeps a dot while a seen card waits, and drops it once the card stops waiting', () => {
    noteCards(SESSION, list(3, card('c_review', 'review', 'waiting')))
    noteSeen(SESSION, 3)
    const { container, rerender } = render(<VoiceMarker />)
    expect(container.querySelector('.sk-badge')).toBeNull()
    expect(container.querySelector('.vtb-wait')?.getAttribute('aria-label')).toBe('a card waits on you')
    act(() => noteCards(SESSION, list(4, card('c_review', 'review', 'done', 2))))
    rerender(<VoiceMarker />)
    expect(container.querySelector('.vtb-wait')).toBeNull()
    expect(container.innerHTML).toBe('')
  })

  it('posts seen_seq while Talk is on screen, and the number clears', async () => {
    const cards = list(8, card('c_plan', 'plan', 'proposed'))
    const f = fake({ cards })
    held.current = f.session
    const { rerender } = render(
      <>
        <VoiceFloatLayer onTalk={false} go={vi.fn()} />
        <VoiceMarker />
      </>,
    )
    expect(screen.getByLabelText(/1 new in Voice it: Plan/)).toBeTruthy()
    expect(fetchMock.write).not.toHaveBeenCalled()
    await act(async () => {
      rerender(
        <>
          <VoiceFloatLayer onTalk go={vi.fn()} />
          <VoiceMarker />
        </>,
      )
    })
    expect(fetchMock.write).toHaveBeenCalledTimes(1)
    const [target, method, body] = fetchMock.write.mock.calls[0] as [{ url: string }, string, unknown]
    expect(target.url).toBe(VOICE_SEEN.replace('{id}', SESSION))
    expect(method).toBe('POST')
    expect(body).toEqual({ seq: 8 })
    expect(screen.queryByLabelText(/new in Voice it/)).toBeNull()
    expect(screen.getByLabelText('a card waits on you')).toBeTruthy()
  })
})

describe('the keyboard', () => {
  it('Escape inside the view stops the session', () => {
    const f = fake()
    render(<FloatingView held={f.session} go={vi.fn()} />)
    fireEvent.keyDown(screen.getByRole('button', { name: /^Voice it, listening/ }), { key: 'Escape' })
    expect(f.end).toHaveBeenCalledTimes(1)
  })

  it('Space and M do nothing unless the view has focus', () => {
    const f = fake({ mic: { muted: false, mode: 'ptt', held: false } })
    render(
      <>
        <button type="button">elsewhere</button>
        <FloatingView held={f.session} go={vi.fn()} />
      </>,
    )
    const elsewhere = screen.getByRole('button', { name: 'elsewhere' })
    elsewhere.focus()
    fireEvent.keyDown(elsewhere, { key: 'm' })
    fireEvent.keyDown(elsewhere, { key: ' ', code: 'Space' })
    fireEvent.keyDown(window, { key: 'm' })
    fireEvent.keyDown(document.body, { key: ' ', code: 'Space' })
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(f.setMuted).not.toHaveBeenCalled()
    expect(f.hold).not.toHaveBeenCalled()
    expect(f.end).not.toHaveBeenCalled()

    const ring = screen.getByRole('button', { name: /^Voice it, listening/ })
    ring.focus()
    fireEvent.keyDown(ring, { key: 'm' })
    expect(f.setMuted).toHaveBeenCalledWith(true)
    fireEvent.keyDown(ring, { key: ' ', code: 'Space' })
    expect(f.hold).toHaveBeenCalledWith(true)
    // Released on Stop, Space ends the hold and never presses Stop.
    const stop = screen.getByRole('button', { name: 'Stop' })
    expect(fireEvent.keyUp(stop, { key: ' ', code: 'Space' })).toBe(false)
    expect(f.end).not.toHaveBeenCalled()
  })
})

describe('where it sits', () => {
  it('stays folded and in its dragged corner across a route change', () => {
    held.current = fake().session
    const go = vi.fn()
    const first = render(<VoiceFloatLayer onTalk={false} go={go} />)
    const ring = () => screen.getByRole('button', { name: /^Voice it, listening/ })
    // A press without a move folds it to the ring alone.
    fireEvent.pointerDown(ring(), { clientX: 900, clientY: 700, pointerId: 1 })
    fireEvent.pointerUp(ring(), { clientX: 900, clientY: 700, pointerId: 1 })
    expect(ring().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: 'Open Voice it' })).toBeNull()
    // A drag to the top left keeps it there.
    fireEvent.pointerDown(ring(), { clientX: 900, clientY: 700, pointerId: 1 })
    fireEvent.pointerMove(ring(), { clientX: 400, clientY: 300, pointerId: 1 })
    fireEvent.pointerMove(ring(), { clientX: 20, clientY: 20, pointerId: 1 })
    fireEvent.pointerUp(ring(), { clientX: 20, clientY: 20, pointerId: 1 })
    expect(first.container.querySelector('.vtb')?.className).toContain('is-tl')
    // Dragged back across, it lands in the corner it was dropped nearest to.
    fireEvent.pointerDown(ring(), { clientX: 20, clientY: 20, pointerId: 1 })
    fireEvent.pointerMove(ring(), { clientX: window.innerWidth - 10, clientY: 30, pointerId: 1 })
    fireEvent.pointerUp(ring(), { clientX: window.innerWidth - 10, clientY: 30, pointerId: 1 })
    expect(first.container.querySelector('.vtb')?.className).toContain('is-tr')
    fireEvent.pointerDown(ring(), { clientX: window.innerWidth - 10, clientY: 30, pointerId: 1 })
    fireEvent.pointerMove(ring(), { clientX: 15, clientY: 25, pointerId: 1 })
    fireEvent.pointerUp(ring(), { clientX: 15, clientY: 25, pointerId: 1 })
    expect(first.container.querySelector('.vtb')?.className).toContain('is-tl')

    // Talk, then another page: the shell draws it again, folded, in its corner.
    first.rerender(<VoiceFloatLayer onTalk go={go} />)
    expect(first.container.querySelector('.vtb')).toBeNull()
    first.unmount()
    const again = render(<VoiceFloatLayer onTalk={false} go={go} />)
    const box = again.container.querySelector('.vtb')
    expect(box?.className).toContain('is-tl')
    expect(box?.className).toContain('is-collapsed')
    expect(JSON.parse(window.sessionStorage.getItem(PLACE_KEY) ?? '{}')).toEqual({ corner: 'tl', collapsed: true })

    // Unfolded, "Open Voice it" goes to Talk.
    fireEvent.click(screen.getByRole('button', { name: /^Voice it, listening/ }), { detail: 0 })
    fireEvent.click(screen.getByRole('button', { name: 'Open Voice it' }))
    expect(go).toHaveBeenCalledWith('voice/talk')
  })
})

describe('a left corner', () => {
  it('sits clear of the side panel: it starts at the page\'s left edge, wherever the panel ends', () => {
    held.current = fake().session
    window.sessionStorage.setItem(PLACE_KEY, JSON.stringify({ corner: 'tl', collapsed: false }))
    // The spine (84px) and the open side panel (236px) end where the page begins.
    let left = 320
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('sk-main') ? new DOMRect(left, 0, 1120, 900) : new DOMRect(0, 0, 0, 0)
    })
    try {
      const { container } = render(
        <div className="sk-main">
          <VoiceFloatLayer onTalk={false} go={vi.fn()} />
        </div>,
      )
      const box = container.querySelector<HTMLElement>('.vtb')
      expect(box?.className).toContain('is-tl')
      expect(box?.style.getPropertyValue('--vtb-left')).toBe('320px')
      // The panel folded: the page starts at the spine, and so does the corner.
      left = 84
      act(() => {
        window.dispatchEvent(new Event('resize'))
      })
      expect(box?.style.getPropertyValue('--vtb-left')).toBe('84px')
    } finally {
      rect.mockRestore()
    }
  })
})

describe('when nothing is drawn', () => {
  it('draws nothing with no live session', () => {
    for (const phase of ['idle', 'connecting', 'ending'] as const) {
      held.current = fake({ phase }).session
      const { container, unmount } = render(<VoiceFloatLayer onTalk={false} go={vi.fn()} />)
      expect(container.innerHTML).toBe('')
      unmount()
    }
  })

  it('draws nothing with no holder, and nothing on Talk', () => {
    const bare = render(<VoiceFloatLayer onTalk={false} go={vi.fn()} />)
    expect(bare.container.innerHTML).toBe('')
    bare.unmount()
    held.current = fake().session
    const talk = render(<VoiceFloatLayer onTalk go={vi.fn()} />)
    expect(talk.container.innerHTML).toBe('')
  })

  it('clears the marker when the session ends', () => {
    noteCards(SESSION, list(2, card('c_plan', 'plan', 'proposed')))
    held.current = fake({ phase: 'idle' }).session
    render(
      <>
        <VoiceFloatLayer onTalk={false} go={vi.fn()} />
        <VoiceMarker />
      </>,
    )
    expect(screen.queryByLabelText(/new in Voice it/)).toBeNull()
  })
})
