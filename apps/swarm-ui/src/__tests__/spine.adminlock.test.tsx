/**
 * ADMIN IS NEVER DRAWN UNLOCKED BEFORE THE SESSION READ SAYS ADMIN.
 *
 * The spine's Admin button carries a lock for a non-admin. It used to carry
 * one only once `/v1/tenants/me` had been read and said "not admin", so for
 * the length of that read -- the first thing every tab does -- a non-admin saw
 * Admin unlocked, and then saw the lock appear. The rule held here: while the
 * read is unread, Admin is drawn locked; it unlocks only when the read says
 * `is_admin: true`, and stays locked when it says false.
 *
 * MUTATION: put `who !== null &&` back into the spine's `locked` and the
 * unread case goes red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, waitFor } from '@testing-library/react'

const ME = (isAdmin: boolean) => ({
  tenant: { tenant_id: 'eng', display_name: 'Engineering' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})

/** A session read the test answers when it chooses; every other read fails. */
function stubSession(): (isAdmin: boolean) => void {
  let answer: ((r: Response) => void) | null = null
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    if (String(input).includes('/v1/tenants/me')) {
      return new Promise<Response>((resolve) => {
        answer = resolve
      })
    }
    return Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } }))
  }) as unknown as typeof fetch
  return (isAdmin) => {
    answer?.(new Response(JSON.stringify(ME(isAdmin)), { status: 200, headers: { 'content-type': 'application/json' } }))
  }
}

async function liveShell() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section="admin" tab="limits" title="Pool limits" go={vi.fn()} foot={null}>
      {null}
    </SkyShell>,
  )
}

const adminButton = () => document.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="admin"]')!

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('the spine Admin lock before the session is read', () => {
  it('draws Admin locked while the session read is unread', async () => {
    stubSession()
    await liveShell()
    expect(adminButton().querySelector('.sk-lkd'), 'Admin was drawn unlocked before the read').not.toBeNull()
    expect(adminButton().title).not.toBe('Admin')
    // The panel's Admin title carries the lock too, and claims nothing yet:
    // "admins only" is a fact about this caller, and it is not known.
    expect(document.querySelector('.sk-panel .sk-pt .sk-lk')).not.toBeNull()
    expect(document.querySelector('.sk-pnote')).toBeNull()
  })

  it('unlocks Admin once the read says admin', async () => {
    const answer = stubSession()
    await liveShell()
    await act(async () => answer(true))
    await waitFor(() => expect(adminButton().querySelector('.sk-lkd')).toBeNull())
    expect(adminButton().title).toBe('Admin')
    expect(document.querySelector('.sk-panel .sk-lk')).toBeNull()
  })

  it('keeps Admin locked once the read says not admin', async () => {
    const answer = stubSession()
    await liveShell()
    await act(async () => answer(false))
    await waitFor(() => expect(document.querySelector('.sk-pnote')).not.toBeNull())
    expect(adminButton().querySelector('.sk-lkd')).not.toBeNull()
    expect(adminButton().title).toBe('Admin (admins only)')
  })
})
