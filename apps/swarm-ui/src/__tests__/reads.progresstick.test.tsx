// A LONG READ SHOWS A QUIET PROGRESS TICK (visual QA pass 1, the owner's pick
// of 2026-11-13). The runner sizes preview and the over-time Run each take
// 6-30 s on the live deployment, and both said a static `reading…` sentence
// meanwhile: nothing on screen moved, so a read at 25 s looked like a read
// that had hung. Each now shows `reading m:ss`, the clock counting up from the
// read's start once a second; what the read is for sits behind a tooltip.
//
// Each assertion names the mutation that turns it red.

import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { PlatformTime } from '../platformTimeApi'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, ReadTick, forgetOverTime } = await import('../PlatformOverTime')

const text = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.useRealTimers()
  loadPlatformTime.mockReset()
  forgetOverTime()
})

describe('ReadTick', () => {
  it('counts up from the read’s start once a second, as an indeterminate progress bar', () => {
    vi.useFakeTimers()
    render(<ReadTick since={Date.now()} about="Reading the sizes." />)
    const tick = screen.getByRole('progressbar')
    expect(text(tick)).toBe('reading 0:00')
    act(() => {
      vi.advanceTimersByTime(7_000)
    })
    // MUTATION: draw a static `reading…` with no clock.
    expect(text(tick)).toBe('reading 0:07')
    act(() => {
      vi.advanceTimersByTime(25_000)
    })
    expect(text(tick)).toBe('reading 0:32')
    // Indeterminate: no value, so no reader is told a percentage nobody knows.
    expect(tick.hasAttribute('aria-valuenow')).toBe(false)
    expect(tick.getAttribute('aria-valuetext')).toBe('reading, 32 s so far')
    // What the read is for is the tooltip, not words on the surface.
    expect(tick.getAttribute('title')).toBe('Reading the sizes.')
  })
})

describe('Platform counts, over time, while a Run reads', () => {
  it('ticks beside the Run and in the unread card, and stops when the read lands', async () => {
    vi.useFakeTimers()
    let land: (r: Result<PlatformTime>) => void = () => undefined
    loadPlatformTime.mockImplementation(() => new Promise((resolve) => (land = resolve)))
    render(<PlatformOverTime />)
    act(() => {
      screen.getByRole('button', { name: /^Run/ }).click()
    })
    act(() => {
      vi.advanceTimersByTime(12_000)
    })
    const ticks = screen.getAllByRole('progressbar')
    // MUTATION: put back the provenance's `reading now…` and the card's `Reading now.`.
    expect(ticks).toHaveLength(2)
    expect(document.querySelector('.pot-prov [role="progressbar"]')).not.toBeNull()
    expect(document.querySelector('.pot-notrun [role="progressbar"]')).not.toBeNull()
    for (const t of ticks) expect(text(t)).toBe('reading 0:12')
    await act(async () => {
      land({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'broke' } } as Result<PlatformTime>)
    })
    expect(screen.queryAllByRole('progressbar')).toHaveLength(0)
  })
})
