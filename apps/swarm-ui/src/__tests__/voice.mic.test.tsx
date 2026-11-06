// VOICE IT'S MIC AND THE IDLE STOP (docs/design/voice-it.md §3.10; section
// 10, phase one, "The Talk pane draws the agent's voice as an oscilloscope,
// offers mute and push-to-talk, and counts speech as input for the idle
// stop").
//
//   * open mic: the mic track is on, and Mute turns it off without ending the
//     session;
//   * push-to-talk: the track is enabled ONLY while Space (outside a text
//     field) or the Hold to talk button is held, and off the moment it is let
//     go; M mutes;
//   * every final caption, from either side, while a session is live is input
//     for the console's 15-minute idle stop (Shell.tsx `noteActivity`), so a
//     poller stays active through twenty minutes of talk with no key or
//     pointer; once the session ends, the rule applies again.
//
// MUTATIONS: enable the track on push-to-talk's selection rather than on the
// hold; leave it on after keyup; take Space typed into a text box as a hold;
// drop the noteActivity call; call it on partial lines only.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { IDLE_STOP_MS, useIdleStop } from '../Shell'
import { FakePeer, FakeTrack, installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

import { TalkPane } from '../voice/Talk'

beforeEach(() => {
  fetchMock.write.mockImplementation(async (target: { url: string }) =>
    target.url === '/v1/voice/sessions'
      ? { status: 'ok', data: { session_id: 'vs-0002', answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
      : { status: 'ok', data: null, fetchedAt: Date.now() },
  )
})
afterEach(() => {
  fetchMock.write.mockReset()
  vi.useRealTimers()
})

async function start(): Promise<FakePeer> {
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
  return FakePeer.last
}

describe('the mic', () => {
  let mic: FakeTrack
  beforeEach(() => {
    ;({ mic } = installMedia())
  })

  it('is open by default, and Mute turns the track off and keeps the session', async () => {
    render(<TalkPane />)
    const pc = await start()
    expect(mic.enabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Mute' }))
    expect(mic.enabled).toBe(false)
    expect(pc.closed).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Unmute' }))
    expect(mic.enabled).toBe(true)
    fireEvent.keyDown(window, { key: 'm' })
    expect(mic.enabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Unmute' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('on push-to-talk enables the track only while Space is held', async () => {
    render(<TalkPane />)
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Push to talk' }))
    expect(mic.enabled).toBe(false)
    fireEvent.keyDown(window, { key: ' ', code: 'Space' })
    expect(mic.enabled).toBe(true)
    // A held key repeats; it stays on, and one keyup turns it off.
    fireEvent.keyDown(window, { key: ' ', code: 'Space', repeat: true })
    expect(mic.enabled).toBe(true)
    fireEvent.keyUp(window, { key: ' ', code: 'Space' })
    expect(mic.enabled).toBe(false)
    // Leaving the tab while holding lets go.
    fireEvent.keyDown(window, { key: ' ', code: 'Space' })
    expect(mic.enabled).toBe(true)
    fireEvent.blur(window)
    expect(mic.enabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Open mic' }))
    expect(mic.enabled).toBe(true)
  })

  it('on push-to-talk enables the track only while the Hold to talk button is pressed', async () => {
    render(<TalkPane />)
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Push to talk' }))
    const hold = screen.getByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(hold)
    expect(mic.enabled).toBe(true)
    expect(hold.getAttribute('aria-pressed')).toBe('true')
    fireEvent.pointerUp(hold)
    expect(mic.enabled).toBe(false)
    fireEvent.pointerDown(hold)
    fireEvent.pointerLeave(hold)
    expect(mic.enabled).toBe(false)
  })

  it('does not take Space typed into a text field as a hold, and muted push-to-talk stays off', async () => {
    render(
      <>
        <input aria-label="Elsewhere" />
        <TalkPane />
      </>,
    )
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Push to talk' }))
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Elsewhere' }), { key: ' ', code: 'Space' })
    expect(mic.enabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Mute' }))
    fireEvent.keyDown(window, { key: ' ', code: 'Space' })
    expect(mic.enabled).toBe(false)
  })
})

function Poller() {
  const { idle } = useIdleStop(true)
  return <p data-testid="poller">{idle ? 'paused' : 'polling'}</p>
}

describe('speech and the idle stop', () => {
  it('keeps a poller active through twenty minutes of talk with no key or pointer, then lets the rule apply again', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    installMedia()
    render(
      <>
        <Poller />
        <TalkPane />
      </>,
    )
    const pc = await start()
    const poller = screen.getByTestId('poller')
    const step = 5 * 60_000
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        vi.advanceTimersByTime(step)
      })
      const side = i % 2 === 0 ? 'input' : 'output'
      await act(async () => {
        pc.events.deliver({ type: `session.${side}_transcript.delta`, item_id: `t${i}`, delta: 'still ' })
        pc.events.deliver({ type: `session.${side}_transcript.done`, item_id: `t${i}`, transcript: 'still here' })
      })
    }
    expect(4 * step).toBeGreaterThan(IDLE_STOP_MS)
    expect(poller.textContent).toBe('polling')

    // Partial lines alone are not proof of anyone: only final segments count.
    await act(async () => {
      vi.advanceTimersByTime(IDLE_STOP_MS - 1000)
      pc.events.deliver({ type: 'session.input_transcript.delta', item_id: 'p', delta: 'hm' })
      vi.advanceTimersByTime(2000)
    })
    expect(poller.textContent).toBe('paused')
  })

  it('stops counting speech once the session has ended', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    installMedia()
    render(
      <>
        <Poller />
        <TalkPane />
      </>,
    )
    const pc = await start()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    await act(async () => {
      vi.advanceTimersByTime(IDLE_STOP_MS - 60_000)
      // A late event from a channel that is closing is not someone at the console.
      pc.events.deliver({ type: 'session.output_transcript.done', item_id: 'late', transcript: 'goodbye' })
      vi.advanceTimersByTime(2 * 60_000)
    })
    expect(screen.getByTestId('poller').textContent).toBe('paused')
  })
})
