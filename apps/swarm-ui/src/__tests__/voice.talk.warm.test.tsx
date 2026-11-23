// THE WARM REQUEST (docs/design/voice-it-own-speech.md §4.1 "The warm request";
// owner, 23 November 2026; voice/warm.ts, voice/Talk.tsx).
//
//   * Talk, opened with no session, posts `POST /v1/voice/speech/warm` once
//     and nothing else; a re-render or a session's end asks nothing again;
//   * opened while a session is held, it asks nothing;
//   * swarm-api's `{warming: true, within_seconds}` reads "Speech is starting"
//     in the status, with the boot's usual progress mark, where "Not connected"
//     stood, for those seconds at most; the why is behind the status's tooltip;
//   * any other answer, a refusal included, changes nothing on the page: the
//     start says its own refusal when the person presses it.
//
// MUTATIONS: post the warm on every render; post it with a session live; show
// the status for `warming: false`; keep the status past `within_seconds`;
// draw a refusal of the warm as a problem line.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    get USE_FIXTURES() {
      return false
    },
  }
})

vi.mock('../fetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../fetch')>()
  return {
    ...actual,
    write: fetchMock.write,
    read: ((target, ...rest) =>
      target.url === '/v1/voice/settings'
        ? Promise.resolve({ status: 'ok', data: { settings: { engine: 'gpt-live', debug: false } }, fetchedAt: Date.now() })
        : actual.read(target, ...rest)) as typeof actual.read,
  }
})

import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'
import { VOICE_SPEECH_WARM } from '../voice/warm'

const SESSION = 'vs-0001'
const status = () => document.querySelector('.vt-status')?.textContent ?? ''
const warms = () => fetchMock.write.mock.calls.filter(([t]) => (t as { url: string }).url === VOICE_SPEECH_WARM)

function answer(warm: unknown): void {
  fetchMock.write.mockImplementation(async (target: { url: string }) => {
    if (target.url === VOICE_SPEECH_WARM) return warm
    if (target.url === '/v1/voice/sessions') {
      return { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 answer-1' }, fetchedAt: Date.now() }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

const warming = (seconds = 240) => ({ status: 'ok', data: { warming: true, reason: 'started', within_seconds: seconds }, fetchedAt: Date.now() })

describe('Talk warms the speech machine once when it opens', () => {
  beforeEach(() => {
    installMedia({ audioContext: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    fetchMock.write.mockReset()
  })

  it('posts the one warm request on open, with no field, and never again on a re-render', async () => {
    answer(warming())
    const view = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    expect(warms()).toHaveLength(1)
    expect(warms()[0]?.[1]).toBe('POST')
    expect(warms()[0]?.[2]).toEqual({})
    view.rerender(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    expect(warms()).toHaveLength(1)
    expect(fetchMock.write.mock.calls).toHaveLength(1)
  })

  it('says Speech is starting with the progress mark while the machine starts, then no more', async () => {
    answer(warming(240))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    expect(status()).toBe('Speech is starting')
    const mark = document.querySelector('.vt-status .vt-boot-step.is-now')
    expect(mark?.textContent).toBe('Speech is starting')
    // The why is the tooltip's, not a sentence on the page.
    expect(screen.getByRole('status').querySelector('[tabindex="0"]')).toBeTruthy()
    await act(async () => {
      vi.advanceTimersByTime(239_000)
    })
    expect(status()).toBe('Speech is starting')
    await act(async () => {
      vi.advanceTimersByTime(2_000)
    })
    expect(status()).toBe('Not connected')
  })

  it('reads the session, not the warm, once a session starts, and asks nothing again after it ends', async () => {
    answer(warming())
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)
    expect(status()).not.toContain('Speech is starting')
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    expect(warms()).toHaveLength(1)
  })

  it.each([
    ['nothing warmed', { status: 'ok', data: { warming: false, reason: 'held' }, fetchedAt: Date.now() }],
    ['a refusal', { status: 'error', error: { status: 409, kind: 'conflict', code: 'conflict', message: 'Refused: today is at the $300 day cap.' } }],
    ['an empty answer', { status: 'ok', data: null, fetchedAt: Date.now() }],
  ])('changes nothing on the page for %s', async (_name, warm) => {
    answer(warm)
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    expect(warms()).toHaveLength(1)
    expect(status()).toBe('Not connected')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('asks nothing when Talk opens on a session already held', async () => {
    answer({ status: 'ok', data: { warming: false, reason: 'held' }, fetchedAt: Date.now() })
    const view = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)
    fetchMock.write.mockClear()
    // Leave Talk and come back with the call held by the holder.
    view.rerender(<VoiceSessionHolder><div /></VoiceSessionHolder>)
    view.rerender(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await settle(act)
    expect(warms()).toHaveLength(0)
  })
})
