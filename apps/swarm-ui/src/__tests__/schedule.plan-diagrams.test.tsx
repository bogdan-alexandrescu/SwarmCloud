// "DIAGRAMS IN PLANS" ON THE SCHEDULE'S EDIT SHEET
// (docs/design/plans-pipeline.md §6.4, lane U12).
//
// WHAT EACH CASE HOLDS:
//   * a schedule whose type the catalogue marks `proposes` shows inherit / on /
//     off as one segmented choice; a type that does not propose, and a
//     catalogue that does not say, show none;
//   * a schedule with no `plan_diagrams` stored is `inherit` (the API's default);
//   * the PATCH carries `plan_diagrams` only when it changed: a save of another
//     field never sends it, and a choice put back leaves nothing to save;
//   * each of the three values is read, chosen and sent as itself;
//   * "Changes against revision N" lists it, and at 390px it wraps, never scrolls.
//
// The bodies are `routes/schedules.py`'s shapes. Placeholders only.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const LABEL = 'Diagrams in plans'
const VALUES = ['inherit', 'on', 'off'] as const
type Value = (typeof VALUES)[number]

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(over: Json = {}): Json {
  return {
    schedule_id: 'sch_a',
    tenant_id: 'eng',
    name: 'Weekday sweep',
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    params: {},
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-11', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    ...over,
  }
}

function typeRow(name: string, over: Json = {}): Json {
  return {
    name,
    description: `The ${name} type.`,
    params_schema: { properties: {} },
    default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    floor_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    min_interval_minutes: 15,
    budget_caps: null,
    scopes: ['all', 'repos'],
    creatable_scopes: ['repos', 'all'],
    available: true,
    disabled_reason: null,
    ...over,
  }
}

const SWEEP = typeRow('issue-sweep', { proposes: true })
const REPORT = typeRow('cost-report', { proposes: false })

const REPO = {
  repo_id: 'repo_a',
  owner: 'example-org',
  repo: 'example-api',
  default_branch: 'main',
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
}

const PREVIEW = { preview: { words: 'at 09:00, Monday through Friday', next: ['2026-10-12T09:00:00+01:00'], min_gap_minutes: 1440, refusal: null } }

/** Opens the edit form of `stored`; a PATCH is answered with the next revision. */
async function edit(stored: Json = schedule(), types: Json[] = [SWEEP, REPORT], effective: Json = { value: 'on', from: 'platform' }) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: stored, firings: [] } }
    if (m === 'PATCH' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: { ...stored, revision: 3 } } }
    if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types, tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [REPO] } }
    if (m === 'GET' && path === '/v1/workspaces/eng/plan-settings') return { status: 200, body: { tenant_id: 'eng', plans: { diagrams: null, chat_model_class: null }, effective: { diagrams: effective } } }
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: PREVIEW }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const { unmount } = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_a&edit=1" spy={vi.fn()} />)
  await screen.findByTestId('sc-form', undefined, WAIT)
  // The Gates card is drawn, so an absent control is absent and not late.
  expect(screen.getByRole('radiogroup', { name: 'Plan' })).toBeTruthy()
  return { calls, unmount }
}

const group = () => screen.getByRole('radiogroup', { name: LABEL })
const radios = () => within(group()).getAllByRole('radio')
const chosen = () => radios().filter((r) => r.getAttribute('aria-checked') === 'true').map((r) => r.textContent!.replace(/ \(on\)| \(off\)/, ''))
const pick = (word: Value) => fireEvent.click(within(group()).getByRole('radio', { name: word === 'inherit' ? /^inherit/ : word }))
const save = () => screen.getByRole('button', { name: 'Save changes' })

type Calls = Awaited<ReturnType<typeof edit>>['calls']
const patches = (calls: Calls) => calls.filter((c) => c.method === 'PATCH' && c.url.split('?')[0] === '/v1/schedules/sch_a')

async function saved(calls: Calls): Promise<unknown> {
  fireEvent.click(save())
  await waitFor(() => expect(patches(calls)).toHaveLength(1), WAIT)
  return patches(calls)[0]!.body
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('who has the choice', () => {
  it('shows a sweep schedule inherit, on and off, with inherit chosen where none is stored', async () => {
    // MUTATION: draw it without the default and no radio is checked.
    await edit()
    expect(radios().map((r) => r.textContent!.replace(/ \(on\)| \(off\)/, ''))).toEqual([...VALUES])
    expect(chosen()).toEqual(['inherit'])
  })

  it('shows nothing for a type that does not propose', async () => {
    // MUTATION: draw it for every type.
    await edit(schedule({ type: 'cost-report' }))
    expect(screen.queryByText(LABEL)).toBeNull()
  })

  it('shows nothing where the catalogue does not say', async () => {
    // MUTATION: test `proposes !== false` and an older catalogue draws it.
    await edit(schedule(), [typeRow('issue-sweep')])
    expect(screen.queryByText(LABEL)).toBeNull()
  })

  it('shows nothing for a type the catalogue does not serve this account', async () => {
    await edit(schedule(), [REPORT])
    expect(screen.queryByText(LABEL)).toBeNull()
  })
})

describe('what the PATCH carries', () => {
  it.each([
    ['inherit', 'on'],
    ['inherit', 'off'],
    ['on', 'inherit'],
    ['on', 'off'],
    ['off', 'inherit'],
    ['off', 'on'],
  ] as [Value, Value][])('reads %s, and sends %s and the revision alone', async (from, to) => {
    // MUTATION: send null for inherit, and the API reads "unchanged".
    const { calls } = await edit(schedule({ plan_diagrams: from }))
    expect(chosen()).toEqual([from])
    pick(to)
    expect(chosen()).toEqual([to])
    expect(await saved(calls)).toEqual({ revision: 2, plan_diagrams: to })
  })

  it.each([undefined, ...VALUES])('never sends it with another field’s change (stored %s)', async (stored) => {
    // MUTATION: send the draft's value whatever it was.
    const { calls } = await edit(schedule(stored === undefined ? {} : { plan_diagrams: stored }))
    fireEvent.click(screen.getByRole('button', { name: 'Weekly on Monday' }))
    expect(await saved(calls)).toEqual({ revision: 2, cron: '0 9 * * 1' })
  })

  it('never sends it for a type that does not propose, whatever is stored', async () => {
    const { calls } = await edit(schedule({ type: 'cost-report', plan_diagrams: 'off' }))
    fireEvent.click(screen.getByRole('button', { name: 'Weekly on Monday' }))
    expect(await saved(calls)).toEqual({ revision: 2, cron: '0 9 * * 1' })
  })

  it('has nothing to save once the choice is put back', async () => {
    // MUTATION: compare against undefined rather than inherit, and putting it back is a change.
    const { calls } = await edit()
    const sheet = screen.getByTestId('ae-sheet')
    pick('off')
    expect(visible(sheet)).toContain('Changes against revision 2')
    expect(within(sheet).getByText(LABEL)).toBeTruthy()
    expect(within(sheet).getByText('inherit → off')).toBeTruthy()
    pick('inherit')
    expect(visible(sheet)).not.toContain('Changes against revision')
    fireEvent.click(save())
    expect(patches(calls)).toHaveLength(0)
  })
})

describe('what inherit resolves to', () => {
  it.each(['on', 'off'] as const)('names the effective %s in the inherit option alone', async (effective) => {
    // MUTATION: leave the option bare, or put the words on another option.
    await edit(schedule(), [SWEEP, REPORT], { value: effective, from: 'workspace' })
    await screen.findByRole('radio', { name: `inherit (${effective})` }, WAIT)
    expect(within(group()).getAllByRole('radio').map((r) => r.textContent)).toEqual([`inherit (${effective})`, 'on', 'off'])
  })

  it('keeps the bare word where the setting was not read', async () => {
    await edit(schedule(), [SWEEP, REPORT], { value: 'unknown' })
    await new Promise((r) => setTimeout(r, 50))
    expect(radios().map((r) => r.textContent)).toEqual([...VALUES])
  })
})

describe('how it is drawn', () => {
  it('adds no words to the form but its label and its three values', async () => {
    // MUTATION: add a hint under the group.
    const form = () => visible(screen.getByTestId('sc-form')).replace(/\s+/g, '')
    const { unmount } = await edit()
    await screen.findByRole('radio', { name: 'inherit (on)' }, WAIT)
    await waitFor(() => expect(form()).toContain('Nextfive'), WAIT)
    const withIt = form().replace('inherit(on)', 'inherit')
    unmount()
    await edit(schedule(), [typeRow('issue-sweep', { proposes: false }), REPORT])
    await waitFor(() => expect(form()).toContain('Nextfive'), WAIT)
    expect(withIt).toContain('Diagramsinplansinheritonoff')
    expect(withIt.replace('Diagramsinplansinheritonoff', '')).toBe(form())
  })

  it('is the sheet’s own segmented control, which wraps rather than scrolls at 390', async () => {
    // MUTATION: draw a select, or another group's classes.
    await edit()
    expect(group().className).toBe(screen.getByRole('radiogroup', { name: 'Who may approve' }).className)
    expect(painted(group(), 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(group(), 'max-width', PHONE)).toBe('100%')
  })
})
