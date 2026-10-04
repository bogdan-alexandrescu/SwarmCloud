/**
 * THE TENANT SWITCHER (owner decision 2026-10-01; docs/multi-tenancy.md).
 *
 * The choice SELECTS among the caller's verified memberships and never grants
 * one: the server refuses any `X-Swarm-Tenant` it did not confirm. What the
 * client must get right is narrower and is held here:
 *
 *   * the header leaves from ONE place (fetch.ts's `apiHeaders`), on every
 *     read and write, and only when a choice is stored;
 *   * the choice is per browser, and storage that throws costs nothing;
 *   * a stored tenant the caller is no longer in is dropped on the first
 *     `tenant_not_member` 403 and the default used -- and no other 403 drops it;
 *   * the spine's tenant block is a switcher only for more than one tenant,
 *     and switching re-reads the screen under the new tenant.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useState, type ReactNode } from 'react'
import STYLES from '../styles.css?raw'

import { TENANT_HEADER, TENANT_PREF, apiHeaders, chooseTenant, chosenTenant, read, route, write, type Result } from '../fetch'
import { artifactRawUrl } from '../api'
import { postWorkflow } from '../SubmitWorkflow'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })
const refused = (code: string, message: string) =>
  new Response(JSON.stringify({ code, message }), { status: 403, headers: JSON_HEADERS })

/** Every request's X-Swarm-Tenant, in order; `null` where none was sent. */
function sentTenants(fetchMock: ReturnType<typeof vi.fn>): (string | null)[] {
  return fetchMock.mock.calls.map(([, init]) => {
    const h = new Headers((init as RequestInit | undefined)?.headers)
    return h.get(TENANT_HEADER)
  })
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  try {
    window.localStorage.clear()
  } catch {
    /* a test that broke storage put it back already */
  }
})

describe('the header leaves from the one fetch helper', () => {
  it('is named X-Swarm-Tenant', () => {
    expect(TENANT_HEADER).toBe('X-Swarm-Tenant')
  })

  it('is not sent when no tenant was chosen', async () => {
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await read(route('/v1/capacity'), () => false)
    await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(sentTenants(f)).toEqual([null, null])
  })

  it('is sent on every read and write once a tenant is chosen', async () => {
    chooseTenant('research')
    expect(window.localStorage.getItem(TENANT_PREF)).toBe('research')
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await read(route('/v1/capacity'), () => false)
    await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(sentTenants(f)).toEqual(['research', 'research'])
    expect(apiHeaders({ accept: 'application/json' })).toEqual({ accept: 'application/json', [TENANT_HEADER]: 'research' })
  })

  it('costs nothing when storage throws', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('private window')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('private window')
    })
    expect(() => chooseTenant('research')).not.toThrow()
    expect(chosenTenant()).toBeNull()
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('ok')
    expect(sentTenants(f)).toEqual([null])
    getItem.mockRestore()
    setItem.mockRestore()
  })

  it('every direct fetch() outside fetch.ts builds its headers with apiHeaders', () => {
    // A submit path that called `fetch` with its own headers would file the
    // work under the DEFAULT tenant while the switcher shows another one.
    const dir = join(__dirname, '..')
    const files = readdirSync(dir).filter((n) => /\.(ts|tsx)$/.test(n) && n !== 'fetch.ts')
    const offenders: string[] = []
    let calls = 0
    for (const name of files) {
      const src = readFileSync(join(dir, name), 'utf8')
      for (const m of src.matchAll(/\bawait fetch\(/g)) {
        calls++
        // The call's own options object: up to the line that closes it.
        const rest = src.slice(m.index!)
        const call = rest.slice(0, rest.search(/\n\s*\}\)/))
        if (!call.includes('headers: apiHeaders(')) offenders.push(name)
        // And forgets a refused stored tenant, as read() and write() do.
        if (!src.includes('dropRefusedTenant(')) offenders.push(`${name} (no dropRefusedTenant)`)
      }
    }
    // The sweep ran over the two submit paths, not over nothing.
    expect(calls).toBeGreaterThanOrEqual(2)
    expect(offenders).toEqual([])
  })
})

describe('a stored tenant the caller is no longer in', () => {
  it('is dropped on the first tenant_not_member 403, and the read retried on the default', async () => {
    chooseTenant('research')
    const f = vi
      .fn()
      .mockResolvedValueOnce(refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
      .mockResolvedValueOnce(ok({ pools: ['default'] }))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('ok')
    expect(sentTenants(f)).toEqual(['research', null])
    expect(chosenTenant()).toBeNull()
    expect(window.localStorage.getItem(TENANT_PREF)).toBeNull()
  })

  it('is dropped on a refused write, which is NOT retried under another tenant', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    expect(chosenTenant()).toBeNull()
  })

  it('is kept on any other 403', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('forbidden', 'admin group membership is required for this operation'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/admin/tenants'), () => false)
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    expect(chosenTenant()).toBe('research')
  })
})

// ---------------------------------------------------------------------------
// The spine's tenant block
// ---------------------------------------------------------------------------

const ENG = { tenant_id: 'eng', display_name: 'eng@example.com' }
const RESEARCH = { tenant_id: 'research', display_name: 'research@example.com' }

function me(tenantId: string) {
  return {
    tenant: { tenant_id: tenantId, display_name: tenantId === 'eng' ? 'Engineering' : 'Research' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

/** A live API with `mine` as the caller's verified memberships. */
function liveApi(mine: { tenant_id: string; display_name: string }[]) {
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) return ok(me(chosen ?? mine[0]?.tenant_id ?? 'eng'))
    return ok({ pools: [] })
  })
  globalThis.fetch = f as unknown as typeof fetch
  return f
}

/** A screen whose read is counted, so a re-read after switching is visible. */
let screenReads = 0
function CountingScreen() {
  const [n, setN] = useState(0)
  useEffect(() => {
    screenReads++
    setN(screenReads)
  }, [])
  return <p data-reads={n}>screen</p>
}

/** The shell's own module instances, so a test's child shares its stores. */
type Mods = { Shell: typeof import('../Shell') }

async function shell(opts: { keep?: boolean; child?: (m: Mods) => ReactNode } = {}) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  const Shell = await import('../Shell')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null} keepOnSwitch={opts.keep ?? false}>
      {opts.child === undefined ? <CountingScreen /> : opts.child({ Shell })}
    </SkyShell>,
  )
}

const switcher = () => document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
const picker = () => document.querySelector<HTMLElement>('.sk-tpop[role="dialog"]')
const rowFor = (name: string) =>
  [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop .sk-to')].find((b) => b.querySelector('b')?.textContent === name)

describe('the spine tenant block', () => {
  it('is a static label with copy-id for one tenant', async () => {
    liveApi([ENG])
    await shell()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
    expect(switcher()).toBeNull()
    expect(document.querySelector('.sk-tenant .sk-cp')).not.toBeNull()
  })

  it('is a static label for a caller in no tenant group', async () => {
    liveApi([])
    await shell()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')).not.toBeNull())
    expect(switcher()).toBeNull()
  })

  it('is a switcher for more than one (2A): the block opens the list, and choosing stores, sends and re-reads', async () => {
    // MUTATION: keep the <select>, drop the "1 of 2", or stop re-reading.
    const f = liveApi([ENG, RESEARCH])
    screenReads = 0
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    const btn = switcher()!
    expect(document.querySelector('.sk-tenant select'), 'the select is back').toBeNull()
    expect(btn.querySelector('small')?.textContent).toBe('tenant · 1 of 2')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    // The copy id stays with the block.
    expect(document.querySelector('.sk-tenant .sk-cp')).not.toBeNull()

    fireEvent.click(btn)
    const list = picker()!
    expect(list.getAttribute('aria-label')).toBe('Act as')
    expect(list.textContent).toContain('Act as · 2 tenants you are a member of')
    expect(rowFor('eng@example.com')?.getAttribute('aria-current')).toBe('true')
    expect(rowFor('research@example.com')?.getAttribute('aria-current')).toBeNull()
    const readsBefore = screenReads
    const callsBefore = f.mock.calls.length

    await act(async () => {
      fireEvent.click(rowFor('research@example.com')!)
    })

    expect(picker()).toBeNull()
    expect(window.localStorage.getItem(TENANT_PREF)).toBe('research')
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Research'))
    // The screen under the shell was mounted afresh, so its reads ran again.
    expect(screenReads).toBeGreaterThan(readsBefore)
    // And every read after the switch carried the new tenant.
    const after = sentTenants(f).slice(callsBefore)
    expect(after.length).toBeGreaterThan(0)
    expect(after.every((t) => t === 'research')).toBe(true)
  })

  it('acknowledges the switch in a toast that carries the way back', async () => {
    // MUTATION: drop the toast, or its "Back to" action.
    liveApi([ENG, RESEARCH])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    fireEvent.click(switcher()!)
    await act(async () => {
      fireEvent.click(rowFor('research@example.com')!)
    })
    const toast = document.querySelector('.c-toast')
    expect(toast?.textContent).toContain('Now acting as research@example.com')
    const back = [...document.querySelectorAll<HTMLButtonElement>('.c-toast button')].find((b) => b.textContent === 'Back to Engineering')
    expect(back, 'the toast has no way back').toBeDefined()
    await act(async () => {
      fireEvent.click(back!)
    })
    expect(window.localStorage.getItem(TENANT_PREF)).toBe('eng')
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
  })

  it('keeps the tenant on the collapsed spine as a tile that opens the same list', async () => {
    // MUTATION: draw no tile when the panel is collapsed.
    window.localStorage.setItem('swarm.shell.collapsed', '1')
    liveApi([ENG, RESEARCH])
    await shell()
    expect(document.querySelector('.sk-panel'), 'the panel is not collapsed').toBeNull()
    const tile = await waitFor(() => {
      const t = document.querySelector<HTMLButtonElement>('.sk-spine button.sk-ttile')
      expect(t).not.toBeNull()
      return t!
    })
    // THE INITIAL ONLY, THE NAME IN THE TOOLTIP (U10a D26): `E Engin…` was
    // a cut name in a 44px tile.
    expect((tile.textContent ?? '').trim()).toBe('E')
    expect(tile.getAttribute('title')).toContain('Engineering')
    expect(tile.getAttribute('aria-label')).toContain('Engineering')
    fireEvent.click(tile)
    expect(picker()?.className).toContain('is-tile')
    expect(rowFor('research@example.com')).toBeDefined()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(picker()).toBeNull()
  })

  it('puts a chip in the phone header that opens a bottom sheet of 44px rows', async () => {
    // MUTATION: keep the read-only `.sk-tn` for a switcher.
    liveApi([ENG, RESEARCH])
    await shell()
    const chip = await waitFor(() => {
      const c = document.querySelector<HTMLButtonElement>('.sk-pbar .sk-tchip')
      expect(c).not.toBeNull()
      return c!
    })
    expect(document.querySelector('.sk-pbar .sk-tn')).toBeNull()
    fireEvent.click(chip)
    const sheet = picker()!
    expect(sheet.className).toContain('is-sheet')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(STYLES).toMatch(/\.sk-tpop\.is-sheet \.sk-to \{[^}]*min-height: 44px/)
    expect(document.querySelector('.sk-scrim.is-sheet')).not.toBeNull()
  })
})

/** A form whose draft is component state, as Submit's are. */
function draftForm(useSubmitAs: () => string | null) {
  return function DraftForm() {
    const [draft, setDraft] = useState('')
    const as = useSubmitAs()
    return (
      <form>
        <input aria-label="draft" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button type="submit">{as === null ? 'Submit one task' : `Submit as ${as}`}</button>
      </form>
    )
  }
}

let formReads = 0
const loadForm = async (): Promise<Result<{ n: number }>> => {
  formReads++
  return { status: 'ok', data: { n: formReads }, fetchedAt: Date.now() }
}

describe('an unsent form across a tenant switch (2A)', () => {
  it('is KEPT, re-read in place, and its button names the new tenant', async () => {
    // MUTATION: key the kept page on the tenant again (the #501 discard), or
    // leave the button saying nothing about where it now submits.
    liveApi([ENG, RESEARCH])
    formReads = 0
    await shell({
      keep: true,
      child: ({ Shell }) => {
        const DraftForm = draftForm(Shell.useSubmitAs)
        return (
          <Shell.Screen title="Submit a task" load={loadForm}>
            {(d) => (
              <>
                <p data-read={d.n}>read {d.n}</p>
                <DraftForm />
              </>
            )}
          </Shell.Screen>
        )
      },
    })
    const field = await screen.findByLabelText('draft')
    fireEvent.change(field, { target: { value: 'half a prompt' } })
    expect(screen.getByRole('button', { name: 'Submit one task' })).toBeTruthy()
    await waitFor(() => expect(switcher()).not.toBeNull())
    const readsBefore = formReads

    fireEvent.click(switcher()!)
    await act(async () => {
      fireEvent.click(rowFor('research@example.com')!)
    })

    // The draft survived: the same input, still holding what was typed.
    expect((screen.getByLabelText('draft') as HTMLInputElement).value).toBe('half a prompt')
    // What the form was drawn from was read again, as the new tenant.
    await waitFor(() => expect(formReads).toBeGreaterThan(readsBefore))
    // The button and the banner name where it will now go.
    expect(screen.getByRole('button', { name: 'Submit as research@example.com' })).toBeTruthy()
    const banner = document.querySelector('.sk-kept .c-banner')
    expect(banner?.textContent).toContain('You switched to research@example.com with an unsent form open.')
    expect(banner?.textContent).toContain('it will now submit as research@example.com')
    const back = [...banner!.querySelectorAll('button')].find((b) => b.textContent === 'Switch back to Engineering')
    expect(back, 'the banner has no way back').toBeDefined()
    await act(async () => {
      fireEvent.click(back!)
    })
    expect(window.localStorage.getItem(TENANT_PREF)).toBe('eng')
    expect((screen.getByLabelText('draft') as HTMLInputElement).value).toBe('half a prompt')
  })

  it('is not kept on a page that is not a form: a switch remounts it', async () => {
    liveApi([ENG, RESEARCH])
    await shell({
      child: ({ Shell }) => {
        const DraftForm = draftForm(Shell.useSubmitAs)
        return <DraftForm />
      },
    })
    fireEvent.change(await screen.findByLabelText('draft'), { target: { value: 'typed' } })
    await waitFor(() => expect(switcher()).not.toBeNull())
    fireEvent.click(switcher()!)
    await act(async () => {
      fireEvent.click(rowFor('research@example.com')!)
    })
    expect((screen.getByLabelText('draft') as HTMLInputElement).value).toBe('')
    expect(document.querySelector('.sk-kept')).toBeNull()
  })
})

describe('a refused tenant that storage will not forget', () => {
  it('is not retried with the same header', async () => {
    chooseTenant('research')
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    removeItem.mockRestore()
  })
})

describe('a URL the browser fetches by itself', () => {
  // An <img src>, a download href and an "open full" tab carry no header, so
  // the choice rides as ?tenant= -- validated by the API exactly as the header.
  it('carries the chosen tenant as ?tenant=', () => {
    chooseTenant('research')
    const url = new URL(artifactRawUrl('task_r', 'out.txt', 'inline'), 'http://x')
    expect(url.pathname).toBe('/v1/tasks/task_r/artifacts/raw')
    expect(url.searchParams.get('tenant')).toBe('research')
    expect(url.searchParams.get('name')).toBe('out.txt')
  })

  it('carries nothing when no tenant was chosen', () => {
    const url = new URL(artifactRawUrl('task_r', 'out.txt', 'attachment'), 'http://x')
    expect(url.searchParams.has('tenant')).toBe(false)
  })
})

describe('a direct submit path refused for its tenant', () => {
  it('drops the stored tenant, is not retried, and is not an admin refusal', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await postWorkflow({ tasks: [] })
    expect(f).toHaveBeenCalledTimes(1)
    expect(sentTenants(f)).toEqual(['research'])
    expect(chosenTenant()).toBeNull()
    expect(r.kind).toBe('rejected')
    if (r.kind === 'rejected') expect(r.error.kind).toBe('tenant_unresolved')
  })

  it('keeps the stored tenant on any other 403', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('forbidden', 'admin group membership is required for this operation'))
    globalThis.fetch = f as unknown as typeof fetch
    await postWorkflow({ tasks: [] })
    expect(chosenTenant()).toBe('research')
  })
})
