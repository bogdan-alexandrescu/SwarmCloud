// The browser tab of an open approval read "apr_1 · Approvals": its raw id,
// though its sheet heads it by its summary. `useApprovalName` reads the
// approval and names the tab by its summary (else its kind).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({ loadApproval: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), loadApproval: api.loadApproval }))

afterEach(() => {
  cleanup()
  api.loadApproval.mockReset()
})

describe('an open approval names its tab by its summary', () => {
  it('reads the approval and answers its summary, else its kind, never the id', async () => {
    const { useApprovalName } = await import('../App')
    const seen: (string | null)[] = []
    function Probe({ id }: { id: string | null }) {
      seen.push(useApprovalName(id))
      return null
    }
    api.loadApproval.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { approval: { approval_id: 'apr_1', kind: 'project_run', summary: ' Plan open issues ', subject: {}, state: 'pending', requested_at: null, expires_at: null } } })
    const { rerender } = render(<Probe id="apr_1" />)
    // MUTATION: title the tab by the raw id -> "apr_1".
    await waitFor(() => expect(seen.at(-1)).toBe('Plan open issues'))
    expect(api.loadApproval).toHaveBeenCalledWith('apr_1')

    api.loadApproval.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { approval: { approval_id: 'apr_2', kind: 'contract', summary: '', subject: {}, state: 'pending', requested_at: null, expires_at: null } } })
    rerender(<Probe id="apr_2" />)
    // Another approval's name is never shown for this one while it is read.
    expect(seen.at(-1)).toBeNull()
    await waitFor(() => expect(seen.at(-1)).toBe('contract'))

    rerender(<Probe id={null} />)
    expect(seen.at(-1)).toBeNull()
  })
})
