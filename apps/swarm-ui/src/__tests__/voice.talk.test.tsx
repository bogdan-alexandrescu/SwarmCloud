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
//   * End posts the session's end, closes the peer and stops the mic.
//
// MUTATIONS: put the partial line inside the log; connect the analyser to the
// context's destination; drop the end POST; leave the mic track running.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeAudioContext, FakePeer, FakeStream, FakeTrack, installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

import { TalkPane } from '../voice/Talk'
import { applyCaption, captionEvent, type CaptionLine } from '../voice/captions'

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
    const ctx = FakeAudioContext.all[0]
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

    const finals = Array.from(log.querySelectorAll('.vi-cap')).map((p) => [
      p.querySelector('.vi-cap-who')?.textContent,
      p.querySelector('.vi-cap-text')?.textContent,
    ])
    expect(finals).toEqual([
      ['You', 'Add CSV export to Runs.'],
      ['Voice it', 'On it, drawing it.'],
    ])
    // Events that carry no caption change nothing.
    await act(async () => pc.events.deliver({ type: 'session.updated' }))
    expect(log.querySelectorAll('.vi-cap')).toHaveLength(2)
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
    expect(screen.getByRole('alert').textContent).toMatch(/could not start/i)
    expect(pc.closed).toBe(true)
    expect(mic.stop).toHaveBeenCalled()
  })

  it('draws the card area empty, with the phase-two line and no cards', () => {
    render(<TalkPane />)
    const cards = screen.getByRole('region', { name: 'Cards' })
    expect(cards.textContent).toMatch(/cards arrive in phase two/i)
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
