/**
 * ADMIN › SETTINGS › PLAN REVIEW (docs/design/plans-pipeline.md §6.4, §7.8;
 * lane U10 PLATFORM-PLAN-SETTINGS).
 *
 * The platform's level of the plan-review settings, saved through
 * `PUT /v1/admin/settings` as the page's other settings are: the diagram
 * default (on / off), the chat's model class (a class `claude-code` carries)
 * and the four caps (re-plans 0-20, diagram asks 0-100, chat turns 0-200,
 * review dollars 0-500.00, to the cent).
 *
 *   * the defaults read 5 / 20 / 40 / $25, sonnet, on;
 *   * a cap outside its range is refused before the save, with the range;
 *   * a save sends only the keys that changed;
 *   * a non-admin never sees the group.
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'

type Json = Record<string, unknown>
type Calls = { method: string; url: string; body: unknown }[]

const DEFAULT_CAPS: Json = { replans: 5, diagram_asks: 20, chat_turns: 40, review_usd: 25.0, set_by: null, set_at: null }

/** `GET /v1/admin/settings` as `Store.get_platform_settings` serves it, the plan-review keys at their defaults. */
const platformSettings = (over: Json = {}): Json => ({
  merge_by_default: false,
  max_additional_repositories: 5,
  additional_repositories_budget_gib: 6,
  model_prices: {},
  agent_budget_ceiling: {},
  jev_token_rates: {},
  plan_diagrams_default: true,
  plan_chat_model_class: 'sonnet',
  plan_review_caps: DEFAULT_CAPS,
  updated_at: null,
  updated_by: null,
  settings_document: 'missing',
  ...over,
})

const runtimes: Json = {
  runtimes: {
    'claude-code': { model_classes: ['haiku', 'sonnet', 'opus'], default_model_class: 'sonnet', models: {} },
  },
}

/** What the API answers to a PUT it accepts: the settings as stored, caps named replacing theirs. */
function stored(body: Json) {
  const caps = { ...DEFAULT_CAPS, ...((body.plan_review_caps as Json | undefined) ?? {}) }
  return { status: 200, body: platformSettings({ ...body, plan_review_caps: caps, settings_document: 'present' }) }
}

function adminApi(read: Json | null, put?: (body: Json) => { status: number; body: unknown }, isAdmin = true): Calls {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/tenants/me') {
      return { status: 200, body: { tenant: { tenant_id: 'eng' }, principal: { email: 'ops@example.com', domain: 'example.com', groups: [], is_admin: isAdmin } } }
    }
    if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: runtimes }
    if (m === 'GET' && url === '/v1/admin/settings') {
      return read === null
        ? { status: 403, body: { code: 'admin_required', message: 'platform admins only' } }
        : { status: 200, body: read }
    }
    if (m === 'PUT' && url === '/v1/admin/settings' && put !== undefined) return put(body as Json)
    return null
  })
}

const puts = (calls: Calls) => calls.filter((c) => c.method === 'PUT' && c.url === '/v1/admin/settings')

async function card(admin: boolean | null = true) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PlanReviewSettingsCard } = await import('../AdminSettings')
  render(<PlanReviewSettingsCard admin={admin} />)
  const heading = await screen.findByRole('heading', { name: 'Plan review' })
  const section = heading.closest('section') as HTMLElement
  await waitFor(() => expect(within(section).getByRole('button', { name: 'Save plan review' })).toBeTruthy())
  return section
}

const cap = (section: HTMLElement, name: string) => within(section).getByRole('spinbutton', { name }) as HTMLInputElement
const pressed = (section: HTMLElement, group: string) =>
  within(within(section).getByRole('group', { name: group })).getAllByRole('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent)
const saveButton = (section: HTMLElement) => within(section).getByRole('button', { name: 'Save plan review' }) as HTMLButtonElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Admin › Settings › Plan review', () => {
  it('reads the defaults: 5 / 20 / 40 / $25, sonnet, on', async () => {
    adminApi(platformSettings())
    const section = await card()
    // MUTATION: draw another cap, or the dollars as a whole-dollar figure lost to rounding -> fails.
    expect(cap(section, 'Re-plans').value).toBe('5')
    expect(cap(section, 'Diagram asks').value).toBe('20')
    expect(cap(section, 'Chat turns').value).toBe('40')
    expect(cap(section, 'Review dollars').value).toBe('25')
    // MUTATION: open the switch off, or the model at another class -> fails.
    expect(pressed(section, 'Diagrams in plans')).toEqual(['on'])
    await waitFor(() => expect(pressed(section, 'Chat model')).toEqual(['sonnet']))
    // The chat's choices are the classes `claude-code` carries.
    const classes = within(within(section).getByRole('group', { name: 'Chat model' })).getAllByRole('button').map((b) => b.textContent)
    expect(classes).toEqual(['haiku', 'sonnet', 'opus'])
    // Nothing changed, so nothing to save.
    expect(saveButton(section).disabled).toBe(true)
    // Each range sits behind a help mark, not on the card.
    expect(within(section).getByRole('button', { name: 'About Re-plans' })).toBeTruthy()
    expect(within(section).queryByText(/0 to 20/)).toBeNull()
  })

  it('refuses a cap outside its range before the save, naming the range', async () => {
    const calls = adminApi(platformSettings(), stored)
    const section = await card()
    fireEvent.change(cap(section, 'Re-plans'), { target: { value: '21' } })
    // MUTATION: send the PUT and let the API refuse it -> a PUT is recorded, fails.
    const alert = await within(section).findByRole('alert')
    expect(alert.textContent).toContain('Re-plans: 0 to 20')
    expect(saveButton(section).disabled).toBe(true)
    fireEvent.click(saveButton(section))
    fireEvent.submit(saveButton(section).closest('form') as HTMLFormElement)
    await new Promise((r) => setTimeout(r, 20))
    expect(puts(calls)).toEqual([])
    // Dollars go to the cent, and no further.
    fireEvent.change(cap(section, 'Re-plans'), { target: { value: '5' } })
    fireEvent.change(cap(section, 'Review dollars'), { target: { value: '500.01' } })
    expect((await within(section).findByRole('alert')).textContent).toContain('Review dollars: $0 to $500.00')
    fireEvent.change(cap(section, 'Review dollars'), { target: { value: '12.345' } })
    expect((await within(section).findByRole('alert')).textContent).toContain('Review dollars: $0 to $500.00')
    // A count is whole.
    fireEvent.change(cap(section, 'Review dollars'), { target: { value: '25' } })
    fireEvent.change(cap(section, 'Chat turns'), { target: { value: '4.5' } })
    expect((await within(section).findByRole('alert')).textContent).toContain('Chat turns: 0 to 200')
    fireEvent.submit(saveButton(section).closest('form') as HTMLFormElement)
    await new Promise((r) => setTimeout(r, 20))
    expect(puts(calls)).toEqual([])
  })

  it('accepts 0, the cap that turns a kind of ask off', async () => {
    const calls = adminApi(platformSettings(), stored)
    const section = await card()
    fireEvent.change(cap(section, 'Diagram asks'), { target: { value: '0' } })
    expect(within(section).queryByRole('alert')).toBeNull()
    fireEvent.click(saveButton(section))
    await waitFor(() => expect(puts(calls)).toHaveLength(1))
    expect(puts(calls)[0]!.body).toEqual({ plan_review_caps: { diagram_asks: 0 } })
  })

  it('sends only the keys that changed', async () => {
    const calls = adminApi(platformSettings(), stored)
    const section = await card()
    await waitFor(() => expect(pressed(section, 'Chat model')).toEqual(['sonnet']))
    fireEvent.change(cap(section, 'Chat turns'), { target: { value: '60' } })
    fireEvent.change(cap(section, 'Review dollars'), { target: { value: '30.50' } })
    fireEvent.click(within(within(section).getByRole('group', { name: 'Diagrams in plans' })).getByRole('button', { name: 'off' }))
    fireEvent.click(saveButton(section))
    await waitFor(() => expect(puts(calls)).toHaveLength(1))
    // MUTATION: send every cap, the model class unchanged, or another setting of the page -> fails.
    expect(puts(calls)[0]!.body).toEqual({
      plan_diagrams_default: false,
      plan_review_caps: { chat_turns: 60, review_usd: 30.5 },
    })
    // The card then draws what the API stored, and has nothing left to save.
    await waitFor(() => expect(saveButton(section).disabled).toBe(true))
    expect(cap(section, 'Chat turns').value).toBe('60')
    expect(pressed(section, 'Diagrams in plans')).toEqual(['off'])

    // A second save sends the model class alone.
    fireEvent.click(within(within(section).getByRole('group', { name: 'Chat model' })).getByRole('button', { name: 'opus' }))
    fireEvent.click(saveButton(section))
    await waitFor(() => expect(puts(calls)).toHaveLength(2))
    expect(puts(calls)[1]!.body).toEqual({ plan_chat_model_class: 'opus' })
  })

  it('draws a refused save as the API wrote it', async () => {
    const calls = adminApi(platformSettings(), () => ({
      status: 422,
      body: { code: 'validation_failed', message: "plan_chat_model_class 'opus' is not a class claude-code carries" },
    }))
    const section = await card()
    await waitFor(() => expect(pressed(section, 'Chat model')).toEqual(['sonnet']))
    fireEvent.click(within(within(section).getByRole('group', { name: 'Chat model' })).getByRole('button', { name: 'opus' }))
    fireEvent.click(saveButton(section))
    await waitFor(() => expect(puts(calls)).toHaveLength(1))
    const alert = await within(section).findByRole('alert')
    expect(alert.textContent).toContain('not a class claude-code carries')
  })
})

describe('a non-admin never sees the group', () => {
  it('draws nothing for a non-admin, and reads nothing', async () => {
    const calls = adminApi(platformSettings(), undefined, false)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { PlanReviewSettingsCard } = await import('../AdminSettings')
    const { container } = render(<PlanReviewSettingsCard admin={false} />)
    await new Promise((r) => setTimeout(r, 30))
    expect(container.textContent).toBe('')
    expect(calls.filter((c) => c.url === '/v1/admin/settings')).toEqual([])
  })

  it('draws nothing when the settings read answers 403', async () => {
    adminApi(null)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { PlanReviewSettingsCard } = await import('../AdminSettings')
    const { container } = render(<PlanReviewSettingsCard admin={null} />)
    await new Promise((r) => setTimeout(r, 60))
    expect(container.textContent).toBe('')
  })

  it('the Admin settings screen carries the group for an admin alone', async () => {
    adminApi(platformSettings(), undefined, false)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    let mod = await import('../AdminSettings')
    render(<mod.AdminSettingsScreen />)
    await waitFor(() => expect(screen.getByText('GitHub fallback token')).toBeTruthy())
    await new Promise((r) => setTimeout(r, 30))
    // MUTATION: draw the group for anyone signed in -> fails.
    expect(screen.queryByRole('heading', { name: 'Plan review' })).toBeNull()
    cleanup()

    adminApi(platformSettings())
    vi.resetModules()
    mod = await import('../AdminSettings')
    render(<mod.AdminSettingsScreen />)
    // MUTATION: leave the group off the page -> fails.
    expect(await screen.findByRole('heading', { name: 'Plan review' })).toBeTruthy()
  })
})
