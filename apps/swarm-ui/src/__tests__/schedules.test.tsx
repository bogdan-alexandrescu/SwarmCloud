// AUTOMATE › SCHEDULES (docs/schedules.md §6, lane S7): the section, the list,
// one schedule's page, the section's badge and Overview's "Waiting on you".
//
// WHAT EACH CASE HOLDS (§9's S7 acceptance, for the screens this lane draws):
//   * `automate` is a literal section between Work and Capacity, its one tab
//     routes to the list, and `/automate/schedules/<id>` to one schedule;
//   * the list draws the columns the owner listed; a spend some attempt never
//     reported carries `Mark kind="partial"` and the count unreported, never
//     the floor alone; a tenant with no schedule gets the `Absent` state, not a
//     table of nothing; each registration's index cadence is a read-only
//     built-in row linking to its Settings;
//   * a schedule that never fired shows `Absent`, not "0 runs"; its history
//     links each firing's work; the audit tab reads the audit route;
//   * at 390px a row is a record, the header hidden;
//   * the spine's Automate badge counts the inbox, draws nothing for a
//     measured zero, and a dash -- never 0 -- for a failed read;
//   * the Overview card appears only when something waits.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s and
// `approvals.to_api`'s shapes. Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { SECTIONS, fromAddress, canonical, spineOf } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES, approvalsBadge } from '../Spine'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

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
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: { firing_id: `${id}:1`, slot: hoursFromNow(-21), outcome: 'succeeded', ended_at: hoursFromNow(-20) },
    consecutive_failures: 0,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
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

function firing(id: string, over: Json = {}): Json {
  return {
    firing_id: id,
    schedule_id: 'sch_a',
    type: 'issue-sweep',
    slot: hoursFromNow(-21),
    fired_at: hoursFromNow(-21),
    trigger: 'cron',
    state: 'done',
    work: [{ kind: 'issue_run', id: 'run_8f2a', repo_id: 'repo_a' }],
    skip: null,
    dry_run: null,
    approval_id: null,
    outcome: 'succeeded',
    cost: { reported_usd: 11.3, unreported_attempts: 0 },
    ended_at: hoursFromNow(-20),
    ...over,
  }
}

type Routes = Record<string, { status: number; body: unknown }>

async function mountList(routes: Routes) {
  const calls = serve((m, url) => (m === 'GET' ? routes[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<SchedulesScreen view={null} go={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

async function mountDetail(routes: Routes) {
  const calls = serve((m, url) => (m === 'GET' ? routes[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<SchedulesScreen view="schedule=sch_a" go={go} />)
  return { ...utils, calls, go }
}

/** Stub `matchMedia`, which jsdom does not have. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Automate section', () => {
  it('is a literal section between Work and Capacity, with Schedules as its tab', () => {
    const ids = SECTIONS.map((s) => s.id)
    expect(ids.indexOf('automate')).toBe(ids.indexOf('work') + 1)
    expect(ids.indexOf('capacity')).toBe(ids.indexOf('automate') + 1)
    const automate = SECTIONS.find((s) => s.id === 'automate')!
    expect(automate.label).toBe('Automate')
    expect(automate.question.trim().endsWith('?')).toBe(true)
    expect(automate.tabs).toEqual([{ id: 'schedules', label: 'Schedules' }])
    expect(PANEL_PAGES.automate.map((p) => p.to)).toEqual(['automate/schedules'])
  })

  it('routes the list and one schedule, both ways', () => {
    expect(spineOf('automate', 'schedules')).toBe('automate')
    expect(addressToPath('automate/schedules')).toBe('/automate/schedules')
    expect(pathToAddress('/automate')?.address).toBe('automate/schedules')
    // MUTATION: drop `schedule` from keptKeys and the id is lost on the way in.
    const one = fromAddress('automate/schedules?schedule=sch_a')
    expect(one).toMatchObject({ sectionId: 'automate', tab: 'schedules', view: 'schedule=sch_a' })
    expect(canonical(one)).toBe('automate/schedules?schedule=sch_a')
    expect(addressToPath(canonical(one))).toBe('/automate/schedules/sch_a')
    expect(pathToAddress('/automate/schedules/sch_a')?.address).toBe('automate/schedules?schedule=sch_a')
  })
})

describe('the list', () => {
  it('draws one row per schedule with the columns the owner listed', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a', { pending_approvals: 2 })], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    const heads = [...document.querySelectorAll('.sc-table:not(.is-builtin) thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['Name · type', 'Repository', 'When', 'Next run', 'Last run', 'Spend today', 'Approvals', 'State'])
    const row = rowOf('sch_a')!
    expect(visible(row)).toContain('Schedule sch_a')
    expect(visible(row)).toContain('issue-sweep · R2')
    expect(visible(row)).toContain('at 09:00, Monday through Friday')
    expect(visible(row)).toContain('Europe/London')
    expect(visible(row)).toContain('$3.10')
    expect(visible(row)).toContain('2 waiting')
    expect(visible(row)).toContain('enabled')
    await waitFor(() => expect(visible(row)).toContain('example-org/example-api'), WAIT)
  })

  it('marks a spend some attempt never reported as partial, with the count', async () => {
    // MUTATION: draw `reported_usd` alone when coverage is partial.
    await mountList({
      '/v1/schedules': {
        status: 200,
        body: { schedules: [schedule('sch_a', { spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 2, coverage: 'partial' } })], tenant_id: 'eng' },
      },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    const cell = rowOf('sch_a')!.querySelector('td[data-label="Spend today"]')!
    expect(cell.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(cell)).toContain('$3.10')
    expect(visible(cell)).toContain('2 attempts unreported')
  })

  it('says a never-fired, paused schedule has no last run and no next run, rather than 0', async () => {
    await mountList({
      '/v1/schedules': {
        status: 200,
        body: {
          schedules: [schedule('sch_b', { state: 'paused', next_run_at: null, last_firing: null, pause: { by: 'a@example.com', reason: 'holiday' } })],
          tenant_id: 'eng',
        },
      },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
    const row = rowOf('sch_b')!
    expect(visible(row.querySelector('td[data-label="Last run"]'))).toBe('never fired')
    expect(visible(row.querySelector('td[data-label="Next run"]'))).toContain('paused')
    expect(visible(row.querySelector('td[data-label="State"]'))).toContain('holiday')
  })

  it('draws the Absent state for a tenant with no schedule, and still the built-in rows', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    await screen.findByText('No schedules yet', undefined, WAIT)
    expect(document.querySelector('.sc-table:not(.is-builtin)')).toBeNull()
    const builtIn = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('tr[data-builtin="repo_a"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(builtIn)).toContain('Index · example-org/example-api')
    expect(visible(builtIn)).toContain('24 h + on change')
    expect(visible(builtIn)).toContain('at most every 30 min')
    expect(within(builtIn).getByRole('link', { name: /Settings/ }).getAttribute('href')).toBe('/repositories/repo_a/settings')
  })

  it('says the built-in rows were not read when the repositories read fails', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
      '/v1/repositories': { status: 500, body: { code: 'internal', message: 'boom' } },
    })
    await screen.findByText('Built-in rows not read', undefined, WAIT)
  })

  it('opens a schedule by its address', async () => {
    const { go } = await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    const link = await screen.findByRole('link', { name: 'Schedule sch_a' }, WAIT)
    expect(link.getAttribute('href')).toBe('/automate/schedules/sch_a')
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_a')
  })

  it('is a record per schedule on a phone, every cell named', async () => {
    // MUTATION: drop the 560px block from schedules.css.
    media(true)
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    expect(painted(document.querySelector<HTMLElement>('.sc-table thead')!, 'display', PHONE)).toBe('none')
    const row = rowOf('sch_a')!
    expect(painted(row, 'display', PHONE)).toBe('block')
    for (const td of row.querySelectorAll<HTMLElement>('td')) {
      expect(td.getAttribute('data-label'), 'a cell with no name').toBeTruthy()
      expect(painted(td, 'display', PHONE)).toBe('block')
    }
  })
})

describe('one schedule', () => {
  it('shows Absent for a schedule that never fired, not "0 runs"', async () => {
    await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a', { last_firing: null }), firings: [] } },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    await screen.findByText('Never fired', undefined, WAIT)
    expect(document.body.textContent).not.toMatch(/\b0 runs\b/)
  })

  it('lists its firings, newest first, linking the work each made', async () => {
    const { go } = await mountDetail({
      '/v1/schedules/sch_a': {
        status: 200,
        body: {
          schedule: schedule('sch_a'),
          firings: [
            firing('sch_a:2', { cost: { reported_usd: 9.8, unreported_attempts: 1 } }),
            firing('sch_a:1', { work: [], state: 'done', outcome: 'skipped', skip: { code: 'OVERLAP' }, cost: null }),
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    const rows = document.querySelectorAll<HTMLElement>('tr[data-firing]')
    expect([...rows].map((r) => r.dataset.firing)).toEqual(['sch_a:2', 'sch_a:1'])
    const run = within(rows[0]!).getByRole('link', { name: 'run_8f2a' })
    expect(run.getAttribute('href')).toBe('/runs/run_8f2a')
    fireEvent.click(run)
    expect(go).toHaveBeenCalledWith('work/runs?run=run_8f2a')
    const cost = rows[0]!.querySelector('td[data-label="Cost"]')!
    expect(cost.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(cost)).toContain('1 attempt unreported')
    expect(visible(rows[1]!)).toContain('OVERLAP')
  })

  it('reads the audit when its tab is opened, newest first as served', async () => {
    const { calls } = await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } },
      '/v1/schedules/sch_a/audit': {
        status: 200,
        body: {
          audit: [
            { schedule_id: 'sch_a', action: 'pause', by: 'operator@example.com', at: hoursFromNow(-1), detail: { reason: 'holiday' } },
            { schedule_id: 'sch_a', action: 'create', by: 'operator@example.com', at: hoursFromNow(-40), detail: {} },
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    expect(calls.some((c) => c.url.startsWith('/v1/schedules/sch_a/audit'))).toBe(false)
    fireEvent.click(screen.getByRole('tab', { name: /Audit/ }))
    await waitFor(() => expect(document.querySelectorAll('li[data-audit]')).toHaveLength(2), WAIT)
    const entries = [...document.querySelectorAll<HTMLElement>('li[data-audit]')]
    expect(entries.map((e) => e.dataset.audit)).toEqual(['pause', 'create'])
    expect(visible(entries[0]!)).toContain('holiday')
  })

  it('shows the gate and the budget as read, with the hard stops beside them', async () => {
    await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('tab', { name: /Gate/ }))
    const gate = await screen.findByTestId('sc-gate', undefined, WAIT)
    const facts = [...gate.querySelectorAll('.sc-fact')].map((f) => `${visible(f.querySelector('dt'))}: ${visible(f.querySelector('dd'))}`)
    expect(facts).toContain('Merge: approve')
    expect(facts).toContain('Expiry: 72 h')
    expect(visible(gate)).toContain('.github/workflows/')
    fireEvent.click(screen.getByRole('tab', { name: /Budget/ }))
    const budget = await screen.findByTestId('sc-budget', undefined, WAIT)
    expect(visible(budget)).toContain('$15.00')
    expect(visible(budget)).toContain('$120.00')
  })
})

describe('the Automate badge', () => {
  const ok = (n: number) => ({ status: 'ok' as const, fetchedAt: 0, data: { approvals: Array.from({ length: n }, () => ({})) } })

  it('counts the inbox, draws nothing for none, and a dash for a failed read', () => {
    expect(approvalsBadge(ok(4))).toMatchObject({ n: 4, alert: true })
    expect(approvalsBadge({ status: 'empty', fetchedAt: 0 })).toBeNull()
    expect(approvalsBadge({ status: 'loading', since: 0 })).toBeNull()
    // MUTATION: map a failure to `{ n: 0 }`.
    const failed = approvalsBadge({ status: 'error', error: { kind: 'server', message: 'boom', httpStatus: 500, code: null } as never })
    expect(failed?.n).toBeNull()
    expect(failed?.why).toMatch(/not read/)
  })

  it('is drawn on the spine beside Automate', async () => {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/approvals') ? { status: 200, body: { approvals: [{ approval_id: 'a1' }, { approval_id: 'a2' }] } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="automate" tab="schedules" title="Schedules" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const link = document.querySelector<HTMLElement>('.sk-spine a[data-sec="automate"]')!
    await waitFor(() => expect(link.querySelector('.sk-badge')?.textContent).toBe('2'), WAIT)
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('Schedules')
  })
})

describe('Waiting on you', () => {
  async function mountCard(body: unknown, status = 200) {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/approvals') ? { status, body } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WaitingOnYouCard } = await import('../Schedules')
    const go = vi.fn()
    const utils = render(<WaitingOnYouCard go={go} />)
    return { ...utils, go }
  }

  it('draws nothing when nothing waits', async () => {
    const { container } = await mountCard({ approvals: [] })
    await new Promise((r) => setTimeout(r, 50))
    expect(container.innerHTML).toBe('')
  })

  it('lists what waits, each linking to its subject', async () => {
    const { go } = await mountCard({
      approvals: [
        { approval_id: 'run:run_1', kind: 'plan', subject: { run_id: 'run_1', issue: 'example-api#12' }, summary: 'example-api#12: fix it', state: 'pending', requested_at: hoursFromNow(-2), expires_at: null, projected: true },
        { approval_id: 'merge:x', kind: 'merge', subject: { run_id: 'run_2', schedule_id: 'sch_a' }, summary: 'merge run_2', state: 'pending', requested_at: hoursFromNow(-1), expires_at: hoursFromNow(70) },
        { approval_id: 'run:sch_a:9', kind: 'run', subject: { schedule_id: 'sch_a', firing_id: 'sch_a:9' }, summary: 'Weekday sweep', state: 'pending', requested_at: hoursFromNow(-1), expires_at: null },
      ],
    })
    const card = await screen.findByRole('region', { name: /Waiting on you/ }, WAIT)
    expect(visible(card)).toContain('3')
    expect(visible(card)).toContain('hold no capacity')
    const links = within(card).getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/runs/run_1', '/runs/run_2', '/automate/schedules/sch_a'])
    fireEvent.click(links[2]!)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_a')
  })
})
