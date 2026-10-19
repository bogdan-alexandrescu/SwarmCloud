/**
 * THE PICKER'S LABEL COLUMN (PICKER-LABELS; part of #1505).
 *
 * Measured 2026-10-19 in Chrome on swarm.saga.xyz: the row for "Your
 * workspace" read "defaultu-bogdan". A row that was both current and the
 * default drew two `.sk-ck` labels into `.sk-to`'s three-column grid
 * (`24px 1fr auto`); the second wrapped into the next grid row's 24px first
 * column and spilled over the tenant id's `<small>`. So a row carries ONE
 * label in the third column, holding whichever apply, joined with " · ".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { TENANT_HEADER, TENANT_PREF } from '../fetch'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

type Mine = { tenant_id: string; display_name: string; kind: 'user' | 'group'; is_default: boolean }

/** A live API whose headerless requests act in the default, as the server's do. */
function liveApi(mine: Mine[]) {
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    const acting = chosen ?? mine.find((m) => m.is_default)?.tenant_id ?? mine[0]!.tenant_id
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: { tenant_id: acting, display_name: acting === 'eng' ? 'Engineering' : acting },
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
        environment: 'dev',
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  })
  globalThis.fetch = f as unknown as typeof fetch
}

async function openPicker() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null} keepOnSwitch={false}>
      <p>screen</p>
    </SkyShell>,
  )
  const btn = await waitFor(() => {
    const b = document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
    expect(b).not.toBeNull()
    return b!
  })
  fireEvent.click(btn)
  expect(document.querySelector('.sk-tpop[role="dialog"]')).not.toBeNull()
}

const rowFor = (name: string) =>
  [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop .sk-to')].find((b) => b.querySelector('b')?.textContent === name)

/** The row's label elements: every child of the row other than its initial, its name and its tenant id. */
const labelsOf = (row: HTMLElement) =>
  [...row.children].filter((e): e is HTMLElement => e instanceof HTMLElement && !e.matches('.sk-tg, b, small'))

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

const own = (is_default: boolean): Mine => ({ tenant_id: 'u-ana-b', display_name: 'Your workspace (u-ana-b)', kind: 'user', is_default })
const eng = (is_default: boolean): Mine => ({ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', is_default })

describe('a picker row names current and default in one label', () => {
  it('reads "current · default" in one element when the row is both, and keeps the tenant id apart', async () => {
    // MUTATION: render `current` and `default` as two `.sk-ck` spans again.
    liveApi([own(true), eng(false)])
    await openPicker()
    const row = rowFor('Your workspace')!
    expect(row, 'no row for your workspace').toBeDefined()
    expect(row.getAttribute('aria-current')).toBe('true')
    const labels = labelsOf(row)
    expect(labels).toHaveLength(1)
    expect(labels[0]!.textContent).toBe('current · default')
    // The tenant id is its own element, holding only the id.
    const id = row.querySelector('small')!
    expect(id.textContent).toBe('u-ana-b')
    expect(labels[0]!.contains(id)).toBe(false)
    expect(id.contains(labels[0]!)).toBe(false)
    // And the other row, neither current nor default, carries no label.
    expect(labelsOf(rowFor('eng@example.com')!)).toHaveLength(0)
  })

  it('reads "default" on a row that is only the default, and "current" on one that is only current', async () => {
    // MUTATION: draw "current · default" whenever either applies.
    window.sessionStorage.setItem(TENANT_PREF, 'eng')
    liveApi([own(true), eng(false)])
    await openPicker()
    await waitFor(() => expect(rowFor('Engineering')?.getAttribute('aria-current')).toBe('true'))
    const current = labelsOf(rowFor('Engineering')!)
    expect(current.map((l) => l.textContent)).toEqual(['current'])
    const dflt = labelsOf(rowFor('Your workspace')!)
    expect(dflt.map((l) => l.textContent)).toEqual(['default'])
    expect(rowFor('Your workspace')!.querySelector('small')?.textContent).toBe('u-ana-b')
  })
})
