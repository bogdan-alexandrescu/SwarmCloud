/**
 * THE TENANT SWITCHER (owner decision 2026-10-01; docs/multi-tenancy.md).
 *
 * The choice SELECTS among the caller's verified memberships and never grants
 * one: the server refuses any `X-Swarm-Tenant` it did not confirm. What the
 * client must get right is narrower and is held here:
 *
 *   * the header leaves from ONE place (fetch.ts's `apiHeaders`), on every
 *     read and write, and only when a choice is stored;
 *   * the choice lasts for the TAB'S SESSION (user-profile.md §4.5, Q2): it
 *     survives a reload, a new tab opens in the default, an old per-browser
 *     choice is removed and never honoured, and "Back to default" clears it;
 *     storage that throws costs nothing;
 *   * a headerless request refused with `preferences_unavailable` (Q9) is
 *     said beside the switcher, where naming a workspace is the next action;
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
import { checkpointDownloadHref } from '../CheckpointBrowser'

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
    window.sessionStorage.clear()
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
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('research')
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

describe('the switch lasts for the tab’s session (Q2)', () => {
  /** A reload: the page's modules are evaluated afresh over the same storage. */
  async function reloaded() {
    vi.resetModules()
    return import('../fetch')
  }

  it('is written to sessionStorage, survives a reload, and is absent in a new session', async () => {
    // MUTATION: keep the choice in localStorage -- it outlives the session.
    chooseTenant('research')
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('research')
    expect(window.localStorage.getItem(TENANT_PREF), 'the switch outlives the tab').toBeNull()
    const again = await reloaded()
    expect(again.chosenTenant(), 'a reload lost the switch').toBe('research')
    // A new tab or window: its sessionStorage starts empty, localStorage is shared.
    window.sessionStorage.clear()
    const fresh = await reloaded()
    expect(fresh.chosenTenant(), 'a new session kept the old switch').toBeNull()
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await fresh.read(fresh.route('/v1/capacity'), () => false)
    expect(sentTenants(f)).toEqual([null])
  })

  it('removes an old localStorage choice on first load and does not honour it', async () => {
    // MUTATION: drop the one-time removal, or read localStorage as a fallback.
    window.localStorage.setItem(TENANT_PREF, 'research')
    const loaded = await reloaded()
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await loaded.read(loaded.route('/v1/capacity'), () => false)
    expect(sentTenants(f), 'the old per-browser choice was sent').toEqual([null])
    expect(loaded.chosenTenant()).toBeNull()
    expect(window.localStorage.getItem(TENANT_PREF), 'the old choice was left behind').toBeNull()
  })

  it('"Back to default" (a null choice) clears it, and the next request names no workspace', async () => {
    chooseTenant('research')
    chooseTenant(null)
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBeNull()
    expect(chosenTenant()).toBeNull()
    expect(apiHeaders({ accept: 'application/json' })).toEqual({ accept: 'application/json' })
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
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBeNull()
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
function liveApi(mine: { tenant_id: string; display_name: string; kind?: 'user' | 'group' }[]) {
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

/** The API's 503 for a headerless request whose default is unknown (§4.5, Q9). */
const PREFS_MESSAGE =
  'Your SwarmCloud preferences could not be read, so your default workspace is not known and nothing was submitted. Name a workspace and retry.'
const prefsDown = () =>
  new Response(JSON.stringify({ code: 'preferences_unavailable', message: PREFS_MESSAGE }), { status: 503, headers: JSON_HEADERS })

/** A live API whose preferences are unreadable: every headerless request outside the three exempt reads is a 503. */
function prefsDownApi(mine: { tenant_id: string; display_name: string }[]) {
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) return ok(me(chosen ?? mine[0]?.tenant_id ?? 'eng'))
    if (chosen === null) return prefsDown()
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
    expect(btn.querySelector('small')?.textContent).toBe('workspace · 1 of 2')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    // The copy id stays with the block.
    expect(document.querySelector('.sk-tenant .sk-cp')).not.toBeNull()

    fireEvent.click(btn)
    const list = picker()!
    expect(list.getAttribute('aria-label')).toBe('Act as')
    expect(list.textContent).toContain('Act as · 2 workspaces you are a member of')
    // The current tenant is named as the panel names it (QA G1-06), not by its group email.
    expect(rowFor('Engineering')?.getAttribute('aria-current')).toBe('true')
    expect(rowFor('research')?.getAttribute('aria-current')).toBeNull()
    const readsBefore = screenReads
    const callsBefore = f.mock.calls.length

    await act(async () => {
      fireEvent.click(rowFor('research')!)
    })

    expect(picker()).toBeNull()
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('research')
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
      fireEvent.click(rowFor('research')!)
    })
    const toast = document.querySelector('.c-toast')
    expect(toast?.textContent).toContain('Now acting as research')
    const back = [...document.querySelectorAll<HTMLButtonElement>('.c-toast button')].find((b) => b.textContent === 'Back to Engineering')
    expect(back, 'the toast has no way back').toBeDefined()
    await act(async () => {
      fireEvent.click(back!)
    })
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('eng')
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
  })

  it('names your own personal workspace "Your workspace", first and current by default (#1200)', async () => {
    // MUTATION: drop the kind === 'user' branch in nameTenants, or the block's own label.
    const OWN = { tenant_id: 'u-ana-b', display_name: 'Your workspace (u-ana-b)', kind: 'user' as const }
    liveApi([OWN, { ...ENG, kind: 'group' as const }])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    expect(switcher()!.querySelector('b')?.textContent).toBe('Your workspace')
    expect(switcher()!.querySelector('small')?.textContent).toBe('personal · 1 of 2')
    fireEvent.click(switcher()!)
    expect(rowFor('Your workspace')?.getAttribute('aria-current')).toBe('true')
    expect(rowFor('Your workspace')?.querySelector('small')?.textContent).toBe('u-ana-b')
    await act(async () => {
      fireEvent.click(rowFor('eng')!)
    })
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('eng')
    const back = [...document.querySelectorAll<HTMLButtonElement>('.c-toast button')].find((b) => b.textContent === 'Back to Your workspace')
    expect(back, 'the way back does not name your workspace').toBeDefined()
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
    expect(rowFor('research')).toBeDefined()
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
        <button type="submit">{as === null ? 'Start one agent' : `Submit as ${as}`}</button>
      </form>
    )
  }
}

let formReads = 0
const loadForm = async (): Promise<Result<{ n: number }>> => {
  formReads++
  return { status: 'ok', data: { n: formReads }, fetchedAt: Date.now() }
}

describe('the default workspace in the tenant block (§4.5)', () => {
  const backButton = () =>
    [...document.querySelectorAll<HTMLButtonElement>('.sk-tenant button')].find((b) => b.textContent === 'Back to default')

  it('marks the default, offers "Back to default" only over a switch, and clears the switch', async () => {
    // MUTATION: drop the button, or have it keep the session's choice.
    liveApi([{ ...ENG, is_default: true } as typeof ENG, RESEARCH])
    window.sessionStorage.setItem(TENANT_PREF, 'research')
    await shell()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Research'))
    // Research is not the default, so the block does not call it one.
    expect(switcher()!.querySelector('small')?.textContent).toBe('workspace · 2 of 2')
    fireEvent.click(switcher()!)
    expect(rowFor('eng')?.textContent).toContain('default')
    expect(rowFor('Research')?.textContent).not.toContain('default')
    expect(picker()?.textContent).toContain('Kept for this tab’s session')
    fireEvent.keyDown(document, { key: 'Escape' })

    expect(backButton(), 'no way back to the default over a switch').toBeDefined()
    await act(async () => {
      fireEvent.click(backButton()!)
    })
    expect(window.sessionStorage.getItem(TENANT_PREF), 'Back to default kept the switch').toBeNull()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
    expect(switcher()!.querySelector('small')?.textContent).toBe('workspace · 1 of 2 · default')
    // In the default already, there is nothing to go back to.
    expect(backButton()).toBeUndefined()
  })

  it('shows a 503 preferences_unavailable message beside the switcher, until a workspace is picked', async () => {
    // MUTATION: raise the 503 only to the screen, and draw nothing in the panel.
    prefsDownApi([ENG, RESEARCH])
    await shell()
    const alert = await waitFor(() => {
      const a = document.querySelector<HTMLElement>('.sk-tprefs [role="alert"]')
      expect(a, 'the panel did not say why nothing was read').not.toBeNull()
      return a!
    })
    expect(alert.textContent).toContain(PREFS_MESSAGE)
    // Beside the switcher: the block's next sibling in the panel.
    expect(document.querySelector('.sk-panel .sk-tenant + .sk-tprefs')).not.toBeNull()
    fireEvent.click(switcher()!)
    await act(async () => {
      fireEvent.click(rowFor('research')!)
    })
    await waitFor(() => expect(document.querySelector('.sk-tprefs')).toBeNull())
  })
})

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
    expect(screen.getByRole('button', { name: 'Start one agent' })).toBeTruthy()
    await waitFor(() => expect(switcher()).not.toBeNull())
    const readsBefore = formReads

    fireEvent.click(switcher()!)
    await act(async () => {
      fireEvent.click(rowFor('research')!)
    })

    // The draft survived: the same input, still holding what was typed.
    expect((screen.getByLabelText('draft') as HTMLInputElement).value).toBe('half a prompt')
    // What the form was drawn from was read again, as the new tenant.
    await waitFor(() => expect(formReads).toBeGreaterThan(readsBefore))
    // The button and the banner name where it will now go.
    expect(screen.getByRole('button', { name: 'Submit as research' })).toBeTruthy()
    const banner = document.querySelector('.sk-kept .c-banner')
    expect(banner?.textContent).toContain('You switched to research with an unsent form open.')
    expect(banner?.textContent).toContain('it will now submit as research')
    const back = [...banner!.querySelectorAll('button')].find((b) => b.textContent === 'Switch back to Engineering')
    expect(back, 'the banner has no way back').toBeDefined()
    await act(async () => {
      fireEvent.click(back!)
    })
    expect(window.sessionStorage.getItem(TENANT_PREF)).toBe('eng')
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
      fireEvent.click(rowFor('research')!)
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

  it('carries the chosen tenant on the checkpoint download link', () => {
    chooseTenant('research')
    const url = new URL(checkpointDownloadHref('task_r', 'att_1', 'ckpt-00001'), 'http://x')
    expect(url.pathname).toBe('/v1/tasks/task_r/checkpoints/ckpt-00001/content')
    expect(url.searchParams.get('tenant')).toBe('research')
    expect(url.searchParams.get('attempt_id')).toBe('att_1')
  })

  it('carries nothing on the checkpoint download link when no tenant was chosen', () => {
    const url = new URL(checkpointDownloadHref('task_r', 'att_1', 'ckpt-00001'), 'http://x')
    expect(url.searchParams.has('tenant')).toBe(false)
  })
})

/** The live `postWorkflow`: these hand it real responses, so the fixture build's
 *  answer to the submit (QA2-FIX-07, runs2-02) is off for it alone; the rest of
 *  this file keeps the fixture reads it measures. */
async function livePostWorkflow(): Promise<typeof import('../SubmitWorkflow').postWorkflow> {
  vi.doMock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), USE_FIXTURES: false }))
  vi.resetModules()
  try {
    return (await import('../SubmitWorkflow')).postWorkflow
  } finally {
    vi.doUnmock('../api')
  }
}

describe('a direct submit path refused for its tenant', () => {
  it('drops the stored tenant, is not retried, and is not an admin refusal', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await (await livePostWorkflow())({ tasks: [] })
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
    await (await livePostWorkflow())({ tasks: [] })
    expect(chosenTenant()).toBe('research')
  })
})
