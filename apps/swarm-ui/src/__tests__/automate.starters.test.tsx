// THE EMPTY SCHEDULES PAGE'S STARTER CARDS, AND THE TYPE THEY CARRY (the
// owner's pick on the Automate visual check, part of #2417; the cards mock-up's
// empty-none frame).
//
// WHAT EACH CASE HOLDS:
//   * no schedules draws one starter card per served schedule type, each with
//     Use; Use opens New schedule at `/automate/schedules/new?type=<name>`;
//   * that address opens the form with that type already chosen;
//   * a `type` the catalogue does not serve is ignored: the form starts on its
//     first usable type, as a bare New schedule does;
//   * paths.ts carries the type through the address both ways and drops one
//     that is not a type name.
//
// The bodies are `routes/schedules.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const DESK: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>

function type(name: string, over: Json = {}): Json {
  return {
    name,
    description: `The ${name} type.`,
    params_schema: { properties: {} },
    default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    min_interval_minutes: 15,
    budget_caps: { per_run_usd: 5, per_day_usd: 40, max_concurrent: 1 },
    scopes: ['repos'],
    creatable_scopes: ['repos'],
    available: true,
    disabled_reason: null,
    ...over,
  }
}

const TYPES = [type('issue-sweep', { tier: 'R2' }), type('cost-report', { tier: 'R0' }), type('failure-review', { tier: 'R1' })]

const EMPTY = { schedules: [], summary: { by_state: {}, pending_approvals: 0, oldest_expires_at: null, unread: null }, tenant_id: 'eng' }

function handler(m: string, url: string): { status: number; body: unknown } | null {
  const path = url.split('?')[0]!
  if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: EMPTY }
  if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [], tenant_id: 'eng' } }
  if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
  if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types: TYPES, tenant_id: 'eng' } }
  if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: { preview: { words: 'weekdays at 09:00', next: [], min_gap_minutes: 1440, refusal: null } } }
  return null
}

async function mount(view: string | null) {
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={view} spy={go} />)
  return { go }
}

/** The Type tile the form has checked. */
async function chosenType(): Promise<string> {
  const f = await screen.findByTestId('sc-form', undefined, WAIT)
  const group = within(f).getByRole('radiogroup', { name: 'Type' })
  const checked = within(group)
    .getAllByRole('radio')
    .filter((r) => r.getAttribute('aria-checked') === 'true')
  expect(checked).toHaveLength(1)
  return checked[0]!.textContent ?? ''
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the empty Schedules page: a starter card per type', () => {
  it('draws one starter card per served type, each with Use opening New schedule with that type', async () => {
    const { go } = await mount(null)
    const none = await screen.findByTestId('acd-none', undefined, WAIT)
    // MUTATION: draw the label alone (the build before this lane) and there is no card.
    const cards = await waitFor(() => {
      const found = [...none.querySelectorAll<HTMLElement>('section.acd-starter')]
      expect(found).toHaveLength(3)
      return found
    }, WAIT)
    expect(cards.map((c) => c.getAttribute('aria-label'))).toEqual(['issue-sweep', 'cost-report', 'failure-review'])
    expect(cards.map((c) => within(c).getByRole('heading', { level: 3 }).textContent)).toEqual(['issue-sweep', 'cost-report', 'failure-review'])
    expect(within(cards[1]!).getByText('R0')).toBeTruthy()
    const use = within(cards[1]!).getByRole('link', { name: 'Use' })
    // MUTATION: drop the type from the address and Use opens a bare New schedule.
    expect(use.getAttribute('href')).toBe('/automate/schedules/new?type=cost-report')
    fireEvent.click(use)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=new&type=cost-report')
  })

  it('lays the starter cards out in one column at 390 and a grid at 1440', async () => {
    await mount(null)
    const grid = await screen.findByTestId('acd-starters', undefined, WAIT)
    expect(painted(grid, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(grid, 'grid-template-columns', DESK)).toBe('repeat(auto-fill, minmax(300px, 1fr))')
    for (const card of grid.querySelectorAll<HTMLElement>('section.acd-starter')) expect(painted(card, 'min-width', PHONE)).toBe('0')
  })

  it('opens New schedule with the type the address names already chosen', async () => {
    await mount('schedule=new&type=failure-review')
    // MUTATION: ignore `type` and the form starts on issue-sweep, the first usable type.
    await waitFor(async () => expect(await chosenType()).toContain('failure-review'), WAIT)
  })

  it('ignores a type the catalogue does not serve: the form starts on its first usable type', async () => {
    await mount('schedule=new&type=no-such-type')
    await waitFor(async () => expect(await chosenType()).toContain('issue-sweep'), WAIT)
  })
})

describe('paths.ts carries the new schedule’s type', () => {
  it('writes the type as the new-schedule path’s query, and reads it back', () => {
    expect(addressToPath('automate/schedules?schedule=new&type=cost-report')).toBe('/automate/schedules/new?type=cost-report')
    expect(pathToAddress('/automate/schedules/new', '?type=cost-report')?.address).toBe('automate/schedules?schedule=new&type=cost-report')
    // A bare New schedule is unchanged.
    expect(addressToPath('automate/schedules?schedule=new')).toBe('/automate/schedules/new')
    expect(pathToAddress('/automate/schedules/new')?.address).toBe('automate/schedules?schedule=new')
  })

  it('drops a type value that is not a type name', () => {
    expect(addressToPath('automate/schedules?schedule=new&type=..%2Fx%20y')).toBe('/automate/schedules/new')
    expect(pathToAddress('/automate/schedules/new', '?type=%3Cb%3E')?.address).toBe('automate/schedules?schedule=new')
    // A type rides only on the new-schedule address, never on one schedule's.
    expect(addressToPath('automate/schedules?schedule=sch_a&type=cost-report')).toBe('/automate/schedules/sch_a')
  })
})

describe('App keeps the new schedule’s type on the Schedules address', () => {
  it('carries `type` through fromAddress and canonical, so Use lands on that type', () => {
    // MUTATION: drop `type` from App's keptKeys and the view is `schedule=new`, bare.
    const at = fromAddress('automate/schedules?schedule=new&type=cost-report')
    expect(at).toMatchObject({ sectionId: 'automate', tab: 'schedules', view: 'schedule=new&type=cost-report' })
    expect(canonical(at)).toBe('automate/schedules?schedule=new&type=cost-report')
    expect(addressToPath(canonical(at))).toBe('/automate/schedules/new?type=cost-report')
  })
})
