// THE CONSOLE NO LONGER SAYS THE LIVE VIEW IS MASKED (lane LIVE-NOMASK-COPY;
// part of #2298).
//
// BGD decided on 2026-10-25 that the live browser stops redacting any part of
// the screen, in any situation: lane LIVE-NOMASK removes the painting from the
// worker's live gate. Every frame the live view, the thumbnails and the
// recordings show is the screen as Chromium drew it, so copy that tells a
// reader a frame is masked, covered or blanked is now false. Text redaction
// (logs, prompts, artifacts: `masked N`) is not this and stays.
//
// MUTATIONS: put "masked" back in the thumbnail's live tooltip, the
// recordings' caption or the hand-off checklist, and its case fails.

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, USE_FIXTURES: false }
})

import { STATUS_NOTE } from '../LiveThumb'
import { RecordingsPanel } from '../Recordings'
import { HandoffChecklist } from '../TheaterTimeline'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('no copy says the live view is masked', () => {
  it('the thumbnail’s live tooltip is the live view, not a masked one', () => {
    expect(STATUS_NOTE.live).toBe(
      'The live view of this step’s browser, at most one frame a second. Open the Browser tab for the full view.',
    )
    for (const note of Object.values(STATUS_NOTE)) expect(note).not.toMatch(/mask|cover|blank/i)
  })

  it('the recordings’ caption says the frames as watched, with no "Masked"', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ tenant_id: 'eng', task_id: 'task_a', recordings: [], segments: [] })))
    const { container } = render(<RecordingsPanel task={{ id: 'task_a', tenant_id: 'eng' }} />)
    await act(async () => {
      await Promise.resolve()
    })
    const note = container.querySelector('.rec-note')?.textContent ?? ''
    expect(note).toContain('The frames as the person watching saw them.')
    expect(note).not.toMatch(/mask/i)
  })

  it('the theater’s hand-off checklist says nothing is masked', () => {
    const deadline = new Date(Date.now() + 5 * 60_000).toISOString()
    const { container } = render(
      <HandoffChecklist handoff={{ reason: 'Solve the check on the page.', expect: 'example.com', deadline }} />,
    )
    expect(container.textContent ?? '').not.toMatch(/mask/i)
  })
})
