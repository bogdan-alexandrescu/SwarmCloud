// VOICE IT, TALK: THE STAGE (direction A, the owner's pick of 5 November 2026).
//
//   * the stage draws the visual the routes answer first (the one drawn last):
//     its tab, where it came from and its line range, its numbered lines;
//   * a note's `lines` anchor bands its lines and carries its pin, and the pin
//     opens the note's words;
//   * a read-back the gate holds is a card with what was heard, what will run,
//     its clock, and Go and Not that, each posted to its read-back route;
//   * on a phone (390) nothing is wider than the page and nothing pans: the
//     lines wrap, the frame has no window of its own, the tabs wrap instead of
//     scrolling with no bar, and the caption strip wraps its words.
//
// MUTATIONS: draw the body without the visual's first line number; drop the
// band from a noted line; post Go to not-that; set the tabs back to
// `overflow-x: auto` or the code back to `white-space: pre`; give the hold a
// fixed 320px column on the phone.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import STAGE_CSS from '../styles/talk-stage.css?raw'
import TALK_CSS from '../styles/talk-pane.css?raw'
import STYLES from '../styles.css?raw'
import { CardList } from '../voice/CardList'
import { cascade } from './cssgate'

const fetchMock = vi.hoisted(() => {
  // The routes, not api.ts's fixtures: fixture mode is decided when api.ts loads.
  vi.stubEnv('VITE_LIVE', '1')
  return { read: vi.fn(), write: vi.fn() }
})

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  USE_FIXTURES: false,
}))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { stageFeed, TalkStage, type StageFeed } from '../voice/Stage'

const ok = (data: unknown) => ({ status: 'ok', fetchedAt: Date.now(), data })

const VISUAL = {
  visual_id: 'vv_slice',
  kind: 'code',
  title: 'WorkflowSteps.tsx',
  version: 2,
  source: { repo: 'acme-labs/console', ref: 'main', path: 'src/WorkflowSteps.tsx', from: 40, to: 44, pull: null },
  parts: [],
  lines: { from: 40, to: 44 },
  body_ref: 'tenants/t/voice/vp_t/visuals/vv_slice/2.txt',
  created_by: 'planner',
  session_id: 'vs_t',
  project_id: 'vp_t',
  created_at: '2026-11-06T09:00:00Z',
  updated_at: '2026-11-06T09:01:00Z',
}
const LOG = { ...VISUAL, visual_id: 'vv_log', kind: 'log', title: 'e2e, step 4', source: { task_id: 'task_e2e', step: 'e2e' }, lines: { from: 1, to: 1 } }
const BODY = [
  'export function StepFilters() {',
  '  const counts = useMemo(() => countByState(steps), [steps])',
  '  return (',
  '    <div className="wf-filters" role="toolbar" aria-label="Filter steps">',
  '  )',
].join('\n')
const NOTE = {
  note_id: 'vn_one',
  visual_id: 'vv_slice',
  visual_version: 2,
  anchor: { type: 'lines', from: 42, to: 43 },
  text: 'Keep the filter row in view when the steps scroll.',
  by: 'alice@example.com',
  via: 'voice',
  state: 'open',
  work: null,
  created_at: '2026-11-06T09:02:00Z',
  updated_at: '2026-11-06T09:02:00Z',
  session_id: 'vs_t',
}
const HOLD = {
  readback_id: 'rb_one',
  session_id: 'vs_t',
  heard: 'cancel the nightly run',
  action: 'cancel',
  target_name: 'run nightly-e2e #482',
  method: 'POST',
  route: '/v1/runs/{id}/cancel',
  state: 'pending',
  card_id: null,
  expires_at: '',
}

/** The routes' answers, by template, as swarm-api serves them. */
function answer(hold: unknown) {
  fetchMock.read.mockImplementation(async (target: { template: string }) => {
    if (target.template === '/v1/voice/projects/{project_id}/visuals') return ok({ visuals: [VISUAL, LOG] })
    if (target.template === '/v1/voice/projects/{project_id}/visuals/{visual_id}') return ok({ visual: VISUAL, body: BODY, notes: [NOTE] })
    if (target.template === '/v1/voice/sessions/{session_id}/readbacks/pending') return ok({ readback: hold })
    throw new Error(`no answer for ${target.template}`)
  })
}

beforeEach(() => {
  fetchMock.read.mockReset()
  fetchMock.write.mockReset()
})
afterEach(() => vi.useRealTimers())

describe('the stage draws the visual the routes answer', () => {
  it('draws the visual, where it came from, its lines and its anchor', async () => {
    answer(null)
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const stage = await screen.findByRole('region', { name: 'Stage' })
    const tabs = within(stage).getByRole('navigation', { name: 'Visuals' })
    expect(within(tabs).getByRole('button', { name: /WorkflowSteps\.tsx/ }).getAttribute('aria-pressed')).toBe('true')
    expect(within(tabs).getByRole('button', { name: /e2e, step 4/ }).getAttribute('aria-pressed')).toBe('false')
    const frame = await within(stage).findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(within(frame).getByText('src/WorkflowSteps.tsx @ main')).toBeTruthy()
    expect(within(frame).getByText('L40–44')).toBeTruthy()
    // The body is numbered from the slice's own first line, not from 1.
    const lines = [...frame.querySelectorAll<HTMLElement>('li.vts-line')]
    expect(lines.map((l) => l.dataset.line)).toEqual(['40', '41', '42', '43', '44'])
    expect(lines[3]!.textContent).toContain('role="toolbar"')
    // The note's lines anchor bands lines 42 and 43, and nothing else.
    expect(lines.filter((l) => l.classList.contains('is-noted')).map((l) => l.dataset.line)).toEqual(['42', '43'])
    expect(fetchMock.read.mock.calls.map(([t]) => t.url)).toContain('/v1/voice/projects/vp_t/visuals/vv_slice')
  })

  it('pins a note on its first line, counts it on its tab, and opens its words', async () => {
    answer(null)
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const pin = await screen.findByRole('button', { name: 'Note 1' })
    expect(pin.closest('li')!.getAttribute('data-line')).toBe('42')
    expect(screen.getByLabelText('1 notes').textContent).toBe('1')
    expect(screen.queryByRole('note')).toBeNull()
    fireEvent.click(pin)
    expect(within(screen.getByRole('note')).getByText(NOTE.text)).toBeTruthy()
  })

  it('draws nothing while the project has no visual and nothing is held', async () => {
    fetchMock.read.mockImplementation(async (target: { template: string }) =>
      target.template.endsWith('/pending') ? ok({ readback: null }) : ok({ visuals: [] }),
    )
    const { container } = render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(fetchMock.read).toHaveBeenCalled()
    expect(container.innerHTML).toBe('')
  })
})

describe('the read-back hold', () => {
  const held = () => ({ ...HOLD, expires_at: new Date(Date.now() + 40_000).toISOString() })

  it('shows what was heard, what will run and its clock', async () => {
    answer(held())
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const card = await screen.findByRole('region', { name: 'Read-back' })
    expect(within(card).getByText('“cancel the nightly run”')).toBeTruthy()
    expect(within(card).getByText('Will cancel')).toBeTruthy()
    expect(within(card).getByText('run nightly-e2e #482')).toBeTruthy()
    expect(within(card).getByText(/^\d+s$/).textContent).toMatch(/^(39|40)s$/)
    expect(fetchMock.read.mock.calls.map(([t]) => t.url)).toContain('/v1/voice/sessions/vs_t/readbacks/pending')
  })

  it('posts Go to the go route and draws the answer', async () => {
    answer(held())
    fetchMock.write.mockResolvedValue(ok({ readback: { ...HOLD, state: 'confirmed' }, ran: true }))
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const card = await screen.findByRole('region', { name: 'Read-back' })
    await act(async () => {
      fireEvent.click(within(card).getByRole('button', { name: 'Go' }))
    })
    expect(fetchMock.write).toHaveBeenCalledTimes(1)
    const [target, method] = fetchMock.write.mock.calls[0]!
    expect(target.url).toBe('/v1/voice/sessions/vs_t/readbacks/rb_one/go')
    expect(method).toBe('POST')
    expect(within(card).getByRole('status').textContent).toBe('Go')
    expect(within(card).queryByRole('button', { name: 'Not that' })).toBeNull()
  })

  it('posts Not that to the not-that route', async () => {
    answer(held())
    fetchMock.write.mockResolvedValue(ok({ readback: { ...HOLD, state: 'refused' } }))
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const card = await screen.findByRole('region', { name: 'Read-back' })
    await act(async () => {
      fireEvent.click(within(card).getByRole('button', { name: 'Not that' }))
    })
    const [target, method] = fetchMock.write.mock.calls[0]!
    expect(target.url).toBe('/v1/voice/sessions/vs_t/readbacks/rb_one/not-that')
    expect(method).toBe('POST')
    expect(within(card).getByRole('status').textContent).toBe('Not that')
  })

  it("shows swarm-api's own sentence when the answer is refused", async () => {
    answer(held())
    fetchMock.write.mockResolvedValue({ status: 'error', error: { message: 'That read-back is no longer pending; nothing ran.' } })
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const card = await screen.findByRole('region', { name: 'Read-back' })
    await act(async () => {
      fireEvent.click(within(card).getByRole('button', { name: 'Go' }))
    })
    expect(within(card).getByRole('alert').textContent).toBe('That read-back is no longer pending; nothing ran.')
  })

  it('draws the clock as a ring around an opaque disc, so its seconds sit on the surface', async () => {
    answer(held())
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live />)
    const card = await screen.findByRole('region', { name: 'Read-back' })
    const clock = within(card).getByText(/^\d+s$/)
    const sheets = [STYLES, STAGE_CSS].join('\n')
    for (const width of [1280, 390]) {
      const won = (prop: string | readonly string[]) => cascade(sheets, clock, prop, { width }).winner?.value ?? null
      // A mask with no composite never cuts the ring's hole, and a mask that
      // cut it would cut the seconds with it: the clock carries no mask.
      expect(won(['mask', '-webkit-mask', 'mask-image', '-webkit-mask-image'])).toBeNull()
      // The disc is the top layer, the conic fill the ring behind it.
      const bg = won(['background', 'background-image']) ?? ''
      expect(bg).toMatch(/^radial-gradient\(circle, var\(--surface\) \d+%, transparent \d+%\), conic-gradient\(/)
    }
  })

  it('asks for no read-back while no session is live', async () => {
    answer(held())
    render(<TalkStage projectId="vp_t" sessionId={null} live={false} />)
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(fetchMock.read.mock.calls.map(([t]) => t.template)).not.toContain('/v1/voice/sessions/{session_id}/readbacks/pending')
  })
})

describe('on a phone nothing is wider than 390 and nothing pans', () => {
  // The order the bundle loads them: Stage.tsx's import of talk-stage.css comes
  // before Talk.tsx's own import of talk-pane.css.
  const SHEETS = [STYLES, STAGE_CSS, TALK_CSS].join('\n')
  const PHONE = { width: 390 }
  const won = (el: Element, prop: string | readonly string[]): string | null => cascade(SHEETS, el, prop, PHONE).winner?.value ?? null
  /** The px a value fixes, summed over a track list ("4ch minmax(0, 1fr) 320px" fixes 320). */
  const fixedPx = (v: string | null): number => (v === null ? 0 : [...v.replace(/minmax\([^)]*\)/g, '').matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((n, m) => n + parseFloat(m[1]!), 0))

  async function drawn(): Promise<HTMLElement> {
    answer({ ...HOLD, expires_at: new Date(Date.now() + 40_000).toISOString() })
    render(
      <div className="vt has-stage">
        <TalkStage projectId="vp_t" sessionId="vs_t" live />
        <div className="vt-subs">
          <p className="vt-cap is-you">
            <span className="vt-cap-who">You</span>
            <span className="vt-cap-text">keep the filter row in view when the steps scroll past the top</span>
          </p>
        </div>
      </div>,
    )
    await screen.findByRole('region', { name: 'Read-back' })
    await screen.findByRole('button', { name: 'Note 1' })
    return document.querySelector<HTMLElement>('.vt')!
  }

  it('fixes no width, minimum width or column wider than the page', async () => {
    const page = await drawn()
    const wide: string[] = []
    for (const el of [page, ...page.querySelectorAll('*')]) {
      for (const prop of ['width', 'min-width', 'grid-template-columns', 'flex-basis']) {
        const px = fixedPx(won(el, prop))
        if (px > 390 - 2 * 10) wide.push(`${el.className || el.tagName} ${prop} ${px}px`)
      }
    }
    expect(wide).toEqual([])
    // The hold takes the column above the visual, not a 320px column beside it.
    expect(won(page.querySelector('.vts-main')!, 'grid-template-columns')).toBe('minmax(0, 1fr)')
  })

  it('wraps every line of the visual and gives it no window of its own', async () => {
    const page = await drawn()
    for (const code of page.querySelectorAll('.vts-code')) {
      expect(won(code, 'white-space')).toBe('pre-wrap')
      expect(won(code, 'overflow-wrap')).toBe('anywhere')
    }
    const frame = page.querySelector('.vts-frame')!
    expect(won(frame, ['overflow', 'overflow-x', 'overflow-y'])).toBe('visible')
    expect(won(frame, 'max-height')).toBe('none')
    const panning = [page, ...page.querySelectorAll('*')].filter((el) => /auto|scroll/.test(won(el, ['overflow', 'overflow-x']) ?? ''))
    expect(panning.map((el) => el.className)).toEqual([])
  })

  it('wraps the tabs instead of scrolling them with no bar', async () => {
    const page = await drawn()
    const tabs = page.querySelector('.vts-tabs')!
    expect(won(tabs, 'flex-wrap')).toBe('wrap')
    expect(won(tabs, ['overflow', 'overflow-x'])).toBeNull()
    expect(won(tabs, 'scrollbar-width')).toBeNull()
  })

  it('wraps the caption strip\'s words and rests it on whole lines', async () => {
    const page = await drawn()
    expect(won(page.querySelector('.vt-cap-text')!, 'white-space')).toBe('normal')
    expect(won(page.querySelector('.vt-cap-text')!, 'overflow-wrap')).toBe('anywhere')
    expect(won(page.querySelector('.vt-subs')!, 'scroll-snap-type')).toBe('y proximity')
    expect(won(page.querySelector('.vt-cap')!, 'scroll-snap-align')).toBe('end')
  })
})

// THE STAGE IS LIVE (part of #2688): the session's stream carries the
// planner's focus, a visual drawn or drawn again, and a note pinned or
// resolved (swarm_api/voice/stream.py `STAGE_TYPES`); the stage applies each
// without waiting for its next read.
//
// ONE STREAM (the owner's pick, 7 November 2026): the stage opens none of
// its own. The card list reads the session's one stream and passes the
// stage's events up (`onStageEvent`); Talk.tsx hands them to the stage
// through a `stageFeed()`.
//
// MUTATIONS: listen for none of the three; draw a visual.upsert without
// re-reading it; leave the stage on its tab after a focus; band no focused
// line; re-read nothing on a note.upsert; open a stream of the stage's own
// beside the card list's; drop the card list's hand-up of the stage's
// events; band the focus with an inline style instead of talk-stage.css's
// rule; leave the focus caption with no rule.

/** The session's stream, driven by the test. */
class FakeEventSource extends EventTarget {
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = 1
  onerror: ((e: Event) => void) | null = null
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
}

describe('the stage applies the session stream live', () => {
  let listed: unknown[]
  let notes: unknown[]
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    listed = [LOG]
    notes = []
    fetchMock.read.mockImplementation(async (target: { template: string; url: string }) => {
      if (target.template === '/v1/voice/projects/{project_id}/visuals') return ok({ visuals: listed })
      if (target.url.endsWith('/vv_slice')) return ok({ visual: VISUAL, body: BODY, notes })
      if (target.url.endsWith('/vv_log')) return ok({ visual: LOG, body: 'step e2e failed', notes: [] })
      if (target.template === '/v1/voice/sessions/{session_id}/readbacks/pending') return ok({ readback: null })
      throw new Error(`no answer for ${target.template}`)
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  const stream = (): FakeEventSource => {
    const s = FakeEventSource.all.find((e) => e.url.startsWith('/v1/voice/sessions/vs_t/events'))
    if (s === undefined) throw new Error('the card list opened no stream')
    return s
  }

  /** The stage and the card list as Talk.tsx seats them: the card list's stream feeds the stage. */
  function Live({ feed = stageFeed(), sessionId = 'vs_t', live = true }: { feed?: StageFeed; sessionId?: string | null; live?: boolean }) {
    return (
      <>
        <TalkStage projectId="vp_t" sessionId={sessionId} live={live} feed={feed} />
        <CardList sessionId={sessionId} onStageEvent={feed.emit} />
      </>
    )
  }

  it('draws a visual that arrives on the stream, then moves to the visual and lines the planner points at', async () => {
    render(<Live />)
    await screen.findByRole('figure', { name: 'e2e, step 4' })
    listed = [VISUAL, LOG]
    await act(async () => stream().frame('visual.upsert', 1, { visual: VISUAL }))
    const frame = await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(within(frame).getByText('L40–44')).toBeTruthy()

    await act(async () => stream().frame('focus', 2, { visual_id: 'vv_log', anchor: null, caption: null }))
    expect(screen.getByRole('button', { name: /e2e, step 4/ }).getAttribute('aria-pressed')).toBe('true')
    await screen.findByRole('figure', { name: 'e2e, step 4' })

    await act(async () =>
      stream().frame('focus', 3, { visual_id: 'vv_slice', anchor: { type: 'lines', from: 41, to: 42 }, caption: 'The counts are read here.' }),
    )
    const back = await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    const focused = [...back.querySelectorAll<HTMLElement>('li.vts-line.is-focus')].map((l) => l.dataset.line)
    expect(focused).toEqual(['41', '42'])
    expect(within(back).getByText('The counts are read here.')).toBeTruthy()
    // The page never reloaded: one stream, the card list's, opened once.
    expect(FakeEventSource.all.filter((e) => e.url.startsWith('/v1/voice/sessions/vs_t/events'))).toHaveLength(1)
    // The band is talk-stage.css's rule, not an inline style, and it wins over a note's band.
    const line = back.querySelector<HTMLElement>('li.vts-line[data-line="42"]')!
    expect(line.getAttribute('style')).toBeNull()
    const sheets = [STYLES, STAGE_CSS].join('\n')
    const won = (prop: string) => cascade(sheets, line, prop, { width: 1280 }).winner?.value ?? null
    expect(won('background')).toBe('color-mix(in srgb, var(--info) 12%, transparent)')
    expect(won('box-shadow')).toBe('inset 3px 0 0 var(--info)')
    line.classList.add('is-noted')
    expect(won('box-shadow')).toBe('inset 3px 0 0 var(--info)')
    // The planner's caption is talk-stage.css's rule too, set off in the focus colour on a line of its own.
    const caption = within(back).getByText('The counts are read here.')
    expect(caption.getAttribute('style')).toBeNull()
    const capWon = (prop: string) => cascade(sheets, caption, prop, { width: 1280 }).winner?.value ?? null
    expect(capWon('color')).toBe('var(--info)')
    expect(capWon('flex-basis')).toBe('100%')
  })

  it("shares the card list's stream: one connection for the session, each reading its own events", async () => {
    render(<Live />)
    await screen.findByRole('figure', { name: 'e2e, step 4' })
    expect(FakeEventSource.all).toHaveLength(1)
    listed = [VISUAL, LOG]
    await act(async () => stream().frame('visual.upsert', 1, { visual: VISUAL }))
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(screen.getByText('No cards in this session yet.')).toBeTruthy()
    expect(FakeEventSource.all).toHaveLength(1)
  })

  it('opens no stream of its own: a live stage with no card list reads nothing', async () => {
    listed = [VISUAL]
    render(<TalkStage projectId="vp_t" sessionId="vs_t" live feed={stageFeed()} />)
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(FakeEventSource.all).toHaveLength(0)
  })

  it("hands the stage's events up from the card list, and only those", async () => {
    const handed: string[] = []
    render(<CardList sessionId="vs_t" onStageEvent={(ev) => handed.push(`${ev.type}:${ev.seq}`)} />)
    expect(FakeEventSource.all).toHaveLength(1)
    await act(async () => {
      stream().frame('visual.upsert', 1, { visual: VISUAL })
      stream().frame('card.upsert', 2, { card: { card_id: 'vc_x', kind: 'nope', version: 1 } })
      stream().frame('note.upsert', 3, { note: NOTE })
      stream().frame('focus', 4, { visual_id: 'vv_slice', anchor: null, caption: null })
    })
    expect(handed).toEqual(['visual.upsert:1', 'note.upsert:3', 'focus:4'])
  })

  it("moves the stage's focus on a focus event delivered through the card list", async () => {
    listed = [VISUAL, LOG]
    const feed = stageFeed()
    render(<Live feed={feed} />)
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    await act(async () => stream().frame('focus', 1, { visual_id: 'vv_log', anchor: null, caption: null }))
    expect(screen.getByRole('button', { name: /e2e, step 4/ }).getAttribute('aria-pressed')).toBe('true')
    await screen.findByRole('figure', { name: 'e2e, step 4' })
  })

  it('re-reads the visual a note.upsert names, so a pinned note appears and a resolved one leaves', async () => {
    listed = [VISUAL]
    render(<Live />)
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(screen.queryByRole('button', { name: 'Note 1' })).toBeNull()
    notes = [NOTE]
    await act(async () => stream().frame('note.upsert', 1, { note: NOTE }))
    expect(await screen.findByRole('button', { name: 'Note 1' })).toBeTruthy()
    notes = [{ ...NOTE, state: 'resolved' }]
    await act(async () => stream().frame('note.upsert', 2, { note: { ...NOTE, state: 'resolved' } }))
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByRole('button', { name: 'Note 1' })).toBeNull()
  })

  it('opens no stream while no session is live', async () => {
    listed = [VISUAL]
    render(<Live sessionId={null} live={false} />)
    await screen.findByRole('figure', { name: 'WorkflowSteps.tsx' })
    expect(FakeEventSource.all).toHaveLength(0)
  })
})
