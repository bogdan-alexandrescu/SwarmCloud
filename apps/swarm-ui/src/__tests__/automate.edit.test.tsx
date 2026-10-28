// AUTOMATE › A NEW OR EDITED SCHEDULE, PROPOSAL B (#2417, frames A3, A3a, A3b;
// BGD's pick 2026-10-26; docs/web-ui/mockups/automate-b.html).
//
// WHAT EACH CASE HOLDS:
//   * the form is one page of numbered cards, in B's order: 1 Type and
//     template, 2 Target, 3 Cadence, 4 Gates and approvers, 5 Budget and
//     tripwire, 6 Start (a create only);
//   * the side sheet, "Before you create" / "Before you save", counts and
//     lists EVERY problem at once, from the server's own answers (the
//     preview's refusal, the type's served `budget_caps`), so Create stays off
//     and says why before it is pressed (A3a);
//   * a refused cadence strikes its next five through, and the cron field
//     and the budget field each carry their own error in place (A3a);
//   * an edit has the same cards with the type fixed and no Start, names the
//     revision it edits and lists the change against it (A3b);
//   * an edit of a paused schedule leads with "Saving does not resume", the
//     tripwire's pause told apart and an admin's `disabled` read as paused,
//     never "disabled" (the decided word, #2417);
//   * at 390 the cards' grids are one column and the sheet comes after them.
//
// The bodies are `routes/schedules.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(over: Json = {}): Json {
  return {
    schedule_id: 'sch_a',
    tenant_id: 'eng',
    name: 'Bug sweep',
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 * * * *',
    timezone: 'UTC',
    params: { max_new_per_firing: 3, plan_approval: 'auto', merge: 'approve' },
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    state_word: 'active',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(1),
    last_firing: null,
    revision: 7,
    words: 'every hour, at minute 0',
    tier: 'R2',
    spend_today: { day: '2026-10-26', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    ...over,
  }
}

function sweepType(over: Json = {}): Json {
  return {
    name: 'issue-sweep',
    description: 'Open issues that match become issue runs.',
    executor: 'api',
    params_schema: {
      properties: {
        max_new_per_firing: { type: 'integer', title: 'Max New Per Firing', default: 3, minimum: 1 },
        plan_approval: { type: 'string', title: 'Plan Approval', enum: ['auto', 'required'], default: 'auto' },
        merge: { type: 'string', title: 'Merge', enum: ['off', 'approve', 'auto'], default: 'approve' },
      },
    },
    default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    min_interval_minutes: 15,
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
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
}

const PREVIEW = {
  preview: {
    words: 'every hour, at minute 0',
    next: ['2026-10-26T10:00:00+00:00', '2026-10-26T11:00:00+00:00', '2026-10-26T12:00:00+00:00', '2026-10-26T13:00:00+00:00', '2026-10-26T14:00:00+00:00'],
    min_gap_minutes: 60,
    refusal: null,
  },
}

const REFUSED = {
  preview: {
    words: 'every 10 minutes',
    next: ['2026-10-26T09:20:00+00:00', '2026-10-26T09:30:00+00:00', '2026-10-26T09:40:00+00:00', '2026-10-26T09:50:00+00:00', '2026-10-26T10:00:00+00:00'],
    min_gap_minutes: 10,
    refusal: { code: 'interval_too_short', message: 'issue-sweep fires at most every 15 minutes' },
  },
}

function routes(extra: Handler, current: Json = schedule()): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types: [sweepType()], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [REPO] } }
    if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: { schedules: [], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: current, firings: [] } }
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: PREVIEW }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={view} spy={vi.fn()} />)
  return { ...utils, calls }
}

function media(narrow: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: narrow && (query.includes('1023') || query.includes('560')),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

/** The control under a field's own label (`.c-lbl`). */
function field(label: string): HTMLInputElement {
  const lbl = [...document.querySelectorAll('.c-lbl')].find((e) => e.textContent === label)
  const control = lbl?.closest('label')?.querySelector<HTMLInputElement>('input, select')
  if (!control) throw new Error(`no field labelled ${label}`)
  return control
}

const form = () => screen.findByTestId('sc-form', undefined, WAIT)
const sheet = () => screen.getByTestId('ae-sheet')
const cardTitles = (f: HTMLElement) => [...f.querySelectorAll('.ae-card > .ae-card-h > h3')].map((h) => h.textContent)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('A3a: a new schedule', () => {
  it('is one page of numbered cards in B’s order', async () => {
    await mount('schedule=new', routes(() => null))
    const f = await form()
    // MUTATION: draw the old run of `.sc-h3` headings and there is no card.
    expect(cardTitles(f)).toEqual(['1 · Type and template', '2 · Target', '3 · Cadence', '4 · Gates and approvers', '5 · Budget and tripwire', '6 · Start'])
    const cards = [...f.querySelectorAll<HTMLElement>('.ae-card')]
    expect(within(cards[0]!).getByRole('radiogroup', { name: 'Type' })).toBeTruthy()
    // MUTATION: draw Scope or Merge as a `<select>` again and it is a combobox, not a radio group (A3-SEGMENTED).
    expect(within(cards[1]!).getByRole('radiogroup', { name: 'Scope' })).toBeTruthy()
    expect(within(cards[2]!).getByRole('textbox', { name: 'Cron expression' })).toBeTruthy()
    expect(within(cards[3]!).getByRole('radiogroup', { name: 'Merge' })).toBeTruthy()
    expect(within(cards[4]!).getByRole('spinbutton', { name: 'Per run (USD)' })).toBeTruthy()
    expect(within(cards[5]!).getByRole('radiogroup', { name: 'Start' })).toBeTruthy()
    // A card is the surface and its line, never a bare heading.
    expect(painted(cards[0]!, ['border', 'border-width'], WIDE)).not.toBeNull()
  })

  it('counts and lists every problem in the sheet at once, and Create says how many to fix', async () => {
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: REFUSED } : null)))
    await form()
    fireEvent.change(field('Cron expression'), { target: { value: '*/10 * * * *' } })
    fireEvent.change(field('Per run (USD)'), { target: { value: '25' } })
    // MUTATION: keep the old first-match `why` chain and the sheet names one problem, not three.
    await waitFor(() => expect(sheet().querySelectorAll('.ae-probs > li')).toHaveLength(3), WAIT)
    expect(visible(sheet())).toContain('Before you create')
    expect(visible(sheet().querySelector('.ae-count')!)).toBe('3 problems')
    const probs = [...sheet().querySelectorAll('.ae-probs > li')].map((li) => visible(li))
    expect(probs).toEqual([
      'Name the schedule first.',
      'Cadence: this type refuses it.',
      'Per run $25.00 is over issue-sweep’s cap of $15.00.',
    ])
    const create = screen.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement
    expect(create.disabled).toBe(true)
    expect(visible(create.closest('.c-btn-wrap')!)).toContain('Fix the 3 problems to create.')
    expect(sheet().contains(create)).toBe(true)
  })

  it('reads the caps the type serves: a day under one run and a live count over the cap are problems too', async () => {
    await mount('schedule=new', routes(() => null))
    await form()
    fireEvent.change(field('Name'), { target: { value: 'Flaky test sweep' } })
    fireEvent.change(field('Per run (USD)'), { target: { value: '10' } })
    fireEvent.change(field('Per day (USD)'), { target: { value: '5' } })
    fireEvent.change(field('Live runs at most'), { target: { value: '9' } })
    const probs = await waitFor(() => {
      const out = [...sheet().querySelectorAll('.ae-probs > li')].map((li) => visible(li))
      expect(out).toHaveLength(2)
      return out
    }, WAIT)
    // MUTATION: compare against the draft instead of `budget_caps` and nothing is over.
    expect(probs).toEqual(['Per day $5.00 is under one run’s $10.00.', 'Live runs 9 is over issue-sweep’s cap of 8.'])
    expect((screen.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('says each budget and cadence problem in place, under its own field', async () => {
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: REFUSED } : null)))
    await form()
    fireEvent.change(field('Per run (USD)'), { target: { value: '25' } })
    const runErr = field('Per run (USD)').closest('label')!.querySelector('.ae-err')
    // MUTATION: say the cap only in the sheet and the field carries nothing.
    expect(visible(runErr!)).toBe('Over issue-sweep’s cap of $15.00 a run.')
    expect(field('Per run (USD)').getAttribute('aria-invalid')).toBe('true')
    await waitFor(() => expect(field('Cron expression').closest('label')!.querySelector('.ae-err')).not.toBeNull(), WAIT)
    expect(visible(field('Cron expression').closest('label')!.querySelector('.ae-err')!)).toBe('issue-sweep fires at most every 15 minutes')
    expect(painted(runErr!, 'color', WIDE)).toBe('var(--bad-ink)')
  })

  it('strikes a refused cadence’s next five through, and says why', async () => {
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: REFUSED } : null)))
    await form()
    const list = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('ol.sc-next')
      expect(el?.classList.contains('is-refused')).toBe(true)
      return el!
    }, WAIT)
    // MUTATION: drop the refused list's rule and the firings read as if they will happen.
    expect(painted(list, ['text-decoration', 'text-decoration-line'], WIDE)).toContain('line-through')
    expect(visible(screen.getByTestId('sc-preview'))).toContain('Struck through: the server refuses this cadence.')
  })

  it('says a refused cadence’s reason once in the sheet, where the preview already says it', async () => {
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: REFUSED } : null)))
    await form()
    await waitFor(() => expect(visible(sheet())).toContain('This type would refuse it'), WAIT)
    // MUTATION: put the reason back in the sheet's problem and it is said twice, 50px apart.
    expect(visible(sheet()).split('issue-sweep fires at most every 15 minutes')).toHaveLength(2)
    expect([...sheet().querySelectorAll('.ae-probs > li')].map((li) => visible(li))).toContain('Cadence: this type refuses it.')
  })

  it('says only the rule the API holds about a gate below the type’s default', async () => {
    await mount('schedule=new', routes(() => null))
    const f = await form()
    const gates = [...f.querySelectorAll<HTMLElement>('.ae-card')].find((c) => visible(c.querySelector('h3')!) === '4 · Gates and approvers')!
    // MUTATION: the mock-up's "Merge off is below the type's default" back; off is the strictest merge, so anyone may set it.
    expect(visible(gates)).not.toContain('Merge off is below')
    expect(visible(gates)).toContain('A gate below the type’s default needs a platform admin.')
  })

  it('reads no problems once everything is right, and Create is live', async () => {
    await mount('schedule=new', routes(() => null))
    await form()
    fireEvent.change(field('Name'), { target: { value: 'Flaky test sweep' } })
    await waitFor(() => expect(visible(sheet().querySelector('.ae-count')!)).toBe('no problems'), WAIT)
    expect(sheet().querySelector('.ae-probs')).toBeNull()
    expect((screen.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('A3b: editing a schedule', () => {
  it('fixes the type, offers no Start, names the revision and lists the change against it', async () => {
    await mount('schedule=sch_a&edit=1', routes(() => null))
    const f = await form()
    expect(cardTitles(f)).toEqual(['1 · Type', '2 · Target', '3 · Cadence', '4 · Gates and approvers', '5 · Budget and tripwire'])
    expect(screen.getByRole('heading', { name: 'Edit Bug sweep', level: 2 })).toBeTruthy()
    expect(visible(screen.getByRole('region', { name: 'Edit this schedule' }))).toContain('revision 7')
    expect(visible(sheet())).toContain('Before you save')
    expect(sheet().querySelector('.ae-diff')).toBeNull()
    fireEvent.change(field('Per run (USD)'), { target: { value: '12' } })
    // MUTATION: drop the change list and the sheet says nothing of what the save sends.
    const diff = await waitFor(() => {
      const el = sheet().querySelector<HTMLElement>('.ae-diff')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(diff)).toContain('Changes against revision 7')
    const pairs = [...diff.querySelectorAll('dt')].map((dt) => `${dt.textContent}: ${dt.nextElementSibling?.textContent}`)
    expect(pairs).toEqual(['Per run: $15.00 → $12.00'])
    expect((screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('leads a tripwire-paused edit with “Saving does not resume”', async () => {
    const paused = schedule({
      state: 'auto_paused',
      state_word: 'paused_by_tripwire',
      pause: { by: 'system', at: '2026-10-26T07:41:00Z', reason: 'a run cost more than its per-run amount', code: 'RUN_OVER_BUDGET' },
    })
    await mount('schedule=sch_a&edit=1', routes(() => null, paused))
    await form()
    const lead = screen.getByTestId('ae-lead')
    // MUTATION: drop the lead and nothing says a save leaves it paused.
    expect(visible(lead)).toContain('Editing a paused schedule')
    expect(visible(lead)).toContain('Saving does not resume Bug sweep')
    expect(visible(lead)).toContain('It was paused by its tripwire at 26 Oct 07:41Z. Save the change, then resume it from its page.')
  })

  it('reads an admin’s disabled schedule as paused, never “disabled”', async () => {
    await mount('schedule=sch_a&edit=1', routes(() => null, schedule({ state: 'disabled', state_word: 'paused', pause: { by: 'admin', at: '2026-10-26T06:00:00Z' } })))
    await form()
    const lead = screen.getByTestId('ae-lead')
    expect(visible(lead)).toContain('Saving does not resume Bug sweep')
    expect(visible(lead)).toContain('It was paused at 26 Oct 06:00Z.')
    // MUTATION: say the stored state and the word is "disabled".
    expect(visible(lead)).not.toMatch(/disabled/i)
  })

  it('draws no lead for a schedule that is running', async () => {
    await mount('schedule=sch_a&edit=1', routes(() => null))
    await form()
    expect(screen.queryByTestId('ae-lead')).toBeNull()
  })
})

describe('A3 at 390', () => {
  it('puts the cards’ grids in one column and the sheet after the cards', async () => {
    media(true)
    await mount('schedule=new', routes(() => null))
    const f = await form()
    const grids = [...f.querySelectorAll<HTMLElement>('.ae-g3')]
    expect(grids.length).toBeGreaterThan(0)
    // MUTATION: drop the 560px block of automate-edit.css and the budget is three columns on a phone.
    for (const g of grids) expect(painted(g, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(grids[0]!, 'grid-template-columns', WIDE)).toBe('repeat(3, minmax(0, 1fr))')
    const lastCard = [...f.querySelectorAll('.ae-card')].at(-1)!
    expect(lastCard.compareDocumentPosition(sheet()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // One column takes the card padding down so 390 never scrolls sideways.
    expect(painted(f.querySelector('.ae-card')!, ['padding', 'padding-left'], PHONE)).toBe('12px')
  })

  it('says a refused cadence’s reason once in the Cadence card, by the preview under the field', async () => {
    media(true)
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: REFUSED } : null)))
    const f = await form()
    const cadence = [...f.querySelectorAll<HTMLElement>('.ae-card')].find((c) => visible(c.querySelector('h3')!) === '3 · Cadence')!
    await waitFor(() => expect(visible(cadence)).toContain('This type would refuse it'), WAIT)
    // MUTATION: draw the cron field's error at one column too and the card says it twice, 100px apart.
    expect(visible(cadence).split('issue-sweep fires at most every 15 minutes')).toHaveLength(2)
    expect(field('Cron expression').getAttribute('aria-invalid')).toBe('true')
    expect(visible(sheet())).not.toContain('issue-sweep fires at most every 15 minutes')
  })
})
