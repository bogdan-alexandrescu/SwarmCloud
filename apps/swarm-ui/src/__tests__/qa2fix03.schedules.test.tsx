// AUTOMATE › SCHEDULES, visual QA pass 2 (QA2-FIX-03; part of #2694): the
// findings whose main file is Schedules.tsx.
//
// WHAT EACH CASE HOLDS:
//   * automate2-09: a Run now answer is one sentence led by the schedule's
//     name, one colon: "Schedule sch_a fired: it created 2 work items.", not
//     "Schedule sch_a: Fired: the firing created 2 work items.";
//   * automate2-16: for the busy window after Run now is pressed, focus stays
//     on the pressed button (aria-disabled, the busy word), a second press
//     sends nothing, and the answer's notice takes focus when it arrives;
//   * automate2-21: the delete dialog's actions stack full width at 44px on a
//     phone, Keep it then Delete schedule with its reason under it, and stay
//     the dialog's own row on a desk;
//   * automate2-15: the list records each schedule's name by its id, so the
//     tab title can name the open schedule rather than its id.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const DESK: CascadeEnv = { width: 1280 }

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    params: { max_new_per_firing: 3 },
    // `run: approve`: Run now fires without its spend confirmation (runSpends).
    gate: { plan: 'auto', run: 'approve', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    consecutive_failures: 0,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

function firing(id: string, over: Json = {}): Json {
  return {
    firing_id: id,
    schedule_id: 'sch_a',
    type: 'issue-sweep',
    slot: hoursFromNow(0),
    fired_at: hoursFromNow(0),
    trigger: 'run_now',
    state: 'created',
    work: [
      { kind: 'issue_run', id: 'run_1', repo_id: 'repo_a' },
      { kind: 'issue_run', id: 'run_2', repo_id: 'repo_a' },
    ],
    skip: null,
    dry_run: null,
    approval_id: null,
    outcome: null,
    cost: null,
    ended_at: null,
    ...over,
  }
}

type Reply = { status: number; body: unknown }
type Handler = (method: string, url: string, body: unknown) => Reply | null

const ONE: Record<string, Reply> = {
  '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } },
  '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
  '/v1/repositories': { status: 200, body: { repositories: [] } },
}

async function mountSheet(handler: Handler) {
  const calls = serve((m, url, body) => handler(m, url, body) ?? (m === 'GET' ? ONE[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../Schedules')
  render(<Routed Screen={mod.SchedulesScreen} section="automate/schedules" start="schedule=sch_a" spy={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
  return { calls, mod }
}

const head = () => screen.getByTestId('sc-acts-head')
const runPosts = (calls: { method: string; url: string }[]) => calls.filter((c) => c.method === 'POST' && c.url.split('?')[0] === '/v1/schedules/sch_a:run')

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Run now answer (automate2-09)', () => {
  it('names the schedule once and says what the firing made in one sentence', async () => {
    // MUTATION: join `${s.name}: ` to a `Fired: the firing ...` sentence again and this fails.
    await mountSheet((m, url) => (m === 'POST' && url === '/v1/schedules/sch_a:run' ? { status: 200, body: { firing: firing('sch_a:now') } } : null))
    fireEvent.click(within(head()).getByRole('button', { name: 'Run now' }))
    const said = visible(await screen.findByTestId('sc-said', undefined, WAIT))
    expect(said).toBe('Schedule sch_a fired: it created 2 work items.')
    expect(said).not.toMatch(/Fired:|the firing created/)
  })
})

describe('a pressed action keeps focus while busy (automate2-16)', () => {
  it('holds focus on Run now, aria-disabled, until the notice takes it', async () => {
    // MUTATION: draw Run now with `Button`'s `busy` (disabled) again and focus drops to <body>.
    let answer: (() => void) | null = null
    const { calls } = await mountSheet(() => null)
    const served = globalThis.fetch
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') await new Promise<void>((done) => (answer = done))
      return served(input, init)
    }) as unknown as typeof fetch
    const run = within(head()).getByRole('button', { name: 'Run now' })
    run.focus()
    fireEvent.click(run)
    const busy = await within(head()).findByRole('button', { name: 'Running…' }, WAIT)
    expect(busy).toBe(run)
    expect(busy.hasAttribute('disabled')).toBe(false)
    expect(busy.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(busy)
    // A press while busy sends nothing more.
    fireEvent.click(busy)
    expect(answer).not.toBeNull()
    await act(async () => answer!())
    const said = await screen.findByTestId('sc-said', undefined, WAIT)
    await waitFor(() => expect(document.activeElement).toBe(said), WAIT)
    expect(runPosts(calls)).toHaveLength(1)
  })
})

describe('the delete dialog on a phone (automate2-21)', () => {
  it('stacks Keep it and Delete schedule full width at 44px on a phone, one row on a desk', async () => {
    // MUTATION: drop the `.sc-del-acts` wrapper or its phone rule and the actions wrap as two rows at 36px.
    await mountSheet(() => null)
    fireEvent.click(within(head()).getByRole('button', { name: 'Delete…' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    const acts = dialog.querySelector<HTMLElement>('.sc-del-acts')
    expect(acts).not.toBeNull()
    const keep = within(dialog).getByRole('button', { name: 'Keep it' })
    const del = within(dialog).getByRole('button', { name: 'Delete schedule' })
    // Cancel first, the verb under it, its reason under the verb.
    expect(Array.from(acts!.querySelectorAll('button'))).toEqual([keep, del])
    const wrap = del.closest('.c-btn-wrap')
    expect(wrap?.parentElement).toBe(acts)
    expect(visible(wrap!.querySelector('.c-why'))).toBe('Type the schedule’s name exactly.')

    expect(painted(acts!, 'display', PHONE)).toBe('flex')
    expect(painted(acts!, 'flex-direction', PHONE)).toBe('column')
    expect(painted(wrap!, 'flex-direction', PHONE)).toBe('column')
    for (const b of [keep, del]) {
      expect(painted(b, 'min-height', PHONE)).toBe('44px')
      expect(painted(b, 'width', PHONE)).toBe('100%')
    }
    expect(painted(acts!, 'display', DESK)).toBe('contents')
  })
})

describe("the open schedule's name (automate2-15)", () => {
  it('is recorded from the list under the sheet, and a reader of it hears when it arrives', async () => {
    // MUTATION: drop the list's `rememberScheduleNames` and the name stays null, so the title keeps `sch_a`.
    vi.resetModules()
    const early = await import('../Schedules')
    function Name() {
      return <output data-testid="nm">{early.useScheduleName('sch_a') ?? 'sch_a'}</output>
    }
    render(<Name />)
    expect(visible(screen.getByTestId('nm'))).toBe('sch_a')
    act(() => early.rememberScheduleNames([{ schedule_id: 'sch_a', name: 'Nightly issue sweep' }]))
    expect(visible(screen.getByTestId('nm'))).toBe('Nightly issue sweep')

    const { mod } = await mountSheet(() => null)
    await waitFor(() => expect(mod.scheduleNameOf('sch_a')).toBe('Schedule sch_a'), WAIT)
  })
})

describe('a next firing, in one format (automate2-07)', () => {
  const zone = process.env.TZ
  afterEach(() => {
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
  })

  it('writes the schedule-zone time as the sheet does, with its weekday, and Z for UTC', async () => {
    // MUTATION: format it as `2026-10-30 09:11` (the form's old mono ISO) and this fails.
    const { firingWallTime } = await import('../Schedules')
    expect(firingWallTime('2026-10-30T09:11:00+00:00', 'Europe/London')).toBe('Fri, Oct 30 09:11')
    expect(firingWallTime('2026-10-30T09:11:00+00:00', 'UTC')).toBe('Fri, Oct 30 09:11Z')
    expect(firingWallTime('2026-11-01T23:30:00-03:00', 'America/Argentina/Buenos_Aires')).toBe('Sun, Nov 1 23:30')
  })

  it("adds the reader's time only when it is a different wall time", async () => {
    // MUTATION: return the reader's time whatever it is and the first case repeats the line.
    const { firingReaderTime } = await import('../Schedules')
    process.env.TZ = 'Europe/London'
    expect(firingReaderTime('2026-10-30T09:11:00+00:00')).toBeNull()
    process.env.TZ = 'America/New_York'
    expect(firingReaderTime('2026-10-30T09:11:00+00:00')).toBe('Fri, Oct 30 05:11')
    expect(firingReaderTime('2026-10-31T02:00:00+00:00')).toBe('Fri, Oct 30 22:00')
  })
})
