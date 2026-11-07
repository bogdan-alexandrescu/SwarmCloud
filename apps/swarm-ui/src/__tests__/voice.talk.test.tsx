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

import { FakeAudioContext, FakePeer, FakeStream, FakeTrack, installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))
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

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

import { RESUME_KEY } from '../voice/Projects'
import { CardList, FIXTURE_CARDS, applyStreamEvent, EMPTY_LIST, type VoiceCard } from '../voice/CardList'
import { CARD_KINDS } from '../voice/cards'
import { TalkPane } from '../voice/Talk'
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
    render(<TalkPane />)
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
    const { container } = render(<TalkPane />)
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
    render(<TalkPane />)
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
    render(<TalkPane />)
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
    render(<TalkPane />)
    const pc = await start()
    expect(screen.getByRole('alert').textContent).toBe('Voice it is for its owner only.')
    expect(pc.closed).toBe(true)
    expect(mic.stop).toHaveBeenCalled()
  })

  it('continues the resumed project at the first Start talking, and opens a new one after', async () => {
    const resumed = 'vp_' + '7'.repeat(32)
    window.sessionStorage.setItem(RESUME_KEY, resumed)
    render(<TalkPane />)
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
    render(<TalkPane />)
    await start()
    expect(apiMock.loadVoiceVisuals?.mock.calls.map(([p]) => p)).toContain(project)
  })

  it('keeps the resumed project for the next try when the first start is refused', async () => {
    const resumed = 'vp_' + '8'.repeat(32)
    window.sessionStorage.setItem(RESUME_KEY, resumed)
    fetchMock.write.mockResolvedValueOnce({ status: 'error', error: { kind: 'unavailable', httpStatus: 503, code: null, message: 'Voice it is not deployed.' } })
    render(<TalkPane />)
    await start()
    await start()
    const starts = fetchMock.write.mock.calls.filter(([t]) => t.url === '/v1/voice/sessions')
    expect(starts.map(([, , body]) => body.project_id)).toEqual([resumed, resumed])
  })

  it('draws the card area empty before a session, and says cards appear as you talk', () => {
    render(<TalkPane />)
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
    render(<TalkPane />)
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
    render(<TalkPane />)
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

  it('in fixture mode, the Talk pane draws a fixture card of each kind before a session', () => {
    apiMock.fixtures = true
    render(<TalkPane />)
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
