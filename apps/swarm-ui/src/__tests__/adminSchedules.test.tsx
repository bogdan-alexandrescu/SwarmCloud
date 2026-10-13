// AUTOMATE › SCHEDULES, ALL TENANTS (docs/schedules.md §5.3, §6.1-§6.2,
// §8.4; lane S7, moved by SCHED-ONE-PAGE, owner decision 2026-10-13): every
// tenant's schedules and the schedule tick's last hour, on the one Schedules
// page at `?scope=all`. Admin › Schedules is gone; the switch, the redirect
// and the platform ticks' new home are schedules.onepage.test.tsx's.
//
// WHAT EACH CASE HOLDS:
//   * the platform ticks Admin › Platform counts lists are every Cloud
//     Scheduler job jobs.tf declares;
//   * one table with a tenant column draws `_admin_row`'s fields; a spend some
//     attempt never reported carries `Mark kind="partial"` and the count;
//   * a platform with no schedule in any tenant is the `Absent` state; a tick that reported nothing in its
//     window is said, not drawn as zeros;
//   * pause, disable and re-enable go to the admin routes with the reason;
//     pausing all of a tenant's is typed with its id;
//   * nothing on a row edits or approves the tenant's own decisions;
//   * at 390px a row is a record, every cell named.
//
// The bodies are `routes/schedules.py::_admin_row` and `_tick_health`'s
// shapes. Placeholders only; nothing is token-shaped.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

/**
 * `terraform/modules/scheduler/jobs.tf`'s Cloud Scheduler jobs, in file order:
 * the resource name, and the job's `name` after `${var.name_prefix}-`, with
 * `${each.key}` read as `<tenant>`.
 */
function jobsTf(): { name: string; job: string }[] {
  const tf = readFileSync(join(__dirname, '..', '..', '..', '..', 'terraform', 'modules', 'scheduler', 'jobs.tf'), 'utf8')
  const out: { name: string; job: string }[] = []
  const re = /^resource "google_cloud_scheduler_job" "([a-z_]+)" \{[\s\S]*?^\s*name\s*=\s*"\$\{var\.name_prefix\}-([^"]+)"/gm
  for (const m of tf.matchAll(re)) out.push({ name: m[1]!, job: m[2]!.replace('${each.key}', '<tenant>') })
  return out
}

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function row(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    tier: 'R2',
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    words: 'at 09:00, Monday through Friday',
    next_run_at: hoursFromNow(3),
    last_outcome: 'succeeded',
    last_firing_at: hoursFromNow(-20),
    spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    ...over,
  }
}

const TICK = { window_minutes: 60, ticks: 60, disabled: 0, errors: 0, truncated: 0, fired: 4, max_lateness_seconds: 12, last: null }

function routes(extra: Handler, rows: Json[] = [row('sch_a'), row('sch_b', { tenant_id: 'research', state: 'disabled', next_run_at: null })], tick: Json = TICK): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url.split('?')[0] === '/v1/admin/schedules') return { status: 200, body: { schedules: rows, next_page_token: null, tick } }
    return null
  }
}

async function mount(handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="scope=all" spy={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) =>
  calls.filter((c) => c.method === method && c.url.split('?')[0] === path)

const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)

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

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Automate › Schedules, All tenants', () => {
  it('draws every tenant’s schedules in one table with a tenant column, and the tick’s last hour', async () => {
    await mount(routes(() => null))
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
    expect(visible(rowOf('sch_a')!.querySelector('td[data-label="Tenant"]'))).toBe('eng')
    expect(visible(rowOf('sch_b')!.querySelector('td[data-label="Tenant"]'))).toBe('research')
    expect(visible(rowOf('sch_b')!.querySelector('td[data-label="State"]'))).toContain('disabled')
    expect(visible(screen.getByTestId('as-tick'))).toContain('60 ticks, 4 fired, 0 errors')
    // §5.3: an admin's view is not the tenant's identity -- no link into the
    // tenant's own page, and no approve or edit verb on a row.
    // MUTATION: link the name to `/automate/schedules/<id>`.
    expect(rowOf('sch_a')!.querySelector('a')).toBeNull()
    const verbs = [...rowOf('sch_a')!.querySelectorAll('button')].map((b) => b.textContent)
    expect(verbs).toEqual(['Pause', 'Disable'])
    expect([...rowOf('sch_b')!.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Re-enable'])
  })

  it('marks a spend some attempt never reported as partial, with the count', async () => {
    await mount(routes(() => null, [row('sch_a', { spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 2, coverage: 'partial' } })]))
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    const cell = rowOf('sch_a')!.querySelector('td[data-label="Spend today"]')!
    // MUTATION: draw the floor alone.
    expect(cell.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(cell)).toContain('2 attempts unreported')
  })

  it('is the Absent state when no tenant has a schedule, and says the tick reported nothing', async () => {
    await mount(routes(() => null, [], { ...TICK, ticks: 0, fired: 0, max_lateness_seconds: null }))
    await screen.findByText('No schedules in any tenant', undefined, WAIT)
    // MUTATION: draw "0 ticks, 0 fired" for a window with no report.
    expect(screen.getByText('No tick reported')).toBeTruthy()
  })

  it('ADM-6: lists every Cloud Scheduler job in jobs.tf, by resource and job name, and nothing else', async () => {
    const { PLATFORM_TICKS } = await import('../PlatformCounts')
    const declared = jobsTf()
    // A control: the parse found the jobs the live QA counted (12), less the
    // issue_sweep job deleted on 2026-10-12 (Schedules S9, second change), so
    // an empty or broken read cannot pass as agreement.
    expect(declared.length).toBeGreaterThanOrEqual(11)
    expect(declared.map((j) => j.name)).not.toContain('issue_sweep')
    expect(declared.map((j) => j.name)).toContain('schedule_tick')
    // MUTATION: drop any entry from PLATFORM_TICKS.
    expect(PLATFORM_TICKS.map((t) => ({ name: t.name, job: t.job }))).toEqual(declared)
  })

  it('pauses one schedule with its reason through the admin route, then reads the list again', async () => {
    const { calls } = await mount(
      routes((m, url) => (m === 'POST' && url === '/v1/admin/schedules/sch_a:pause' ? { status: 200, body: { schedule: row('sch_a', { state: 'paused' }) } } : null)),
    )
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    fireEvent.click(within(rowOf('sch_a')!).getByRole('button', { name: 'Pause' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'runaway spend' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/admin/schedules/sch_a:pause')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/admin/schedules/sch_a:pause')[0]!.body).toEqual({ reason: 'runaway spend' })
    await waitFor(() => expect(sent(calls, 'GET', '/v1/admin/schedules').length).toBe(2), WAIT)
  })

  it('says the server’s refusal in the dialog', async () => {
    await mount(
      routes((m, url) =>
        m === 'POST' && url === '/v1/admin/schedules/sch_b:enable' ? { status: 409, body: { code: 'schedule_not_disabled', message: 'this schedule is paused, not disabled' } } : null,
      ),
    )
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
    fireEvent.click(within(rowOf('sch_b')!).getByRole('button', { name: 'Re-enable' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Re-enable' }))
    await within(dialog).findByText(/Not done: this schedule is paused, not disabled/, undefined, WAIT)
  })

  it('pauses all of one tenant’s schedules only with its id typed', async () => {
    const { calls } = await mount(
      routes((m, url) => (m === 'POST' && url === '/v1/admin/tenants/eng/schedules:pause' ? { status: 200, body: { paused: 1 } } : null)),
    )
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    fireEvent.click(screen.getByRole('button', { name: /Pause all of eng’s schedules/ }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    const verb = within(dialog).getByRole('button', { name: 'Pause them all' }) as HTMLButtonElement
    expect(verb.disabled).toBe(true)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'eng' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause them all' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/admin/tenants/eng/schedules:pause')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/admin/tenants/eng/schedules:pause')[0]!.body).toEqual({ confirm: 'eng' })
  })

  it('is a record per schedule on a phone, every cell named', async () => {
    // MUTATION: drop the 560px block from schedules.css.
    media(true)
    await mount(routes(() => null))
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    expect(painted(document.querySelector<HTMLElement>('.sc-table.is-admin thead')!, 'display', PHONE)).toBe('none')
    const r = rowOf('sch_a')!
    expect(painted(r, 'display', PHONE)).toBe('block')
    for (const td of r.querySelectorAll<HTMLElement>('td')) {
      expect(td.getAttribute('data-label'), 'a cell with no name').toBeTruthy()
      expect(painted(td, 'display', PHONE)).toBe('block')
    }
    expect(painted(r.querySelector<HTMLElement>('.as-acts .c-btn')!, 'min-height', PHONE)).toBe('44px')
  })
})

// LIVE QA, AUTOMATE (2026-10-11), AUT-18: the admin row's name opens the
// schedule's own page when its tenant is the one the console works in.
describe('live QA: automate, admin schedules', () => {
  const me = { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'admin@example.com', domain: 'example.com', groups: [], is_admin: true } }

  it('links the name of a schedule in the working tenant, and says to switch tenant for another', async () => {
    const { go } = await mount(routes((m, url) => (m === 'GET' && url === '/v1/tenants/me' ? { status: 200, body: me } : null)))
    // MUTATION: draw every name as plain text again and there is no link.
    const link = await waitFor(() => {
      const a = rowOf('sch_a')!.querySelector<HTMLAnchorElement>('td[data-label="Name · type"] a')
      expect(a).not.toBeNull()
      return a!
    }, WAIT)
    expect(link.getAttribute('href')).toBe('/automate/schedules/sch_a')
    // Another tenant's schedule: its page would read as the working tenant and 404.
    expect(rowOf('sch_b')!.querySelector('td[data-label="Name · type"] a')).toBeNull()
    expect(rowOf('sch_b')!.querySelector('.sc-name')!.getAttribute('title')).toContain('switch the console to tenant research')
    // Last: the page is one route now, so the click opens the schedule's page.
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_a')
  })

  it('links nothing while the working tenant is not read', async () => {
    await mount(routes(() => null))
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    expect(rowOf('sch_a')!.querySelector('td[data-label="Name · type"] a')).toBeNull()
  })
})
