/**
 * QA PASS G1/G3 (swarm.saga.xyz, 2026-10-07): the shell's rail, its tenant
 * names and its titles.
 *
 *   G1-01  a tab opened in the background never made its first frame read, so
 *          the rail's Global pool sat at `—` -- which, by the house legend, is
 *          "nothing recorded", not "still reading".
 *   G3-04  Recent marked workflows running that the list read had as
 *          succeeded: only the newest five refreshed an opened entry, and an
 *          entry no read refreshed kept whatever state it was opened in.
 *   G1-06  one tenant had three names at once: the panel's `me` name, the
 *          list's group email and the page's id.
 *   G1-11  `/nope` kept Overview's identity: its "On this page" anchors, the
 *          phone header's "Overview", no `<h1>`.
 *   G1-12  every page's tab was titled "SwarmCloud".
 *
 * MUTATIONS: restore the hidden-tab return before the first read; draw `—`
 * for a loading meter; slice the offered board to five again; draw a state no
 * read of this load gave; name the current tenant from `/v1/tenants/mine`;
 * drop `missing` from the panel, the `Not found` title or the `page` heading;
 * remove the `document.title` effect -- each turns a case red.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TENANT_HEADER } from '../fetch'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

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

const GLOBAL = { name: 'global', active: 3, effective_limit: 100, available: 97, enabled: true }

/** A live API: `capacity` answers `/v1/capacity` (a Response, or a promise of one). */
function liveApi(opts: { mine?: (typeof ENG)[]; capacity?: () => Promise<Response> } = {}) {
  const mine = opts.mine ?? [ENG]
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) return ok(me(chosen ?? mine[0]?.tenant_id ?? 'eng'))
    if (url.includes('/v1/capacity')) return opts.capacity === undefined ? ok({ pools: [GLOBAL] }) : opts.capacity()
    return ok({})
  })
  globalThis.fetch = f as unknown as typeof fetch
  return f
}

async function shell(section: 'overview' | 'work' = 'overview', tab = 'now') {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section={section} tab={tab} title="Overview" go={vi.fn()} foot={null}>
      <p>screen</p>
    </SkyShell>,
  )
}

const meterValue = () => document.querySelector('.sk-meter .sk-mh b')?.textContent ?? null

let hidden: PropertyDescriptor | undefined
function hideTab() {
  hidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden')
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  if (hidden !== undefined) {
    delete (document as unknown as { hidden?: boolean }).hidden
    hidden = undefined
  }
  localStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('G1-01: the rail meter reads in a background tab, and says when it is still reading', () => {
  it('makes the first capacity read in a tab that loads hidden', async () => {
    hideTab()
    expect(document.hidden).toBe(true)
    const f = liveApi()
    await shell()
    await waitFor(() => expect(meterValue()).toBe('3 / 100'))
    expect(f.mock.calls.some(([u]) => String(u).includes('/v1/capacity'))).toBe(true)
  })

  it('draws `reading…`, not the dash, while the first read is out', async () => {
    let answer: (r: Response) => void = () => {}
    liveApi({ capacity: () => new Promise<Response>((r) => (answer = r)) })
    await shell()
    await waitFor(() => expect(meterValue()).toBe('reading…'))
    await act(async () => answer(ok({ pools: [GLOBAL] })))
    await waitFor(() => expect(meterValue()).toBe('3 / 100'))
  })

  it('keeps the dash for a read that landed without a global pool, and `not read` for a failed one', async () => {
    liveApi({ capacity: async () => ok({ pools: [{ ...GLOBAL, name: 'claude' }] }) })
    await shell()
    await waitFor(() => expect(meterValue()).toBe('—'))
    expect(document.querySelector('.sk-meter .sk-mh b')?.getAttribute('title')).toMatch(/without a global pool/)
    cleanup()
    liveApi({ capacity: async () => new Response(JSON.stringify({ code: 'boom', message: 'boom' }), { status: 500, headers: JSON_HEADERS }) })
    await shell()
    await waitFor(() => expect(meterValue()).toBe('not read'))
  })
})

describe('G3-04: Recent marks only states a read of this page load gave', () => {
  it('refreshes an opened workflow from the whole board read, and withholds the mark from one no read refreshed', async () => {
    vi.resetModules()
    const Spine = await import('../Spine')
    const { STATE_MARK } = await import('../marks')
    localStorage.setItem(
      Spine.RECENT_WORKFLOWS_KEY,
      JSON.stringify([
        { id: 'wf_old', state: 'RUNNING', name: 'old-lane' },
        { id: 'wf_gone', state: 'RUNNING', name: 'gone-lane' },
      ]),
    )
    // The board read, newest first: wf_old is seventh, outside the newest five.
    const board = Array.from({ length: 6 }, (_, i) => ({ id: `wf_${i}`, state: 'RUNNING' as const, name: `n-${i}` }))
    Spine.offerNewestWorkflows([...board, { id: 'wf_old', state: 'SUCCEEDED', name: 'old-lane' }])
    expect(Spine.recentWorkflows().find((w) => w.id === 'wf_old')?.state).toBe('SUCCEEDED')
    expect(Spine.workflowReadThisLoad('wf_old')).toBe(true)
    expect(Spine.workflowReadThisLoad('wf_gone')).toBe(false)

    render(
      <Spine.SkyShell section="work" tab="workflows" title="Workflows" go={vi.fn()} foot={null}>
        {null}
      </Spine.SkyShell>,
    )
    const group = screen.getByRole('group', { name: 'Recent workflows' })
    const row = (name: string) => [...group.querySelectorAll<HTMLElement>('button')].find((b) => b.textContent?.includes(name))!
    const old = row('old-lane').querySelector('.sk-st')!
    expect(old.getAttribute('data-mark')).toBe(STATE_MARK.SUCCEEDED.mark)
    const gone = row('gone-lane').querySelector('.sk-st')!
    expect(gone.getAttribute('data-mark')).toBe('stale')
    expect(gone.querySelector('svg'), 'a state no read of this load gave is drawn as a state').toBeNull()
    expect(gone.textContent).toMatch(/last seen running; not re-read/)
  })

  it('marks a workflow again once it is opened in this load', async () => {
    vi.resetModules()
    const Spine = await import('../Spine')
    localStorage.setItem(Spine.RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_x', state: 'RUNNING', name: null }]))
    expect(Spine.workflowReadThisLoad('wf_x')).toBe(false)
    Spine.rememberWorkflow('wf_x', 'RUNNING')
    expect(Spine.workflowReadThisLoad('wf_x')).toBe(true)
  })
})

describe('G1-06: the tenant has one name in the shell', () => {
  const rows = () => [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop .sk-to')]
  const nameOf = (id: string) => rows().find((b) => b.querySelector('small')?.textContent === id)?.querySelector('b')?.textContent

  it('names the current tenant in the list as the panel does, with its id as the second line', async () => {
    liveApi({ mine: [ENG, RESEARCH] })
    await shell()
    const sw = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
      expect(b).not.toBeNull()
      return b!
    })
    expect(sw.querySelector('b')?.textContent).toBe('Engineering')
    fireEvent.click(sw)
    expect(nameOf('eng')).toBe('Engineering')
    // Never acted as here: the group email is the only name there is.
    expect(nameOf('research')).toBe('research@example.com')
    expect(rows().find((b) => b.querySelector('small')?.textContent === 'eng')?.getAttribute('title')).toBe('registered as eng@example.com')

    await act(async () => {
      fireEvent.click(rows().find((b) => b.querySelector('small')?.textContent === 'research')!)
    })
    await waitFor(() => expect(document.querySelector('.sk-tenant .sk-tsw b')?.textContent).toBe('Research'))
    fireEvent.click(document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')!)
    // The tenant left keeps the name `me` gave it.
    expect(nameOf('eng')).toBe('Engineering')
    expect(nameOf('research')).toBe('Research')
  })
})

describe('G1-11 and G1-12: the 404 is titled as one, and every tab says its page', () => {
  async function app(path: string) {
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', path)
    render(<App />)
  }

  it('drops Overview’s anchors from the panel, titles the phone header and the tab "Not found", and heads the page with an h1', async () => {
    await app('/nope')
    const h1 = await screen.findByRole('heading', { level: 1, name: /No page at \/nope/ })
    expect(h1).toBeTruthy()
    expect(document.querySelectorAll('h1')).toHaveLength(1)
    expect([...document.querySelectorAll('.sk-pt')].map((e) => e.textContent)).not.toContain('On this page')
    expect(document.querySelector('.sk-pbar > b')?.textContent).toBe('Not found')
    await waitFor(() => expect(document.title).toBe('Not found · SwarmCloud'))
  })

  it('titles each page, and an open object by its id first', async () => {
    const { documentTitleOf } = await import('../App')
    expect(documentTitleOf('SwarmCloud', null)).toBe('SwarmCloud')
    await app('/overview')
    await waitFor(() => expect(document.title).toBe('Overview · SwarmCloud'))
    // Overview's anchors are still Overview's own.
    expect([...document.querySelectorAll('.sk-pt')].map((e) => e.textContent)).toContain('On this page')
    cleanup()
    await app('/help')
    await waitFor(() => expect(document.title).toBe('Help · SwarmCloud'))
    cleanup()
    await app('/workflows/wf_title_probe')
    await waitFor(() => expect(document.title).toBe('wf_title_probe · Workflows · SwarmCloud'))
  })
})
