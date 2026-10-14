// A parked hand-off says "parked -- press I'm ready to resume", never "expired" (LB-RETURN; part of #1030).
//
// MEASURED by the live-browser acceptance group, 2026-10-14: a hand-off whose
// unwatched minute ran out closed its live session with `hold_expired`, and the
// Browser tab said "The live browser has closed (hold_expired)" over a task
// that a single press of I'm ready resumes. The person read it as over.

import { describe, expect, it } from 'vitest'

import { closedSessionNote } from '../LiveBrowser'

describe('closedSessionNote', () => {
  it('says a parked hand-off is parked and how to resume it', () => {
    const say = closedSessionNote('hold_expired', { state: 'PARKED', park_reason: 'HUMAN_REQUIRED' })
    expect(say).toMatch(/parked/i)
    expect(say).toContain('I’m ready')
    expect(say).not.toMatch(/expired/i)
  })

  it('says the same while the worker is still parking it', () => {
    const say = closedSessionNote('hold_expired', { state: 'RUNNING', park_reason: null })
    expect(say).toMatch(/parking/i)
    expect(say).not.toMatch(/expired/i)
  })

  it('keeps the plain closed line for any other cause', () => {
    expect(closedSessionNote('attempt_ended', { state: 'SUCCEEDED', park_reason: null })).toBe(
      'The live browser has closed (attempt_ended). The recording is kept for seven days.',
    )
    expect(closedSessionNote(null, { state: 'RUNNING', park_reason: null })).toBe(
      'The live browser has closed. The recording is kept for seven days.',
    )
  })
})
