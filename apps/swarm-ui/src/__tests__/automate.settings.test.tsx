// AUTOMATE › SETTINGS (docs/design/plans-pipeline.md §2.5, §6.4, §7.8; lane U11).
//
// WHAT EACH CASE HOLDS:
//   * the Plan review card: a workspace admin changes the diagram switch and
//     the chat's model class, each through `PUT /v1/workspaces/{tid}/plan-settings`
//     with the one key changed; a member sees the same values and cannot press;
//   * "the platform's" names the platform's value as the read resolves it;
//   * the caps are one read-only line naming who sets them, with no input for
//     anyone, admin or member;
//   * a refused save shows the server's own reason;
//   * under the card, a schedule whose type `proposes` has a row whose choice
//     sends the schedule's `PATCH` with its revision and `plan_diagrams` only;
//     a type that does not propose has no row;
//   * at 390px the choices wrap rather than widen the page.
//
// The bodies are `plansettings.view`'s and `routes/schedules.py`'s shapes.
// Example data only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const SETTINGS = '/v1/workspaces/eng/plan-settings'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

/** `plansettings.view`: the workspace's choices, what each resolves to, and the caps. */
function planSettings(over: { plans?: Json; effective?: Json; can_edit?: boolean; caps?: Json } = {}): Json {
  return {
    tenant_id: 'eng',
    plans: { diagrams: null, chat_model_class: null, ...over.plans },
    effective: {
      diagrams: { value: true, from: 'platform' },
      chat_model_class: { value: 'sonnet', from: 'platform', fell_through: null },
      ...over.effective,
    },
    caps: { replans: 5, diagram_asks: 20, chat_turns: 40, review_usd: 25, from: 'platform', set_by: null, set_at: null, ...over.caps },
    chat_model_classes: ['haiku', 'sonnet', 'opus'],
    updated_at: null,
    updated_by: null,
    read_at: hoursFromNow(0),
    can_edit: over.can_edit ?? true,
  }
}

function schedule(id: string, name: string, type: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name,
    type,
    scope: { mode: 'all' },
    cron: '0 9 * * 1-5',
    timezone: 'UTC',
    params: {},
    gate: { plan: 'approve', run: 'auto', merge: 'approve' },
    budget: {},
    policy: {},
    state: 'enabled',
    pause: null,
    owner: 'owner@example.com',
    created_by: 'owner@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    revision: 4,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-11-25', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    ...over,
  }
}

function typeRow(name: string, proposes: boolean | undefined): Json {
  return {
    name,
    description: `The ${name} type.`,
    params_schema: { properties: {} },
    default_gate: { plan: 'approve', run: 'auto', merge: 'approve' },
    floor_gate: { plan: 'approve', run: 'auto', merge: 'approve' },
    min_interval_minutes: 15,
    budget_caps: null,
    scopes: ['all'],
    available: true,
    disabled_reason: null,
    ...(proposes === undefined ? {} : { proposes }),
  }
}

const SCHEDULES = [
  schedule('sch_sweep', 'Weekday sweep', 'issue-sweep'),
  schedule('sch_review', 'Nightly failure review', 'failure-review', { plan_diagrams: 'off', revision: 7 }),
  schedule('sch_cost', 'Weekly cost report', 'cost-report'),
  schedule('sch_old', 'Older type', 'legacy-type'),
]
const TYPES = [typeRow('issue-sweep', true), typeRow('failure-review', true), typeRow('cost-report', false), typeRow('legacy-type', undefined)]

interface Opts {
  settings?: Json
  put?: (body: Json) => Reply
  patch?: (id: string, body: Json) => Reply
}

/** The workspace's view after a PUT the server accepted: `inherit` clears a key. */
function afterPut(settings: Json, body: Json): Json {
  const plans = { ...(settings.plans as Json) }
  for (const key of ['diagrams', 'chat_model_class'] as const) {
    if (body[key] !== undefined) plans[key] = body[key] === 'inherit' ? null : body[key]
  }
  return { ...settings, plans }
}

/** A schedule's PATCH, answered with the next revision unless `opts.patch` refuses it. */
function patchReply(opts: Opts, id: string, body: Json): Reply {
  const refused = opts.patch?.(id, body) ?? null
  if (refused !== null) return refused
  const was = SCHEDULES.find((s) => s.schedule_id === id)!
  return { status: 200, body: { schedule: { ...was, ...body, revision: (was.revision as number) + 1 } } }
}

async function open(opts: Opts = {}) {
  let settings = opts.settings ?? planSettings()
  const calls = serve((m, url, body) => {
    const path = url.split('?')[0]!
    const key = `${m} ${path}`
    if (key === `GET ${SETTINGS}`) return { status: 200, body: settings }
    if (key === `PUT ${SETTINGS}`) {
      const refused = opts.put?.(body as Json) ?? null
      if (refused !== null) return refused
      settings = afterPut(settings, body as Json)
      return { status: 200, body: settings }
    }
    if (key === 'GET /v1/schedules') return { status: 200, body: { schedules: SCHEDULES, tenant_id: 'eng' } }
    if (key === 'GET /v1/schedule-types') return { status: 200, body: { types: TYPES, tenant_id: 'eng' } }
    const patched = m === 'PATCH' ? /^\/v1\/schedules\/([^/]+)$/.exec(path) : null
    return patched === null ? null : patchReply(opts, patched[1]!, body as Json)
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AutomateSettingsScreen } = await import('../AutomateSettings')
  const utils = render(<AutomateSettingsScreen />)
  await screen.findByTestId('plan-review', undefined, WAIT)
  return { calls, ...utils }
}

type Calls = Awaited<ReturnType<typeof open>>['calls']
const puts = (calls: Calls) => calls.filter((c) => c.method === 'PUT' && c.url.split('?')[0] === SETTINGS)
const patches = (calls: Calls) => calls.filter((c) => c.method === 'PATCH')

const group = (name: string) => screen.getByRole('group', { name })
const options = (name: string) => within(group(name)).getAllByRole('button')
const labels = (name: string) => options(name).map((b) => visible(b))
const pressed = (name: string) => options(name).filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => visible(b))
const press = (name: string, label: string | RegExp) => fireEvent.click(within(group(name)).getByRole('button', { name: label }))
const DIAGRAMS = 'Diagrams in plans'
const MODEL = 'Chat model'
const row = (name: string) => screen.getByRole('group', { name: `${DIAGRAMS}: ${name}` })

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Plan review card', () => {
  it('lets a workspace admin change the diagram switch, sending that key alone', async () => {
    // MUTATION: send the whole map, or send `true` for on.
    const { calls } = await open()
    expect(pressed(DIAGRAMS)).toEqual(['platform’s (on)'])
    press(DIAGRAMS, 'off')
    await waitFor(() => expect(puts(calls)).toHaveLength(1), WAIT)
    expect(puts(calls)[0]!.body).toEqual({ diagrams: 'off' })
    await waitFor(() => expect(pressed(DIAGRAMS)).toEqual(['off']), WAIT)
    press(DIAGRAMS, /^platform’s/)
    await waitFor(() => expect(puts(calls)).toHaveLength(2), WAIT)
    expect(puts(calls)[1]!.body).toEqual({ diagrams: 'inherit' })
  })

  it('lets a workspace admin change the chat’s model class, sending that key alone', async () => {
    // MUTATION: offer a class the profile does not carry, or send the label.
    const { calls } = await open()
    expect(labels(MODEL)).toEqual(['haiku', 'sonnet', 'opus', 'platform’s (sonnet)'])
    expect(pressed(MODEL)).toEqual(['platform’s (sonnet)'])
    press(MODEL, 'opus')
    await waitFor(() => expect(puts(calls)).toHaveLength(1), WAIT)
    expect(puts(calls)[0]!.body).toEqual({ chat_model_class: 'opus' })
    await waitFor(() => expect(pressed(MODEL)).toEqual(['opus']), WAIT)
  })

  it('shows a member the same values with nothing to press', async () => {
    // MUTATION: draw the member's choices enabled; the server would 403 each.
    const { calls } = await open({ settings: planSettings({ can_edit: false, plans: { diagrams: 'off' }, effective: { diagrams: { value: false, from: 'workspace' } } }) })
    expect(pressed(DIAGRAMS)).toEqual(['off'])
    expect(pressed(MODEL)).toEqual(['platform’s (sonnet)'])
    for (const b of [...options(DIAGRAMS), ...options(MODEL)]) expect((b as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(group(DIAGRAMS)).getByRole('button', { name: 'on' }))
    expect(puts(calls)).toHaveLength(0)
  })

  it.each([
    [true, 'platform’s (on)'],
    [false, 'platform’s (off)'],
  ] as const)('names the platform’s value %s in its own option', async (value, label) => {
    // MUTATION: leave the option bare, or name the workspace's value in it.
    await open({ settings: planSettings({ effective: { diagrams: { value, from: 'platform' }, chat_model_class: { value: 'haiku', from: 'platform', fell_through: null } } }) })
    expect(labels(DIAGRAMS)).toEqual(['on', 'off', label])
    expect(labels(MODEL)).toContain('platform’s (haiku)')
  })

  it('has no input for the caps, for an admin or a member, and names who sets them', async () => {
    // MUTATION: draw a cap as a field, or drop who sets them.
    for (const can_edit of [true, false]) {
      const { unmount } = await open({ settings: planSettings({ can_edit }) })
      const caps = screen.getByTestId('plan-caps')
      for (const role of ['textbox', 'spinbutton', 'combobox', 'button', 'checkbox', 'radio', 'slider', 'switch'] as const) {
        expect(within(caps).queryAllByRole(role)).toHaveLength(0)
      }
      expect(visible(caps)).toContain('5')
      expect(visible(caps)).toContain('20')
      expect(visible(caps)).toContain('40')
      expect(visible(caps)).toContain('$25')
      expect(visible(caps)).toContain('platform admin')
      unmount()
    }
  })

  it('shows the server’s own reason when a save is refused', async () => {
    // MUTATION: show a canned line instead of the refusal's message.
    const reason = 'Only the workspace eng admin may change its plan settings. Nothing was changed.'
    const { calls } = await open({ put: () => ({ status: 403, body: { code: 'role_admin_required', message: reason } }) })
    press(DIAGRAMS, 'off')
    await waitFor(() => expect(puts(calls)).toHaveLength(1), WAIT)
    const alert = await screen.findByRole('alert', undefined, WAIT)
    expect(visible(alert)).toContain(reason)
    expect(pressed(DIAGRAMS)).toEqual(['platform’s (on)'])
  })
})

describe('the schedules that plan', () => {
  it('lists a type that proposes and no other', async () => {
    // MUTATION: list every schedule, or one whose catalogue does not say.
    await open()
    await screen.findByRole('group', { name: `${DIAGRAMS}: Weekday sweep` }, WAIT)
    expect(row('Nightly failure review')).toBeTruthy()
    expect(screen.queryByRole('group', { name: `${DIAGRAMS}: Weekly cost report` })).toBeNull()
    expect(screen.queryByRole('group', { name: `${DIAGRAMS}: Older type` })).toBeNull()
    expect(screen.queryByText('Weekly cost report')).toBeNull()
  })

  it('names what following the workspace resolves to and shows each schedule’s own choice', async () => {
    await open()
    const sweep = await screen.findByRole('group', { name: `${DIAGRAMS}: Weekday sweep` }, WAIT)
    expect(within(sweep).getAllByRole('button').map((b) => visible(b))).toEqual(['workspace’s (on)', 'on', 'off'])
    expect(within(sweep).getAllByRole('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => visible(b))).toEqual(['workspace’s (on)'])
    const review = row('Nightly failure review')
    expect(within(review).getAllByRole('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => visible(b))).toEqual(['off'])
  })

  it('sends the schedule’s PATCH with its revision and plan_diagrams only', async () => {
    // MUTATION: send the whole schedule, or route it through the plan-settings PUT.
    const { calls } = await open()
    const sweep = await screen.findByRole('group', { name: `${DIAGRAMS}: Weekday sweep` }, WAIT)
    fireEvent.click(within(sweep).getByRole('button', { name: 'on' }))
    await waitFor(() => expect(patches(calls)).toHaveLength(1), WAIT)
    expect(patches(calls)[0]!.url.split('?')[0]).toBe('/v1/schedules/sch_sweep')
    expect(patches(calls)[0]!.body).toEqual({ revision: 4, plan_diagrams: 'on' })
    expect(puts(calls)).toHaveLength(0)
    await waitFor(() => expect(within(row('Weekday sweep')).getByRole('button', { name: 'on' }).getAttribute('aria-pressed')).toBe('true'), WAIT)
    fireEvent.click(within(row('Nightly failure review')).getByRole('button', { name: /^workspace’s/ }))
    await waitFor(() => expect(patches(calls)).toHaveLength(2), WAIT)
    expect(patches(calls)[1]!.body).toEqual({ revision: 7, plan_diagrams: 'inherit' })
  })

  it('shows the server’s own reason when a schedule’s save is refused', async () => {
    const reason = 'The schedule changed since revision 4; read it again.'
    const { calls } = await open({ patch: () => ({ status: 409, body: { code: 'schedule_changed', message: reason } }) })
    const sweep = await screen.findByRole('group', { name: `${DIAGRAMS}: Weekday sweep` }, WAIT)
    fireEvent.click(within(sweep).getByRole('button', { name: 'off' }))
    await waitFor(() => expect(patches(calls)).toHaveLength(1), WAIT)
    const alert = await screen.findByRole('alert', undefined, WAIT)
    expect(visible(alert)).toContain(reason)
  })
})

describe('the page at 390 pixels', () => {
  it('wraps each choice rather than widening the page', async () => {
    await open()
    await screen.findByRole('group', { name: `${DIAGRAMS}: Weekday sweep` }, WAIT)
    for (const g of [group(DIAGRAMS), group(MODEL), row('Weekday sweep')]) {
      expect(painted(g, 'flex-wrap', PHONE)).toBe('wrap')
    }
  })
})
