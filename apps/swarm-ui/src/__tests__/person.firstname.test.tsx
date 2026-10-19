// FIRST NAME BESIDE THE EMAIL, FULL NAME WHERE THE PERSON IS THE SUBJECT
// (owner, 2026-10-19, after checking the built Profile page: "Show the first
// name for each user, only show full name in the profile page or in the user
// administration sections in admin for each user."; lane PROFILE-FIRSTNAME,
// part of #1505; docs/design/user-profile.md §7.1 amendment).
//
// WHAT EACH CASE HOLDS:
//   * `PersonName`'s default (`first`) prints the saved name's first word
//     beside the whole email, with the full name in the title; `full` prints
//     the whole name; a one-word name prints whole; no name prints the email
//     alone; a hyphen or an apostrophe stays inside the first word;
//   * a long one-word name under `first` is still the part that is cut;
//   * the panel's signed-in line prints the caller's saved first name, read
//     from `GET /v1/profile`, and the email's local part when none is saved
//     or the read fails. The email line under it is unchanged.
//
// Which pages pass `full` (People) and which take the default (Approvals,
// Workflows, the task page) is held in `person.name.test.tsx`, on the pages
// themselves. Placeholder people only.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { PersonName, firstName } from '../PersonName'
import type { Me } from '../types'

const EMAIL = 'bogdan@example.com'

const marker = (root: ParentNode) => root.querySelector<HTMLElement>('[data-person-name]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('PersonName variants', () => {
  it('`first` (the default) prints the first word and the email, with the full name in the title', () => {
    const { container } = render(<PersonName email={EMAIL} name="Bogdan Alexandrescu" />)
    const box = marker(container)!
    expect(box.querySelector('.person-name-name')!.textContent).toBe('Bogdan')
    expect(box.querySelector('.person-name-email')!.textContent).toBe(EMAIL)
    expect(box.textContent).toBe(`Bogdan · ${EMAIL}`)
    expect(box.getAttribute('title')).toBe(`Bogdan Alexandrescu · ${EMAIL}`)
  })

  it('`first` said explicitly is the default', () => {
    const { container } = render(<PersonName email={EMAIL} name="Bogdan Alexandrescu" variant="first" />)
    expect(marker(container)!.textContent).toBe(`Bogdan · ${EMAIL}`)
  })

  it('`full` prints the whole name', () => {
    const { container } = render(<PersonName email={EMAIL} name="Bogdan Alexandrescu" variant="full" />)
    const box = marker(container)!
    expect(box.querySelector('.person-name-name')!.textContent).toBe('Bogdan Alexandrescu')
    expect(box.textContent).toBe(`Bogdan Alexandrescu · ${EMAIL}`)
    expect(box.getAttribute('title')).toBe(`Bogdan Alexandrescu · ${EMAIL}`)
  })

  it('a one-word name prints whole, in either variant', () => {
    for (const variant of ['first', 'full'] as const) {
      const { container, unmount } = render(<PersonName email={EMAIL} name="Bogdan" variant={variant} />)
      expect(marker(container)!.textContent, variant).toBe(`Bogdan · ${EMAIL}`)
      unmount()
    }
  })

  it('no saved name prints the email alone, in either variant', () => {
    for (const variant of ['first', 'full'] as const) {
      for (const name of [null, undefined, '', '  ']) {
        const { container, unmount } = render(<PersonName email={EMAIL} name={name} variant={variant} />)
        const box = marker(container)!
        expect(box.textContent, `${variant} name=${JSON.stringify(name)}`).toBe(EMAIL)
        expect(box.querySelector('.person-name-name')).toBeNull()
        expect(box.getAttribute('title')).toBe(EMAIL)
        unmount()
      }
    }
  })

  it('keeps a hyphen and an apostrophe inside the first word', () => {
    const { container } = render(<PersonName email={EMAIL} name="Mary-Jane O'Neil" />)
    expect(marker(container)!.querySelector('.person-name-name')!.textContent).toBe('Mary-Jane')
    expect(marker(container)!.getAttribute('title')).toBe(`Mary-Jane O'Neil · ${EMAIL}`)
    expect(firstName("D'Arcy Smith")).toBe("D'Arcy")
    expect(firstName('  Ada \t Lovelace ')).toBe('Ada')
  })

  it('a long one-word name under `first` is still the part that is cut, never the email', () => {
    const long = 'A'.repeat(64)
    const { container } = render(
      <div style={{ width: '120px', overflow: 'hidden' }}>
        <PersonName email={EMAIL} name={long} />
      </div>,
    )
    const box = marker(container)!
    const name = box.querySelector<HTMLElement>('.person-name-name')!
    const email = box.querySelector<HTMLElement>('.person-name-email')!
    expect(name.textContent).toBe(long)
    expect(name.style.textOverflow).toBe('ellipsis')
    expect(name.style.flexShrink).toBe('1')
    expect(email.textContent).toBe(EMAIL)
    expect(email.style.flexShrink).toBe('0')
  })
})

// ---------------------------------------------------------------------------
// The panel's signed-in line (Spine.tsx), on the real shell, its reads
// answered through `fetch` the way brand.test.tsx's `sky` answers them.
// ---------------------------------------------------------------------------

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function me(): Me {
  return {
    tenant: {
      tenant_id: 'u-bogdan',
      kind: 'user',
      principal: EMAIL,
      display_name: 'Your workspace',
      created_at: new Date().toISOString(),
      max_active: 4,
      capacity_units: 8,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic'],
      service_account: 'swarm-agent-worker-u-bogdan@example.iam.gserviceaccount.com',
      gcs_prefix: 'tenants/u-bogdan',
      namespace: 'swarm-tenant-u-bogdan',
    },
    principal: { email: EMAIL, domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  } as Me
}

/** The real shell, with `/v1/profile` answering `profile` and every other read but the identity refused. */
async function shell(profile: Response): Promise<{ container: HTMLElement; asked: () => number }> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  let profileReads = 0
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(200, me()))
    if (url.split('?')[0]!.endsWith('/v1/profile')) {
      profileReads += 1
      return Promise.resolve(profile.clone())
    }
    return Promise.resolve(json(503, {}))
  }) as unknown as typeof fetch
  const { SkyShell } = await import('../Spine')
  const { container } = render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      {null}
    </SkyShell>,
  )
  return { container, asked: () => profileReads }
}

const line = (c: HTMLElement) => c.querySelector<HTMLElement>('.sk-pfoot .sk-pfrow > b')

describe("the panel's signed-in line", () => {
  it("shows the caller's saved first name, the full name in its title, and the email below unchanged", async () => {
    const { container, asked } = await shell(json(200, { identity: { email: EMAIL, display_name: 'Bogdan Alexandrescu' } }))
    await waitFor(() => expect(line(container)?.textContent).toBe('Bogdan'), { timeout: 4000 })
    expect(line(container)!.getAttribute('title')).toBe('Bogdan Alexandrescu')
    expect(container.querySelector('.sk-pfoot small')?.textContent).toBe(EMAIL)
    // Once per session, not once per render.
    expect(asked()).toBe(1)
  })

  it('shows the local part when the profile has no saved name', async () => {
    const { container, asked } = await shell(json(200, { identity: { email: EMAIL, display_name: null } }))
    await waitFor(() => expect(asked()).toBe(1), { timeout: 4000 })
    await waitFor(() => expect(line(container)?.textContent).toBe('bogdan'), { timeout: 4000 })
    expect(container.querySelector('.sk-pfoot small')?.textContent).toBe(EMAIL)
  })

  it('shows the local part when the profile cannot be read', async () => {
    const { container, asked } = await shell(json(503, { error: { code: 'unavailable', message: 'down' } }))
    await waitFor(() => expect(asked()).toBe(1), { timeout: 4000 })
    await waitFor(() => expect(line(container)?.textContent).toBe('bogdan'), { timeout: 4000 })
  })
})
