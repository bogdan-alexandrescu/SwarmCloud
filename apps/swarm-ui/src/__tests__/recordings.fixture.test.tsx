// THE FIXTURE BUILD LISTS RECORDINGS FOR ITS LIVE-BROWSER AGENT (BT-012, lane
// BROWSER-TAB-RECORDINGS, part of #2423), so the review step can render the
// recordings list's layout without a deployment. It plays nothing, and says
// so rather than fetching from a route the fixture build does not serve.
//
// MUTATION: return `unserved` for every task in fixture mode again and the
// first case fails; let the fixture player fetch and the second fails.

import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, USE_FIXTURES: true }
})

import { FIXTURE_RECORDED_TASK, RecordingsPanel } from '../Recordings'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the fixture build’s recordings', () => {
  it('the fixture live-browser agent lists one attempt of twelve parts; any other task is unserved', async () => {
    const { container } = render(<RecordingsPanel task={{ id: FIXTURE_RECORDED_TASK, tenant_id: 'u-bogdan' }} />)
    await waitFor(() => expect(container.querySelectorAll('.rec-take')).toHaveLength(1))
    expect(container.querySelectorAll('[data-recording]')).toHaveLength(12)
    const other = render(<RecordingsPanel task={{ id: 'task_8ea5c26d', tenant_id: 'u-bogdan' }} />)
    await waitFor(() => expect(other.container.textContent).toMatch(/not served/i))
  })

  it('its player fetches nothing and says the fixture build serves no recording', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { container } = render(<RecordingsPanel task={{ id: FIXTURE_RECORDED_TASK, tenant_id: 'u-bogdan' }} />)
    await waitFor(() => expect(container.querySelector('.rec-take')).not.toBeNull())
    fireEvent.click([...container.querySelectorAll('button')].find((b) => b.textContent === 'Play')!)
    expect(container.querySelector('video.rec-player')).not.toBeNull()
    expect(container.querySelector('.rec-status')!.textContent).toBe('The fixture build serves no recording, so nothing plays here.')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
