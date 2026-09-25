// THE TIMELINE AND THE TENANTS TABLE, AS BEHAVIOUR.
//
// Work > Timeline had no component test at all. What it draws is a window of
// rows -- bounded by COUNT, labelled by the span the rows turned out to cover
// -- and every defect the 2026-09-25 visual QA pass found on it (epic #84) was
// the screen saying something about that window that the window did not
// support: a time axis that skipped every empty hour, `08 PM` three times with
// no day, `Last 500 tasks` over a window that held all 170, `partial` over a
// table that held every row. The tenants table's credential tags (epic #86)
// ran together into one word.
//
// The api module is replaced with windows built HERE, in the viewer's LOCAL
// time -- `new Date(y, m, d, h, min)` -- because the chart buckets in the
// viewer's zone (`bucketStart`), and a fixture written in UTC would put its
// rows in a different hour on every machine that is not.
//
// Every case below was committed RED against the code it describes before the
// fix landed.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskState, TaskWindow, Tenant } from '../types'

const api = vi.hoisted(() => ({
  loadTaskWindow: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { ActivityScreen, TenantsScreen, axisLabels, nextBucket, outcomeBuckets } = await import('../Activity')

/** An instant in the VIEWER'S zone, 24 September 2026 unless said otherwise. */
const local = (hour: number, minute = 0, day = 24) => new Date(2026, 8, day, hour, minute).toISOString()

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'mock',
    resource_class: 'standard',
    provider: null,
    priority: 0,
    created_at: local(9),
    updated_at: local(9),
    started_at: null,
    completed_at: null,
    submitted_by: 'bogdan@saga.xyz',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

function windowOf(tasks: Task[], moreExist: boolean): TaskWindow {
  const times = tasks.map((t) => t.created_at).sort()
  return { tasks, moreExist, pages: 1, from: times[0] ?? null, to: times[times.length - 1] ?? null }
}

function serve(w: TaskWindow): void {
  api.loadTaskWindow.mockResolvedValue({ status: 'ok', data: w, fetchedAt: Date.now() } satisfies Result<TaskWindow>)
}

async function timeline(w: TaskWindow, bucket?: 'hour' | 'day'): Promise<HTMLElement> {
  serve(w)
  const { container } = render(<ActivityScreen />)
  await screen.findByText(/Outcomes by/)
  if (bucket !== undefined) {
    const group = [...container.querySelectorAll('label')].find((l) => l.textContent?.startsWith('Group by'))
    fireEvent.change(group!.querySelector('select')!, { target: { value: bucket } })
    await screen.findByText(`Outcomes by ${bucket}`)
  }
  return container as HTMLElement
}

/** The chart's columns, each as its task count, read off the title it carries. */
function totals(root: HTMLElement): number[] {
  return [...root.querySelectorAll('.chart .col')].map((c) => Number(/(\d+) tasks$/.exec(c.getAttribute('title') ?? '')?.[1]))
}

function section(root: HTMLElement, heading: string): HTMLElement {
  const h = [...root.querySelectorAll('section h2')].find((el) => el.textContent === heading)
  expect(h, `no ${heading} section`).toBeTruthy()
  return h!.closest('section') as HTMLElement
}

describe('the outcomes chart', () => {
  it('TS-1: draws every hour between the first and the last, and an empty one as zero', async () => {
    const root = await timeline(
      windowOf(
        [
          // Submitted at 08:10 and finished at 10:05. Its submission hour drew a
          // bar-less column of its own: `submitted` was counted there for an
          // overlay that was never drawn.
          task('a', 'SUCCEEDED', { created_at: local(8, 10), completed_at: local(10, 5) }),
          task('b', 'FAILED', { created_at: local(12, 50), completed_at: local(13, 20) }),
        ],
        false,
      ),
      'hour',
    )
    // 10, 11, 12 and 13 -- the idle hours drawn as the measured zeroes they
    // are, and nothing before the first outcome.
    expect(totals(root), 'the x-axis skips the empty hours, or starts at a submission').toEqual([1, 0, 0, 1])
    const first = root.querySelector('.chart .col')!.getAttribute('title')!
    expect(first.startsWith(new Date(2026, 8, 24, 10).toLocaleString())).toBe(true)
  })

  it('TS-2: carries the date at each day boundary when grouped by hour', async () => {
    const root = await timeline(
      windowOf(
        [
          task('late', 'SUCCEEDED', { created_at: local(22, 10, 23), completed_at: local(22, 30, 23) }),
          task('early', 'SUCCEEDED', { created_at: local(1, 10), completed_at: local(1, 30) }),
        ],
        false,
      ),
      'hour',
    )
    const labels = [...root.querySelectorAll('.chart .col-label')].map((l) => l.textContent)
    const date = (d: number) => new Date(2026, 8, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    // 22:00 on the 23rd, 23:00, then midnight on the 24th and 01:00.
    expect(labels).toHaveLength(4)
    expect(labels[0], 'the first column names no day').toBe(date(23))
    expect(labels[2], 'midnight is an hour with no date').toBe(date(24))
    expect(labels[3]).toBe(new Date(2026, 8, 24, 1).toLocaleTimeString(undefined, { hour: '2-digit' }))
  })

  it('TS-22: says which series is on which basis in the legend', async () => {
    const root = await timeline(windowOf([task('open', 'RUNNING', { created_at: local(9) })], false))
    const legend = root.querySelector('.chart-legend')!
    // Two bases, because two series are bucketed differently: the outcomes by
    // `completed_at`, and `still open` by `created_at` -- an open task has no
    // other time. The legend named one basis, after all four swatches.
    const bases = [...legend.querySelectorAll('.cl-basis')]
    expect(bases.map((b) => b.textContent), 'the legend puts `still open` under `completed_at`').toEqual([
      'by completed_at',
      'by created_at',
    ])
    // Each beside the swatches it qualifies: the outcomes, then the open key,
    // then its own basis.
    const after = (a: Element, b: Element) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
    const open = legend.querySelector('.k.open')!
    expect(after(legend.querySelector('.k.cancelled')!, bases[0]!)).toBe(true)
    expect(after(bases[0]!, open)).toBe(true)
    expect(after(open, bases[1]!)).toBe(true)
  })
})

describe('the axis arithmetic', () => {
  const at = (h: number, day = 24) => new Date(2026, 8, day, h).getTime()

  it('steps each bucket to the next one in local time', () => {
    expect(nextBucket(at(23, 23), 'hour')).toBe(at(0, 24))
    expect(nextBucket(at(0, 23), 'day')).toBe(at(0, 24))
    // 21 September 2026 is a Monday, the ISO week's first day.
    expect(nextBucket(new Date(2026, 8, 21).getTime(), 'week')).toBe(new Date(2026, 8, 28).getTime())
    expect(nextBucket(new Date(2026, 8, 1).getTime(), 'month')).toBe(new Date(2026, 9, 1).getTime())
  })

  it('makes no column from a submission alone, and fills every gap between outcomes with a zero', () => {
    const { buckets } = outcomeBuckets(
      [
        task('a', 'SUCCEEDED', { created_at: local(3), completed_at: local(10) }),
        task('b', 'CANCELLED', { created_at: local(4), completed_at: local(14) }),
      ],
      'hour',
    )
    expect(buckets.map(([k]) => new Date(k).getHours())).toEqual([10, 11, 12, 13, 14])
    expect(buckets.map(([, c]) => c.succeeded + c.failed + c.cancelled + c.open)).toEqual([1, 0, 0, 0, 1])
  })

  it('thins labels on a long hourly axis without ever dropping a day', () => {
    // Thirty hours from 10:00 on the 23rd: past the point where every column
    // can carry a label, and across one midnight.
    const keys = Array.from({ length: 30 }, (_, i) => at(10, 23) + i * 3_600_000)
    const labels = axisLabels(keys, 'hour')
    expect(labels).toHaveLength(30)
    const date = (d: number) => new Date(2026, 8, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    // The first column and the first of each day carry the date.
    expect(labels[0]).toBe(date(23))
    const midnight = keys.findIndex((k) => new Date(k).getDate() === 24)
    expect(labels[midnight]).toBe(date(24))
    // No two neighbours are both labelled, so none runs into the next.
    for (let i = 1; i < labels.length; i++) {
      expect(labels[i] !== '' && labels[i - 1] !== '', `columns ${i - 1} and ${i} are both labelled`).toBe(false)
    }
    // And the thinning still labels the axis: every other column at worst.
    expect(labels.filter((l) => l !== '').length).toBeGreaterThanOrEqual(13)
  })

  it('labels every column of a short axis', () => {
    const keys = [at(9), at(10), at(11)]
    expect(axisLabels(keys, 'hour').every((l) => l !== '')).toBe(true)
  })
})

describe('the window it describes', () => {
  it('TS-8: says "All N tasks" when nothing older exists, and "Last N" only when it was cut', async () => {
    // Two rows under the default budget of 500, and nothing older: this window
    // IS the tenant's history. It read `Last 500 tasks`.
    const all = await timeline(
      windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) }), task('b', 'SUCCEEDED', { completed_at: local(11) })], false),
    )
    expect(all.querySelector('.wb-span strong')!.textContent, 'the Rows budget printed over a complete window').toBe(
      'All 2 tasks',
    )
  })

  it('TS-8: says "Last N tasks" with the rows it read when older ones exist', async () => {
    const cut = await timeline(
      windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) }), task('b', 'SUCCEEDED', { completed_at: local(11) })], true),
    )
    expect(cut.querySelector('.wb-span strong')!.textContent).toBe('Last 2 tasks')
  })

  it('TS-10: marks the People table partial only when the window is', async () => {
    const whole = await timeline(windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) })], false))
    expect(
      section(whole, 'People').querySelector('.ctl-mark.is-partial'),
      '`partial` over a table that holds every task',
    ).toBeNull()
    expect(section(whole, 'People').querySelector('.ctl-panel-note')!.textContent).toContain('client-side')
  })

  it('TS-10: keeps the mark when older tasks were not read', async () => {
    const cut = await timeline(windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) })], true))
    expect(section(cut, 'People').querySelector('.ctl-mark.is-partial')).toBeTruthy()
  })
})

describe('the tenants table', () => {
  it('AH-20: wraps the credential tags so they do not run together', async () => {
    const tenant: Tenant = {
      tenant_id: 'eng',
      kind: 'group',
      principal: 'eng@saga.xyz',
      display_name: null,
      created_at: local(9),
      max_active: 10,
      capacity_units: 20,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic', 'openai'],
      service_account: 'swarm-eng@example.iam.gserviceaccount.com',
      gcs_prefix: null,
      namespace: null,
    }
    api.loadTenants.mockResolvedValue({
      status: 'ok',
      data: { tenants: [tenant] },
      fetchedAt: Date.now(),
    } satisfies Result<{ tenants: Tenant[] }>)
    const { container } = render(<TenantsScreen />)
    await screen.findByText('eng@saga.xyz')
    const cell = container.querySelector('td[data-label="Credentials"]')!
    expect(cell.querySelectorAll('.tags > .tag'), 'two tags with nothing spacing them').toHaveLength(2)
  })
})
