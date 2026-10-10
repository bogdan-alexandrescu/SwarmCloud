// AUTOMATE (docs/schedules.md §6, lane S7): Schedules, one schedule, the
// form, Approvals, Admin › Schedules and Overview's "Waiting on you" card.
//
// WHAT EACH CASE HOLDS (§9's S7 acceptance: each screen's empty state is
// `Absent`, a partial spend is `Mark kind="partial"`, and each has a 390px
// layout):
//   * the list draws a never-fired schedule as "never fired", not "0 runs",
//     and an unreported attempt as `$3.10 · 2 attempts unreported` beside a
//     partial mark; no schedules is the zero-kind `Absent`, with the built-in
//     index rows still drawn and linked to their Settings;
//   * at 390 every table row is a card and the inbox is one column;
//   * the gate card's SD3 switch posts to `:merge-mode` with the revision it
//     read, a `confirmation_required` refusal opens the typed confirm, and a
//     type whose floor forbids `merge: auto` is offered no switch;
//   * approve sends the digest shown; reject needs a reason;
//   * Admin › Schedules has no control that edits a tenant's gate;
//   * the Overview card draws nothing while nothing is waiting.
//
// Example data: example-org/example-*, operator@example.com.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ago, DAY, HOUR, repo, serve, visible } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

const WAIT = { timeout: 4000 }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

type Json = Record<string, unknown>

function schedule(over: Json = {}): Json {
  return {
    schedule_id: 'sch_0a1b2c3d4e5f',
    tenant_id: 'eng',
    name: 'Nightly sweep',
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_0a1b2c3d4e5f6071'] },
    cron: '0 2 * * *',
    timezone: 'Europe/London',
    params: {},
    gate: { run: 'auto', plan: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 5, per_day_usd: 20, max_concurrent: 2 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    next_run_at: new Date(Date.now() + 6 * HOUR).toISOString(),
    last_firing: null,
    revision: 4,
    words: 'every day at 02:00',
    tier: 'R2',
    spend_today: { day: '2026-10-10', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    ...over,
  }
}

const REPOS = { repositories: [repo()] }

async function mountSchedules(view: string | null, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  return { ...render(<SchedulesScreen view={view} go={go} />), go }
}

const sheets = () => ALL.map(([, t]) => t).join('\n')

describe('Automate › Schedules, the list', () => {
  it('draws never-fired and a partial spend as what they are, not as zeros', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/schedules') {
        return {
          status: 200,
          body: {
            tenant_id: 'eng',
            schedules: [
              schedule(),
              schedule({
                schedule_id: 'sch_111111111111',
                name: 'Weekday plans',
                type: 'issue-plan-only',
                last_firing: { firing_id: 'f1', outcome: 'succeeded', ended_at: ago(HOUR) },
                spend_today: { day: '2026-10-10', reported_usd: 3.1, unreported_attempts: 2, coverage: 'partial' },
                pending_approvals: 1,
              }),
            ],
          },
        }
      }
      if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
      return null
    })
    await mountSchedules(null)
    await waitFor(() => expect(document.querySelectorAll('.au-table .au-row')).toHaveLength(2), WAIT)
    const [first, second] = Array.from(document.querySelectorAll('.au-table .au-row'))
    expect(visible(first!.querySelector('[data-label="Last run"]'))).toBe('never fired')
    expect(visible(first!)).not.toMatch(/\b0 runs\b/)
    const spend = second!.querySelector('[data-label="Spend today"]')!
    expect(visible(spend)).toContain('$3.10 · 2 attempts unreported')
    const mark = spend.querySelector('.ctl-mark.is-partial')
    expect(mark, 'a partial spend carries the partial mark').not.toBeNull()
    expect(mark!.getAttribute('aria-label')).toMatch(/floor/)
    // A complete spend is the figure alone.
    expect(first!.querySelector('[data-label="Spend today"] .ctl-mark')).toBeNull()
    expect(visible(second!.querySelector('.au-pend'))).toBe('1 waiting')
    // The repository is named, not its id, once the registrations are read.
    await waitFor(() => expect(visible(first!.querySelector('[data-label="Repository"]'))).toBe('example-org/example-api'), WAIT)
  })

  it('says there are none with the zero-kind Absent, and still draws the built-in index rows', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/schedules') return { status: 200, body: { tenant_id: 'eng', schedules: [] } }
      if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
      return null
    })
    await mountSchedules(null)
    await waitFor(() => expect(screen.getByText('No schedules yet')).toBeTruthy(), WAIT)
    const empty = screen.getByText('No schedules yet').closest('.ctl-empty')!
    expect(empty.querySelector('.ctl-mark.is-zero')).not.toBeNull()
    await waitFor(() => expect(document.querySelectorAll('.au-builtin-row')).toHaveLength(1), WAIT)
    const row = document.querySelector('.au-builtin-row')!
    expect(visible(row)).toContain('Index · example-org/example-api · on change, at most every 30 min, and every 24 h')
    expect(row.querySelector('a')!.getAttribute('href')).toBe('/repositories/repo_0a1b2c3d4e5f6071/settings')
  })

  it('at 390 each row is a card that names its own fields', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/schedules') return { status: 200, body: { tenant_id: 'eng', schedules: [schedule()] } }
      if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
      return null
    })
    await mountSchedules(null)
    await waitFor(() => expect(document.querySelector('.au-table .au-row')).not.toBeNull(), WAIT)
    const head = document.querySelector('.au-table thead')!
    const cell = document.querySelector('.au-table td')!
    expect(cascade(sheets(), head, 'display', { width: 390 }).winner?.value).toBe('none')
    expect(cascade(sheets(), head, 'display', { width: 1280 }).winner?.value).not.toBe('none')
    expect(cascade(sheets(), cell, 'display', { width: 390 }).winner?.value).toBe('block')
    expect(cascade(sheets(), cell, 'content', { width: 390 }, 'before').winner?.value).toContain('attr(data-label)')
  })
})

describe('one schedule', () => {
  function detailRoutes(s: Json, extra: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null = () => null) {
    return serve((m, url, body) => {
      if (m === 'GET' && url === `/v1/schedules/${String(s.schedule_id)}`) return { status: 200, body: { schedule: s, firings: [] } }
      if (m === 'GET' && url === '/v1/schedule-types') {
        return {
          status: 200,
          body: {
            types: [
              { name: 'issue-sweep', description: 'Sweeps issues', default_gate: { merge: 'approve' }, floor_gate: { merge: 'auto' }, min_interval_minutes: 60, scopes: ['repos'], creatable_scopes: ['repos'], available: true, disabled_reason: null },
              { name: 'issue-plan-only', description: 'Plans issues', default_gate: { merge: 'off' }, floor_gate: { merge: 'off' }, min_interval_minutes: 60, scopes: ['repos'], creatable_scopes: ['repos'], available: true, disabled_reason: null },
            ],
          },
        }
      }
      return extra(m, url, body)
    })
  }

  it('says a schedule that has never fired with the zero-kind Absent, not "0 runs"', async () => {
    detailRoutes(schedule())
    await mountSchedules('schedule=sch_0a1b2c3d4e5f')
    await waitFor(() => expect(screen.getByText('This schedule has not fired')).toBeTruthy(), WAIT)
    expect(screen.getByText('This schedule has not fired').closest('.ctl-empty')!.querySelector('.ctl-mark.is-zero')).not.toBeNull()
  })

  it('switches merging to automatic through :merge-mode with the revision read, typed when the API asks', async () => {
    let asked = 0
    const calls = detailRoutes(schedule({ tenant_id: 'u-operator' }), (m, url, body) => {
      if (m === 'POST' && url === '/v1/schedules/sch_0a1b2c3d4e5f:merge-mode') {
        asked += 1
        const b = body as Json
        if (b.confirm === undefined) return { status: 422, body: { code: 'confirmation_required', message: "type the schedule's name to switch merging to automatic" } }
        return { status: 200, body: { schedule: schedule({ tenant_id: 'u-operator', gate: { run: 'auto', plan: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 }, revision: 5 }) } }
      }
      return null
    })
    await mountSchedules('schedule=sch_0a1b2c3d4e5f&tab=gate')
    const button = await screen.findByRole('button', { name: 'Switch to fully automatic' }, WAIT)
    // A one-person workspace is told why there is no second person.
    expect(visible(document.querySelector('.au-switch'))).toContain('only member of this workspace')
    expect(visible(document.querySelector('.au-switch'))).toContain('.github/workflows/')
    fireEvent.click(button)
    const field = await screen.findByLabelText(/to confirm/, {}, WAIT)
    fireEvent.change(field, { target: { value: 'Nightly sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(asked).toBe(2), WAIT)
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts.map((c) => c.body)).toEqual([
      { mode: 'auto', revision: 4 },
      { mode: 'auto', revision: 4, confirm: 'Nightly sweep' },
    ])
    // Never through the edit, which refuses `merge: auto`.
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })

  it('offers no switch to a type whose floor never merges unattended', async () => {
    detailRoutes(schedule({ type: 'issue-plan-only', gate: { run: 'auto', plan: 'approve', merge: 'off', approvers: 'members', approval_ttl_hours: 72 } }))
    await mountSchedules('schedule=sch_0a1b2c3d4e5f&tab=gate')
    await waitFor(() => expect(visible(document.querySelector('.au-switch'))).toContain('never merges unattended'), WAIT)
    expect(screen.queryByRole('button', { name: 'Switch to fully automatic' })).toBeNull()
  })

  it('says a refusal on the card, in the API’s words', async () => {
    detailRoutes(schedule(), (m, url) =>
      m === 'POST' && url === '/v1/schedules/sch_0a1b2c3d4e5f:merge-mode'
        ? { status: 403, body: { code: 'second_person_required', message: 'another member must' } }
        : null,
    )
    await mountSchedules('schedule=sch_0a1b2c3d4e5f&tab=gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Switch to fully automatic' }, WAIT))
    await waitFor(() => expect(visible(document.querySelector('.au-switch'))).toContain('second_person_required'), WAIT)
  })
})

describe('the form', () => {
  it('previews from the tick’s parser and creates by type name, with no merge: auto on offer', async () => {
    const calls = serve((m, url, body) => {
      if (m === 'GET' && url === '/v1/schedule-types') {
        return { status: 200, body: { types: [{ name: 'observer', description: 'Reports', default_gate: { run: 'auto', plan: 'auto', merge: 'off' }, floor_gate: {}, min_interval_minutes: 60, scopes: ['repos', 'all'], creatable_scopes: ['repos', 'all'], available: true, disabled_reason: null }] } }
      }
      if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
      if (m === 'POST' && url === '/v1/schedules:preview') {
        const b = body as Json
        return { status: 200, body: { preview: { words: `words for ${String(b.cron)}`, next: [new Date(Date.now() + DAY).toISOString()], min_gap_minutes: 1440, refusal: null } } }
      }
      if (m === 'POST' && url === '/v1/schedules') return { status: 201, body: { schedule: schedule({ schedule_id: 'sch_222222222222', type: 'observer' }), created: true } }
      return null
    })
    const { go } = await mountSchedules('page=new')
    await screen.findByText('Choose a type', {}, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Nightly' }))
    await waitFor(() => expect(screen.getByText('words for 0 2 * * *')).toBeTruthy(), WAIT)
    const merge = screen.getByLabelText('Merge') as HTMLSelectElement
    expect(Array.from(merge.options).map((o) => o.value)).not.toContain('auto')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Weekly report' } })
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'observer' } })
    fireEvent.click(screen.getByLabelText(/All, as registered/))
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_222222222222'), WAIT)
    const create = calls.find((c) => c.method === 'POST' && c.url === '/v1/schedules')!.body as Json
    expect(create.type).toBe('observer')
    expect(create.scope).toEqual({ mode: 'all' })
    expect(create.cron).toBe('0 2 * * *')
    // Invariant 10: a type by name, never an image, a command or a profile.
    for (const k of ['image', 'command', 'runner_profile', 'resources']) expect(create).not.toHaveProperty(k)
  })
})

describe('Automate › Approvals', () => {
  async function mountInbox(view: string | null, go = vi.fn()) {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ApprovalsScreen } = await import('../Approvals')
    return { ...render(<ApprovalsScreen view={view} go={go} />), go }
  }
  const ITEM = {
    approval_id: 'apr_0a1b2c3d',
    tenant_id: 'eng',
    kind: 'merge',
    subject: { schedule_id: 'sch_0a1b2c3d4e5f', run_id: 'run_1', pr: 42 },
    digest: { head_sha: 'abc1234', verdict: 'MERGE' },
    summary: 'example-org/example-api#42',
    approvers: 'members',
    state: 'pending',
    requested_at: ago(2 * HOUR),
    expires_at: new Date(Date.now() + 2 * DAY).toISOString(),
  }

  it('says nothing is waiting with the zero-kind Absent', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/approvals' ? { status: 200, body: { tenant_id: 'eng', approvals: [] } } : null))
    await mountInbox(null)
    await waitFor(() => expect(screen.getByText('Nothing is waiting on you')).toBeTruthy(), WAIT)
    expect(screen.getByText('Nothing is waiting on you').closest('.ctl-empty')!.querySelector('.ctl-mark.is-zero')).not.toBeNull()
  })

  it('approves the digest shown, and a rejection needs a reason', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/approvals') return { status: 200, body: { tenant_id: 'eng', approvals: [ITEM] } }
      if (m === 'GET' && url === '/v1/approvals/apr_0a1b2c3d') return { status: 200, body: { approval: ITEM, green_sha: 'abc1234', verdict: 'MERGE', protected_files: [] } }
      if (m === 'POST' && url === '/v1/approvals/apr_0a1b2c3d:approve') return { status: 200, body: { approval: { ...ITEM, state: 'approved' } } }
      return null
    })
    await mountInbox('item=apr_0a1b2c3d')
    await waitFor(() => expect(document.querySelector('.au-decide')).not.toBeNull(), WAIT)
    const reject = screen.getByRole('button', { name: 'Reject' }) as HTMLButtonElement
    expect(reject.disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/approvals/apr_0a1b2c3d:approve')).toBe(true), WAIT)
    expect(calls.find((c) => c.url === '/v1/approvals/apr_0a1b2c3d:approve')!.body).toEqual({ digest: ITEM.digest })
  })

  it('asks a one-person workspace to type the text the API names, and sends exactly that', async () => {
    const held = { ...ITEM, approval_id: 'run:run_9', kind: 'hold', subject: { run_id: 'run_9' }, digest: 'd9', hold: { code: 'NEEDS_SECOND_MEMBER', approvers: 'second_member' } }
    const expected = 'terraform/bootstrap/main.tf'
    const calls = serve((m, url, body) => {
      if (m === 'GET' && url === '/v1/approvals') return { status: 200, body: { tenant_id: 'u-operator', approvals: [held] } }
      if (m === 'GET' && url === '/v1/approvals/run%3Arun_9') return { status: 200, body: { approval: held } }
      if (m === 'POST' && url === '/v1/approvals/run%3Arun_9:approve') {
        return (body as Json).confirm === expected
          ? { status: 200, body: { approval: { ...held, state: 'approved' } } }
          : { status: 403, body: { code: 'hold_approver_required', message: 'held', detail: { confirm: expected } } }
      }
      return null
    })
    await mountInbox('item=run%3Arun_9')
    await waitFor(() => expect(document.querySelector('.au-decide')).not.toBeNull(), WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    const field = await screen.findByLabelText(/to confirm/, {}, WAIT)
    fireEvent.change(field, { target: { value: expected } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' }).at(-1)!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(2), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.body)).toEqual([{ digest: 'd9' }, { digest: 'd9', confirm: expected }])
  })

  it('is split list and detail on a desktop and one column at 390', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/approvals') return { status: 200, body: { tenant_id: 'eng', approvals: [ITEM] } }
      return null
    })
    await mountInbox(null)
    await waitFor(() => expect(document.querySelector('.au-inbox')).not.toBeNull(), WAIT)
    const inbox = document.querySelector('.au-inbox')!
    expect(cascade(sheets(), inbox, 'grid-template-columns', { width: 1280 }).winner?.value).toBe('minmax(260px, 2fr) minmax(0, 3fr)')
    expect(cascade(sheets(), inbox, 'grid-template-columns', { width: 390 }).winner?.value).toBe('minmax(0, 1fr)')
  })
})

describe('Admin › Schedules', () => {
  async function mountAdmin() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AdminSchedulesScreen } = await import('../AdminSchedules')
    return render(<AdminSchedulesScreen />)
  }
  const row = (over: Json = {}): Json => ({
    schedule_id: 'sch_0a1b2c3d4e5f', tenant_id: 'eng', name: 'Nightly sweep', type: 'issue-sweep', tier: 'R2', cron: '0 2 * * *',
    timezone: 'UTC', words: 'every day at 02:00', next_run_at: null, last_outcome: null, last_firing_at: null,
    spend_today: { day: '2026-10-10', reported_usd: 1.5, unreported_attempts: 1, coverage: 'partial' }, pending_approvals: 0,
    state: 'enabled', pause: null, owner: 'operator@example.com', ...over,
  })

  it('lists across tenants with a partial mark, and has no control over a tenant’s gate', async () => {
    serve((m, url) =>
      m === 'GET' && url === '/v1/admin/schedules'
        ? { status: 200, body: { schedules: [row(), row({ schedule_id: 'sch_2', tenant_id: 'u-operator', state: 'disabled' })], next_page_token: null, tick: { window_minutes: 60, ticks: 60, disabled: 0, errors: 0, truncated: 0, fired: 3, max_lateness_seconds: 4.2, last: null } } }
        : null,
    )
    await mountAdmin()
    await waitFor(() => expect(document.querySelectorAll('.au-table .au-row')).toHaveLength(2), WAIT)
    const first = document.querySelector('.au-table .au-row')!
    expect(first.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(first.querySelector('[data-label="Last outcome"]'))).toBe('never fired')
    const buttons = Array.from(document.querySelectorAll('.au-table button')).map((b) => visible(b))
    expect(new Set(buttons)).toEqual(new Set(['Pause', 'Disable', 'Re-enable']))
    expect(document.body.textContent).not.toMatch(/Switch to fully automatic|Approve\b/)
  })

  it('says no tenant has one with the zero-kind Absent', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/admin/schedules' ? { status: 200, body: { schedules: [], next_page_token: null, tick: null } } : null))
    await mountAdmin()
    await waitFor(() => expect(screen.getByText('No tenant has a schedule')).toBeTruthy(), WAIT)
  })
})

describe('Overview’s "Waiting on you" card', () => {
  async function mountCard() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WaitingOnYouCard } = await import('../Approvals')
    return render(<WaitingOnYouCard />)
  }

  it('draws nothing while nothing is waiting', async () => {
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/approvals' ? { status: 200, body: { tenant_id: 'eng', approvals: [] } } : null))
    await mountCard()
    await waitFor(() => expect(calls.length).toBe(1), WAIT)
    expect(document.querySelector('.au-waiting')).toBeNull()
  })

  it('appears with the count and a way to the inbox when something is', async () => {
    serve((m, url) =>
      m === 'GET' && url === '/v1/approvals'
        ? { status: 200, body: { tenant_id: 'eng', approvals: [{ approval_id: 'run:run_1', tenant_id: 'eng', kind: 'plan', subject: { run_id: 'run_1' }, digest: 'd', summary: 'example-org/example-api#7: plan', approvers: 'members', state: 'pending', requested_at: ago(HOUR), expires_at: null, projected: true }] } }
        : null,
    )
    await mountCard()
    await waitFor(() => expect(document.querySelector('.au-waiting')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.au-waiting'))).toContain('1 item waiting')
    expect(screen.getByText('Open approvals').getAttribute('href')).toBe('/approvals')
  })
})
