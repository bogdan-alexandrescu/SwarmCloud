/**
 * ADMIN IS SHOWN ONLY TO ADMINS (ADMIN-ONLY; part of #2157, BGD 2026-10-23:
 * "Members and viewers see no Admin"; docs/web-ui/mockups/config-e.html S1b
 * platform admin, S1c workspace admin, S1g member with no Admin item).
 *
 * spine.adminrole.test.tsx and config.nav.test.tsx pin a member, the admin of
 * only a personal workspace, and a member who is also a viewer elsewhere, and
 * they send only members to Admin addresses. No case held someone whose ONLY
 * group role is `viewer`, and none sent a viewer to a platform Admin address.
 * These two cases are that person.
 *
 * The API is what refuses them (tests/unit/control_plane/
 * test_admin_surface_refuses_non_admins.py); this is the console not drawing
 * what it would refuse.
 *
 * MUTATION: in Spine.tsx count any group role other than `member` as
 * administering a workspace, and both cases go red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

type Json = Record<string, unknown>

const ME: Json = {
  tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}
const VIEWER_ONLY: Json[] = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'viewer', source: 'override' } }]

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function serve() {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const reply = url.includes('/v1/tenants/mine') ? VIEWER_ONLY : url.includes('/v1/tenants/me') ? ME : undefined
    return Promise.resolve(reply === undefined ? json({ detail: 'Not Found' }, 404) : json(reply))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
}

const adminItem = () => document.querySelector('.sk-spine a[data-sec="admin"]')
// The foot's second line is the role since CONFIG-ADMIN-POLISH (S1a); the email is its title.
const signedIn = () => expect(document.querySelector('.sk-pfoot small')?.getAttribute('title')).toBe('ana.b@example.com')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('a viewer sees no Admin (ADMIN-ONLY)', () => {
  it('draws no Admin in the spine for someone whose only role is viewer', async () => {
    serve()
    await app('/overview')
    await waitFor(signedIn)
    await waitFor(() => expect(document.querySelector('.sk-spine a[data-sec="capacity"]')).not.toBeNull())
    expect(adminItem(), 'a viewer was shown Admin').toBeNull()
  })

  // admin-shell-23 (BGD 2026-10-28, part of #2596) keeps the address and says
  // the refusal in its place, where CONFIG-NAV sent them to Overview.
  it('refuses a viewer in place at every platform Admin address', async () => {
    for (const path of ['/admin/limits', '/admin/runner-sizes', '/admin/tenants', '/admin/counts', '/admin/people']) {
      serve()
      await app(path)
      await waitFor(() => expect(document.querySelector('[data-refused="admin"]'), path).not.toBeNull())
      expect(window.location.pathname, path).toBe(path)
      expect(adminItem(), path).toBeNull()
      cleanup()
    }
  })
})
