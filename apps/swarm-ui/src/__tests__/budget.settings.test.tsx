/**
 * THE PER-AGENT BUDGET'S SETTINGS (BUDGET-UI-SETTINGS; part of #2459,
 * docs/design/agent-budget.md §2, §4).
 *
 *   * Config › Workspace settings › Per-agent budget: Off (the default) or a
 *     limit in dollars or tokens, saved through
 *     `PUT /v1/workspaces/{tenant_id}/budget`, the platform ceiling drawn as
 *     the most it can be, and a default above it refused 422
 *     `budget_above_ceiling`, said in words. A member reads it with no control.
 *   * Admin › Pool limits › Agent budget ceiling: off or a limit per unit,
 *     saved as `agent_budget_ceiling` through `PUT /v1/admin/settings`.
 *
 * Both say what a budget does: told to finish and publish at 80%, stopped at
 * its next turn boundary at 100%, its work kept, "stopped at its budget".
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Calls = { method: string; url: string; body: unknown }[]

const BOUNDS = { usd: { min_exclusive: 0, max: 100000 }, tokens: { min: 1, max: 10000000000 } }

const budgetRead = (over: Json = {}): Json => ({
  tenant_id: 'eng',
  budget: null,
  ceiling: { usd: 20 },
  bounds: BOUNDS,
  set_at: null,
  set_by: null,
  can_edit: true,
  ...over,
})

const profile: Json = {
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: null, is_admin: false },
  workspaces: [{ tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: 'admin', source: 'override' }, is_acting: true }],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
}

/** The workspace page's reads, with `/v1/budget` answering `read` and its PUT answering `put`. */
function workspaceApi(read: Json, put?: (body: Json) => { status: number; body: unknown }): Calls {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile }
    if (m === 'GET' && url === '/v1/budget') return { status: 200, body: read }
    if (m === 'PUT' && url === '/v1/workspaces/eng/budget' && put !== undefined) return put(body as Json)
    return null
  })
}

/** What the API answers to a PUT it accepts: the view with the default saved. */
const accepted = (body: Json) => ({ status: 200, body: budgetRead({ budget: body.budget, set_by: 'ana.b@example.com', set_at: new Date().toISOString() }) })

async function workspacePage() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspaceSettingsScreen } = await import('../WorkspaceSettings')
  render(<WorkspaceSettingsScreen part="settings" />)
  const heading = await screen.findByRole('heading', { name: 'Per-agent budget' })
  const card = heading.closest('section') as HTMLElement
  await waitFor(() => expect(within(card).getByTestId('ws-budget')).toBeTruthy())
  return card
}

const puts = (calls: Calls, url: string) => calls.filter((c) => c.method === 'PUT' && c.url === url)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Config › Workspace settings › Per-agent budget', () => {
  it('is Off by default, says what a budget does and draws the platform ceiling', async () => {
    workspaceApi(budgetRead())
    const card = await workspacePage()
    // MUTATION: draw a limit, or no Now row, for a workspace that set none -> fails.
    expect(visible(within(card).getByTestId('ws-budget-now'))).toBe('Off')
    // MUTATION: drop the Off choice or check another by default -> fails.
    const off = within(card).getByRole('radio', { name: /^Off/ }) as HTMLInputElement
    expect(off.checked).toBe(true)
    expect(visible(off.closest('label'))).toContain('(the default)')
    // MUTATION: draw the amount field while the budget is off -> fails.
    expect(within(card).queryByRole('textbox')).toBeNull()
    // MUTATION: drop the copy that says what 80% and 100% do -> fails.
    expect(visible(card)).toContain('At 80% of its budget an agent is told to finish and publish')
    expect(visible(card)).toContain('it stops at its next turn boundary, keeps its work and ends “stopped at its budget”')
    // MUTATION: leave the ceiling out -> fails.
    expect(visible(card)).toContain('$20.00, the most a budget here can be')
    // Nothing changed, so nothing to save.
    expect((within(card).getByRole('button', { name: 'Save budget' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves a dollar limit through the PUT and draws what the API stored', async () => {
    const calls = workspaceApi(budgetRead(), accepted)
    const card = await workspacePage()
    fireEvent.click(within(card).getByRole('radio', { name: /^Dollars/ }))
    const amount = within(card).getByRole('textbox', { name: 'Dollars per agent' })
    // MUTATION: drop the ceiling from the amount's hint -> fails.
    expect(visible(card)).toContain('At most $20.00, the platform ceiling.')
    fireEvent.change(amount, { target: { value: '5' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save budget' }))
    // MUTATION: send the unit under another name, the amount as a string, or PUT another route -> fails.
    await waitFor(() => expect(puts(calls, '/v1/workspaces/eng/budget')).toHaveLength(1))
    expect(puts(calls, '/v1/workspaces/eng/budget')[0]!.body).toEqual({ budget: { unit: 'usd', amount: 5 } })
    // MUTATION: draw the form's figure rather than the response's -> the "Now" row and the status disagree, fails.
    expect(await within(card).findByRole('status')).toBeTruthy()
    expect(visible(within(card).getByRole('status'))).toContain('Saved: $5.00 per agent.')
    expect(visible(within(card).getByTestId('ws-budget-now'))).toBe('$5.00 per agent')
  })

  it('saves a token limit, with the commas a person types taken out', async () => {
    const calls = workspaceApi(budgetRead({ ceiling: {} }), accepted)
    const card = await workspacePage()
    fireEvent.click(within(card).getByRole('radio', { name: /^Tokens/ }))
    // MUTATION: claim a ceiling in a unit it does not name -> fails.
    expect(visible(card)).toContain('No platform ceiling in tokens.')
    fireEvent.change(within(card).getByRole('textbox', { name: 'Tokens per agent' }), { target: { value: '15,000,000' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save budget' }))
    await waitFor(() => expect(puts(calls, '/v1/workspaces/eng/budget')).toHaveLength(1))
    // MUTATION: parse "15,000,000" as 15 or send it as typed -> fails.
    expect(puts(calls, '/v1/workspaces/eng/budget')[0]!.body).toEqual({ budget: { unit: 'tokens', amount: 15000000 } })
    expect(await within(card).findByText('15,000,000 tokens per agent')).toBeTruthy()
  })

  it('turns a set budget off with `budget: null`', async () => {
    const calls = workspaceApi(budgetRead({ budget: { unit: 'usd', amount: 5 }, set_by: 'kim@example.com', set_at: new Date().toISOString() }), accepted)
    const card = await workspacePage()
    // MUTATION: open the form at Off whatever was stored -> fails.
    expect((within(card).getByRole('radio', { name: /^Dollars/ }) as HTMLInputElement).checked).toBe(true)
    expect((within(card).getByRole('textbox', { name: 'Dollars per agent' }) as HTMLInputElement).value).toBe('5')
    expect(visible(card)).toContain('Set by kim@example.com')
    fireEvent.click(within(card).getByRole('radio', { name: /^Off/ }))
    fireEvent.click(within(card).getByRole('button', { name: 'Save budget' }))
    await waitFor(() => expect(puts(calls, '/v1/workspaces/eng/budget')).toHaveLength(1))
    // MUTATION: send `{}` or an amount of 0 for Off -> fails.
    expect(puts(calls, '/v1/workspaces/eng/budget')[0]!.body).toEqual({ budget: null })
    expect(visible(await within(card).findByRole('status'))).toContain('Saved: the budget is off.')
  })

  it('says a default above the platform ceiling is refused, in words, and keeps what was stored', async () => {
    const refusal = {
      code: 'budget_above_ceiling',
      message: 'a workspace budget of $50.00 is above the platform ceiling of $20.00. Nothing was changed.',
      detail: { budget: { usd: 50 }, ceiling: { usd: 20 } },
    }
    const calls = workspaceApi(budgetRead(), () => ({ status: 422, body: refusal }))
    const card = await workspacePage()
    fireEvent.click(within(card).getByRole('radio', { name: /^Dollars/ }))
    fireEvent.change(within(card).getByRole('textbox', { name: 'Dollars per agent' }), { target: { value: '50' } })
    // MUTATION: say nothing before the save about a figure over the ceiling -> fails.
    expect(visible(card)).toContain('Above the platform ceiling of $20.00: saving it will be refused.')
    // The API is the judge: the save is not blocked here.
    fireEvent.click(within(card).getByRole('button', { name: 'Save budget' }))
    await waitFor(() => expect(puts(calls, '/v1/workspaces/eng/budget')).toHaveLength(1))
    const alert = await within(card).findByRole('alert')
    // MUTATION: draw the bare code, or a generic failure, for the refusal -> fails.
    expect(visible(alert)).toContain('The budget was not saved')
    expect(visible(alert)).toContain('$50.00 is above the platform ceiling of $20.00, so nothing was changed. Save $20.00 or less')
    // MUTATION: draw the refused figure as saved -> fails.
    expect(visible(within(card).getByTestId('ws-budget-now'))).toBe('Off')
    expect(within(card).queryByRole('status')).toBeNull()
  })

  it('lets a member read the budget and the ceiling, with no control to change them', async () => {
    workspaceApi(budgetRead({ budget: { unit: 'tokens', amount: 15000000 }, can_edit: false }))
    const card = await workspacePage()
    // MUTATION: draw the form for a caller the API says may not edit -> fails.
    expect(within(card).queryByRole('radio')).toBeNull()
    expect(within(card).queryByRole('textbox')).toBeNull()
    expect(within(card).queryByRole('button', { name: 'Save budget' })).toBeNull()
    // MUTATION: hide the figure from a member -> fails.
    expect(visible(within(card).getByTestId('ws-budget-now'))).toBe('15,000,000 tokens per agent')
    expect(visible(card)).toContain('$20.00, the most a budget here can be')
    expect(visible(card)).toContain('Only the workspace’s owner or admin, or a platform admin, can change this.')
    expect(visible(card)).toContain('stopped at its budget')
  })
})

// ---------------------------------------------------------------------------

const platformSettings = (ceiling: Json): Json => ({
  merge_by_default: false,
  max_additional_repositories: 5,
  additional_repositories_budget_gib: 6,
  model_prices: {},
  agent_budget_ceiling: ceiling,
  updated_at: null,
  updated_by: null,
  settings_document: 'present',
})

function adminApi(ceiling: Json | null, put?: (body: Json) => { status: number; body: unknown }): Calls {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/admin/settings') {
      return ceiling === null
        ? { status: 403, body: { code: 'admin_required', message: 'platform admins only' } }
        : { status: 200, body: platformSettings(ceiling) }
    }
    if (m === 'PUT' && url === '/v1/admin/settings' && put !== undefined) return put(body as Json)
    return null
  })
}

/** The API stores the ceiling it was sent and answers the settings, as `Store.set_platform_settings` does. */
const stored = (body: Json) => ({ status: 200, body: platformSettings(body.agent_budget_ceiling as Json) })

async function ceilingCard() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AgentBudgetCeilingCard } = await import('../AdminSettings')
  render(<AgentBudgetCeilingCard />)
  const heading = await screen.findByRole('heading', { name: 'Agent budget ceiling' })
  const card = heading.closest('section') as HTMLElement
  await waitFor(() => expect(within(card).queryByText('Reading…')).toBeNull())
  return card
}

describe('Admin › Pool limits › Agent budget ceiling', () => {
  it('is Off by default and says what a budget does, and that the ceiling turns nothing on', async () => {
    adminApi({})
    const card = await ceilingCard()
    // MUTATION: draw a limit, or no Off choice, for a ceiling nobody set -> fails.
    const off = within(card).getByRole('radio', { name: /^Off/ }) as HTMLInputElement
    expect(off.checked).toBe(true)
    expect(within(card).queryByRole('textbox')).toBeNull()
    // MUTATION: drop the copy -> fails.
    expect(visible(card)).toContain('It turns nothing on: a workspace that sets no budget has none.')
    expect(visible(card)).toContain('At 80% of its budget an agent is told to finish and publish')
    expect(visible(card)).toContain('stopped at its budget')
    expect((within(card).getByRole('button', { name: 'Save ceiling' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves a dollar ceiling as `agent_budget_ceiling` and nothing else', async () => {
    const calls = adminApi({}, stored)
    const card = await ceilingCard()
    fireEvent.click(within(card).getByRole('radio', { name: /^A limit/ }))
    // MUTATION: allow a save with neither unit named -> fails.
    expect(visible(card)).toContain('Name a ceiling in dollars, in tokens or in both, or choose Off.')
    expect((within(card).getByRole('button', { name: 'Save ceiling' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(within(card).getByRole('textbox', { name: 'Dollars per agent, at most' }), { target: { value: '20' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save ceiling' }))
    await waitFor(() => expect(puts(calls, '/v1/admin/settings')).toHaveLength(1))
    // MUTATION: send another setting beside it, or a null for the unit left blank -> fails.
    expect(puts(calls, '/v1/admin/settings')[0]!.body).toEqual({ agent_budget_ceiling: { usd: 20 } })
    expect(visible(await within(card).findByRole('status'))).toBe('Saved: at most $20.00 per agent.')
  })

  it('saves a token ceiling', async () => {
    const calls = adminApi({}, stored)
    const card = await ceilingCard()
    fireEvent.click(within(card).getByRole('radio', { name: /^A limit/ }))
    fireEvent.change(within(card).getByRole('textbox', { name: 'Tokens per agent, at most' }), { target: { value: '15,000,000' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save ceiling' }))
    await waitFor(() => expect(puts(calls, '/v1/admin/settings')).toHaveLength(1))
    expect(puts(calls, '/v1/admin/settings')[0]!.body).toEqual({ agent_budget_ceiling: { tokens: 15000000 } })
    expect(visible(await within(card).findByRole('status'))).toBe('Saved: at most 15,000,000 tokens per agent.')
  })

  it('turns a set ceiling off with `{}`', async () => {
    const calls = adminApi({ usd: 20, tokens: 15000000 }, stored)
    const card = await ceilingCard()
    // MUTATION: open at Off, or drop a stored unit, whatever was stored -> fails.
    expect((within(card).getByRole('radio', { name: /^A limit/ }) as HTMLInputElement).checked).toBe(true)
    expect((within(card).getByRole('textbox', { name: 'Dollars per agent, at most' }) as HTMLInputElement).value).toBe('20')
    expect((within(card).getByRole('textbox', { name: 'Tokens per agent, at most' }) as HTMLInputElement).value).toBe('15000000')
    fireEvent.click(within(card).getByRole('radio', { name: /^Off/ }))
    fireEvent.click(within(card).getByRole('button', { name: 'Save ceiling' }))
    await waitFor(() => expect(puts(calls, '/v1/admin/settings')).toHaveLength(1))
    // MUTATION: send null, which the API reads as "leave it as it is" -> fails.
    expect(puts(calls, '/v1/admin/settings')[0]!.body).toEqual({ agent_budget_ceiling: {} })
    expect(visible(await within(card).findByRole('status'))).toBe('Saved: no ceiling.')
  })

  it('draws a refused save as the API wrote it, with nothing marked saved', async () => {
    const calls = adminApi({}, () => ({
      status: 422,
      body: { code: 'validation_failed', message: 'agent_budget_ceiling.usd: Input should be less than or equal to 100000' },
    }))
    const card = await ceilingCard()
    fireEvent.click(within(card).getByRole('radio', { name: /^A limit/ }))
    fireEvent.change(within(card).getByRole('textbox', { name: 'Dollars per agent, at most' }), { target: { value: '200000' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save ceiling' }))
    await waitFor(() => expect(puts(calls, '/v1/admin/settings')).toHaveLength(1))
    const alert = await within(card).findByRole('alert')
    expect(visible(alert)).toContain('The ceiling was not saved')
    expect(visible(alert)).toContain('less than or equal to 100000')
    expect(within(card).queryByRole('status')).toBeNull()
  })

  it('tells anyone who is not a platform admin so, with no control', async () => {
    adminApi(null)
    const card = await ceilingCard()
    // MUTATION: draw "Not read" or the form over a 403 -> fails.
    expect(visible(card)).toContain('Platform admins only.')
    expect(within(card).queryByRole('radio')).toBeNull()
  })
})

describe('the budget refusal in words', () => {
  it('names the ceiling in the unit asked, and falls back to the API\'s sentence', async () => {
    vi.resetModules()
    const { budgetRefusalWords } = await import('../api')
    const err = (detail: unknown) => ({ kind: 'validation' as never, httpStatus: 422, code: 'budget_above_ceiling', message: 'as the API wrote it', detail })
    expect(budgetRefusalWords(err({ budget: { tokens: 20000000 }, ceiling: { usd: 20, tokens: 15000000 } }))).toContain(
      '20,000,000 tokens is above the platform ceiling of 15,000,000 tokens',
    )
    // MUTATION: invent a ceiling the detail does not name -> fails.
    expect(budgetRefusalWords(err({ budget: { usd: 50 } }))).toBe('as the API wrote it')
    // Another refusal is not this one's to word.
    expect(budgetRefusalWords({ ...err({}), code: 'validation_failed' })).toBeNull()
  })
})
