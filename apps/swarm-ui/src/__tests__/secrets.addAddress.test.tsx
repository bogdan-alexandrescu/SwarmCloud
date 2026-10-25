// CONFIG › SECRETS' ADD-A-SECRET ADDRESS (lane MCP-LEAD-FORM, part of #2157;
// BGD 2026-10-25, "Open the form, consumer picked", proposal E S5a: the MCP
// servers lead's button "opens setting it").
//
// `/config/secrets?add=secret&consumer=<consumer>` (paths.ts
// `secretsAddAddress`) opens Add a secret -- the wizard SSC-UI built -- with
// that consumer ticked at step 3. It is not `?action=add`, the /sc one-time
// link's address, and the page never says it was opened from a link.
//
//   * the new address opens the form with consumer mcp:kb picked, and a
//     bookmark of it survives the router (it goes through `secretsLinkView`);
//   * `?action=add` with a link's token still says "Opened from a one-time
//     link", and the new address does not;
//   * an unknown consumer, or one of the wrong shape, opens the form with
//     nothing picked;
//   * a reader who may not add secrets sees the page as today, with no form.
//
// Fixtures only; no network. Every token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { addressToPath, pathToAddress, secretsAddAddress } from '../paths'
import { canonical, fromAddress } from '../App'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const AT = '2026-10-20T09:30:00+00:00'
const minted = () => ['own', 'add'].join('') + 'Qw4'.repeat(10)

const CONSUMERS: Json = {
  consumers: [
    { consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: ['api_key', 'opaque'] }], grantable: true, note: '' },
    { consumer: 'mcp:kb', kind: 'mcp', name: 'kb', slots: [{ slot: 'auth', kinds: ['api_key', 'opaque'] }], grantable: true, note: '' },
    { consumer: 'mcp:docs-search', kind: 'mcp', name: 'docs-search', slots: [{ slot: 'auth', kinds: ['api_key', 'opaque'] }], grantable: true, note: '' },
  ],
  kinds: ['api_key', 'opaque'],
  providers: ['brave', 'other'],
}

function api(canManage = true) {
  return serve((m, url) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      const scope = new URLSearchParams(u.slice(u.indexOf('?') + 1)).get('scope') ?? 'workspace'
      return { status: 200, body: { tenant_id: 'eng', scope, personal_tenant_id: 'u-ana', can_manage: canManage, rows: [] } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'eng', can_manage: canManage, secrets: [] } }
    if (m === 'POST' && u.endsWith('/links')) return { status: 201, body: { url: null, path: `/config/secrets?action=add&t=${minted()}`, expires_at: AT } }
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Secrets')
}

/** Open the wizard's step 3, Who reads it, and list the ticked consumers. */
function picked(sheet: HTMLElement): string[] {
  const next = () => fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
  next()
  next()
  return within(sheet).getAllByRole('checkbox').filter((b) => (b as HTMLInputElement).checked).map((b) => b.closest('label')!.querySelector('code')!.textContent ?? '')
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('the address of Add a secret, a consumer picked', () => {
  it('is its own address, through paths.ts, and a bookmark of it survives the router', () => {
    const address = secretsAddAddress('mcp:kb')
    // MUTATION: reuse `?action=add` -> the page says it was opened from a one-time link and this fails.
    expect(address).not.toMatch(/action=/)
    const path = addressToPath(address)
    const q = new URLSearchParams(path.slice(path.indexOf('?') + 1))
    expect(path.startsWith('/config/secrets?')).toBe(true)
    expect(q.get('add')).toBe('secret')
    expect(q.get('consumer')).toBe('mcp:kb')
    // MUTATION: leave `add`/`consumer` out of `secretsLinkView` -> the router drops them and this fails.
    const p = pathToAddress('/config/secrets', path.slice(path.indexOf('?')))
    const route = fromAddress(p!.address)
    expect(route).toMatchObject({ sectionId: 'config', tab: 'secrets' })
    expect(addressToPath(canonical(route))).toBe(path)
    // No consumer: the form, nothing picked.
    expect(addressToPath(secretsAddAddress(null))).toBe('/config/secrets?add=secret')
  })

  it('keeps no consumer of the wrong shape, and none without the form', () => {
    const view = (search: string) => fromAddress(pathToAddress('/config/secrets', search)!.address).view ?? null
    expect(view('?add=secret&consumer=%3Cscript%3E')).toBe('add=secret')
    expect(view('?consumer=mcp:kb')).toBeNull()
    expect(view('?add=everything&consumer=mcp:kb')).toBeNull()
    // A one-time link's form wins: `add` never rides along with `action`.
    expect(view('?action=add&add=secret&consumer=mcp:kb')).toBe('action=add')
  })

  it('opens the form with consumer mcp:kb picked, and never says it came from a one-time link', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="add=secret&consumer=mcp:kb" />)
    // MUTATION: ignore `add` -> no dialog opens and this fails.
    const sheet = await screen.findByRole('dialog', { name: /add a secret/i })
    expect(screen.queryByText(/opened from a one-time link/i)).toBeNull()
    // MUTATION: open the wizard with no consumer -> nothing is ticked and this fails.
    expect(picked(sheet)).toEqual(['mcp:kb'])
  })

  it('`?action=add` with its link still says it was opened from a one-time link; the new address does not', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="action=add" token={minted()} />)
    await screen.findByText(/opened from a one-time link/i)
    expect(screen.queryByRole('dialog')).toBeNull()
    cleanup()
    render(<SecretsScreen view="add=secret&consumer=mcp:kb" />)
    await screen.findByRole('dialog', { name: /add a secret/i })
    expect(screen.queryByText(/opened from a one-time link/i)).toBeNull()
    expect(screen.queryByRole('form', { name: /add a secret/i })).toBeNull()
  })

  it('opens the form with nothing picked for a consumer the catalogue does not list', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="add=secret&consumer=mcp:nope" />)
    const sheet = await screen.findByRole('dialog', { name: /add a secret/i })
    // MUTATION: pass the address's consumer through unchecked -> step 4 counts a grant nobody sees.
    expect(picked(sheet)).toEqual([])
    fireEvent.change(within(sheet).getByLabelText(/^name$/i), { target: { value: 'kb-token' } })
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    expect(visible(sheet)).not.toMatch(/granted it/i)
  })

  it('a reader who may not add secrets sees the page as today, with no form', async () => {
    api(false)
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="add=secret&consumer=mcp:kb" />)
    await screen.findByRole('heading', { name: 'Secrets' })
    await waitFor(() => expect(document.querySelector('.sec-scope')).not.toBeNull())
    // Let the reads land, then look: still no form.
    await new Promise((r) => setTimeout(r, 20))
    // MUTATION: open the wizard without asking can_manage -> fails.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button', { name: /^add a secret$/i })).toBeNull()
  })

  it('closing the form leaves its address, so a reload does not reopen it', async () => {
    api()
    window.history.replaceState(null, '', '/config/secrets?add=secret&consumer=mcp%3Akb')
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="add=secret&consumer=mcp:kb" />)
    const sheet = await screen.findByRole('dialog', { name: /add a secret/i })
    fireEvent.click(within(sheet).getByRole('button', { name: /^close$/i }))
    expect(screen.queryByRole('dialog')).toBeNull()
    // MUTATION: keep `add` in the bar on close -> fails.
    expect(window.location.pathname).toBe('/config/secrets')
    expect(window.location.search).toBe('')
  })
})
