// VOICE IT'S CAPTIONS FORM SENTENCES (the live deployment, 12 November 2026:
// both sides' words appeared one word per line; the trace counted 43
// transcript pieces and 0 finals). GPT-Live sends the browser transcript
// deltas, each with its own event id and no item id, and no final event. The
// console joins a side's deltas into one line and closes it as a sentence on
// the first of: a final for that side; the other side starting to speak; the
// server's end of that side's turn or response; a pause of CAPTION_PAUSE_MS.
//
//   * 43 recorded deltas and no final draw whole sentences, not 43 lines;
//   * the person's deltas interrupted by the voice close the person's line;
//   * a final still replaces the open line;
//   * a one-second gap closes a line; the end of the voice's audio closes it;
//   * the role=log receives each sentence once, and no delta's words are lost
//     or repeated.
//
// MUTATIONS: key a delta by its event id; close a line only on a final; drop
// the pause timer; append a closed line's text again when it closes.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { applyCaption, captionEvent, CAPTION_PAUSE_MS, closeCaption, type CaptionLine } from '../voice/captions'
import { FakePeer, installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'

/** Invented sentences, spoken a word at a time. */
const PERSON = [
  'Please draw the release plan for example-org and keep the first card short.',
  'Then add a second card with the open review items.',
]
const VOICE = ['Sure, drawing the release plan now, one moment.', 'Both cards are on the stage at example.com now, have a look.']

/** A sentence as the wire sends it: one delta per word, its space leading the next word. */
function words(sentence: string): string[] {
  return sentence.split(' ').map((w, i) => (i === 0 ? w : ` ${w}`))
}

let eventSeq = 0
function delta(side: 'input' | 'output', text: string): Record<string, string> {
  eventSeq += 1
  return { type: `session.${side}_transcript.delta`, event_id: `event_${eventSeq}`, delta: text }
}

/** The recorded talk: 43 deltas, no item id, no final. */
function recorded(): { side: 'input' | 'output'; text: string }[] {
  const out: { side: 'input' | 'output'; text: string }[] = []
  for (const w of words(PERSON[0] ?? '')) out.push({ side: 'input', text: w })
  for (const w of words(VOICE[0] ?? '')) out.push({ side: 'output', text: w })
  for (const w of words(PERSON[1] ?? '')) out.push({ side: 'input', text: w })
  for (const w of words(VOICE[1] ?? '')) out.push({ side: 'output', text: w })
  return out
}

beforeEach(() => {
  installMedia()
  fetchMock.write.mockImplementation(async (target: { url: string }) =>
    target.url === '/v1/voice/sessions'
      ? { status: 'ok', data: { session_id: 'vs-0003', answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
      : { status: 'ok', data: null, fetchedAt: Date.now() },
  )
})
afterEach(() => {
  fetchMock.write.mockReset()
  vi.useRealTimers()
})

async function start(): Promise<FakePeer> {
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
  return FakePeer.last
}

/** The role=log's lines, speaker and text. */
function logged(): [string, string][] {
  const log = screen.getByRole('log', { name: 'Captions' })
  return Array.from(log.querySelectorAll('.vt-cap')).map((p) => [
    p.querySelector('.vt-cap-who')?.textContent ?? '',
    p.querySelector('.vt-cap-text')?.textContent ?? '',
  ])
}

/** The open lines drawn outside the log. */
function open(): string[] {
  return Array.from(document.querySelectorAll('.is-partial .vt-cap-text')).map((p) => (p.textContent ?? '').replace(/…$/, ''))
}

describe('captions on the page', () => {
  it('draws 43 deltas and no final as whole sentences, each logged once', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    const talk = recorded()
    expect(talk).toHaveLength(43)
    for (const d of talk) {
      await act(async () => pc.events.deliver(delta(d.side, d.text)))
      // The open line is redrawn in place: never more than one per side.
      expect(open().length).toBeLessThanOrEqual(1)
    }
    await act(async () => {
      vi.advanceTimersByTime(CAPTION_PAUSE_MS + 100)
    })
    expect(logged()).toEqual([
      ['You', PERSON[0]],
      ['Voice it', VOICE[0]],
      ['You', PERSON[1]],
      ['Voice it', VOICE[1]],
    ])
    expect(open()).toEqual([])
  })

  it("closes the person's line when the voice starts speaking, and keeps the open line out of the log", async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    for (const w of words('Show me the')) await act(async () => pc.events.deliver(delta('input', w)))
    expect(logged()).toEqual([])
    expect(open()).toEqual(['Show me the'])
    await act(async () => pc.events.deliver(delta('output', 'One')))
    expect(logged()).toEqual([['You', 'Show me the']])
    expect(open()).toEqual(['One'])
  })

  it('closes a line after a one-second pause, and the next delta starts a new one', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    for (const w of words('First thought.')) await act(async () => pc.events.deliver(delta('input', w)))
    await act(async () => {
      vi.advanceTimersByTime(CAPTION_PAUSE_MS - 200)
    })
    // Still speaking: under the pause, the line stays open.
    expect(logged()).toEqual([])
    await act(async () => {
      vi.advanceTimersByTime(400)
    })
    expect(logged()).toEqual([['You', 'First thought.']])
    for (const w of words('Second thought.')) await act(async () => pc.events.deliver(delta('input', w)))
    expect(open()).toEqual(['Second thought.'])
    await act(async () => {
      vi.advanceTimersByTime(CAPTION_PAUSE_MS + 100)
    })
    expect(logged()).toEqual([
      ['You', 'First thought.'],
      ['You', 'Second thought.'],
    ])
  })

  it("closes the voice's line when its audio stops", async () => {
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const pc = await start()
    for (const w of words('All done.')) await act(async () => pc.events.deliver(delta('output', w)))
    await act(async () => pc.events.deliver({ type: 'session.output_audio.stopped', event_id: 'event_end' }))
    expect(logged()).toEqual([['Voice it', 'All done.']])
  })
})

describe('joining deltas', () => {
  it('reads a delta with no item id as part of its side, not as its own segment', () => {
    expect(captionEvent(delta('input', 'Hi'))).toEqual({ side: 'you', key: null, text: 'Hi', final: false })
  })

  it('a final still replaces the open line, keyed or not', () => {
    let lines: CaptionLine[] = []
    for (const w of words('ship the plan')) lines = applyCaption(lines, { side: 'you', key: null, text: w, final: false })
    expect(lines.map((l) => [l.text, l.final])).toEqual([['ship the plan', false]])
    lines = applyCaption(lines, { side: 'you', key: 'u1', text: 'Ship the plan.', final: true })
    expect(lines.map((l) => [l.side, l.text, l.final])).toEqual([['you', 'Ship the plan.', true]])
  })

  it('a final for a segment a pause split replaces both parts with its one sentence', () => {
    let lines: CaptionLine[] = []
    lines = applyCaption(lines, { side: 'agent', key: 'a1', text: 'One moment', final: false })
    lines = closeCaption(lines, 'agent')
    lines = applyCaption(lines, { side: 'agent', key: 'a1', text: ' please.', final: false })
    lines = applyCaption(lines, { side: 'agent', key: 'a1', text: 'One moment please.', final: true })
    expect(lines.map((l) => [l.text, l.final])).toEqual([['One moment please.', true]])
  })

  it('a new item closes the open line of its side', () => {
    let lines: CaptionLine[] = []
    lines = applyCaption(lines, { side: 'you', key: 'u1', text: 'Yes.', final: false })
    lines = applyCaption(lines, { side: 'you', key: 'u2', text: 'And more', final: false })
    expect(lines.map((l) => [l.text, l.final])).toEqual([
      ['Yes.', true],
      ['And more', false],
    ])
  })

  it("loses and repeats no delta's words, whatever closes the lines", () => {
    const talk = recorded()
    let lines: CaptionLine[] = []
    talk.forEach((d, i) => {
      lines = applyCaption(lines, { side: d.side === 'input' ? 'you' : 'agent', key: null, text: d.text, final: false })
      // A pause every seventh delta, mid-sentence.
      if (i % 7 === 6) lines = closeCaption(lines, d.side === 'input' ? 'you' : 'agent')
    })
    lines = closeCaption(closeCaption(lines, 'you'), 'agent')
    expect(lines.every((l) => l.final)).toBe(true)
    const said = (side: 'you' | 'agent') =>
      lines
        .filter((l) => l.side === side)
        .map((l) => l.text)
        .join(' ')
        .split(/\s+/)
    expect(said('you')).toEqual(`${PERSON[0]} ${PERSON[1]}`.split(' '))
    expect(said('agent')).toEqual(`${VOICE[0]} ${VOICE[1]}`.split(' '))
  })
})
