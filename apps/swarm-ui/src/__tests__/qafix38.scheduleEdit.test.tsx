// QA1-FIX-38 (part of #2596): the new-schedule form's findings from the
// 2026-10-28 visual QA pass, each pinned on the form the console draws.
//
// WHAT EACH CASE HOLDS:
//   * automate-09: the Gates card says the hard stops in the schedule page's
//     words (held for a second member, the owner in a platform repository;
//     workflows refused), drawn from the one `HardStops` both places use;
//   * automate-16: the Type tiles and the Start choice are one tab stop each,
//     and the arrows move and select, as the radio pattern requires;
//   * automate-21: the preview's interval is in the template card's words
//     ("at least 1 day apart"), and a served day reads "Oct 28";
//   * automate-24: each problem in the sheet is a button that focuses the
//     field it names.
//
// The bodies are `routes/schedules.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

function sweepType(over: Json = {}): Json {
  return {
    name: 'issue-sweep',
    description: 'Open issues that match become issue runs.',
    executor: 'api',
    params_schema: { properties: { max_new_per_firing: { type: 'integer', title: 'Max New Per Firing', default: 3, minimum: 1 } } },
    default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    min_interval_minutes: 60,
    budget_caps: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    scopes: ['all', 'repos'],
    creatable_scopes: ['repos', 'all'],
    available: true,
    disabled_reason: null,
    ...over,
  }
}

const REPO = {
  repo_id: 'repo_a',
  owner: 'example-org',
  repo: 'example-api',
  default_branch: 'main',
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: null },
}

const DAILY = {
  preview: {
    words: 'every day at 02:00',
    next: ['2026-10-29T02:00:00+00:00', '2026-10-30T02:00:00+00:00', '2026-10-31T02:00:00+00:00', '2026-11-01T02:00:00+00:00', '2026-11-02T02:00:00+00:00'],
    min_gap_minutes: 1440,
    refusal: null,
  },
}

function routes(): Handler {
  return (m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedule-types') {
      return { status: 200, body: { types: [sweepType(), sweepType({ name: 'flaky-test-sweep', description: 'Flaky tests become issue runs.' }), sweepType({ name: 'docs-drift' })], tenant_id: 'eng' } }
    }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [REPO] } }
    if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: { schedules: [], tenant_id: 'eng' } }
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: DAILY }
    return null
  }
}

async function mount() {
  serve(routes())
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=new" spy={vi.fn()} />)
  return screen.findByTestId('sc-form', undefined, WAIT)
}

/** The control under a field's own label (`.c-lbl`). */
function field(label: string): HTMLInputElement {
  const lbl = [...document.querySelectorAll('.c-lbl')].find((e) => e.textContent === label)
  const control = lbl?.closest('label')?.querySelector<HTMLInputElement>('input, select')
  if (!control) throw new Error(`no field labelled ${label}`)
  return control
}

const radios = (group: HTMLElement) => within(group).getAllByRole('radio') as HTMLButtonElement[]
const stops = (group: HTMLElement) => radios(group).filter((r) => r.tabIndex === 0).map((r) => visible(r))

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('QA1 automate-09: one wording for the hard stops', () => {
  it('says the schedule page’s hard stops in the Gates card, from the one shared sentence', async () => {
    const f = await mount()
    const said = visible(within(f).getByTestId('ae-hard-stops'))
    // MUTATION: put back "a fix touching IAM, the contract, secrets, tenancy, shared files, workflows or cost is held for the owner" and this fails.
    expect(said).toContain('is held for a second member, or in a platform repository for the owner, whatever the gate')
    expect(said).toContain('a change to .github/workflows/ is refused')
    expect(said).not.toContain('shared files, workflows or cost')
    const { HardStops } = await import('../ScheduleEdit')
    const { container } = render(<p>{<HardStops />}</p>)
    // The page's Gate tab draws the same component, so the two cannot drift.
    expect(said).toContain(visible(container))
  })
})

describe('QA1 automate-16: the Type tiles and the Start choice are radio groups a keyboard can use', () => {
  it('gives each group one tab stop, on its checked radio', async () => {
    const f = await mount()
    const type = within(f).getByRole('radiogroup', { name: 'Type' })
    const start = within(f).getByRole('radiogroup', { name: 'Start' })
    // MUTATION: drop `useRovingRadios` and every tile and every Start radio is a tab stop.
    await waitFor(() => expect(stops(type)).toHaveLength(1), WAIT)
    expect(radios(type).find((r) => r.tabIndex === 0)!.getAttribute('aria-checked')).toBe('true')
    const checked = radios(start).find((r) => r.getAttribute('aria-checked') === 'true')!
    expect(stops(start)).toEqual([visible(checked)])
  })

  it('moves and selects with the arrows, wrapping at the ends', async () => {
    const f = await mount()
    const start = within(f).getByRole('radiogroup', { name: 'Start' })
    const [enabled, dryRun, paused] = radios(start)
    fireEvent.click(enabled!)
    await waitFor(() => expect(enabled!.getAttribute('aria-checked')).toBe('true'), WAIT)
    enabled!.focus()
    // MUTATION: drop the form's `onKeyDown={roveRadios}` and ArrowRight leaves Enabled checked and focused.
    fireEvent.keyDown(enabled!, { key: 'ArrowRight' })
    await waitFor(() => expect(dryRun!.getAttribute('aria-checked')).toBe('true'), WAIT)
    expect(document.activeElement).toBe(dryRun)
    expect(stops(start)).toEqual(['Dry run first'])
    fireEvent.keyDown(dryRun!, { key: 'ArrowLeft' })
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' })
    await waitFor(() => expect(paused!.getAttribute('aria-checked')).toBe('true'), WAIT)
    expect(document.activeElement).toBe(paused)

    const type = within(f).getByRole('radiogroup', { name: 'Type' })
    const tiles = radios(type)
    tiles[0]!.focus()
    fireEvent.keyDown(tiles[0]!, { key: 'ArrowDown' })
    await waitFor(() => expect(tiles[1]!.getAttribute('aria-checked')).toBe('true'), WAIT)
    expect(document.activeElement).toBe(tiles[1])
  })
})

describe('QA1 automate-21: units and days in words', () => {
  it('says the preview’s shortest gap as the template card does', async () => {
    const f = await mount()
    // MUTATION: put back `{p.min_gap_minutes} min` and the preview says "at least 1440 min apart".
    await waitFor(() => expect(visible(within(f).getByTestId('sc-preview'))).toContain('at least 1 day apart'), WAIT)
    expect(visible(f)).not.toContain('1440 min')
  })

  it('writes a served day as "Oct 28", and anything else as served', async () => {
    const { dayWords } = await import('../ScheduleEdit')
    // MUTATION: return the day as served and this reads "2026-10-28".
    expect(dayWords('2026-10-28')).toBe('Oct 28')
    expect(dayWords('2026-01-05')).toBe('Jan 5')
    expect(dayWords('2026-02-30')).toBe('2026-02-30')
    expect(dayWords('today')).toBe('today')
  })
})

describe('QA1 automate-24: each problem goes to its field', () => {
  it('is a button that focuses the field it names', async () => {
    await mount()
    fireEvent.change(field('Per run (USD)'), { target: { value: '25' } })
    const sheet = screen.getByTestId('ae-sheet')
    // MUTATION: draw each problem as plain text in its <li> and there is no button to press.
    const name = await within(sheet).findByRole('button', { name: 'Name the schedule first.' }, WAIT)
    fireEvent.click(name)
    expect(document.activeElement).toBe(field('Name'))
    fireEvent.click(within(sheet).getByRole('button', { name: /^Per run \$25\.00 is over/ }))
    expect(document.activeElement).toBe(field('Per run (USD)'))
  })

  it('takes "Choose at least one repository." to the repository list', async () => {
    const f = await mount()
    const box = within(f).getByRole('checkbox', { name: 'example-org/example-api' })
    // The one registered repository starts chosen; unchoosing it is the problem.
    await waitFor(() => expect((box as HTMLInputElement).checked).toBe(true), WAIT)
    fireEvent.click(box)
    const sheet = screen.getByTestId('ae-sheet')
    const repos = await within(sheet).findByRole('button', { name: 'Choose at least one repository.' }, WAIT)
    fireEvent.click(repos)
    expect(document.activeElement).toBe(box)
  })
})
