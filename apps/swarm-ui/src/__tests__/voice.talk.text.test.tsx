// VOICE IT'S TEXT CONVERSATION (the owner's choice of 4 November 2026: GPT-Live
// when an OpenAI API key is in the Voice it pool, else a text conversation on
// the ChatGPT plan with the browser's speech in and out).
//
//   * a start answered `mode: "text"` opens no WebRTC: the offer's peer is
//     closed unanswered, the mic stays for mute and the oscilloscope, and the
//     browser's speech recognition listens, continuous, with interim results;
//   * the phrase being heard is the live caption (muted, outside the log);
//     each final phrase is one POST to the session's turns route, and both
//     sides land in the log; the reply is spoken by speechSynthesis in one
//     voice, and speaking again, Stop speaking or End cancels it;
//   * mute aborts the recogniser (a muted word is never sent), push-to-talk
//     listens only while held, a recogniser that ends by itself is restarted,
//     and a phrase counts as activity for the idle stop;
//   * no speech recognition (or a refused one): one line and a text box that
//     sends the same turns;
//   * the ring taps the mic and draws the spoken reply as a level;
//   * a 429 says when the plan's limit resets, a 409 ends the call here, and
//     any other refusal shows swarm-api's own sentence;
//   * a start answered `mode: "live"` is the GPT-Live call, unchanged.
//
// MUTATIONS: apply a remote description in text mode; send interim results as
// turns; keep speaking after a new phrase; stop rather than abort on mute;
// drop the restart after the recogniser's own end; send the turn without
// `text`; show Cloud Run's 429 sentence for the plan's limit.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeAudioContext, FakePeer, installMedia, settle, type FakeTrack } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))
const shellMock = vi.hoisted(() => ({ noteActivity: vi.fn() }))

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  USE_FIXTURES: false,
}))

vi.mock('../fetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../fetch')>()
  return {
    ...actual,
    write: fetchMock.write,
    // The ChatGPT plan is a GPT-Live deployment: Settings' engine is gpt-live, so Start sends the offer.
    read: ((target, ...rest) =>
      target.url === '/v1/voice/settings'
        ? Promise.resolve({ status: 'ok', data: { settings: { engine: 'gpt-live' } }, fetchedAt: Date.now() })
        : actual.read(target, ...rest)) as typeof actual.read,
  }
})

vi.mock('../Shell', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../Shell')>()),
  noteActivity: shellMock.noteActivity,
}))

import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'
import { limitResetsAt, limitSentence } from '../voice/rtc'
import { Listener, NO_RECOGNITION, pickVoice } from '../voice/speech'

const SESSION = 'vs-0002'
const TURNS = `/v1/voice/sessions/${SESSION}/turns`

// ---- the browser's speech, faked ----------------------------------------------------

class FakeRecognition {
  static all: FakeRecognition[] = []
  static get last(): FakeRecognition {
    const r = FakeRecognition.all[FakeRecognition.all.length - 1]
    if (r === undefined) throw new Error('no SpeechRecognition was made')
    return r
  }
  continuous = false
  interimResults = false
  lang = ''
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  running = false
  start = vi.fn(() => {
    if (this.running) throw new Error('InvalidStateError')
    this.running = true
  })
  stop = vi.fn()
  abort = vi.fn()
  constructor() {
    FakeRecognition.all.push(this)
  }
  /** One result, as the recogniser reports a phrase being heard or heard whole. */
  hear(text: string, isFinal: boolean): void {
    const result = Object.assign([{ transcript: text }], { isFinal })
    this.onresult?.({ resultIndex: 0, results: [result] })
  }
  /** The recogniser stops (after a silence of its own, or after stop/abort). */
  end(): void {
    this.running = false
    this.onend?.()
  }
  fail(error: string): void {
    this.onerror?.({ error })
    this.end()
  }
}

class FakeUtterance {
  voice: unknown = null
  lang = ''
  onstart: (() => void) | null = null
  onend: (() => void) | null = null
  onerror: (() => void) | null = null
  onboundary: (() => void) | null = null
  constructor(readonly text: string) {}
}

const VOICES = [
  { name: 'Other', lang: 'fr-FR', default: true },
  { name: 'English', lang: 'en-US', default: false },
]

function installSpeech(opts: { recognition?: boolean } = {}) {
  FakeRecognition.all = []
  const spoken: FakeUtterance[] = []
  const synth = {
    speak: vi.fn((u: FakeUtterance) => spoken.push(u)),
    cancel: vi.fn(),
    getVoices: vi.fn(() => VOICES),
  }
  vi.stubGlobal('speechSynthesis', synth)
  vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
  if (opts.recognition !== false) vi.stubGlobal('webkitSpeechRecognition', FakeRecognition)
  return { synth, spoken }
}

// ---- swarm-api, faked ----------------------------------------------------------------

type Answer = { status: 'ok'; data: unknown; fetchedAt: number } | { status: 'error'; error: Record<string, unknown> }

function serve(opts: { mode?: string; turn?: (body: { text: string }) => Answer } = {}): void {
  let n = 0
  fetchMock.write.mockImplementation(async (target: { url: string }, _m: string, body: { text: string }) => {
    if (target.url === '/v1/voice/sessions') {
      const data = opts.mode === 'live' ? { session_id: SESSION, mode: 'live', answer_sdp: 'v=0 fake-answer' } : { session_id: SESSION, mode: opts.mode ?? 'text' }
      return { status: 'ok', data, fetchedAt: Date.now() }
    }
    if (target.url === TURNS) {
      if (opts.turn) return opts.turn(body)
      n += 1
      return { status: 'ok', data: { turn_id: `turn-${n}`, reply: `Reply ${n} to ${body.text}`, cards: [] }, fetchedAt: Date.now() }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

function turnBodies(): unknown[] {
  return fetchMock.write.mock.calls.filter(([t]) => t.url === TURNS).map(([, method, body]) => [method, body])
}

async function start(): Promise<void> {
  // The page reads Settings' engine when it opens.
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
}

function logLines(): string[][] {
  const log = screen.getByRole('log', { name: 'Captions' })
  return Array.from(log.querySelectorAll('.vt-cap')).map((p) => [
    p.querySelector('.vt-cap-who')?.textContent ?? '',
    p.querySelector('.vt-cap-text')?.textContent ?? '',
  ])
}

describe('the Talk pane in text mode (the ChatGPT plan)', () => {
  let mic: FakeTrack
  let speech: ReturnType<typeof installSpeech>
  beforeEach(() => {
    ;({ mic } = installMedia({ audioContext: true }))
    speech = installSpeech()
    serve()
    // The meter's reads: not what this suite measures, answered with nothing.
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })))
  })
  afterEach(() => {
    fetchMock.write.mockReset()
    shellMock.noteActivity.mockReset()
    vi.unstubAllGlobals()
  })

  it('opens no WebRTC: the peer is closed unanswered, and the browser listens instead', async () => {
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const pc = FakePeer.last
    expect(pc.remote).toBeNull()
    expect(pc.closed).toBe(true)
    expect(mic.stop).not.toHaveBeenCalled()
    expect((container.querySelector('audio') as HTMLAudioElement & { srcObject: unknown }).srcObject ?? null).toBeNull()
    const rec = FakeRecognition.last
    expect(rec.continuous).toBe(true)
    expect(rec.interimResults).toBe(true)
    expect(rec.start).toHaveBeenCalledTimes(1)
    expect(document.querySelector('.vt-status')?.textContent).toBe('Live, on the ChatGPT plan')
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
  })

  it('draws the phrase being heard as the live caption, sends each final phrase as a turn, and speaks the reply', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const rec = FakeRecognition.last
    await act(async () => rec.hear('Add CSV', false))
    const log = screen.getByRole('log', { name: 'Captions' })
    expect(within(log).queryByText(/Add CSV/)).toBeNull()
    expect(screen.getByText(/Add CSV/).closest('[aria-hidden="true"]')).not.toBeNull()
    expect(turnBodies()).toEqual([])

    await act(async () => rec.hear('Add CSV export to Runs.', true))
    await settle(act)
    expect(turnBodies()).toEqual([['POST', { text: 'Add CSV export to Runs.' }]])
    expect(logLines()).toEqual([
      ['You', 'Add CSV export to Runs.'],
      ['Voice it', 'Reply 1 to Add CSV export to Runs.'],
    ])
    expect(screen.queryByText('Add CSV')).toBeNull()
    expect(speech.spoken.map((u) => u.text)).toEqual(['Reply 1 to Add CSV export to Runs.'])
    // A recognised phrase is activity for the idle stop.
    expect(shellMock.noteActivity).toHaveBeenCalled()
  })

  it('speaks every reply in one voice, and speaking again, Stop speaking or End cancels it', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const rec = FakeRecognition.last
    await act(async () => rec.hear('One.', true))
    await settle(act)
    const first = speech.spoken[0]
    await act(async () => first?.onstart?.())
    expect(screen.getByRole('button', { name: 'Stop speaking' })).toBeTruthy()

    speech.synth.cancel.mockClear()
    await act(async () => rec.hear('Wait', false))
    expect(speech.synth.cancel).toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Stop speaking' })).toBeNull()

    await act(async () => rec.hear('Two.', true))
    await settle(act)
    const second = speech.spoken[1]
    expect(second?.voice).toBe(first?.voice)
    expect((first?.voice as { name: string }).name).toBe('English')

    await act(async () => second?.onstart?.())
    speech.synth.cancel.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Stop speaking' }))
    expect(speech.synth.cancel).toHaveBeenCalledTimes(1)

    await act(async () => rec.hear('Three.', true))
    await settle(act)
    await act(async () => speech.spoken[2]?.onstart?.())
    speech.synth.cancel.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    expect(speech.synth.cancel).toHaveBeenCalled()
    expect(rec.abort).toHaveBeenCalled()
    expect(mic.stop).toHaveBeenCalled()
    expect(fetchMock.write.mock.calls.some(([t]) => t.url === `/v1/voice/sessions/${SESSION}/end`)).toBe(true)
  })

  it('mute discards what is being heard; push-to-talk listens only while held; a recogniser that pauses is restarted', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const rec = FakeRecognition.last

    // The recogniser's own end after a silence: listened to again.
    await act(async () => rec.end())
    expect(rec.start).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Mute' }))
    expect(rec.abort).toHaveBeenCalledTimes(1)
    expect(mic.enabled).toBe(false)
    await act(async () => rec.hear('Said while muted.', true))
    await act(async () => rec.end())
    await settle(act)
    expect(turnBodies()).toEqual([])
    expect(rec.start).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Unmute' }))
    expect(rec.start).toHaveBeenCalledTimes(3)

    fireEvent.click(screen.getByRole('button', { name: 'Push to talk' }))
    expect(rec.stop).toHaveBeenCalledTimes(1)
    await act(async () => rec.end())
    expect(rec.start).toHaveBeenCalledTimes(3)

    const holdButton = screen.getByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(holdButton)
    expect(rec.start).toHaveBeenCalledTimes(4)
    fireEvent.pointerUp(holdButton)
    expect(rec.stop).toHaveBeenCalledTimes(2)
    // Letting go still sends the phrase that was being heard.
    await act(async () => rec.hear('Held phrase.', true))
    await settle(act)
    expect(turnBodies()).toEqual([['POST', { text: 'Held phrase.' }]])
  })

  it('taps the mic for the oscilloscope and draws the spoken reply as a level', async () => {
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const tapped = FakeAudioContext.all.flatMap((c) => c.sources.map((s) => s.stream.tracks[0]))
    expect(tapped).toContain(mic)
    expect(container.querySelector('.vt-ring-level')).toBeNull()
    await act(async () => FakeRecognition.last.hear('Hello.', true))
    await settle(act)
    await act(async () => speech.spoken[0]?.onstart?.())
    const bar = container.querySelector('.vt-ring-level')
    expect(bar).not.toBeNull()
    expect(bar?.getAttribute('data-level')).toBe('55')
    expect(container.querySelector('.vt-ring')?.getAttribute('data-state')).toBe('speaking')
    await act(async () => speech.spoken[0]?.onboundary?.())
    expect(container.querySelector('.vt-ring-level')?.getAttribute('data-level')).toBe('100')
    await act(async () => speech.spoken[0]?.onend?.())
    expect(container.querySelector('.vt-ring-level')).toBeNull()
    expect(container.querySelector('.vt-ring')?.getAttribute('data-state')).toBe('listening')
  })

  it('says so in one line and offers a text box where the browser has no speech recognition', async () => {
    vi.unstubAllGlobals()
    ;({ mic } = installMedia({ audioContext: true }))
    speech = installSpeech({ recognition: false })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    expect(screen.getByText(NO_RECOGNITION)).toBeTruthy()
    const box = screen.getByRole('textbox', { name: 'Say to Voice it' })
    expect(box.getAttribute('maxlength')).toBe('4000')
    fireEvent.change(box, { target: { value: 'Typed turn.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await settle(act)
    expect(turnBodies()).toEqual([['POST', { text: 'Typed turn.' }]])
    expect(logLines()).toEqual([
      ['You', 'Typed turn.'],
      ['Voice it', 'Reply 1 to Typed turn.'],
    ])
    expect(speech.spoken.map((u) => u.text)).toEqual(['Reply 1 to Typed turn.'])
    expect((box as HTMLInputElement).value).toBe('')
  })

  it('falls back to the text box when the browser refuses speech recognition', async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    await act(async () => FakeRecognition.last.fail('not-allowed'))
    expect(screen.getByText(/not allowed to recognise speech/)).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Say to Voice it' })).toBeTruthy()
  })

  it("says when the plan's usage limit resets on a 429, and keeps the session", async () => {
    const resets = '2026-11-04T09:30:00Z'
    serve({
      turn: () => ({
        status: 'error',
        error: {
          kind: 'rate_limited', httpStatus: 429, code: 'plan_usage_limited', message: "SwarmCloud's API had no instance free (Cloud Run); retried when the pause ends.",
          detail: { next_reset_at: resets },
        },
      }),
    })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    await act(async () => FakeRecognition.last.hear('Hello.', true))
    await settle(act)
    const alert = document.querySelector('.vt-problem')?.textContent ?? ''
    expect(alert).toBe(limitSentence(new Date(resets)))
    expect(alert).toMatch(/usage limit is reached; it resets/)
    expect(logLines()).toEqual([['You', 'Hello.']])
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
  })

  it("shows swarm-api's sentence for any other refusal, and a 409 ends the call here", async () => {
    let status = 422
    serve({
      turn: () => ({ status: 'error', error: { kind: status === 409 ? 'conflict' : 'invalid', httpStatus: status, code: null, message: `swarm-api says ${status}.` } }),
    })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    await act(async () => FakeRecognition.last.hear('First.', true))
    await settle(act)
    expect(document.querySelector('.vt-problem')?.textContent).toBe('swarm-api says 422.')
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()

    status = 409
    await act(async () => FakeRecognition.last.hear('Second.', true))
    await settle(act)
    expect(document.querySelector('.vt-problem')?.textContent).toBe('swarm-api says 409.')
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
    expect(mic.stop).toHaveBeenCalled()
    expect(FakeRecognition.last.abort).toHaveBeenCalled()
  })

  it('keeps the GPT-Live call for a start answered mode "live"', async () => {
    serve({ mode: 'live' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    expect(FakePeer.last.remote).toEqual({ type: 'answer', sdp: 'v=0 fake-answer' })
    expect(FakePeer.last.closed).toBe(false)
    expect(FakeRecognition.all).toHaveLength(0)
    expect(document.querySelector('.vt-status')?.textContent).toBe('Live')
  })

  it('refuses a start that names a mode it does not know, and holds no mic open', async () => {
    serve({ mode: 'video' })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    expect(document.querySelector('.vt-problem')?.textContent).toMatch(/a mode this console does not know \(video\)/)
    expect(mic.stop).toHaveBeenCalled()
  })
})

describe('the speech helpers', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('a listener sends only final phrases, and drops what it hears after an abort', () => {
    FakeRecognition.all = []
    const finals: string[] = []
    const heard: string[] = []
    const l = new Listener(FakeRecognition, { onInterim: (t) => heard.push(t), onFinal: (t) => finals.push(t), onUnavailable: () => undefined })
    const rec = FakeRecognition.last
    l.listen(true)
    rec.hear('par', false)
    rec.hear('  ', true)
    rec.hear('whole phrase', true)
    l.abort()
    rec.hear('after abort', true)
    expect(finals).toEqual(['whole phrase'])
    expect(heard).toContain('par')
    rec.end()
    expect(rec.start).toHaveBeenCalledTimes(1)
  })

  it('picks a voice in the page language once there are voices', () => {
    const v = (name: string, lang: string, d = false) => ({ name, lang, default: d }) as SpeechSynthesisVoice
    expect(pickVoice([], 'en-GB')).toBeNull()
    expect(pickVoice([v('a', 'fr-FR', true), v('b', 'en-US'), v('c', 'en-GB', true)], 'en-GB')?.name).toBe('c')
    expect(pickVoice([v('a', 'fr-FR', true)], 'en-GB')?.name).toBe('a')
  })

  it("reads when the plan's limit resets from the refusal's detail, else Retry-After", () => {
    const base = { kind: 'rate_limited' as const, httpStatus: 429, code: null, message: '' }
    expect(limitResetsAt({ ...base, detail: { next_reset_at: '2026-11-04T09:30:00Z' } })?.toISOString()).toBe('2026-11-04T09:30:00.000Z')
    expect(limitResetsAt({ ...base, retryAfterSeconds: 60 }, 1_000)?.getTime()).toBe(61_000)
    expect(limitResetsAt(base)).toBeNull()
    expect(limitSentence(null)).toMatch(/did not say when it resets/)
  })
})
