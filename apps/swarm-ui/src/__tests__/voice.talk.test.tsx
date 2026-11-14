// VOICE IT'S TALK PANE: the call and the captions (docs/design/voice-it.md
// §3.5, §3.10; section 10, phase one, "The Talk pane plays the agent from the
// WebRTC track and captions both sides as the speech arrives").
//
//   * Start asks for the mic with echo cancellation, makes one peer with the
//     mic's track and an events channel, sends the offer to swarm-api's Voice
//     it session route and applies the answer it returns;
//   * the agent's voice plays from the WebRTC track itself, on an <audio>
//     element, and the oscilloscope's analyser is tapped beside it: the
//     analyser is connected to nothing, so the voice never goes through Web
//     Audio and the browser's echo cancellation stays on;
//   * captions of both sides land in the role=log region; a partial line is
//     drawn muted outside it, so only final segments are announced;
//   * End posts the session's end, closes the peer and stops the mic;
//   * after Resume (Projects.tsx hands the project over), the first Start
//     talking sends that project as project_id, so the session continues it;
//     a later start opens a new one.
//
// MUTATIONS: put the partial line inside the log; connect the analyser to the
// context's destination; drop the end POST; leave the mic track running;
// start without the resumed project.
//
// THE CARDS BESIDE IT (section 10, phase two, "The console renders work,
// review, plan, budget, stop and details cards"): the list reads the
// session's card stream, keeps cards in the order they arrived, updates a
// card in place by id and only for a newer version, draws no card of an
// unknown kind and logs that kind once, and a resumed stream (a repeat, or a
// reconnect from `?after=`) never draws a card twice.
//
// MUTATIONS: draw a card whose kind the catalogue lacks; append an upsert of
// a known card id; accept an older version; drop the sequence check; reopen
// the stream without `after`.
//
// THE CARDS' STYLESHEET: every `vt-card*` class the card markup carries has a
// rule in styles/talk-pane.css, and the rules draw from console tokens only.
//
// MUTATIONS: drop a card rule; give a card rule a literal colour.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { cascade } from './cssgate'
import { colour, contrast, over, probe, resolveSheet, resolveVars, tokenTables, FLOORS, type RGBA } from './spaceprobe'
import { FakeAudioContext, FakePeer, FakeStream, FakeTrack, installMedia, settle } from './voice.fakes'

// The deployment's engine as Settings reads it: GPT-Live unless a test names another; 'pending' is a read not answered yet.
const fetchMock = vi.hoisted(() => ({ write: vi.fn(), engine: 'gpt-live' as string | null, debug: false }))
const vadMock = vi.hoisted(() => ({
  start: vi.fn(),
  setMic: vi.fn(),
  close: vi.fn(),
  handlers: null as null | { onStart: () => void; onFrame: (f: ArrayBuffer) => void; onEnd: () => void },
}))
const apiMock = vi.hoisted(() => ({ fixtures: false, loadWorkflow: vi.fn(), loadVoiceVisuals: null as null | ReturnType<typeof vi.fn> }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // The stage's list read, passed through to api.ts: a test reads which project it asked for.
  apiMock.loadVoiceVisuals = vi.fn(actual.loadVoiceVisuals)
  return {
    ...actual,
    loadVoiceVisuals: apiMock.loadVoiceVisuals,
    get USE_FIXTURES() {
      return apiMock.fixtures
    },
    loadWorkflow: apiMock.loadWorkflow,
  }
})

vi.mock('../AwaitInputCard', () => ({
  AwaitInputCard: ({ workflowId, stepId }: { workflowId: string; stepId: string }) => (
    <section data-testid="await-input" data-workflow={workflowId} data-step={stepId}>
      the await-input card
    </section>
  ),
}))

vi.mock('../fetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../fetch')>()
  return {
    ...actual,
    write: fetchMock.write,
    // Settings' engine, when a test names one; every other read is the real one.
    read: ((target, ...rest) =>
      target.url === '/v1/voice/settings' && fetchMock.engine === 'pending'
        ? new Promise(() => undefined)
        : target.url === '/v1/voice/settings' && fetchMock.engine !== null
          ? Promise.resolve({ status: 'ok', data: { settings: { engine: fetchMock.engine, debug: fetchMock.debug } }, fetchedAt: Date.now() })
          : actual.read(target, ...rest)) as typeof actual.read,
  }
})

// Voice detection, faked: a test plays the gate's events and reads what the mic told it.
vi.mock('../voice/vad', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../voice/vad')>()),
  startVoiceDetection: vadMock.start,
}))

import { RESUME_KEY } from '../voice/Projects'
import { CardList, FIXTURE_CARDS, applyStreamEvent, EMPTY_LIST, type VoiceCard } from '../voice/CardList'
import { CARD_KINDS } from '../voice/cards'
import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'
import { applyCaption, captionEvent, type CaptionLine } from '../voice/captions'
import { connectTalk } from '../voice/rtc'

const SESSION = 'vs-0001'

function answerStarts(): void {
  fetchMock.write.mockImplementation(async (target: { url: string }) => {
    if (target.url === '/v1/voice/sessions') {
      return { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

async function start(): Promise<FakePeer> {
  // The page reads Settings' engine when it opens: GPT-Live sends the offer.
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
  return FakePeer.last
}

describe('the Talk pane', () => {
  let mic: FakeTrack
  let getUserMedia: ReturnType<typeof vi.fn>
  beforeEach(() => {
    ;({ mic, getUserMedia } = installMedia({ audioContext: true }))
    answerStarts()
  })
  afterEach(() => {
    fetchMock.write.mockReset()
    window.sessionStorage.clear()
  })

  it('starts the session through swarm-api with the offer, and applies the answer', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    expect(pc.sent.map((s) => s.track)).toEqual([mic])
    expect(pc.channels.map((c) => c.label)).toEqual(['oai-events'])
    const [target, method, body] = fetchMock.write.mock.calls[0] ?? []
    expect(target.url).toBe('/v1/voice/sessions')
    expect(method).toBe('POST')
    expect(body).toEqual({ offer_sdp: 'v=0 fake-offer' })
    expect(pc.remote).toEqual({ type: 'answer', sdp: 'v=0 fake-answer' })
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
  })

  it("plays the agent from the WebRTC track and taps the analyser beside it, connected to nothing", async () => {
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    const voice = new FakeStream([new FakeTrack()])
    await act(async () => pc.remoteTrack(voice))
    const audio = container.querySelector('audio') as HTMLAudioElement & { srcObject: unknown }
    expect(audio).not.toBeNull()
    expect(audio.srcObject).toBe(voice)
    expect(audio.autoplay).toBe(true)
    const ctx = FakeAudioContext.all.find((c) => c.sources.some((s) => s.stream === voice))
    expect(ctx).toBeDefined()
    const source = ctx?.sources.find((s) => s.stream === voice)
    expect(source).toBeDefined()
    const analyser = ctx?.analysers.find((a) => source?.connect.mock.calls.some((c) => c[0] === a))
    expect(analyser?.fftSize).toBe(2048)
    // Nothing reaches the speakers through Web Audio: the analyser is a dead end.
    expect(analyser?.connect).not.toHaveBeenCalled()
    for (const s of ctx?.sources ?? []) {
      expect(s.connect.mock.calls.some((c) => c[0] === ctx?.destination)).toBe(false)
    }
  })

  it('lands both sides in the log, and announces only final segments', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    const log = screen.getByRole('log', { name: 'Captions' })
    expect(log.getAttribute('aria-live')).toBe('polite')

    await act(async () => pc.events.deliver({ type: 'session.input_transcript.delta', item_id: 'u1', delta: 'Add CSV' }))
    // Partial: on screen, muted, outside the log and hidden from the reader.
    expect(within(log).queryByText(/Add CSV/)).toBeNull()
    const partial = screen.getByText(/Add CSV/)
    expect(partial.closest('[aria-hidden="true"]')).not.toBeNull()
    expect(partial.closest('.is-partial')).not.toBeNull()

    await act(async () =>
      pc.events.deliver({ type: 'session.input_transcript.done', item_id: 'u1', transcript: 'Add CSV export to Runs.' }),
    )
    expect(within(log).getByText('Add CSV export to Runs.')).toBeTruthy()
    expect(screen.queryByText('Add CSV')).toBeNull()

    await act(async () => pc.events.deliver({ type: 'session.output_transcript.delta', item_id: 'a1', delta: 'On it, ' }))
    await act(async () => pc.events.deliver({ type: 'session.output_transcript.delta', item_id: 'a1', delta: 'drawing it.' }))
    expect(within(log).queryByText(/On it/)).toBeNull()
    expect(screen.getByText(/On it, drawing it\./).closest('[aria-hidden="true"]')).not.toBeNull()
    await act(async () => pc.events.deliver({ type: 'session.output_transcript.done', item_id: 'a1', transcript: 'On it, drawing it.' }))

    const finals = Array.from(log.querySelectorAll('.vt-cap')).map((p) => [
      p.querySelector('.vt-cap-who')?.textContent,
      p.querySelector('.vt-cap-text')?.textContent,
    ])
    expect(finals).toEqual([
      ['You', 'Add CSV export to Runs.'],
      ['Voice it', 'On it, drawing it.'],
    ])
    // Events that carry no caption change nothing.
    await act(async () => pc.events.deliver({ type: 'session.updated' }))
    expect(log.querySelectorAll('.vt-cap')).toHaveLength(2)
  })

  it('ends the session through swarm-api, closes the peer and stops the mic', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    const ended = fetchMock.write.mock.calls.find(([t]) => t.url === `/v1/voice/sessions/${SESSION}/end`)
    expect(ended?.[1]).toBe('POST')
    expect(pc.closed).toBe(true)
    expect(mic.stop).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
  })

  it('says why when swarm-api refuses the start, and holds no mic open', async () => {
    fetchMock.write.mockResolvedValue({ status: 'error', error: { kind: 'forbidden', httpStatus: 403, code: null, message: 'Voice it is for its owner only.' } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    expect(screen.getByRole('alert').textContent).toBe('Voice it is for its owner only.')
    expect(pc.closed).toBe(true)
    expect(mic.stop).toHaveBeenCalled()
  })

  it('continues the resumed project at the first Start talking, and opens a new one after', async () => {
    const resumed = 'vp_' + '7'.repeat(32)
    window.sessionStorage.setItem(RESUME_KEY, resumed)
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const starts = () => fetchMock.write.mock.calls.filter(([t]) => t.url === '/v1/voice/sessions')
    expect(starts()[0]?.[2]).toEqual({ offer_sdp: 'v=0 fake-offer', project_id: resumed })
    expect(window.sessionStorage.getItem(RESUME_KEY)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    await start()
    expect(starts()[1]?.[2]).toEqual({ offer_sdp: 'v=0 fake-offer' })
  })

  it("keeps a new session's project_id on the call, and the stage reads that project's visuals", async () => {
    const project = 'vp_' + '9'.repeat(32)
    fetchMock.write.mockImplementation(async (target: { url: string }) =>
      target.url === '/v1/voice/sessions'
        ? { status: 'ok', data: { project_id: project, session_id: SESSION, answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
        : { status: 'ok', data: null, fetchedAt: Date.now() },
    )
    const call = await connectTalk({ onEvent: () => undefined, onVoice: () => undefined, onDropped: () => undefined })
    expect(call.projectId).toBe(project)
    call.hangUp()

    apiMock.loadVoiceVisuals?.mockClear()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    expect(apiMock.loadVoiceVisuals?.mock.calls.map(([p]) => p)).toContain(project)
  })

  it('keeps the resumed project for the next try when the first start is refused', async () => {
    const resumed = 'vp_' + '8'.repeat(32)
    window.sessionStorage.setItem(RESUME_KEY, resumed)
    fetchMock.write.mockResolvedValueOnce({ status: 'error', error: { kind: 'unavailable', httpStatus: 503, code: null, message: 'Voice it is not deployed.' } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    await start()
    const starts = fetchMock.write.mock.calls.filter(([t]) => t.url === '/v1/voice/sessions')
    expect(starts.map(([, , body]) => body.project_id)).toEqual([resumed, resumed])
  })

  it('draws the card area empty before a session, and says cards appear as you talk', () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const cards = screen.getByRole('region', { name: 'Cards' })
    expect(cards.textContent).toMatch(/no cards yet/i)
    expect(cards.textContent).not.toMatch(/phase two/i)
    expect(cards.querySelectorAll('article')).toHaveLength(0)
  })
})

describe('caption events', () => {
  it('reads the Live names and the Realtime names, and nothing else', () => {
    expect(captionEvent({ type: 'session.input_transcript.delta', item_id: 'u', delta: 'a' })).toEqual({ side: 'you', key: 'u', text: 'a', final: false })
    expect(captionEvent({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'u', transcript: 'b' })).toEqual({
      side: 'you',
      key: 'u',
      text: 'b',
      final: true,
    })
    expect(captionEvent({ type: 'response.output_audio_transcript.done', item_id: 'a', transcript: 'c' })).toEqual({ side: 'agent', key: 'a', text: 'c', final: true })
    expect(captionEvent({ type: 'response.output_audio_transcript.delta', item_id: 'a', delta: 'c' })).toEqual({ side: 'agent', key: 'a', text: 'c', final: false })
    expect(captionEvent({ type: 'session.updated' })).toBeNull()
    expect(captionEvent('not json')).toBeNull()
  })

  it('grows a partial line by its deltas and replaces it with the final text', () => {
    let lines: CaptionLine[] = []
    lines = applyCaption(lines, { side: 'agent', key: 'a', text: 'One ', final: false })
    lines = applyCaption(lines, { side: 'agent', key: 'a', text: 'two', final: false })
    expect(lines.map((l) => [l.text, l.final])).toEqual([['One two', false]])
    lines = applyCaption(lines, { side: 'agent', key: 'a', text: 'One, two.', final: true })
    expect(lines.map((l) => [l.text, l.final])).toEqual([['One, two.', true]])
    // A final whose deltas never came still lands; an empty final drops the line.
    lines = applyCaption(lines, { side: 'you', key: 'u', text: 'Three.', final: true })
    lines = applyCaption(lines, { side: 'you', key: 'v', text: 'um', final: false })
    lines = applyCaption(lines, { side: 'you', key: 'v', text: ' ', final: true })
    expect(lines.map((l) => l.text)).toEqual(['One, two.', 'Three.'])
  })
})

// ---- the cards ---------------------------------------------------------------

/** An EventSource the test drives: frames carry their sequence as the SSE id. */
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
  upsert(seq: number, card: VoiceCard | Record<string, unknown>): void {
    this.frame('card.upsert', seq, { card })
  }
  /** The server refused or the session ended: EventSource gives up. */
  giveUp(): void {
    this.readyState = 2
    this.onerror?.(new Event('error'))
  }
  static get last(): FakeEventSource {
    const s = FakeEventSource.all[FakeEventSource.all.length - 1]
    if (s === undefined) throw new Error('no EventSource was opened')
    return s
  }
}

function card(kind: string, id: string, version: number, title: string, body: Record<string, unknown> = {}): VoiceCard {
  const fixture = FIXTURE_CARDS.find((c) => c.kind === kind)
  const base = fixture ?? FIXTURE_CARDS[0]
  if (base === undefined) throw new Error('no fixture cards')
  return {
    ...base,
    kind,
    card_id: id,
    version,
    title,
    body: { ...(fixture?.body ?? {}), ...body },
  }
}

function drawn(root: ParentNode = document): string[] {
  return Array.from(root.querySelectorAll('article')).map((a) => `${a.getAttribute('data-kind')}:${a.getAttribute('data-card-id')}`)
}

const LISTED = ['work', 'review', 'plan', 'budget', 'stop', 'details_input']

describe('the cards beside the Talk pane', () => {
  let warn: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    apiMock.fixtures = false
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    warn.mockRestore()
    apiMock.loadWorkflow.mockReset()
    fetchMock.write.mockReset()
  })

  it('has a fixture card of each kind this lane draws, all in the catalogue', () => {
    expect(FIXTURE_CARDS.map((c) => c.kind)).toEqual(LISTED)
    const known = new Set(CARD_KINDS.map((k) => k.kind))
    for (const c of FIXTURE_CARDS) {
      expect(known.has(c.kind)).toBe(true)
      const spec = CARD_KINDS.find((k) => k.kind === c.kind)
      expect(spec?.states).toContain(c.state)
      // Every body field is one the catalogue names for the kind.
      expect(Object.keys(c.body ?? {}).every((f) => spec?.fields.includes(f))).toBe(true)
    }
  })

  it('renders each kind from its fixture, with the links out and nothing that acts', () => {
    const { container } = render(<CardList sessionId={null} fixtures={FIXTURE_CARDS} />)
    expect(drawn(container)).toEqual(FIXTURE_CARDS.map((c) => `${c.kind}:${c.card_id}`))

    const work = screen.getByRole('article', { name: 'Work: Card renderers lane' })
    expect(within(work).getByRole('link', { name: 'Open in Work' }).getAttribute('href')).toBe('#work/workflows?wf=wf-fixture')
    expect(work.textContent).toContain('example/repo')
    expect(work.textContent).toContain('apps/swarm-ui/src/voice/CardList.tsx')
    expect(work.textContent).toContain('$6 reserved')

    const review = screen.getByRole('article', { name: 'Review: Cards render beside the Talk pane' })
    expect(within(review).getByRole('link', { name: 'Open diff' }).getAttribute('href')).toBe('https://github.com/example/repo/pull/1/files')
    expect(review.textContent).toContain('checks pass')
    expect(review.textContent).toContain('review: merge')

    const plan = screen.getByRole('article', { name: 'Plan: Voice it cards' })
    expect(plan.textContent).toContain('Draw every card kind beside the Talk pane.')
    expect(within(plan).getByRole('list', { name: 'Waves' }).children).toHaveLength(2)
    expect(plan.textContent).toContain('Reserved against the session cap')

    const budget = screen.getByRole('article', { name: 'Budget: Budget' })
    expect(within(budget).getAllByRole('row').map((r) => r.querySelector('th, td')?.textContent)).toEqual(['Line', 'Talk', 'Lanes'])
    expect(budget.textContent).toContain('Claude subscription seats')

    const stop = screen.getByRole('article', { name: 'Stop: Held: a workflow file' })
    expect(stop.textContent).toContain('held: a workflow file')
    expect(within(stop).getByRole('link', { name: 'Open where you act' }).getAttribute('href')).toBe('https://github.com/example/repo/pull/1')

    const details = screen.getByRole('article', { name: 'Details: Which theme first?' })
    expect(details.textContent).toContain('Which theme should the mock-ups use first?')
    expect(within(details).getByLabelText('Options').textContent).toBe('LightDark')
    expect(details.textContent).toContain('Say which one, or answer in your own words')

    // Phase two spends nothing: no card draws a button that acts.
    expect(within(container).queryAllByRole('button')).toHaveLength(0)
  })

  it('draws only the catalogue fields, and links nowhere but the console and GitHub', () => {
    const odd = card('stop', 'vc_odd', 1, 'Held', { where: 'https://example.com/elsewhere', html: '<b>not drawn</b>' })
    render(<CardList sessionId={null} fixtures={[odd]} />)
    const stop = screen.getByRole('article', { name: 'Stop: Held' })
    expect(within(stop).queryByRole('link')).toBeNull()
    expect(stop.textContent).toContain('https://example.com/elsewhere')
    expect(stop.textContent).not.toContain('not drawn')
  })

  it('does not draw a card of an unknown kind, and logs the kind once', () => {
    const { container } = render(<CardList sessionId={null} fixtures={[card('work', 'vc_a', 1, 'A'), { ...card('work', 'vc_b', 1, 'B'), kind: 'hologram' }]} />)
    expect(drawn(container)).toEqual(['work:vc_a'])
  })

  it('reads the session stream: in order, in place by id and version, unknown kinds not drawn and logged once', async () => {
    answerStarts()
    installMedia({ audioContext: true })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const es = FakeEventSource.last
    expect(es.url).toBe(`/v1/voice/sessions/${SESSION}/events`)
    const region = screen.getByRole('region', { name: 'Cards' })

    await act(async () => es.upsert(1, card('work', 'vc_w', 1, 'Filed')))
    await act(async () => es.upsert(2, { ...card('work', 'vc_x', 1, 'Odd'), kind: 'hologram' }))
    await act(async () => es.upsert(3, { ...card('work', 'vc_y', 1, 'Odd too'), kind: 'hologram' }))
    await act(async () => es.upsert(4, card('review', 'vc_r', 1, 'Finished')))
    expect(drawn(region)).toEqual(['work:vc_w', 'review:vc_r'])
    expect(warn.mock.calls.filter((c) => String(c[0]).includes('hologram'))).toHaveLength(1)

    // A newer version replaces the card where it stands; an older one changes nothing.
    await act(async () => es.upsert(5, card('work', 'vc_w', 3, 'Running')))
    await act(async () => es.upsert(6, card('work', 'vc_w', 2, 'Stale')))
    expect(drawn(region)).toEqual(['work:vc_w', 'review:vc_r'])
    expect(within(region).getByRole('article', { name: 'Work: Running' })).toBeTruthy()
    expect(within(region).queryByRole('article', { name: 'Work: Stale' })).toBeNull()

    await act(async () => es.frame('card.remove', 7, { card_id: 'vc_r' }))
    expect(drawn(region)).toEqual(['work:vc_w'])
  })

  it('a resumed stream does not duplicate cards, and a reconnect asks for what comes after the last sequence', async () => {
    answerStarts()
    installMedia({ audioContext: true })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const first = FakeEventSource.last
    const region = screen.getByRole('region', { name: 'Cards' })
    await act(async () => first.upsert(1, card('plan', 'vc_p', 1, 'Plan')))
    await act(async () => first.upsert(2, card('budget', 'vc_b', 1, 'Budget')))
    await act(async () => first.frame('session.state', 3, { state: 'thinking' }))

    // The stream repeats itself after its own reconnect: nothing is drawn twice.
    await act(async () => first.upsert(1, card('plan', 'vc_p', 1, 'Plan')))
    await act(async () => first.upsert(2, card('budget', 'vc_b', 1, 'Budget')))
    expect(drawn(region)).toEqual(['plan:vc_p', 'budget:vc_b'])

    // EventSource gave up: Reconnect opens it again from the last sequence seen.
    await act(async () => first.giveUp())
    fireEvent.click(within(region).getByRole('button', { name: 'Reconnect' }))
    await settle(act)
    const second = FakeEventSource.last
    expect(second).not.toBe(first)
    expect(first.readyState).toBe(FakeEventSource.CLOSED)
    expect(second.url).toBe(`/v1/voice/sessions/${SESSION}/events?after=3`)
    await act(async () => second.upsert(2, card('budget', 'vc_b', 1, 'Budget')))
    await act(async () => second.upsert(4, card('stop', 'vc_s', 1, 'Held')))
    expect(drawn(region)).toEqual(['plan:vc_p', 'budget:vc_b', 'stop:vc_s'])
  })

  it('keeps the list pure: a repeated sequence is a no-op', () => {
    const once = applyStreamEvent(EMPTY_LIST, { type: 'card.upsert', seq: 1, data: { card: card('work', 'vc_w', 1, 'W') } })
    expect(applyStreamEvent(once, { type: 'card.upsert', seq: 1, data: { card: card('work', 'vc_w', 2, 'W2') } })).toBe(once)
  })

  it("draws a lane's waiting question with the console's await-input card, found by its task", async () => {
    apiMock.loadWorkflow.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { workflow: { workflow_id: 'wf-1', steps: [{ step_id: 'build', task_id: 't-0' }, { step_id: 'ask', task_id: 't-ask' }] }, tasks: [] },
    })
    const lane = { ...card('details_input', 'vc_d', 1, 'The lane asks', { task_id: 't-ask', question: 'Which?' }), references: { workflow: 'wf-1' } }
    render(<CardList sessionId={null} fixtures={[lane]} />)
    await settle(act)
    expect(apiMock.loadWorkflow).toHaveBeenCalledWith('wf-1')
    const ai = screen.getByTestId('await-input')
    expect([ai.getAttribute('data-workflow'), ai.getAttribute('data-step')]).toEqual(['wf-1', 'ask'])
    expect(screen.getByRole('article', { name: 'Details: The lane asks' }).contains(ai)).toBe(true)
  })

  it('a live session opens exactly one stream: the card list reads it and the stage is handed its events', async () => {
    installMedia({ audioContext: true })
    answerStarts()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    expect(FakeEventSource.all.map((e) => e.url)).toEqual([`/v1/voice/sessions/${SESSION}/events`])
  })

  it('in fixture mode, the Talk pane draws a fixture card of each kind before a session', () => {
    apiMock.fixtures = true
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const region = screen.getByRole('region', { name: 'Cards' })
    expect(drawn(region).map((d) => d.split(':')[0])).toEqual(LISTED)
  })
})

describe('the cards are seated by talk-pane.css', () => {
  const voice = join(__dirname, '..', 'voice')
  const markup = ['CardList.tsx', 'cards/WorkCards.tsx', 'cards/InputCards.tsx']
    .map((f) => readFileSync(join(voice, f), 'utf8'))
    .join('\n')
  const css = readFileSync(join(__dirname, '..', 'styles', 'talk-pane.css'), 'utf8')

  it('every vt-card class the markup carries has a rule', () => {
    const used = [...new Set([...markup.matchAll(/className="[^"]*?\b(vt-card[a-z-]*)/g)].map((m) => m[1]))].sort()
    expect(used.length).toBeGreaterThanOrEqual(12)
    const missing = used.filter((c) => !new RegExp(`\\.${c}(?![a-z-])`).test(css))
    expect(missing).toEqual([])
  })

  it('the card rules use console tokens, never a literal colour', () => {
    const rules = css.split('\n').filter((l) => l.includes('.vt-card'))
    expect(rules.length).toBeGreaterThan(0)
    for (const r of rules) expect(r).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i)
  })
})

// OUR OWN SPEECH ON THE TALK PAGE (docs/design/voice-it-own-speech.md §3.3,
// §4, §7, §9; section 14, row U1).
//
//   * Settings' engine `own`: Start opens the mic and posts no offer, opens no
//     peer, and a `speech` session opens the audio socket;
//   * the machine's boot is phases with no prose: each done one a tick named
//     for a reader, the current one in words; then the state word;
//   * both sides are captioned from the socket: partials muted outside the log;
//   * voice detection gates what is sent: its start, frames and end are the
//     socket's utterance; mute and push-to-talk reach it;
//   * no L4 at the start, or the engine down mid-session: text mode with one
//     line and a close button, the microphone kept beside a text box;
//   * the controls answer whole spoken phrases, and in text mode a Go is the
//     held read-back's answer, not a turn;
//   * a 422 for the offer is GPT-Live, which makes today's call.
//
// MUTATIONS: send the offer on our own engine; draw the boot as a sentence;
// open the socket for a text session; drop setMic on mute; stop the mic in
// text mode; keep the notice after Dismiss; send "go" as a turn; act on a
// control word inside a longer sentence; send the offer while the engine is
// not read yet.

class FakeSocket {
  static all: FakeSocket[] = []
  static get last(): FakeSocket {
    const s = FakeSocket.all[FakeSocket.all.length - 1]
    if (s === undefined) throw new Error('no socket was opened')
    return s
  }
  binaryType = 'blob'
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  sent: (string | ArrayBuffer)[] = []
  closed = false
  constructor(readonly url: string) {
    FakeSocket.all.push(this)
  }
  send(data: string | ArrayBuffer): void {
    this.sent.push(data)
  }
  close(): void {
    this.closed = true
    this.readyState = 3
  }
  texts(): unknown[] {
    return this.sent.filter((d): d is string => typeof d === 'string').map((d) => JSON.parse(d) as unknown)
  }
  async open(): Promise<void> {
    await act(async () => {
      this.readyState = 1
      this.onopen?.(new Event('open'))
    })
  }
  async json(m: Record<string, unknown>): Promise<void> {
    await act(async () => this.onmessage?.({ data: JSON.stringify(m) } as MessageEvent))
  }
}

type Started = { mode: 'speech' | 'text'; fallback?: Record<string, unknown> } | { offer: true }

function serveOwn(started: Started, readback: string | null = null): void {
  fetchMock.write.mockImplementation(async (target: { url: string }, _m: string, body: Record<string, unknown>) => {
    if (target.url === '/v1/voice/sessions') {
      if ('offer' in started) {
        if (body.offer_sdp === undefined) {
          return { status: 'error', error: { httpStatus: 422, message: 'offer_sdp is required', detail: { field: 'offer_sdp' } } }
        }
        return { status: 'ok', data: { session_id: SESSION, mode: 'live', answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
      }
      return { status: 'ok', data: { session_id: SESSION, ...started }, fetchedAt: Date.now() }
    }
    if (target.url === `/v1/voice/sessions/${SESSION}/readbacks/answer`) {
      return { status: 'ok', data: { answer: readback }, fetchedAt: Date.now() }
    }
    if (target.url === `/v1/voice/sessions/${SESSION}/turns`) {
      return { status: 'ok', data: { turn_id: 't-1', reply: 'Noted.', cards: [] }, fetchedAt: Date.now() }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

const posted = (url: string) => fetchMock.write.mock.calls.filter(([t]) => t.url === url)

async function startOwn(): Promise<void> {
  // The page reads Settings' engine when it opens.
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
}

describe('the Talk pane on our own speech', () => {
  let mic: FakeTrack
  beforeEach(() => {
    ;({ mic } = installMedia({ audioContext: true }))
    FakeSocket.all = []
    vi.stubGlobal('WebSocket', FakeSocket)
    // The fixture console makes today's call: an earlier suite may have left it on.
    apiMock.fixtures = false
    fetchMock.engine = 'own'
    vadMock.handlers = null
    vadMock.setMic.mockReset()
    vadMock.close.mockReset()
    vadMock.start.mockReset()
    vadMock.start.mockImplementation(async (_stream: unknown, handlers: NonNullable<typeof vadMock.handlers>) => {
      vadMock.handlers = handlers
      return { model: 'silero', setMic: vadMock.setMic, close: vadMock.close }
    })
  })
  afterEach(() => {
    fetchMock.write.mockReset()
    fetchMock.engine = 'gpt-live'
    vi.unstubAllGlobals()
    window.sessionStorage.clear()
  })

  it('starts with no offer and no peer, and opens the audio socket on the mic', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const [target, method, body] = fetchMock.write.mock.calls[0] ?? []
    expect([target.url, method, body]).toEqual(['/v1/voice/sessions', 'POST', {}])
    expect(FakePeer.all).toHaveLength(0)
    expect(FakeSocket.last.url).toMatch(new RegExp(`^wss?://[^/]+/v1/voice/sessions/${SESSION}/audio$`))
    expect(vadMock.start.mock.calls[0]?.[0]).toMatchObject({ tracks: [mic] })
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
  })

  it("shows the machine's boot as phases with no prose, then the state word", async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const ws = FakeSocket.last
    await ws.open()
    await ws.json({ t: 'status', state: 'starting machine' })
    await ws.json({ t: 'status', state: 'updating speech server' })
    await ws.json({ t: 'status', state: 'fetching model speech-small' })
    const phases = screen.getByRole('list', { name: 'Starting' })
    const items = within(phases).getAllByRole('listitem')
    // Done phases are ticks named for a reader; the current one is its words alone.
    expect(items.map((i) => i.getAttribute('aria-label'))).toEqual(['starting machine', 'updating speech server', null])
    expect(items.slice(0, 2).map((i) => i.textContent)).toEqual(['', ''])
    expect(items[2]?.textContent).toBe('fetching model')
    expect(items[2]?.getAttribute('aria-current')).toBe('step')
    await ws.json({ t: 'status', state: 'ready' })
    expect(screen.queryByRole('list', { name: 'Starting' })).toBeNull()
    expect(screen.getAllByRole('status')[0]?.textContent).toBe('listening')
    await ws.json({ t: 'status', state: 'thinking' })
    expect(screen.getAllByRole('status')[0]?.textContent).toBe('thinking')
  })

  it('captions both sides from the socket, the partial muted outside the log', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const ws = FakeSocket.last
    await ws.open()
    const log = screen.getByRole('log', { name: 'Captions' })
    await ws.json({ t: 'partial', seq: 1, text: 'Add a' })
    expect(within(log).queryByText(/Add a/)).toBeNull()
    expect(screen.getByText('Add a').closest('[aria-hidden="true"]')).not.toBeNull()
    await ws.json({ t: 'final', seq: 1, text: 'Add a chart to Runs.' })
    await ws.json({ t: 'say.start', id: 's1', text: 'Drawing it.' })
    const finals = Array.from(log.querySelectorAll('.vt-cap')).map((p) => [
      p.querySelector('.vt-cap-who')?.textContent,
      p.querySelector('.vt-cap-text')?.textContent,
    ])
    expect(finals).toEqual([
      ['You', 'Add a chart to Runs.'],
      ['Voice it', 'Drawing it.'],
    ])
    expect(screen.queryByText('Add a')).toBeNull()
  })

  it('sends only what voice detection lets through, as one utterance', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const ws = FakeSocket.last
    await ws.open()
    const h = vadMock.handlers
    expect(h).not.toBeNull()
    // Nothing detected, nothing sent.
    expect(ws.sent).toEqual([])
    const frame = new Uint8Array(640).buffer
    h?.onStart()
    h?.onFrame(frame)
    h?.onEnd()
    expect(ws.texts()).toEqual([
      { t: 'utt.start', seq: 1 },
      { t: 'utt.end', seq: 1 },
    ])
    expect(ws.sent.filter((d) => typeof d !== 'string')).toEqual([frame])
  })

  it('mute and push-to-talk reach voice detection, and End closes the socket, the detector and the mic', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    await FakeSocket.last.open()
    fireEvent.click(screen.getByRole('button', { name: 'Mute' }))
    await settle(act)
    expect(vadMock.setMic).toHaveBeenLastCalledWith(expect.objectContaining({ muted: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Unmute' }))
    fireEvent.click(screen.getByRole('button', { name: 'Push to talk' }))
    await settle(act)
    expect(vadMock.setMic).toHaveBeenLastCalledWith({ muted: false, mode: 'ptt', held: false })
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Hold to talk' }))
    await settle(act)
    expect(vadMock.setMic).toHaveBeenLastCalledWith({ muted: false, mode: 'ptt', held: true })
    fireEvent.pointerUp(screen.getByRole('button', { name: 'Hold to talk' }))
    await settle(act)
    expect(vadMock.setMic).toHaveBeenLastCalledWith({ muted: false, mode: 'ptt', held: false })

    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    expect(posted(`/v1/voice/sessions/${SESSION}/end`)).toHaveLength(1)
    expect(FakeSocket.last.closed).toBe(true)
    expect(vadMock.close).toHaveBeenCalled()
    expect(mic.stop).toHaveBeenCalled()
  })

  it('falls back to text mode when no L4 can start: one line a close button dismisses, the mic kept beside a text box', async () => {
    serveOwn({ mode: 'text', fallback: { from: 'own', reason: 'no_l4', zone: 'us-central1-a' } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    expect(FakeSocket.all).toHaveLength(0)
    expect(vadMock.start).not.toHaveBeenCalled()
    expect(screen.getByText('No L4 free in us-central1-a: text mode.')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Say to Voice it' })).toBeTruthy()
    // The browser's microphone stays: mute still works on it.
    expect(mic.stop).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Mute' }).hasAttribute('disabled')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('No L4 free in us-central1-a: text mode.')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Say to Voice it' })).toBeTruthy()
  })

  it('says behind its (i) that a no-L4 fallback is text mode on the account it holds, and when to retry', async () => {
    serveOwn({ mode: 'text', fallback: { from: 'own', reason: 'no_l4', zone: null } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const line = screen.getByText('No L4 free: text mode.').closest('p') as HTMLElement
    const tip = within(line).getByRole('tooltip', { hidden: true })
    expect(tip.textContent).toMatch(/continues as text with Voice it's Claude front, on the account it already holds/)
    expect(tip.textContent).toMatch(/Start again in a few minutes/)
    expect(line.textContent).not.toMatch(/OpenAI/)
    // The talk works: a typed line is a turn on the same session.
    fireEvent.change(screen.getByRole('textbox', { name: 'Say to Voice it' }), { target: { value: 'Plan it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await settle(act)
    expect(posted(`/v1/voice/sessions/${SESSION}/turns`).map(([, , b]) => b)).toEqual([{ text: 'Plan it' }])
  })

  it.each([
    ['no_l4', false],
    ['not_ready', false],
    ['front_refused', true],
    ['speech_not_deployed', true],
  ])('a %s fallback names the ChatGPT plan in its status line only when it runs on the plan', async (reason, plan) => {
    serveOwn({ mode: 'text', fallback: { from: 'own', reason, zone: null } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const status = () => document.querySelector('.vt-status')?.textContent ?? ''
    expect(status()).toMatch(/^Live/)
    expect(/ChatGPT plan/.test(status())).toBe(plan)
    // Dismissing the fallback's line does not change what the session runs on.
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(/ChatGPT plan/.test(status())).toBe(plan)
  })

  describe('a refused start says its own cause in one line, the full sentence behind an (i)', () => {
    function refuseStart(reason: string, message: string): void {
      fetchMock.write.mockImplementation(async (target: { url: string }) => {
        if (target.url === '/v1/voice/sessions') {
          return { status: 'error', error: { kind: 'conflict', httpStatus: 409, code: 'conflict', message, detail: { reason } } }
        }
        return { status: 'ok', data: null, fetchedAt: Date.now() }
      })
    }
    const problem = () => document.querySelector('.vt-problem') as HTMLElement
    const shown = () => problem().querySelector(':scope > span')?.textContent

    it.each([
      ['no_voice_account_available', 'No Voice it plan account is free for a text session; try again in a few minutes.', 'No Voice it plan account free.'],
      ['openai_key_in_use', "Voice it's OpenAI API key is in use by another session; try again when it ends.", 'OpenAI key in use by another session.'],
    ])('%s', async (reason, sentence, line) => {
      refuseStart(reason, sentence)
      render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
      await startOwn()
      expect(shown()).toBe(line)
      expect(within(problem()).getByRole('tooltip', { hidden: true }).textContent).toBe(sentence)
      expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
    })

    it('names the OpenAI key only when a Live start found it in use', async () => {
      refuseStart('no_voice_account_available', 'No Voice it plan account is free for a text session; try again in a few minutes.')
      render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
      await startOwn()
      expect(problem().textContent).not.toMatch(/OpenAI|another session/)
    })

    it("any other refusal shows swarm-api's sentence as it is, with no (i)", async () => {
      refuseStart('voice_pool_refused', 'Voice it needs an OpenAI API key in the Voice it pool: add one on Admin › Accounts')
      render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
      await startOwn()
      expect(problem().textContent).toBe('Voice it needs an OpenAI API key in the Voice it pool: add one on Admin › Accounts')
      expect(within(problem()).queryByRole('tooltip', { hidden: true })).toBeNull()
    })
  })

  it('carries on in text mode when the engine goes down mid-session, on the same mic', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const ws = FakeSocket.last
    await ws.open()
    await ws.json({ t: 'status', state: 'ready' })
    await ws.json({ t: 'status', state: 'engine down' })
    expect(ws.closed).toBe(true)
    expect(vadMock.close).toHaveBeenCalled()
    expect(screen.getByText('Engine down: text mode.')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Say to Voice it' })).toBeTruthy()
    expect(mic.stop).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
  })

  it('answers whole spoken phrases: "mute" mutes, a sentence with the word in it does not', async () => {
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    const ws = FakeSocket.last
    await ws.open()
    await ws.json({ t: 'final', seq: 1, text: 'Can you mute the alerts on Runs?' })
    expect(screen.getByRole('button', { name: 'Mute' })).toBeTruthy()
    await ws.json({ t: 'final', seq: 2, text: 'Mute.' })
    await settle(act)
    expect(screen.getByRole('button', { name: 'Unmute' })).toBeTruthy()
    await ws.json({ t: 'final', seq: 3, text: 'Stop the session.' })
    await settle(act)
    expect(posted(`/v1/voice/sessions/${SESSION}/end`)).toHaveLength(1)
  })

  it('in text mode, a typed Go answers the held read-back and is not sent as a turn', async () => {
    serveOwn({ mode: 'text', fallback: { from: 'own', reason: 'not_ready' } }, 'go')
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    expect(screen.getByText('Speech machine not ready: text mode.')).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox', { name: 'Say to Voice it' }), { target: { value: 'Go' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await settle(act)
    expect(posted(`/v1/voice/sessions/${SESSION}/readbacks/answer`).map(([, , b]) => b)).toEqual([{ text: 'Go' }])
    expect(posted(`/v1/voice/sessions/${SESSION}/turns`)).toHaveLength(0)
  })

  it('a Start before the Settings read answers sends no offer, so an own deployment boots its machine on the socket', async () => {
    fetchMock.engine = 'pending'
    serveOwn({ mode: 'speech' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)
    expect(posted('/v1/voice/sessions').map(([, , b]) => b)).toEqual([{}])
    expect(FakePeer.all).toHaveLength(0)
    expect(FakeSocket.all).toHaveLength(1)
    expect(posted(`/v1/voice/sessions/${SESSION}/end`)).toHaveLength(0)
  })

  it('a 422 for the offer is GPT-Live: the page makes the WebRTC call instead', async () => {
    serveOwn({ offer: true })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await startOwn()
    expect(posted('/v1/voice/sessions').map(([, , b]) => b)).toEqual([{}, { offer_sdp: 'v=0 fake-offer' }])
    expect(FakePeer.all).toHaveLength(1)
    expect(FakeSocket.all).toHaveLength(0)
  })

  // THE BOOT ROW AND THE NOTICE HAVE STYLES OF THEIR OWN (the owner's pick of
  // 8 November 2026, part of #2688): the boot is `vt-boot`, one wrapping row
  // with one gap between every step, a done step its grey check and the current
  // one its words after an open ring; the no-machine notice is `vt-fallback`,
  // a boxed line on the opaque surface with its close button pushed to the end.
  // Weighed with the shipped sheets in load order (talk-stage.css before
  // talk-pane.css, styles.css last) at 1440 and at 390, in both themes.
  //
  // MUTATIONS: give the boot back the status line's class; let the row scroll
  // or fix it wider than the phone; drop the current step's ring; draw the
  // notice as plain dim prose; move the close button off the line's end; put a
  // literal colour in either rule.
  describe('the boot row and the notice draw from talk-stage.css', () => {
    const styles = join(__dirname, '..', 'styles')
    const STAGE = readFileSync(join(styles, 'talk-stage.css'), 'utf8')
    const SHEETS = [STAGE, readFileSync(join(styles, 'talk-pane.css'), 'utf8'), readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')].join('\n')
    const TOKENS = tokenTables(SHEETS)
    const THEMES = ['dark', 'light'] as const
    const won = (el: Element, prop: string | readonly string[], width: number, pseudo: string | null = null) =>
      cascade(SHEETS, el, prop, { width }, pseudo).winner?.value ?? null
    /** A declared colour in `theme`, opaque over `ground` when it has alpha. */
    const ink = (value: string | null, theme: (typeof THEMES)[number], ground?: RGBA): RGBA => {
      const c = colour(resolveVars(value ?? '', TOKENS[theme]))
      if (c === null) throw new Error(`no colour in ${value}`)
      return ground === undefined ? c : over(c, ground)
    }
    const fixedPx = (v: string | null) => (v === null ? 0 : [...v.replace(/minmax\([^)]*\)/g, '').matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((n, m) => n + parseFloat(m[1]!), 0))
    const tooWide = (root: Element, width: number) =>
      [root, ...root.querySelectorAll('*')].flatMap((el) =>
        ['width', 'min-width', 'flex-basis'].filter((p) => fixedPx(won(el, p, width)) > 390 - 2 * 18).map((p) => `${el.className} ${p}`),
      )

    it('the new rules name console tokens, never a literal colour', () => {
      const rules = STAGE.slice(STAGE.indexOf('.vt-boot {'), STAGE.indexOf('.vt-fallback > button')).split('\n')
      expect(rules.filter((l) => /\.vt-(boot|fallback)/.test(l)).length).toBeGreaterThanOrEqual(5)
      for (const r of rules) expect(r).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i)
    })

    it('draws the boot as one even row of ticks, the current step set apart by its ring', async () => {
      serveOwn({ mode: 'speech' })
      render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
      await startOwn()
      const ws = FakeSocket.last
      await ws.open()
      await ws.json({ t: 'status', state: 'starting machine' })
      await ws.json({ t: 'status', state: 'updating speech server' })
      await ws.json({ t: 'status', state: 'fetching model speech-small' })
      const row = screen.getByRole('list', { name: 'Starting' })
      expect(row.className).toBe('vt-boot')
      const steps = within(row).getAllByRole('listitem')
      expect(steps.map((s) => s.className)).toEqual(['vt-boot-step is-done', 'vt-boot-step is-done', 'vt-boot-step is-now'])
      const now = steps[2]!
      const ground = (theme: (typeof THEMES)[number]) => ink(won(row.closest('.vt')!, ['background', 'background-color'], 1440), theme)
      for (const width of [1440, 390]) {
        expect(won(row, 'display', width)).toBe('inline-flex')
        // One row that wraps on a phone, never scrolls, with one gap between every step.
        expect(won(row, 'flex-wrap', width)).toBe('wrap')
        expect(won(row, ['overflow', 'overflow-x'], width)).toBeNull()
        expect(won(row, ['gap', 'column-gap'], width)).toMatch(/^(\d+px )?\d+px$/)
        for (const s of steps) expect([won(s, ['gap', 'column-gap', 'margin', 'margin-left', 'margin-right', 'padding'], width)]).toEqual(['6px'])
        // The current step: its words after an open ring, the done ones a check alone.
        expect(won(now, 'content', width, 'before')).toBe('""')
        expect(won(now, ['border', 'border-color'], width, 'before')).toBe('2px solid var(--info)')
        expect(won(steps[0]!, 'content', width, 'before')).toBeNull()
        expect(steps[0]!.querySelector('.sk-st')?.getAttribute('data-mark')).toBe('succeeded')
        expect(tooWide(row, width)).toEqual([])
      }
      for (const theme of THEMES) {
        const bg = ground(theme)
        // Words at 4.5:1, a check and a ring (non-text) at 3:1, in both themes.
        expect(contrast(ink(won(now, 'color', 1440), theme, bg), bg), `${theme} current words`).toBeGreaterThanOrEqual(4.5)
        const tick = steps[0]!.querySelector('.sk-st')!
        expect(contrast(ink(won(tick, 'color', 1440), theme, bg), bg), `${theme} done check`).toBeGreaterThanOrEqual(3)
        expect(contrast(ink('var(--info)', theme, bg), bg), `${theme} current ring`).toBeGreaterThanOrEqual(3)
      }
    })

    it('draws the notice as its own boxed line, its close button at the end', async () => {
      serveOwn({ mode: 'text', fallback: { from: 'own', reason: 'no_l4', zone: 'us-central1-a' } })
      render(
        <div className="app">
          <VoiceSessionHolder><TalkPane /></VoiceSessionHolder>
        </div>,
      )
      await startOwn()
      const line = screen.getByText('No L4 free in us-central1-a: text mode.').closest('p') as HTMLElement
      expect(line.className).toBe('vt-fallback')
      const close = within(line).getByRole('button', { name: 'Dismiss' })
      for (const width of [1440, 390]) {
        expect(won(line, 'display', width)).toBe('flex')
        expect(won(line, 'flex-basis', width)).toBe('100%')
        expect(won(line, 'background', width)).toBe('var(--surface)')
        expect(won(line, 'border', width)).toBe('1px solid var(--line)')
        expect(won(close, 'margin-left', width)).toBe('auto')
        expect(won(close, 'flex', width)).toBe('none')
        expect(won(line, 'overflow-wrap', width)).toBe('anywhere')
        expect(tooWide(line, width)).toEqual([])
      }
      for (const theme of THEMES) {
        const bg = ink(won(line, 'background', 1440), theme)
        expect(contrast(ink(won(line, 'color', 1440), theme, bg), bg), `${theme} notice words`).toBeGreaterThanOrEqual(4.5)
        expect(contrast(ink('var(--info)', theme, bg), bg), `${theme} notice edge`).toBeGreaterThanOrEqual(3)
        // And the sweep's own probe over the box, its close button included.
        const style = document.createElement('style')
        style.textContent = resolveSheet(`${SHEETS}\nbody { background: var(--bg); }`, theme)
        document.head.appendChild(style)
        try {
          const report = probe(line.parentElement!, [line, close])
          // The box's own border is among what it measured, not skipped.
          expect(report.bordered).toContain(line)
          expect(report.findings.filter((f) => f.kind === 'divider-under-floor').map((f) => `${theme} ${f.where} ${f.measured} ${f.detail}`)).toEqual([])
          expect(report.unresolved).toEqual([])
        } finally {
          style.remove()
        }
      }
      expect(FLOORS.dividerContrast).toBeGreaterThan(1)
    })
  })

})

// ---- debug mode: the turn line ------------------------------------------------
//
// While Settings' Debug is on, each `trace.turn` event on the session's stream
// (swarm-api voice/stream.py) draws the turn's line under the Stage: its
// number, the cards drawn and refused, and why nothing was drawn. Off, no line.
//
// MUTATIONS: draw the line with debug off; keep the first turn's line; drop
// the refused count or the no-cards reason; read the stream twice.

describe("debug mode's turn line on Talk", () => {
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    apiMock.fixtures = false
    answerStarts()
    installMedia({ audioContext: true })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.write.mockReset()
    fetchMock.debug = false
  })

  const turnLine = (): HTMLElement | null => screen.queryByRole('group', { name: 'Last planner turn' })

  it('draws each trace.turn event as the line under the Stage while debug is on', async () => {
    fetchMock.debug = true
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const es = FakeEventSource.last
    expect(FakeEventSource.all).toHaveLength(1)
    expect(turnLine()).toBeNull()

    await act(async () => es.frame('trace.turn', 1, { n: 1, drawn: [{ kind: 'work', title: 'Export lane' }, { kind: 'plan', title: 'Waves' }], refused: [{ kind: 'review', title: null, reason: 'the budget is spent' }], none: null }))
    let line = turnLine()!
    expect(line.querySelector('.vt-turn-n')?.textContent).toBe('Turn 1')
    expect(line.querySelector('.vt-turn-out')?.textContent).toBe('2 cards · 1 refused')
    expect(line.closest('.vt')!.classList.contains('has-turn')).toBe(true)
    expect(line.textContent).toContain('the budget is spent')

    await act(async () => es.frame('trace.turn', 2, { n: 2, drawn: [], refused: [], none: 'answered_in_words' }))
    line = turnLine()!
    expect(line.querySelector('.vt-turn-n')?.textContent).toBe('Turn 2')
    expect(line.querySelector('.vt-turn-out')?.textContent).toBe('0 cards · answered in words')
    expect(line.getAttribute('data-outcome')).toBe('none')
    expect(container.querySelectorAll('.vt-turn')).toHaveLength(1)

    await act(async () => es.frame('trace.turn', 3, { n: 3, drawn: [], refused: [{ kind: 'work', title: 'Lane', reason: 'held' }], none: 'refused' }))
    expect(turnLine()!.querySelector('.vt-turn-out')?.textContent).toBe('0 cards · 1 refused · all refused')
    // The turn opens in full on Telemetry: a real button.
    expect(within(turnLine()!).getByRole('button', { name: 'Trace' })).toBeTruthy()
  })

  it('draws no line while debug is off, whatever the stream carries', async () => {
    fetchMock.debug = false
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const es = FakeEventSource.last
    await act(async () => es.frame('trace.turn', 1, { n: 1, drawn: [{ kind: 'work', title: 'Export lane' }], refused: [], none: null }))
    expect(turnLine()).toBeNull()
    expect(container.querySelector('.vt-turn')).toBeNull()
    expect(container.querySelector('.vt.has-turn')).toBeNull()
  })
})
