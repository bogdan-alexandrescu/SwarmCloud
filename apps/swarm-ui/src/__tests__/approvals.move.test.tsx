// APPROVALS MOVES UNDER AUTOMATE (APPROVALS-MOVE, part of #2391; BGD's
// decision of 2026-10-26 on VQ-030, "Move it, and redirect /approvals").
//
// WHAT EACH CASE HOLDS:
//   * Approvals is at `/automate/approvals`, beside `/automate/schedules`, and
//     one item at `/automate/approvals/<id>`, both ways, its `?kind=` kept;
//   * the old `/approvals` and `/approvals/<id>` still open the page and the
//     item, and the app rewrites the bar to the new address with the id (and
//     the kind) kept, so a notification or a saved link keeps working.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

describe('Approvals’ address', () => {
  it('is /automate/approvals, one item /automate/approvals/<id>, both ways', () => {
    // MUTATION: leave FIXED's `'automate/approvals': '/approvals'` and the page is not under Automate.
    expect(addressToPath('automate/approvals')).toBe('/automate/approvals')
    expect(pathToAddress('/automate/approvals')?.address).toBe('automate/approvals')
    expect(addressToPath('automate/approvals?approval=run%3Arun_8f2a')).toBe('/automate/approvals/run%3Arun_8f2a')
    expect(pathToAddress('/automate/approvals/run%3Arun_8f2a')?.address).toBe('automate/approvals?approval=run%3Arun_8f2a')
    expect(addressToPath('automate/approvals?approval=apr_1&kind=merge')).toBe('/automate/approvals/apr_1?kind=merge')
    expect(pathToAddress('/automate/approvals/apr_1', '?kind=merge')?.address).toBe('automate/approvals?approval=apr_1&kind=merge')
  })

  it.each([
    ['/approvals', '', '/automate/approvals'],
    ['/approvals', '?kind=merge', '/automate/approvals?kind=merge'],
    ['/approvals/apr_1', '', '/automate/approvals/apr_1'],
    ['/approvals/run%3Arun_8f2a', '?kind=plan', '/automate/approvals/run%3Arun_8f2a?kind=plan'],
  ])('lands the old path %s%s on %s', (path, search, want) => {
    const p = pathToAddress(path, search)
    // MUTATION: drop the old paths from pathToAddress and they are a 404.
    expect(p, `${path} is not served`).not.toBeNull()
    expect(addressToPath(canonical(fromAddress(p!.address)))).toBe(want)
  })

  it.each([
    ['/approvals', '/automate/approvals', ''],
    ['/approvals/apr_merge1?kind=merge', '/automate/approvals/apr_merge1', '?kind=merge'],
  ])('rewrites %s in the app to %s, the id kept', async (url, pathname, search) => {
    window.history.replaceState(null, '', url)
    const { App } = await import('../App')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe(pathname))
    expect(window.location.search).toBe(search)
  })
})
