// CAPACITY › LLM PROVIDERS › PLANS (docs/design/llm-providers.md §6, the
// owner's pick of 10 November 2026, direction "Tables and a side sheet").
//
// WHAT EACH CASE HOLDS:
//   * the table groups the plans by vendor and draws each column -- state,
//     the five-hour and seven-day windows, live holders over max agents, the
//     Voice it mark and the lent-to count -- off the account list api.ts's
//     fixture path serves (`loadAccountsBoard` is the real one here);
//   * a lend is a line under its plan's row;
//   * every control in the Add plan, Manage and Lend sheets calls its api.ts
//     function once, with the right arguments;
//   * a control that fails says why in its sheet and leaves the row as it was;
//   * no plans is "No plans", the add control beside it and the read mark;
//   * `?account=` opens that plan's Manage sheet, and one naming a plan not
//     in the list draws neither a sheet nor its scrim;
//   * a lend line draws the borrower's live holders over its lend's cap; the
//     Lend sheet saves each lend with its agents cap; Manage's Per workspace
//     list adds and removes a cap and saves the whole map;
//   * at phone width a window's reset time folds into its tooltip;
//   * the page draws one h1.
//
// Placeholders only; nothing is token-shaped.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, PlanCaps } from '../api'
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

async function openPlans(): Promise<HTMLTableElement> {
  render(<LlmProvidersScreen sub="plans" />)
  return (await screen.findByRole('table', { name: 'Plans' }, WAIT)) as HTMLTableElement
}

/** A plan's row, by its label. */
function row(label: string): HTMLTableRowElement {
  const cell = screen.getAllByText(label, { selector: '.llp-label' })[0]!
  return cell.closest('tr') as HTMLTableRowElement
}

function cell(r: HTMLElement, col: string): string {
  return (r.querySelector(`td[data-l="${col}"]`)?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/** The account list as served, with `caps` on one plan. */
function withCaps(label: string, caps: PlanCaps): void {
  spies.loadAccountsBoard.mockImplementation(async () => {
    const r = (await realLoad()) as Result<AccountsBoard> & { status: 'ok' }
    const accounts = r.data.page.accounts.map((a) => (a.label === label ? { ...a, ...caps } : a))
    return { ...r, data: { ...r.data, page: { ...r.data.page, accounts } } }
  })
}

async function manage(label: string): Promise<HTMLElement> {
  fireEvent.click(within(row(label)).getByRole('button', { name: 'Manage' }))
  return screen.findByRole('dialog')
}

describe('Plans: the table', () => {
  it('groups the plans by vendor and draws each column from the account list', async () => {
    const table = await openPlans()
    const groups = [...table.querySelectorAll('th[scope="colgroup"]')]
    // MUTATION: draw one flat list and the vendor heads are gone.
    expect(groups.map((g) => g.textContent?.replace(/\d+/, '').trim())).toEqual(['Anthropic', 'OpenAI'])
    const openai = groups[1]!.closest('tbody')!
    expect(within(openai).getAllByText(/./, { selector: '.llp-label' }).map((e) => e.textContent)).toEqual(['codex-plan'])
    // Voice it's API key is a key, not a plan: API Keys is its page.
    expect(screen.queryByText('openai-key', { selector: '.llp-label' })).toBeNull()

    const primary = row('primary')
    expect(cell(primary, 'State')).toBe('Available')
    expect(cell(primary, 'Agents')).toBe('2 / 6')
    expect(cell(primary, '5-hour')).toMatch(/^41% · /)
    expect(cell(primary, '7-day')).toMatch(/^63% · /)
    expect(cell(primary, 'Voice it')).toBe('—')
    expect(cell(primary, 'Lent')).toBe('—')
    expect(cell(row('weekly-burn'), 'State')).toBe('Paused')
    expect(cell(row('retired-laptop'), 'State')).toBe('Sign-in needed')
    expect(cell(row('full-six'), 'Agents')).toBe('6 / 6')
    expect(cell(row('full-six'), 'Lent')).toBe('1')
    expect(cell(row('codex-plan'), 'Agents')).toBe('1 / 6')
  })

  it('marks a plan shared with Voice it', async () => {
    spies.loadAccountsBoard.mockImplementation(async () => {
      const r = (await realLoad()) as Result<AccountsBoard> & { status: 'ok' }
      const accounts = r.data.page.accounts.map((a) => (a.label === 'primary' ? { ...a, pools: ['workspace', 'voice-it'] } : a))
      return { ...r, data: { ...r.data, page: { ...r.data.page, accounts } } }
    })
    await openPlans()
    expect(cell(row('primary'), 'Voice it')).toBe('Voice it')
    expect(cell(row('overflow'), 'Voice it')).toBe('—')
  })

  it('draws a lend as a line under its plan', async () => {
    await openPlans()
    const next = row('full-six').nextElementSibling as HTMLElement
    // MUTATION: drop the lend lines and the next row is another plan.
    expect(next.classList.contains('llp-lend')).toBe(true)
    expect(next.textContent).toContain('smoke')
    expect(within(next).getByRole('button', { name: 'Edit lend' })).toBeTruthy()
  })

  it("draws a lend line's live holders over its lend's cap", async () => {
    withCaps('full-six', { lends: { smoke: { max_agents: 2, monthly_spend_cap_usd: null, cap_set: true } }, lend_holders: { smoke: 3 } })
    await openPlans()
    const line = row('full-six').nextElementSibling as HTMLElement
    // MUTATION: drop the figure and the line is the workspace's name alone.
    expect(cell(line, 'Agents')).toBe('3 / 2')
    expect(line.querySelector('.llp-over.is-full')).not.toBeNull()
  })

  it('marks a lend carried over from before lend caps, and draws no figure off a broker that serves no lends', async () => {
    withCaps('full-six', { lends: { smoke: { max_agents: 6, monthly_spend_cap_usd: null, cap_set: false } }, lend_holders: { smoke: 1 } })
    await openPlans()
    expect(cell(row('full-six').nextElementSibling as HTMLElement, 'Agents')).toBe('1 cap not set')
    cleanup()
    spies.loadAccountsBoard.mockImplementation(realLoad)
    await openPlans()
    expect(cell(row('full-six').nextElementSibling as HTMLElement, 'Agents')).toBe('')
  })

  it("folds a window's reset time into its tooltip at phone width", async () => {
    await openPlans()
    const seven = row('primary').querySelector('td[data-l="7-day"]')!
    expect(seven.querySelector('.llp-win-at')?.textContent).toMatch(/^ · /)
    expect(seven.querySelector('.llp-over')?.getAttribute('title')).toMatch(/resets /)
    // MUTATION: drop the phone rule and the date runs into the Voice it cell at 390.
    const phone = PLANS_CSS.slice(PLANS_CSS.indexOf('@media (max-width: 720px)'))
    expect(phone.slice(0, phone.indexOf('\n}'))).toMatch(/\.llp-win-at\s*\{\s*display:\s*none;?\s*\}/)
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
    expect(screen.queryByRole('table', { name: 'Plans' })).toBeNull()
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
    fireEvent.click(within(row('weekly-burn')).getByRole('button', { name: 'Resume' }))
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
    fireEvent.click(within(row('full-six').nextElementSibling as HTMLElement).getByRole('button', { name: 'Edit lend' }))
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
    fireEvent.click(within(row('full-six').nextElementSibling as HTMLElement).getByRole('button', { name: 'Edit lend' }))
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
    expect(cell(row('full-six'), 'Agents')).toBe('6 / 6')
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
    expect(cell(row('primary'), 'Agents')).toBe('2 / 6')
    expect(spies.loadAccountsBoard.mock.calls.length).toBe(reads)
    expect((within(s).getByRole('spinbutton', { name: 'Max agents' }) as HTMLInputElement).value).toBe('40')
  })
})
