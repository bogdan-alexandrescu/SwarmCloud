// LIVE BROWSER, NAMED LOGINS (LB-L12c; docs/design/live-browser.md §5.5.1,
// LB-7). The acceptance test of the lane's row:
//
//   * after Return control on a task in the person's own workspace (`u-<user>`)
//     the Browser tab offers "Save as login" with the account warning, and it
//     never does on a group-tenant task, returned or not;
//   * Save sends a name and an expiry of at most 30 days, default 14, and
//     nothing else -- no cookie, no value, no site the console made up;
//   * Access › Browser logins lists name, site, expiry and last use, and never a
//     value, even when the server sends one;
//   * Revoke sends nothing until the login's name is typed exactly, and Esc is
//     Keep.
//
// WHERE THIS FILE LIVES. The build plan names `apps/swarm-ui/test/`; the
// component layer's `vitest.config.ts` includes only `src/**/*.test.tsx`, so
// a file there would never run. It sits with the others in `src/__tests__/`.
//
// Fixtures only; no network. The one value-shaped string is built at runtime
// and put into a response on purpose, to prove nothing draws it.

import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DAY, HOUR, serve, visible } from './repofixture'
import { painted } from './marks'

const NOW = Date.now()
const iso = (ms: number) => new Date(NOW + ms).toISOString()

/** A cookie-shaped value, built at runtime, that must never reach the DOM. */
const cookieValue = () => ['sess', 'ion='].join('') + 'Qx7'.repeat(14)

const LOGIN = (name: string, site: string, over: Record<string, unknown> = {}) => ({
  name,
  site,
  saved_at: iso(-3 * DAY),
  expires_at: iso(11 * DAY),
  last_used_at: iso(-2 * HOUR),
  ...over,
})

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../BrowserLogins')
}

type Handler = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

const writes = <C extends { method: string }>(calls: C[]) => calls.filter((c) => c.method !== 'GET')

describe('Save as login, on the Browser tab after Return control', () => {
  it('is offered on a personal-workspace task once control is returned, with the account warning', async () => {
    serve(() => null)
    const { SaveAsLoginOffer } = await load()
    const { rerender } = render(<SaveAsLoginOffer taskId="task_1" tenantId="u-alice" site="https://github.com" controlReturned={false} />)
    expect(screen.queryByRole('button', { name: /save as login/i })).toBeNull()

    rerender(<SaveAsLoginOffer taskId="task_1" tenantId="u-alice" site="https://github.com" controlReturned />)
    fireEvent.click(screen.getByRole('button', { name: /save as login/i }))
    const form = screen.getByRole('form', { name: /save as login/i })
    const text = visible(form)
    // The owner's words (§5.5.1, "Whose account"): every agent the person runs
    // in their workspace can act as that account on that site until it expires
    // or is revoked.
    expect(text).toMatch(/every agent you run in your workspace can act as/i)
    expect(text).toContain('https://github.com')
    expect(text).toMatch(/until (it|the login) expires or (is|you) revoke/i)
  })

  it('is never offered on a group-tenant task, returned or not', async () => {
    serve(() => null)
    const { SaveAsLoginOffer, offersSaveAsLogin } = await load()
    for (const returned of [false, true]) {
      const { container, unmount } = render(<SaveAsLoginOffer taskId="task_2" tenantId="eng" site="https://github.com" controlReturned={returned} />)
      expect(container.textContent).toBe('')
      unmount()
    }
    expect(offersSaveAsLogin('eng', true)).toBe(false)
    expect(offersSaveAsLogin('u-alice', true)).toBe(true)
    expect(offersSaveAsLogin('u-alice', false)).toBe(false)
    // A group whose slug merely contains "u-" is still a group.
    expect(offersSaveAsLogin('menu-team', true)).toBe(false)
  })

  it('sends a name and an expiry in days, default 14, and nothing else', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/browser-logins'
        ? { status: 200, body: { login: LOGIN('github-qa', 'https://github.com', { expires_at: iso(14 * DAY), last_used_at: null }) } }
        : null,
    )
    const { SaveAsLoginOffer } = await load()
    render(<SaveAsLoginOffer taskId="task_1" tenantId="u-alice" site="https://github.com" controlReturned />)
    fireEvent.click(screen.getByRole('button', { name: /save as login/i }))
    const days = screen.getByLabelText(/expires after/i) as HTMLInputElement
    expect(days.value).toBe('14')
    expect(days.max).toBe('30')
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: 'github-qa' } })
    fireEvent.click(screen.getByRole('button', { name: /^save login$/i }))
    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toEqual({ method: 'POST', url: '/v1/browser-logins', body: { task_id: 'task_1', name: 'github-qa', expires_in_days: 14 } })
    expect(await screen.findByText(/saved as github-qa/i)).toBeTruthy()
  })

  it('refuses an expiry past 30 days and a name the vault cannot hold, before sending', async () => {
    const calls = serve(() => null)
    const { SaveAsLoginOffer } = await load()
    render(<SaveAsLoginOffer taskId="task_1" tenantId="u-alice" site="https://github.com" controlReturned />)
    fireEvent.click(screen.getByRole('button', { name: /save as login/i }))
    const save = () => screen.getByRole('button', { name: /^save login$/i }) as HTMLButtonElement

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: 'github-qa' } })
    fireEvent.change(screen.getByLabelText(/expires after/i), { target: { value: '31' } })
    expect(save().disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/expires after/i), { target: { value: '0' } })
    expect(save().disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/expires after/i), { target: { value: '30' } })
    expect(save().disabled).toBe(false)

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: 'GitHub QA!' } })
    expect(save().disabled).toBe(true)
    fireEvent.click(save())
    expect(writes(calls)).toHaveLength(0)
  })

  it('shows the server refusal word for word and keeps the form', async () => {
    serve((m, url) =>
      m === 'POST' && url === '/v1/browser-logins'
        ? { status: 409, body: { code: 'browser_login_exists', message: 'A login named github-qa already exists in u-alice.' } }
        : null,
    )
    const { SaveAsLoginOffer } = await load()
    render(<SaveAsLoginOffer taskId="task_1" tenantId="u-alice" site="https://github.com" controlReturned />)
    fireEvent.click(screen.getByRole('button', { name: /save as login/i }))
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: 'github-qa' } })
    fireEvent.click(screen.getByRole('button', { name: /^save login$/i }))
    expect(await screen.findByText('A login named github-qa already exists in u-alice.')).toBeTruthy()
    expect(screen.getByRole('form', { name: /save as login/i })).toBeTruthy()
  })
})

describe('Config › Secrets › Browser logins (Access › Browser logins until GA-MOVE-VIEWS)', () => {
  const api = (logins: unknown[], extra: Handler = () => null) =>
    serve((m, url, body) => {
      const hit = extra(m, url, body)
      if (hit !== null) return hit
      if (m === 'GET' && url === '/v1/browser-logins') return { status: 200, body: { tenant_id: 'u-alice', logins } }
      return null
    })

  it('lists name, site, expiry and last use, and never a value', async () => {
    const leak = cookieValue()
    api([
      LOGIN('github-qa', 'https://github.com', { value: leak, cookies: [{ name: 'sid', value: leak, domain: 'github.com' }] }),
      LOGIN('staging-admin', 'https://staging.example.com', { last_used_at: null }),
    ])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    const list = await screen.findByRole('list', { name: /browser logins/i })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(2)

    const first = visible(rows[0] ?? null)
    expect(first).toContain('github-qa')
    expect(first).toContain('https://github.com')
    expect(first).toMatch(/expires in 1[01]d/)
    expect(first).toMatch(/used 2h ago/)
    expect(visible(rows[1] ?? null)).toMatch(/never used/i)

    expect(document.documentElement.outerHTML).not.toContain(leak)
    expect(document.documentElement.outerHTML).not.toContain('Qx7Qx7')
  })

  it('says an expired login is expired rather than counting forward', async () => {
    api([LOGIN('old', 'https://github.com', { expires_at: iso(-3 * DAY) })])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    const list = await screen.findByRole('list', { name: /browser logins/i })
    expect(visible(list)).toMatch(/expired 3d ago/)
  })

  it('says there are none, in words', async () => {
    api([])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    expect(await screen.findByText(/no browser login saved/i)).toBeTruthy()
  })

  it('reads nothing in a group tenant and says logins live in a personal workspace', async () => {
    const calls = api([])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="eng" />)
    expect(screen.getByText(/only in your personal workspace/i)).toBeTruthy()
    expect(calls).toHaveLength(0)
  })

  it('Revoke asks for the name typed, sends nothing until it matches, and Esc is Keep', async () => {
    const calls = api([LOGIN('github-qa', 'https://github.com')], (m, url) =>
      m === 'DELETE' && url === '/v1/browser-logins/github-qa' ? { status: 200, body: { name: 'github-qa', revoked: true, versions_destroyed: 3 } } : null,
    )
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    const list = await screen.findByRole('list', { name: /browser logins/i })
    fireEvent.click(within(list).getByRole('button', { name: /^revoke github-qa$/i }))
    expect(writes(calls)).toHaveLength(0)

    const ask = screen.getByRole('group', { name: /revoke github-qa/i })
    const confirm = () => within(ask).getByRole('button', { name: /^revoke$/i }) as HTMLButtonElement
    const typed = within(ask).getByLabelText(/type github-qa to revoke/i) as HTMLInputElement
    expect(confirm().disabled).toBe(true)
    fireEvent.change(typed, { target: { value: 'github-q' } })
    expect(confirm().disabled).toBe(true)
    fireEvent.click(confirm())
    expect(writes(calls)).toHaveLength(0)

    // Esc is Keep: the ask closes and nothing was sent.
    fireEvent.keyDown(typed, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: /revoke github-qa/i })).toBeNull()
    expect(writes(calls)).toHaveLength(0)

    fireEvent.click(within(list).getByRole('button', { name: /^revoke github-qa$/i }))
    const again = screen.getByRole('group', { name: /revoke github-qa/i })
    fireEvent.change(within(again).getByLabelText(/type github-qa to revoke/i), { target: { value: 'github-qa' } })
    const go = within(again).getByRole('button', { name: /^revoke$/i }) as HTMLButtonElement
    expect(go.disabled).toBe(false)
    await act(async () => {
      fireEvent.click(go)
    })
    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toMatchObject({ method: 'DELETE', url: '/v1/browser-logins/github-qa' })
  })

  it('is a tab of Config › Secrets, in the address as ?view=logins (GA-MOVE-VIEWS)', async () => {
    window.history.replaceState(null, '', '/config/secrets?view=logins')
    serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: { kinds: [], providers: [], consumers: [] } }
      if (m === 'GET' && url.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'u-alice', kind: 'user', can_manage: true, secrets: [] } }
      if (m === 'GET' && url === '/v1/browser-logins') return { status: 200, body: { tenant_id: 'u-alice', logins: [LOGIN('github-qa', 'https://github.com')] } }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspaceSecretsScreen } = await import('../WorkspaceSecrets')
    render(<WorkspaceSecretsScreen link={null} />)
    expect(screen.getByRole('tab', { name: /browser logins/i }).getAttribute('aria-selected')).toBe('true')
    const list = await screen.findByRole('list', { name: /browser logins/i })
    expect(visible(list)).toContain('github-qa')
    // Back to the secrets drops the view from the address.
    fireEvent.click(screen.getByRole('tab', { name: /^workspace secrets$/i }))
    expect(window.location.search).toBe('')
  })
})

describe('visual QA VQA2-R5: Access › Browser logins', () => {
  const WIDE = { width: 1440 }
  const listed = (logins: unknown[]) =>
    serve((m, url) => (m === 'GET' && url === '/v1/browser-logins' ? { status: 200, body: { tenant_id: 'u-alice', logins } } : null))

  it('V233: Keep and Esc give focus back to the row\'s Revoke, and the typed name is inside the warning', async () => {
    // MUTATION: drop `setRefocus(true)` from onKeep, or move the label and
    // input back out of the Banner.
    listed([LOGIN('github-qa', 'https://github.com')])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    const list = await screen.findByRole('list', { name: /browser logins/i })
    const revoke = () => within(list).getByRole('button', { name: /^revoke github-qa$/i }) as HTMLButtonElement

    fireEvent.click(revoke())
    const ask = screen.getByRole('group', { name: /revoke github-qa/i })
    const typed = within(ask).getByLabelText(/type github-qa to revoke/i) as HTMLInputElement
    expect(document.activeElement).toBe(typed)
    // The name is typed inside the warning it confirms, not under it.
    const banner = typed.closest('.c-banner')
    expect(banner, 'the typed name sits outside the warning').not.toBeNull()
    expect(banner!.querySelector('label')!.textContent).toBe('Type github-qa to revoke')

    fireEvent.keyDown(typed, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: /revoke github-qa/i })).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(revoke()))
    expect(document.activeElement).not.toBe(document.body)

    // Keep, the banner's safe button, does the same.
    fireEvent.click(revoke())
    fireEvent.click(within(screen.getByRole('group', { name: /revoke github-qa/i })).getByRole('button', { name: /^keep$/i }))
    await waitFor(() => expect(document.activeElement).toBe(revoke()))
  })

  it('V234: an expired login is drawn in the warn ink; one still valid keeps the line\'s tone', async () => {
    // MUTATION: drop `is-expired` from the expiry, or its rule in onboarding.css.
    listed([
      LOGIN('live', 'https://github.com', { expires_at: iso(11 * DAY) }),
      LOGIN('old', 'https://github.com', { expires_at: iso(-1 * DAY) }),
    ])
    const { BrowserLoginsPane } = await load()
    render(<BrowserLoginsPane tenantId="u-alice" />)
    await screen.findByRole('list', { name: /browser logins/i })
    const expiry = (name: string) => document.querySelector<HTMLElement>(`li[data-login="${name}"] .lb-expiry`)!
    expect(visible(expiry('old'))).toMatch(/^expired \d+[hd] ago$/)
    expect(visible(expiry('live'))).toMatch(/^expires in /)
    expect(expiry('old').classList.contains('is-expired')).toBe(true)
    expect(expiry('live').classList.contains('is-expired')).toBe(false)
    expect(painted(expiry('old'), 'color', WIDE)).toBe('var(--warn-ink)')
    expect(painted(expiry('live'), 'color', WIDE)).not.toBe('var(--warn-ink)')
  })
})
