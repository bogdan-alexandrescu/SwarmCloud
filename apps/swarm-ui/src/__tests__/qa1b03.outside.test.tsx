/**
 * QA1B-03 (part of #2596): the pass-1 findings whose fixes landed in App.tsx,
 * SubmitChooser.tsx and components/Overlay.tsx, outside the lanes that found
 * them, applied here.
 *
 *   capacity-10          API reads' own "failures only" toggle moves the
 *                        panel's pair, and the filter rides on the address
 *                        (`?failed=1`), so it can be linked and survives a reload.
 *   chrome-C18           the Submit hub's foot names the sidebar, not "the spine".
 *   overview-agents-20   an open agent's tab is titled by its name.
 *   overview-agents-65   an open workflow's tab is titled by its name.
 *   overview-agents-85   TypedConfirm's hint says "id" where an id is typed.
 *   config-32            a Config page's `?` card is titled by its tab.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { App, canonical, fromAddress } from '../App'
import { rememberAgentName } from '../agentlist'
import { TypedConfirm } from '../components'
import { addressToPath, pathToAddress } from '../paths'
import { rememberWorkflow } from '../Spine'
import { SubmitChooser } from '../SubmitChooser'

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

/** `/v1/tenants/me` answered; every other read fails. */
function serve() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/me') && !url.includes('/v1/tenants/mine')) {
      return json({
        tenant: { tenant_id: 'u-ana', display_name: 'Your workspace' },
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
        environment: 'dev',
        environment_declared: true,
      })
    }
    return new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

const panelPressed = () => screen.getByRole('button', { name: 'Failures only' }).getAttribute('aria-pressed')
const pagePressed = () => screen.getByRole('button', { name: 'failures only' }).getAttribute('aria-pressed')

describe('capacity-10: API reads has one failures-only filter, on its address', () => {
  // 90s, and 60s for the page's toggle: this is the file's first render of the
  // whole <App />, so it pays the cold transform of every page it reaches --
  // ~21s on an idle machine, and past 40s on a loaded CI shard (PR #2667's
  // first run). A genuine hang still fails, just later.
  it('presses the panel’s Failures only when the page’s own toggle is pressed, and writes ?failed=1', { timeout: 90_000 }, async () => {
    serve()
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    await waitFor(() => expect(panelPressed()).toBe('false'))
    await waitFor(() => expect(pagePressed()).toBe('false'), { timeout: 60_000 })
    fireEvent.click(screen.getByRole('button', { name: 'failures only' }))
    // MUTATION: drop `onFailuresOnly?.(...)` from ReferenceScreen's toggle and the panel keeps "Every read" pressed.
    await waitFor(() => expect(panelPressed()).toBe('true'))
    // MUTATION: drop API reads from `keptKeys` and the address stays bare.
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/capacity/api-reads?failed=1'))
    fireEvent.click(screen.getByRole('button', { name: 'Every read this tab made' }))
    await waitFor(() => expect(pagePressed()).toBe('false'))
    await waitFor(() => expect(window.location.search).toBe(''))
  })

  it('opens /capacity/api-reads?failed=1 filtered, on the page and in the panel', { timeout: 40_000 }, async () => {
    serve()
    window.history.replaceState(null, '', '/capacity/api-reads?failed=1')
    render(<App />)
    await waitFor(() => expect(panelPressed()).toBe('true'))
    await waitFor(() => expect(pagePressed()).toBe('true'), { timeout: 20_000 })
    expect(window.location.search).toBe('?failed=1')
  })

  it('reads and writes the address both ways', () => {
    const p = pathToAddress('/capacity/api-reads', '?failed=1')
    expect(p?.address).toBe('capacity/api-reads?failed=1')
    const r = fromAddress(p!.address)
    expect(r).toMatchObject({ sectionId: 'capacity', tab: 'api-reads', view: 'failed=1' })
    expect(addressToPath(canonical(r))).toBe('/capacity/api-reads?failed=1')
  })
})

describe('chrome-C18: the Submit hub’s foot names the sidebar', () => {
  it('says the Submit button in the sidebar opens the page', () => {
    serve()
    render(<SubmitChooser go={() => {}} />)
    const foot = document.querySelector('.sb-foot')!
    // MUTATION: restore "The spine’s Submit button" in SubmitChooser.tsx.
    expect(foot.textContent).toContain('The Submit button in the sidebar opens this page')
    expect(foot.textContent).not.toMatch(/spine/i)
  })
})

describe('overview-agents-20 and -65: an open object’s tab is titled by its name', () => {
  it('titles an open agent by the name its split recorded', async () => {
    serve()
    rememberAgentName({ id: 'task_title_named', step_id: 'Fix flaky lease heartbeat test', runner_profile: 'claude-code' })
    window.history.replaceState(null, '', '/agents/live/task_title_named')
    render(<App />)
    // MUTATION: title by `documentObjectOf(at)` alone and the tab reads "task_title_named · …".
    await waitFor(() => expect(document.title).toMatch(/^Fix flaky lease heartbeat test · /))
  })

  it('titles an open workflow by the name Recent holds', async () => {
    serve()
    rememberWorkflow('wf_title_named', null, 'Nightly audit')
    window.history.replaceState(null, '', '/workflows/wf_title_named')
    render(<App />)
    // MUTATION: drop `workflowNamed` from App's title and the tab reads "wf_title_named · Workflows · SwarmCloud".
    await waitFor(() => expect(document.title).toBe('Nightly audit · Workflows · SwarmCloud'))
  })
})

describe('overview-agents-85: TypedConfirm says what is typed', () => {
  it('says "id" when the reader types an id, and "name" by default', () => {
    const { unmount } = render(
      <TypedConfirm title="Cancel this workflow?" name="wf_demo" noun="id" verb="Cancel the workflow" keep="Keep it running" onConfirm={() => {}} onClose={() => {}}>
        <p>1 running step is asked to stop.</p>
      </TypedConfirm>,
    )
    // MUTATION: hard-code "name" in TypedConfirm's hint again.
    expect(document.querySelector('.c-hint')?.textContent).toBe('The button unlocks when the id matches exactly.')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'wf_demo' } })
    expect(document.querySelector('.c-hint')?.textContent).toBe('The id matches.')
    unmount()
    render(
      <TypedConfirm title="Delete it?" name="eng" verb="Delete" keep="Keep it" onConfirm={() => {}} onClose={() => {}}>
        <p>Gone.</p>
      </TypedConfirm>,
    )
    expect(document.querySelector('.c-hint')?.textContent).toBe('The button unlocks when the name matches exactly.')
  })
})

describe('config-32: a Config page’s `?` card is titled by its tab', () => {
  it('titles Config › Secrets’ card by its tab, not "Config answers"', async () => {
    serve()
    window.history.replaceState(null, '', '/config/secrets')
    render(<App />)
    const glyph = await waitFor(() => {
      const g = document.querySelector<HTMLButtonElement>('.ctl-q .ctl-q-glyph')
      expect(g).not.toBeNull()
      return g!
    })
    // MUTATION: pass no `title` to SectionQuestion and both read "Config answers".
    expect(glyph.getAttribute('aria-label')).toMatch(/^Help: (?!Config answers$).+ answers$/)
    fireEvent.click(glyph)
    const title = await waitFor(() => {
      const t = document.querySelector('.ctl-q-card .ctl-q-title')
      expect(t).not.toBeNull()
      return t!
    })
    expect(title.textContent).not.toBe('Config answers')
    expect(title.textContent).toMatch(/ answers$/)
  })
})
