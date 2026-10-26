// ONE ATTEMPT'S RECORDING PLAYS AS ONE VIDEO, AND ITS PLAYER FITS THE PANE
// (BT-012, lane BROWSER-TAB-RECORDINGS, part of #2423).
//
// Measured on the deployed console on 2026-10-26: an attempt's recording was 12
// rows of one-minute .webm parts, each with its own Play button; the player
// reported a duration of Infinity and could not seek (the relay muxed to a
// pipe: tests/unit/live/test_recorder_duration.py); a 1440x987 frame drawn
// 609x417 under a 454 px cap in a 407 px pane, so frame and controls never
// fitted together; Play sat inline on short rows and wrapped on long ones;
// and the text sat 1 px off the card's border.
//
// MUTATIONS: one Play per part instead of per attempt fails the first case;
// drop the source buffer's `sequence` mode or `endOfStream()` and the second
// fails; drop the `onEnded` advance and the third fails; drop `usePaneFit`
// and the fourth fails; put the row back to inline flow, or take the card's
// padding away, and the last fails.

import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, USE_FIXTURES: false }
})

import { PANE_ROOM, RECORDING_TYPE, RecordingsPanel, takesOf, type Recording } from '../Recordings'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const TASK = { id: 'task_browser0000000000001', tenant_id: 'u-ada' }
const ATTEMPT = 'att_1'
const START = 1792984991644

function part(i: number, attempt = ATTEMPT): Recording {
  const name = `rec-${START + i * 60_000}.live.webm`
  return {
    attempt_id: attempt,
    name,
    key: `tenants/${TASK.tenant_id}/tasks/${TASK.id}/attempts/${attempt}/live/${name}`,
    bytes: 52_000 + i * 60_000,
    created_at: new Date(START + (i + 1) * 60_000).toISOString(),
  }
}

const TWELVE = Array.from({ length: 12 }, (_, i) => part(i))

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** The listing route answers `listed`; a raw read answers the part's index as its one byte. */
function serve(listed: Recording[], missing = -1) {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    const u = new URL(String(url), 'http://x')
    if (u.pathname.endsWith('/live/recordings')) return json({ tenant_id: TASK.tenant_id, task_id: TASK.id, segments: listed })
    const i = listed.findIndex((r) => r.name === u.searchParams.get('name'))
    return new Response(new Uint8Array([i]), { status: i < 0 || i === missing ? 404 : 200, headers: { 'content-type': 'video/webm' } })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

class FakeBuffer extends EventTarget {
  mode = 'segments'
  appended: number[] = []
  appendBuffer(bytes: ArrayBuffer) {
    this.appended.push(new Uint8Array(bytes)[0]!)
    queueMicrotask(() => this.dispatchEvent(new Event('updateend')))
  }
}

class FakeMediaSource extends EventTarget {
  static made: FakeMediaSource[] = []
  static isTypeSupported(type: string) {
    return type === RECORDING_TYPE
  }
  readyState = 'closed'
  buffers: { type: string; buffer: FakeBuffer }[] = []
  constructor() {
    super()
    FakeMediaSource.made.push(this)
  }
  addSourceBuffer(type: string) {
    const buffer = new FakeBuffer()
    this.buffers.push({ type, buffer })
    return buffer
  }
  endOfStream() {
    this.readyState = 'ended'
  }
}

const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL

beforeEach(() => {
  FakeMediaSource.made = []
  URL.createObjectURL = vi.fn(() => 'blob:recording')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
})

async function listed(root: HTMLElement): Promise<void> {
  await waitFor(() => expect(root.querySelector('.rec-take')).not.toBeNull())
}

function play(root: HTMLElement): HTMLVideoElement {
  const button = [...root.querySelectorAll('button')].find((b) => b.textContent === 'Play')!
  fireEvent.click(button)
  return root.querySelector<HTMLVideoElement>('video.rec-player')!
}

describe('an attempt is one row with one Play button', () => {
  it('twelve parts of one attempt are one row, one button, and twelve listed parts', async () => {
    serve([...TWELVE, part(0, 'att_2')])
    const { container } = render(<RecordingsPanel task={TASK} />)
    await listed(container)
    const rows = container.querySelectorAll('.rec-take')
    expect([...rows].map((r) => r.getAttribute('data-attempt'))).toEqual([ATTEMPT, 'att_2'])
    expect(container.querySelectorAll('button')).toHaveLength(2)
    expect(rows[0]!.querySelectorAll('[data-recording]')).toHaveLength(12)
    expect(rows[0]!.textContent).toContain('12 parts')
    // Each row's button is in its own column, not inline in the text.
    expect(rows[0]!.querySelector(':scope > .rec-act > button')).not.toBeNull()
    expect(rows[0]!.querySelector(':scope > .rec-what button')).toBeNull()
  })

  it('takesOf groups by attempt and orders each attempt’s parts by name', () => {
    const takes = takesOf([part(2), part(0, 'att_2'), part(0), part(1)])
    expect(takes.map((t) => [t.attempt_id, t.parts.map((p) => p.name)])).toEqual([
      [ATTEMPT, [part(0).name, part(1).name, part(2).name]],
      ['att_2', [part(0, 'att_2').name]],
    ])
    expect(takesOf([{ ...part(0), bytes: null }, part(1)])[0]!.bytes).toBeNull()
  })
})

describe('the player joins the parts into one video', () => {
  it('with MSE: one source buffer in sequence mode, every part in order, then end of stream', async () => {
    vi.stubGlobal('MediaSource', FakeMediaSource)
    const fetchMock = serve(TWELVE)
    const { container } = render(<RecordingsPanel task={TASK} />)
    await listed(container)
    const video = play(container)
    expect(container.querySelectorAll('video')).toHaveLength(1)
    expect(video.getAttribute('src')).toBe('blob:recording')
    const source = FakeMediaSource.made[0]!
    source.readyState = 'open'
    await act(async () => {
      source.dispatchEvent(new Event('sourceopen'))
    })
    await waitFor(() => expect(source.readyState).toBe('ended'))
    expect(source.buffers).toHaveLength(1)
    expect(source.buffers[0]!.type).toBe(RECORDING_TYPE)
    expect(source.buffers[0]!.buffer.mode).toBe('sequence')
    expect(source.buffers[0]!.buffer.appended).toEqual(TWELVE.map((_, i) => i))
    const raws = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/raw'))
    expect(raws).toHaveLength(12)
    expect(raws[0]).toContain(`attempt_id=${ATTEMPT}`)
    await waitFor(() => expect(container.querySelector('.rec-status')!.textContent).toBe('12 parts, joined into one video.'))
  })

  it('a part that cannot be read ends the video there and says which', async () => {
    vi.stubGlobal('MediaSource', FakeMediaSource)
    serve(TWELVE.slice(0, 4), 2)
    const { container } = render(<RecordingsPanel task={TASK} />)
    await listed(container)
    play(container)
    const source = FakeMediaSource.made[0]!
    source.readyState = 'open'
    await act(async () => {
      source.dispatchEvent(new Event('sourceopen'))
    })
    await waitFor(() => expect(container.querySelector('.rec-status[role="alert"]')).not.toBeNull())
    expect(container.querySelector('.rec-status')!.textContent).toBe(
      'Part 3 of 4 could not be read: the route answered 404. The video ends at the parts before it.',
    )
    // What was read still plays, with a finite end.
    expect(source.buffers[0]!.buffer.appended).toEqual([0, 1])
    expect(source.readyState).toBe('ended')
  })

  it('without MSE: one player plays the parts one after another', async () => {
    vi.stubGlobal('MediaSource', undefined)
    serve(TWELVE.slice(0, 3))
    const { container } = render(<RecordingsPanel task={TASK} />)
    await listed(container)
    const video = play(container)
    expect(video.getAttribute('src')).toContain(`name=${TWELVE[0]!.name}`)
    fireEvent.ended(video)
    expect(container.querySelectorAll('video')).toHaveLength(1)
    expect(video.getAttribute('src')).toContain(`name=${TWELVE[1]!.name}`)
    expect(container.querySelector('.rec-status')!.textContent).toMatch(/^Part 2 of 3\./)
  })
})

describe('the player fits the pane it is in', () => {
  it('in a 407 px pane its cap is the pane less room for the row', async () => {
    vi.stubGlobal('MediaSource', undefined)
    serve(TWELVE)
    const { container } = render(
      <div className="ag-split-pane is-attempts">
        <RecordingsPanel task={TASK} />
      </div>,
    )
    await listed(container)
    Object.defineProperty(container.querySelector('.ag-split-pane')!, 'clientHeight', { configurable: true, value: 407 })
    const video = play(container)
    expect(video.style.getPropertyValue('--rec-fit')).toBe(`${407 - PANE_ROOM}px`)
    expect(407 - PANE_ROOM).toBe(359)
  })
})

describe('the rows are laid out as a grid inside a padded card', () => {
  const ENVS: [string, CascadeEnv][] = [
    ['1440', { width: 1440 }],
    ['1024', { width: 1024 }],
    ['390', { width: 390 }],
  ]
  it.each(ENVS)('at %s', async (_label, env) => {
    serve(TWELVE)
    const { container } = render(<RecordingsPanel task={TASK} />)
    await listed(container)
    const row = container.querySelector<HTMLElement>('.rec-take')!
    expect(painted(row, 'display', env)).toBe('grid')
    expect(painted(row.querySelector('.rec-act')!, 'grid-column', env)).toBe('2')
    const panel = container.querySelector<HTMLElement>('.rec-panel')!
    for (const side of ['padding-left', 'padding-right']) {
      const v = painted(panel, [side, 'padding'], env) ?? '0'
      expect(v).not.toMatch(/^0(px)?$/)
    }
  })

  it('the player’s cap reads the pane fit and never exceeds 60vh', () => {
    const v = document.createElement('video')
    v.className = 'rec-player'
    expect(painted(v, 'max-height', { width: 1440 })).toMatch(/^min\(60vh, (var\(--rec-fit, 60vh\)|60vh)\)$/)
  })
})
