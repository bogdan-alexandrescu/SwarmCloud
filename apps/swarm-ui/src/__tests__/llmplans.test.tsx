// CAPACITY › LLM PROVIDERS › PLANS (docs/design/llm-providers.md §6; the
// owner's ask of 11 November 2026: the cards direction in place of the table).
//
// WHAT EACH CASE HOLDS:
//   * the cards group by vendor, one card a plan, off the account list
//     api.ts's fixture path serves (`loadAccountsBoard` is the real one here);
//   * each card leads with the 5-hour and 7-day quota LEFT (100 minus the
//     share used), each with a bar and its next reset as a local time (with
//     the day when not today) and the time until it;
//   * a stale window reads projected (`~`), an unmeasured one `unknown`, a
//     reset not served `reset unknown`;
//   * then the state, live holders over max agents, Voice it and the lends
//     as one line (who, holders over cap);
//   * ONE Manage control per card (the card opens it too), Resume only while
//     paused, no per-lend button: lends are edited in the Manage sheet;
//   * no text on a card below the body step, and a phone at 390 takes one
//     column with nothing wider than the viewport;
//   * every control in the Add plan, Manage and Lend sheets calls its api.ts
//     function once, with the right arguments;
//   * a control that fails says why in its sheet and leaves the card as it was;
//   * no plans is "No plans", the add control beside it and the read mark;
//   * `?account=` opens that plan's Manage sheet, and one naming a plan not
//     in the list draws neither a sheet nor its scrim;
//   * the page draws one h1.
//
// Placeholders only; nothing is token-shaped.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, ServedAccount } from '../api'
import type { Result } from '../fetch'
import PLANS_CSS from '../styles/llm-providers.css?raw'

const OK = { status: 'ok', fetchedAt: 0, data: { ok: true } } as const
const FAIL = {
  status: 'error',
  error: { kind: 'validation', httpStatus: 422, code: null, message: 'max_agents is above the limit' },
} as const

// The reads stay api.ts's own (its fixture path in this build); the writes are
// spies, so the fixture's rows never move and each call is counted.
const spies = vi.hoisted(() => ({
  setAccountState: vi.fn(),
  setAccountMaxAgents: vi.fn(),
  setAccountName: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountPerWorkspaceMax: vi.fn(),
  setAccountModelClasses: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  mintSetupLink: vi.fn(),
  loadAccountsBoard: vi.fn(),
  setAccountPools: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  spies.loadAccountsBoard.mockImplementation(actual.loadAccountsBoard)
  return { ...actual, ...spies, USE_FIXTURES: true }
})
vi.mock('../Accounts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../Accounts')>()),
  setAccountPools: spies.setAccountPools,
}))

const api = await import('../api')
const realLoad = (await vi.importActual<typeof import('../api')>('../api')).loadAccountsBoard
const { LlmProvidersScreen } = await import('../LlmProviders')

const WAIT = { timeout: 5000 } as const

beforeEach(() => {
  spies.loadAccountsBoard.mockImplementation(realLoad)
  for (const k of Object.keys(spies) as (keyof typeof spies)[]) if (k !== 'loadAccountsBoard') spies[k].mockResolvedValue(OK)
  spies.refreshAccount.mockResolvedValue({
    status: 'ok',
    fetchedAt: 0,
    data: { refresh: { tenant_id: 'u-owner', provider: 'anthropic', refreshed: true, reason: 'refreshed', expires_at: null }, account: {} },
  })
  spies.beginAccountSignIn.mockResolvedValue({
    status: 'ok',
    fetchedAt: 0,
    data: { authorize_url: 'https://sign-in.example.test/authorize', state: 'st-1', expires_in_seconds: 600 },
  })
  spies.mintSetupLink.mockResolvedValue({
    status: 'ok',
    fetchedAt: 0,
    data: { kind: 'pool-account', label: 'night', owner: 'owner@example.test', url: 'https://console.example.test/setup/link#one', path: '/setup/link', expires_at: null },
  })
})

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

async function openPlans(): Promise<HTMLElement> {
  render(<LlmProvidersScreen sub="plans" />)
  return screen.findByRole('region', { name: 'Plans' }, WAIT)
}

/** A plan's card, by its label. */
function card(label: string): HTMLElement {
  return screen.getByRole('article', { name: label })
}

/** An element's text with its whitespace folded. */
function text(el: Element | null | undefined): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/** A card's window: its figure, its reset time and the time until it. */
function win(c: HTMLElement, key: 'five_hour' | 'seven_day') {
  const w = c.querySelector(`[data-window="${key}"]`) as HTMLElement
  return { el: w, value: text(w.querySelector('.lpc-q-v')), at: text(w.querySelector('.lpc-q-time')), until: text(w.querySelector('.lpc-q-in')) }
}

/** The account list as served, with `caps` on one plan. */
function withCaps(label: string, caps: Partial<ServedAccount> & { pools?: string[] }): void {
  spies.loadAccountsBoard.mockImplementation(async () => {
    const r = (await realLoad()) as Result<AccountsBoard> & { status: 'ok' }
    const accounts = r.data.page.accounts.map((a) => (a.label === label ? { ...a, ...caps } : a))
    return { ...r, data: { ...r.data, page: { ...r.data.page, accounts } } }
  })
}

async function manage(label: string): Promise<HTMLElement> {
  fireEvent.click(within(card(label)).getByRole('button', { name: `Manage ${label}` }))
  return screen.findByRole('dialog')
}

/** `14:20`, or `Thu 09:00` on another day, in this test's own zone: the card's reset, worked out here. */
function expectedAt(at: number): string {
  const d = new Date(at)
  const n = new Date()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return d.toDateString() === n.toDateString() ? hm : `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`
}

/** The rules of the sheet whose selector names a card class. */
function cardRules(): { sel: string; body: string }[] {
  const flat = PLANS_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  return [...flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ sel: m[1]!.trim(), body: m[2]! })).filter((r) => /\.lpc-/.test(r.sel))
}

describe('Plans: the cards', () => {
  it('groups the plans by vendor, one card a plan', async () => {
    const plans = await openPlans()
    const groups = [...plans.querySelectorAll('section')]
    // MUTATION: draw one flat list and the vendor groups are gone.
    expect(groups.map((g) => g.getAttribute('aria-label'))).toEqual(['Anthropic', 'OpenAI'])
    expect(groups.map((g) => text(g.querySelector('h2')).replace(/\s*\d+$/, ''))).toEqual(['Anthropic', 'OpenAI'])
    expect(within(groups[1]!).getAllByRole('article').map((a) => a.getAttribute('aria-label'))).toEqual(['codex-plan'])
    // Voice it's API key is a key, not a plan: API Keys is its page.
    expect(screen.queryByRole('article', { name: 'openai-key' })).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it("leads each card with the 5-hour and 7-day quota LEFT, 100 minus the share used, each with a bar", async () => {
    await openPlans()
    const primary = card('primary')
    // primary has used 41% of its 5-hour and 63% of its 7-day window.
    // MUTATION: draw the share used and these read 41% and 63%.
    expect(win(primary, 'five_hour').value).toBe('59% left')
    expect(win(primary, 'seven_day').value).toBe('37% left')
    expect((win(primary, 'five_hour').el.querySelector('.lpc-qbar > b') as HTMLElement).style.width).toBe('59%')
    expect(text(card('codex-plan').querySelector('[data-window="five_hour"] .lpc-q-v'))).toBe('78% left')
    // The two figures come first on the card, before its state and holders.
    const order = [...primary.querySelectorAll('.lpc-quota, .lpc-facts')].map((e) => e.className)
    expect(order).toEqual(['lpc-quota', 'lpc-facts'])
  })

  it('draws each reset as its local time and the time until it, with the day when it is not today', async () => {
    const five = Date.now() + (2 * 60 + 10) * 60_000 + 30_000
    const seven = Date.now() + (3 * 24 + 5) * 3_600_000 + 30_000
    withCaps('primary', {
      windows: {
        five_hour: { utilization: 0.25, resets_at: new Date(five).toISOString(), reset: false },
        seven_day: { utilization: 0.5, resets_at: new Date(seven).toISOString(), reset: false },
      },
    })
    await openPlans()
    const w5 = win(card('primary'), 'five_hour')
    const w7 = win(card('primary'), 'seven_day')
    // MUTATION: drop the reset and these are empty.
    expect(w5.at).toBe(expectedAt(five))
    expect(w5.until).toBe('in 2h10m')
    expect(w7.at).toBe(expectedAt(seven))
    expect(w7.at).toMatch(/^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d\d:\d\d$/)
    expect(w7.until).toBe('in 3d5h')
    expect(w5.value).toBe('75% left')
  })

  it('draws a stale window as projected, an unmeasured one as unknown, and an unserved reset as unknown', async () => {
    withCaps('primary', { stale: true })
    await openPlans()
    const stale = win(card('primary'), 'five_hour')
    // MUTATION: drop the mark and a stale figure reads as current.
    expect(stale.value).toBe('~59% left')
    expect(stale.el.classList.contains('is-projected')).toBe(true)
    cleanup()
    withCaps('primary', { observed_at: null })
    await openPlans()
    for (const k of ['five_hour', 'seven_day'] as const) {
      const w = win(card('primary'), k)
      // MUTATION: read a missing reading as 0% used and these say 100% left.
      expect(w.value).toBe('unknown')
      expect(w.at).toBe('unknown')
      expect(w.el.querySelector('.lpc-qbar > b')).toBeNull()
    }
    cleanup()
    withCaps('primary', {
      windows: { five_hour: { utilization: 0.4, resets_at: new Date(Date.now() - 3_600_000).toISOString(), reset: true } },
    })
    await openPlans()
    // A window past its reset has refilled: projected, never current; no next reset was served.
    expect(win(card('primary'), 'five_hour').value).toBe('~100% left')
    expect(win(card('primary'), 'five_hour').at).toBe('unknown')
    expect(win(card('primary'), 'seven_day').value).toBe('unknown')
  })

  it('then draws the state, live holders over max agents, Voice it and the lends as one line', async () => {
    withCaps('full-six', { pools: ['workspace', 'voice-it'], lends: { smoke: { max_agents: 2, monthly_spend_cap_usd: null, cap_set: true } }, lend_holders: { smoke: 3 } })
    await openPlans()
    expect(text(card('primary').querySelector('.llp-st'))).toBe('Available')
    expect(text(card('weekly-burn').querySelector('.llp-st'))).toBe('Paused')
    expect(text(card('retired-laptop').querySelector('.llp-st'))).toBe('Sign-in needed')
    expect(text(card('primary').querySelector('.lpc-agents'))).toBe('2/6 agents')
    expect(text(card('full-six').querySelector('.lpc-agents'))).toBe('6/6 agents')
    expect(text(card('full-six').querySelector('.lpc-voice'))).toBe('Voice it')
    expect(card('primary').querySelector('.lpc-voice')).toBeNull()
    const lends = card('full-six').querySelector('.lpc-lends')!
    // MUTATION: drop the lends line and the card says nothing of who borrows it.
    expect(text(lends)).toBe('Lent smoke 3/2')
    expect(lends.querySelector('.lpc-num.is-bad')).not.toBeNull()
    expect(card('primary').querySelector('.lpc-lends')).toBeNull()
  })

  it('marks a lend carried over from before lend caps, and draws no figure off a broker that serves no lends', async () => {
    withCaps('full-six', { lends: { smoke: { max_agents: 6, monthly_spend_cap_usd: null, cap_set: false } }, lend_holders: { smoke: 1 } })
    await openPlans()
    expect(text(card('full-six').querySelector('.lpc-lends'))).toBe('Lent smoke 1 cap not set')
    cleanup()
    spies.loadAccountsBoard.mockImplementation(realLoad)
    await openPlans()
    expect(text(card('full-six').querySelector('.lpc-lends'))).toBe('Lent smoke')
  })

  it('gives each card ONE Manage control, Resume only while paused, and no per-lend button', async () => {
    await openPlans()
    const cards = screen.getAllByRole('article')
    expect(cards.length).toBeGreaterThan(3)
    for (const c of cards) {
      const names = within(c).queryAllByRole('button').map((b) => b.textContent)
      const label = c.getAttribute('aria-label')!
      // A plan lent to this workspace is its owner's to manage: its card carries no control.
      const owned = c.getAttribute('data-plan')!.startsWith('u-owner:')
      // MUTATION: put a button on each lend line and full-six carries three.
      expect(names, label).toEqual(!owned ? [] : label === 'weekly-burn' ? ['Resume', 'Manage'] : ['Manage'])
    }
    expect(cards.some((c) => !c.getAttribute('data-plan')!.startsWith('u-owner:'))).toBe(true)
    expect(screen.queryByRole('button', { name: /Edit lend/ })).toBeNull()
  })

  it('opens Manage from anywhere on the card', async () => {
    await openPlans()
    fireEvent.click(card('primary').querySelector('.lpc-quota')!)
    expect(await screen.findByRole('dialog', { name: 'primary' })).toBeTruthy()
  })

  it('draws no text on a card below the body step: the name at the lead step, the quota at the figure step', async () => {
    const rules = cardRules()
    expect(rules.length).toBeGreaterThan(20)
    for (const r of rules) {
      for (const m of r.body.matchAll(/font(?:-size)?\s*:\s*([^;]+)/g)) {
        // MUTATION: write a card label at --t-micro (or a raw 12px) and this names it.
        expect(m[1], r.sel).not.toMatch(/--t-(micro|meta)|\d+(\.\d+)?px/)
      }
    }
    const size = (sel: string) => rules.find((r) => r.sel === sel)?.body.match(/font-size:\s*([^;]+)/)?.[1]?.trim()
    expect(size('.lpc-card')).toBe('var(--t-body)')
    expect(size('.lpc-title')).toBe('var(--t-lead)')
    expect(size('.lpc-q-n')).toBe('var(--t-figure)')
    expect(size('.lpc-q-l')).toBe('var(--t-body)')
    expect(size('.lpc-q-at')).toBe('var(--t-body)')
    // The console's small button is --t-micro: the card's are the regular size, lifted to the body step.
    expect(size('.lpc-foot .c-btn')).toBe('var(--t-body)')
    await openPlans()
    for (const c of screen.getAllByRole('article')) {
      expect(c.querySelector('.c-btn.is-sm, .ctl-mark, .llp-dim'), c.getAttribute('aria-label')!).toBeNull()
    }
  })

  it('fits a phone at 390: one column, nothing wider than the viewport', () => {
    const rules = cardRules()
    const body = (sel: string) => rules.find((r) => r.sel === sel)?.body ?? ''
    // MUTATION: a grid track of a fixed 380px runs past a 390 viewport's padding.
    expect(body('.lpc-grid')).toMatch(/minmax\(min\(100%, \d+px\), 1fr\)/)
    expect(body('.lpc-card')).toMatch(/min-width: 0/)
    expect(body('.lpc-card')).toMatch(/max-width: 100%/)
    const phone = PLANS_CSS.slice(PLANS_CSS.indexOf('@media (max-width: 720px)'))
    expect(phone.slice(0, phone.indexOf('\n}'))).toMatch(/\.lpc-grid\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);?\s*\}/)
    for (const r of rules) {
      for (const m of r.body.matchAll(/(?:^|;)\s*(?:min-)?width\s*:\s*(\d+)px/g)) expect(Number(m[1]), r.sel).toBeLessThanOrEqual(120)
    }
  })

  it('draws "No plans", Add plan and the read mark when there are none', async () => {
    spies.loadAccountsBoard.mockImplementation(async () => {
      const r = (await realLoad()) as Result<AccountsBoard> & { status: 'ok' }
      const accounts = r.data.page.accounts.filter((a) => a.provider === 'openai-api')
      return { ...r, data: { ...r.data, page: { ...r.data.page, accounts } } }
    })
    render(<LlmProvidersScreen sub="plans" />)
    const empty = await screen.findByRole('region', { name: 'No plans' }, WAIT)
    expect(within(empty).getByRole('heading', { name: /No plans/ })).toBeTruthy()
    expect(within(empty).getByRole('button', { name: 'Add plan' })).toBeTruthy()
    expect(empty.querySelector('.ctl-mark.is-zero')?.textContent).toBe('read · 0')
    expect(screen.queryByRole('region', { name: 'Plans' })).toBeNull()
  })

  it('draws one h1', async () => {
    await openPlans()
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['LLM Providers'])
  })

  it('opens the Manage sheet of the plan ?account= names', async () => {
    window.history.replaceState(null, '', '/capacity/llm-providers/plans?account=u-owner%3Afull-six')
    await openPlans()
    const sheet = await screen.findByRole('dialog', { name: 'full-six' })
    expect(within(sheet).getByRole('button', { name: 'Remove plan' })).toBeTruthy()
    expect(document.querySelector('.llp-scrim')).not.toBeNull()
  })

  it('draws no scrim when ?account= names a plan not in the list', async () => {
    window.history.replaceState(null, '', '/capacity/llm-providers/plans?account=u-owner%3Agone')
    await openPlans()
    // MUTATION: draw the scrim for any linked id and the page is dimmed with nothing over it.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.querySelector('.llp-scrim')).toBeNull()
    expect(document.querySelector('.llp-page.has-sheet')).toBeNull()
  })
})

describe('Plans: every control calls its route once', () => {
  it('Pause asks for a reason, then calls setAccountState', async () => {
    await openPlans()
    const s = await manage('primary')
    fireEvent.click(within(s).getByRole('button', { name: 'Pause' }))
    fireEvent.change(within(s).getByRole('textbox', { name: 'Reason' }), { target: { value: 'release run' } })
    fireEvent.click(within(s).getByRole('button', { name: 'Pause plan' }))
    await waitFor(() => expect(api.setAccountState).toHaveBeenCalledTimes(1))
    expect(api.setAccountState).toHaveBeenCalledWith('u-owner:primary', 'PAUSED', 'release run')
  })

  it('Drain asks for a reason, then calls setAccountState', async () => {
    await openPlans()
    const s = await manage('primary')
    fireEvent.click(within(s).getByRole('button', { name: 'Drain' }))
    fireEvent.change(within(s).getByRole('textbox', { name: 'Reason' }), { target: { value: 'moving off' } })
    fireEvent.click(within(s).getByRole('button', { name: 'Drain plan' }))
    await waitFor(() => expect(api.setAccountState).toHaveBeenCalledTimes(1))
    expect(api.setAccountState).toHaveBeenCalledWith('u-owner:primary', 'DRAINING', 'moving off')
  })

  it('Resume on a paused row calls setAccountState with AVAILABLE', async () => {
    await openPlans()
    fireEvent.click(within(card('weekly-burn')).getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.setAccountState).toHaveBeenCalledTimes(1))
    expect(api.setAccountState).toHaveBeenCalledWith('u-owner:weekly-burn', 'AVAILABLE', '')
  })

  it('Refresh calls refreshAccount', async () => {
    await openPlans()
    const s = await manage('primary')
    fireEvent.click(within(s).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.refreshAccount).toHaveBeenCalledTimes(1))
    expect(api.refreshAccount).toHaveBeenCalledWith('u-owner:primary')
  })

  it('Save sends each changed field to its own route, once', async () => {
    await openPlans()
    const s = await manage('primary')
    fireEvent.change(within(s).getByRole('spinbutton', { name: 'Max agents' }), { target: { value: '4' } })
    fireEvent.click(within(s).getByRole('button', { name: 'opus' }))
    fireEvent.change(within(s).getByRole('textbox', { name: 'Display name' }), { target: { value: 'Team main' } })
    fireEvent.click(within(s).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.setAccountName).toHaveBeenCalledTimes(1))
    expect(api.setAccountMaxAgents).toHaveBeenCalledTimes(1)
    expect(api.setAccountMaxAgents).toHaveBeenCalledWith('u-owner:primary', 4)
    expect(api.setAccountModelClasses).toHaveBeenCalledTimes(1)
    expect(api.setAccountModelClasses).toHaveBeenCalledWith('u-owner:primary', ['fable', 'sonnet', 'haiku'])
    expect(api.setAccountName).toHaveBeenCalledWith('u-owner:primary', 'Team main')
    expect(spies.setAccountPools).not.toHaveBeenCalled()
  })

  it('Share with Voice it asks for the label, then calls the Voice it pool route', async () => {
    await openPlans()
    const s = await manage('primary')
    fireEvent.click(within(s).getByRole('switch', { name: 'Share with Voice it' }))
    const save = within(s).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(within(s).getByRole('textbox', { name: 'Type primary to confirm' }), { target: { value: 'primary' } })
    fireEvent.click(save)
    await waitFor(() => expect(spies.setAccountPools).toHaveBeenCalledTimes(1))
    expect(spies.setAccountPools).toHaveBeenCalledWith('u-owner:primary', ['workspace', 'voice-it'], 'primary')
  })

  it('Lend adds the workspace with its agents cap to the whole set', async () => {
    await openPlans()
    const s = await manage('full-six')
    fireEvent.click(within(s).getByRole('button', { name: 'Lend' }))
    const lend = await screen.findByRole('dialog', { name: 'Lend full-six' })
    fireEvent.change(within(lend).getByRole('textbox', { name: 'Workspace' }), { target: { value: 'harbor' } })
    const agents = within(lend).getByRole('spinbutton', { name: 'Agents at once' })
    for (const bad of ['0', '7', '1.5']) {
      fireEvent.change(agents, { target: { value: bad } })
      expect((within(lend).getByRole('button', { name: 'Lend' }) as HTMLButtonElement).disabled, `${bad} armed Lend`).toBe(true)
    }
    fireEvent.change(agents, { target: { value: '3' } })
    fireEvent.click(within(lend).getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(api.setAccountLending).toHaveBeenCalledTimes(1))
    // smoke was lent before lend caps: it keeps the plan's own max agents, the limit it already had.
    expect(api.setAccountLending).toHaveBeenCalledWith('u-owner:full-six', { smoke: 6, harbor: 3 })
  })

  it("Edit lend opens on that lend and saves its new cap", async () => {
    withCaps('full-six', { lends: { smoke: { max_agents: 2, monthly_spend_cap_usd: null, cap_set: true } }, lend_holders: { smoke: 1 } })
    await openPlans()
    const s = await manage('full-six')
    fireEvent.click(within(s).getByRole('button', { name: 'Edit lend to smoke' }))
    const lend = await screen.findByRole('dialog', { name: 'Lend full-six' })
    expect((within(lend).getByRole('textbox', { name: 'Workspace' }) as HTMLInputElement).value).toBe('smoke')
    const agents = within(lend).getByRole('spinbutton', { name: 'Agents at once' }) as HTMLInputElement
    expect(agents.value).toBe('2')
    fireEvent.change(agents, { target: { value: '4' } })
    fireEvent.click(within(lend).getByRole('button', { name: 'Save cap' }))
    await waitFor(() => expect(api.setAccountLending).toHaveBeenCalledTimes(1))
    expect(api.setAccountLending).toHaveBeenCalledWith('u-owner:full-six', { smoke: 4 })
  })

  it('End takes the workspace off the list', async () => {
    await openPlans()
    const s = await manage('full-six')
    fireEvent.click(within(s).getByRole('button', { name: 'Edit lend to smoke' }))
    const lend = await screen.findByRole('dialog', { name: 'Lend full-six' })
    fireEvent.click(within(lend).getByRole('button', { name: 'End lend to smoke' }))
    await waitFor(() => expect(api.setAccountLending).toHaveBeenCalledTimes(1))
    expect(api.setAccountLending).toHaveBeenCalledWith('u-owner:full-six', [])
  })

  it('Remove plan asks for the label, then calls removeAccount', async () => {
    await openPlans()
    const s = await manage('overflow')
    fireEvent.click(within(s).getByRole('button', { name: 'Remove plan' }))
    const del = await screen.findByRole('dialog', { name: 'Remove plan overflow' })
    const go = within(del).getByRole('button', { name: 'Remove plan' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.change(within(del).getByRole('textbox', { name: 'Type overflow to remove' }), { target: { value: 'overflow' } })
    fireEvent.click(go)
    await waitFor(() => expect(api.removeAccount).toHaveBeenCalledTimes(1))
    expect(api.removeAccount).toHaveBeenCalledWith('u-owner:overflow')
  })

  it('Add plan signs in here: begin, then the pasted code', async () => {
    await openPlans()
    fireEvent.click(screen.getByRole('button', { name: 'Add plan' }))
    const add = await screen.findByRole('dialog', { name: 'Add plan' })
    fireEvent.change(within(add).getByRole('textbox', { name: 'Label' }), { target: { value: 'night' } })
    fireEvent.click(within(add).getByRole('button', { name: 'Start sign-in' }))
    await waitFor(() => expect(api.beginAccountSignIn).toHaveBeenCalledTimes(1))
    expect(api.beginAccountSignIn).toHaveBeenCalledWith({ label: 'night', lend_to: [] })
    fireEvent.change(await within(add).findByRole('textbox', { name: 'Sign-in code' }), { target: { value: 'code-1' } })
    fireEvent.click(within(add).getByRole('button', { name: 'Add plan' }))
    await waitFor(() => expect(api.finishAccountSignIn).toHaveBeenCalledTimes(1))
    expect(api.finishAccountSignIn).toHaveBeenCalledWith({ state: 'st-1', code: 'code-1' })
  })

  it("Add plan by the plan's owner mints a one-time link", async () => {
    await openPlans()
    fireEvent.click(screen.getByRole('button', { name: 'Add plan' }))
    const add = await screen.findByRole('dialog', { name: 'Add plan' })
    fireEvent.click(within(add).getByRole('radio', { name: "The plan's owner" }))
    fireEvent.change(within(add).getByRole('textbox', { name: 'Label' }), { target: { value: 'night' } })
    fireEvent.change(within(add).getByRole('textbox', { name: "Owner's email" }), { target: { value: 'owner@example.test' } })
    fireEvent.click(within(add).getByRole('button', { name: 'Create link' }))
    await waitFor(() => expect(api.mintSetupLink).toHaveBeenCalledTimes(1))
    expect(api.mintSetupLink).toHaveBeenCalledWith({ kind: 'pool-account', label: 'night', owner: 'owner@example.test' })
    expect(await within(add).findByDisplayValue('https://console.example.test/setup/link#one')).toBeTruthy()
  })
})

describe('Plans: the per-workspace caps', () => {
  it('adds and removes a cap, and Save sends the whole map once', async () => {
    withCaps('full-six', { per_workspace_max: { smoke: 2 } })
    await openPlans()
    const s = await manage('full-six')
    const list = within(s).getByRole('table', { name: 'Per workspace' })
    expect((within(list).getByRole('spinbutton', { name: 'smoke max agents' }) as HTMLInputElement).value).toBe('2')
    fireEvent.click(within(s).getByRole('button', { name: 'Remove cap for smoke' }))
    fireEvent.click(within(s).getByRole('button', { name: 'Add workspace cap' }))
    fireEvent.change(within(s).getByRole('spinbutton', { name: 'u-owner max agents' }), { target: { value: '3' } })
    // Only the owner and its borrowers may carry a cap: with both listed, there is nothing left to add.
    fireEvent.click(within(s).getByRole('button', { name: 'Add workspace cap' }))
    expect(within(s).queryByRole('button', { name: 'Add workspace cap' })).toBeNull()
    fireEvent.click(within(s).getByRole('button', { name: 'Remove cap for smoke' }))
    fireEvent.click(within(s).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.setAccountPerWorkspaceMax).toHaveBeenCalledTimes(1))
    expect(api.setAccountPerWorkspaceMax).toHaveBeenCalledWith('u-owner:full-six', { 'u-owner': 3 })
    expect(api.setAccountMaxAgents).not.toHaveBeenCalled()
  })

  it('a failed save says why and leaves the row and the list as they were', async () => {
    withCaps('full-six', { per_workspace_max: { smoke: 2 } })
    spies.setAccountPerWorkspaceMax.mockResolvedValue(FAIL)
    await openPlans()
    const reads = spies.loadAccountsBoard.mock.calls.length
    const s = await manage('full-six')
    fireEvent.change(within(s).getByRole('spinbutton', { name: 'smoke max agents' }), { target: { value: '5' } })
    await act(async () => {
      fireEvent.click(within(s).getByRole('button', { name: 'Save' }))
    })
    expect((await within(s).findByRole('alert')).textContent).toContain('max_agents is above the limit')
    expect(api.setAccountPerWorkspaceMax).toHaveBeenCalledWith('u-owner:full-six', { smoke: 5 })
    expect(spies.loadAccountsBoard.mock.calls.length).toBe(reads)
    expect(text(card('full-six').querySelector('.lpc-agents'))).toBe('6/6 agents')
    expect((within(s).getByRole('spinbutton', { name: 'smoke max agents' }) as HTMLInputElement).value).toBe('5')
  })
})

describe('Plans: a control that fails', () => {
  it('says why in its sheet and leaves the row as it was', async () => {
    spies.setAccountMaxAgents.mockResolvedValue(FAIL)
    await openPlans()
    const reads = spies.loadAccountsBoard.mock.calls.length
    const s = await manage('primary')
    fireEvent.change(within(s).getByRole('spinbutton', { name: 'Max agents' }), { target: { value: '40' } })
    await act(async () => {
      fireEvent.click(within(s).getByRole('button', { name: 'Save' }))
    })
    // MUTATION: swallow the failure and the sheet says nothing.
    expect((await within(s).findByRole('alert')).textContent).toContain('max_agents is above the limit')
    expect(api.setAccountMaxAgents).toHaveBeenCalledTimes(1)
    expect(text(card('primary').querySelector('.lpc-agents'))).toBe('2/6 agents')
    expect(spies.loadAccountsBoard.mock.calls.length).toBe(reads)
    expect((within(s).getByRole('spinbutton', { name: 'Max agents' }) as HTMLInputElement).value).toBe('40')
  })
})
