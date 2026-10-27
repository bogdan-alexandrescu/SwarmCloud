// Pins setup.ts's ADVERSARIAL CLOCK (#2379, F10/F11): every UI test file starts at
// 23:30Z on today's UTC date and runs forward from there, so a test that assumes
// "now + 2h is still today" fails on every run, not two hours a night. Removing the
// clock, or moving it off the day line, turns this file red.

import { describe, expect, it, onTestFinished, vi } from 'vitest'

const REAL_DATE = Symbol.for('swarm-ui.setup.realDate')
const realm = globalThis as typeof globalThis & { [REAL_DATE]?: DateConstructor }

describe("setup.ts's adversarial clock", () => {
  it('replaces the global Date and keeps the real one under its symbol', () => {
    const RealDate = realm[REAL_DATE]
    expect(RealDate).toBeDefined()
    expect(globalThis.Date).not.toBe(RealDate)
  })

  it("reads 23:30Z or later, before midnight, on the real clock's UTC date", () => {
    const RealDate = realm[REAL_DATE]!
    const now = new Date()
    expect(now.getUTCHours()).toBe(23)
    expect(now.getUTCMinutes()).toBeGreaterThanOrEqual(30)
    // Date.now() and new Date() agree: both read the moved clock.
    expect(Math.abs(Date.now() - now.getTime())).toBeLessThan(1000)
    // Only the time of day moves; the date stays the real one (unless the run itself
    // crossed midnight UTC since this file loaded, which a file never takes long enough to do).
    const real = new RealDate(RealDate.now())
    expect(now.toISOString().slice(0, 10)).toBe(real.toISOString().slice(0, 10))
  })

  it('runs forward in real time', async () => {
    const before = Date.now()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(Date.now()).toBeGreaterThan(before)
  })

  it('leaves explicit instants and instanceof alone', () => {
    expect(new Date(0).getTime()).toBe(0)
    expect(new Date('2026-01-02T03:04:05Z').toISOString()).toBe('2026-01-02T03:04:05.000Z')
    expect(new Date()).toBeInstanceOf(Date)
    expect(Date.UTC(2026, 0, 2)).toBe(1767312000000)
  })

  it('lets a spy on Date.now pin the clock', () => {
    vi.spyOn(Date, 'now').mockReturnValue(5)
    expect(Date.now()).toBe(5)
  })

  it('lets vi.setSystemTime pin an instant of its own', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    vi.setSystemTime(new Date('2026-01-02T03:04:05Z'))
    expect(new Date().toISOString()).toBe('2026-01-02T03:04:05.000Z')
  })
})
