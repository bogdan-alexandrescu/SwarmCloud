// The fixture build's Plan open issues sheet: its Audit tab read the dev
// server, which serves nothing, and showed a failure. Read through the real
// loader, as qa2fix07.api.fixtures.test.ts reads the schedule itself.

import { describe, expect, it, vi } from 'vitest'

describe('the fixture build serves a listed schedule its audit', () => {
  it('answers an empty audit for a listed schedule and the route’s 404 for any other id', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
    vi.resetModules()
    const fetched = vi.fn(() => Promise.reject(new Error('the fixture build fetched the dev server')))
    vi.stubGlobal('fetch', fetched)
    const api = await import('../api')
    // MUTATION: drop the USE_FIXTURES branch -> the dev server's 404, a failed Audit tab.
    const audit = await api.loadScheduleAudit('sch_3f9a0c1e7b25')
    expect(audit.status).toBe('empty')
    expect(fetched).not.toHaveBeenCalled()
    const other = await api.loadScheduleAudit('sch_not_listed')
    expect(other.status).toBe('error')
    vi.unstubAllGlobals()
  })
})
