// CAPACITY › LLM PROVIDERS (lane LP-U1; docs/design/llm-providers.md §6.1, the
// owner's pick of 10 November 2026, direction "Tables and a side sheet").
//
// WHAT EACH CASE HOLDS:
//   * Plans is `/capacity/llm-providers/plans` and API Keys
//     `/capacity/llm-providers/api-keys`, both ways, and each renders its
//     sub-page under the sub-page control: Plans the plans table, API Keys its
//     empty state with Add key disabled behind an (i);
//   * the old `/capacity/accounts` lands on Plans with its `?account=` kept,
//     in the router and in the app's bar;
//   * the side panel draws LLM Providers with Plans and API Keys as its first
//     two children, the open one lit.
//
// Placeholders only; nothing is token-shaped.

import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

// The plans table is LlmPlans.tsx's; llmplans.test.tsx holds it. A stand-in
// here keeps this file about the tab, not about the table's reads. On Plans
// the table's head is the page's one head, with the tab's (i) (`info`), and
// the strip right under it (`nav`), so the stand-in draws what it is given in
// that order; llmproviders.head.test.tsx holds the real page's every read state.
vi.mock('../LlmPlans', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../LlmPlans')>()),
  LlmPlansScreen: ({ title, info, nav }: { title?: string; info?: ReactNode; nav?: ReactNode }) => (
    <>
      <h1>{title}</h1>
      {info}
      {nav}
      <p data-testid="plan-board">plans table</p>
    </>
  ),
}))

const { canonical, fromAddress } = await import('../App')
const { addressToPath, pathToAddress } = await import('../paths')
const { PANEL_PAGES } = await import('../Spine')

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

/** A path as the router resolves it and writes it back. */
function landsOn(path: string, search = ''): string {
  const p = pathToAddress(path, search)
  expect(p, `${path} is not served`).not.toBeNull()
  return addressToPath(canonical(fromAddress(p!.address)))
}

describe('Capacity › LLM Providers’ addresses', () => {
  it.each([
    ['/capacity/llm-providers/plans', 'plans'],
    ['/capacity/llm-providers/api-keys', 'api-keys'],
  ])('serves %s as the llm-providers tab', (path, page) => {
    // MUTATION: drop the paths from paths.ts FIXED and each is a 404.
    const p = pathToAddress(path)
    expect(p?.address).toBe(path.slice(1))
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('capacity')
    expect(r.tab).toBe('llm-providers')
    expect(r.page).toBe(page)
    expect(landsOn(path)).toBe(path)
  })

  it('opens Plans for the tab with no sub-page named', () => {
    expect(addressToPath(canonical(fromAddress('capacity/llm-providers')))).toBe('/capacity/llm-providers/plans')
  })

  it('keeps Plans’ chosen account and view on its address', () => {
    expect(landsOn('/capacity/llm-providers/plans', '?account=eng%3Ab&view=timeline')).toBe('/capacity/llm-providers/plans?account=eng%3Ab&view=timeline')
  })

  it.each([
    ['/capacity/accounts', '', '/capacity/llm-providers/plans'],
    ['/capacity/accounts', '?account=eng%3Ab', '/capacity/llm-providers/plans?account=eng%3Ab'],
    ['/capacity/accounts', '?account=eng%3Ab&view=timeline', '/capacity/llm-providers/plans?account=eng%3Ab&view=timeline'],
  ])('lands the old %s%s on %s', (path, search, want) => {
    // MUTATION: drop the redirect in fromAddress and Accounts stays where it was.
    expect(landsOn(path, search)).toBe(want)
  })

  it('lands the old hash #capacity/accounts?account= on Plans with the account', () => {
    expect(canonical(fromAddress('capacity/accounts?account=eng%3Ab'))).toBe('capacity/llm-providers/plans?account=eng%3Ab')
    expect(canonical(fromAddress('settings/accounts'))).toBe('capacity/llm-providers/plans')
  })
})

describe('Capacity › LLM Providers in the app', () => {
  async function open(url: string) {
    window.history.replaceState(null, '', url)
    const { App } = await import('../App')
    render(<App />)
  }

  it('rewrites /capacity/accounts?account= to Plans, the account kept, and draws the plans table', async () => {
    await open('/capacity/accounts?account=eng%3Ab')
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/llm-providers/plans'))
    expect(window.location.search).toBe('?account=eng%3Ab')
    expect(await screen.findByTestId('plan-board')).toBeTruthy()
  })

  it('draws Plans under the sub-page control, Plans current', async () => {
    await open('/capacity/llm-providers/plans')
    const nav = await screen.findByRole('navigation', { name: 'LLM Providers' })
    const links = within(nav).getAllByRole('link')
    expect(links.map((a) => a.textContent)).toEqual(['Plans', 'API Keys'])
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/capacity/llm-providers/plans', '/capacity/llm-providers/api-keys'])
    expect(within(nav).getByRole('link', { current: 'page' }).textContent).toBe('Plans')
    expect(screen.getByTestId('plan-board')).toBeTruthy()
    // ONE HEAD (qa.head Q2/Q10): the board's, titled with the tab's name.
    // MUTATION: draw LlmProviders' own PageHead on Plans too -> two.
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['LLM Providers'])
  })

  it('draws API Keys’ empty state, its Add key a disabled button with an (i)', async () => {
    await open('/capacity/llm-providers/api-keys')
    const nav = await screen.findByRole('navigation', { name: 'LLM Providers' })
    expect(within(nav).getByRole('link', { current: 'page' }).textContent).toBe('API Keys')
    expect(screen.getByRole('heading', { name: /^No API keys/ })).toBeTruthy()
    const add = screen.getByRole('button', { name: 'Add key' }) as HTMLButtonElement
    // MUTATION: enable it, and a button that calls nothing is on the page.
    expect(add.disabled).toBe(true)
    expect(add.closest('.c-btn-wrap')?.querySelector('.acct-i')).not.toBeNull()
    expect(screen.queryByTestId('plan-board')).toBeNull()
  })

  it('moves between the sub-pages from the control', async () => {
    await open('/capacity/llm-providers/plans')
    const nav = await screen.findByRole('navigation', { name: 'LLM Providers' })
    within(nav).getByRole('link', { name: 'API Keys' }).click()
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/llm-providers/api-keys'))
    expect(await screen.findByRole('heading', { name: /^No API keys/ })).toBeTruthy()
  })
})

describe('the side panel’s LLM Providers', () => {
  it('replaces Accounts with LLM Providers, Plans and API Keys its first two children', () => {
    // MUTATION: leave the Accounts row in PANEL_PAGES and these are absent.
    const row = PANEL_PAGES.capacity.find((p) => p.key === 'llm-providers')
    expect(row?.label).toBe('LLM Providers')
    expect(row?.to).toBe('capacity/llm-providers/plans')
    expect(row?.kids?.slice(0, 2).map((k) => [k.label, k.to])).toEqual([
      ['Plans', 'capacity/llm-providers/plans'],
      ['API Keys', 'capacity/llm-providers/api-keys'],
    ])
    expect(PANEL_PAGES.capacity.some((p) => p.key === 'accounts')).toBe(false)
  })

  it.each([
    ['/capacity/llm-providers/plans', 'Plans'],
    ['/capacity/llm-providers/api-keys', 'API Keys'],
  ])('on %s shows both children under LLM Providers, %s lit', async (url, lit) => {
    window.history.replaceState(null, '', url)
    const { App } = await import('../App')
    render(<App />)
    await screen.findByRole('navigation', { name: 'LLM Providers' })
    const panel = document.querySelector('.sk-panel')!
    const group = [...panel.querySelectorAll('a.sk-pk.is-group')].find((a) => a.textContent?.includes('LLM Providers'))
    expect(group, 'no LLM Providers group in the panel').toBeTruthy()
    const kids = [...group!.parentElement!.querySelectorAll('.sk-kids a.sk-kid')].map((a) => a.textContent)
    expect(kids.slice(0, 2)).toEqual(['Plans', 'API Keys'])
    expect([...panel.querySelectorAll('[aria-current="page"]')].map((a) => a.textContent)).toEqual([lit])
  })
})
