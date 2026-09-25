// #184: CPU IN DETAILS, AS PEAK AND MEAN CORES AGAINST THE LIMIT -- and the
// runner log named for what it is.
//
// THE DEFECT. Details drew `cpu not sampled` on every attempt -- one hatched
// row with a request and nothing beside it -- while the worker had measured
// cpu-seconds, peak cores and mean cores from cgroup `cpu.stat` all along.
// `attempts?include=usage` now serves each attempt's newest reading, and the
// owner's decision is a CPU bar in the style of memory and workspace, against
// the runtime's CPU limit.
//
// THE HONESTY RULES, per case below: a figure is drawn only when measured; a
// reading's provenance is in the `by` column in the memory row's words; the
// ceiling says where it came from (cgroup, else the class); over the limit is
// the track's over-ceiling hatch; and every kind of "no reading" is a distinct
// phrase on ONE hatched row, never a zero and never two identical rows.
//
// MUTATIONS: draw `peak_cpu_cores ?? 0`; take the class cpu over the cgroup
// limit; drop the over-ceiling excess; say `never measured` for a reading that
// was not served; put the "Output, as the agent wrote it" title back; age a
// live reading off `measured_at` on the browser clock instead of the server's
// `age_seconds`; carry the reading's age and kind ONLY in `.ctl-util-by`,
// which the sheet hides below 560px.

import STYLES from '../styles.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { AttemptRow } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const WAIT = { timeout: 5000 }

/**
 * An attempt row as `attempts?include=usage` serves it; `undefined` is a row
 * served without one, and `null` is the route's own `usage: null` (a row its
 * map did not cover), which is the same fact.
 */
function withUsage(usage: Record<string, unknown> | undefined | null, over: Partial<AttemptRow> = {}): AttemptRow {
  const row = attempt(1, over)
  if (usage === undefined) return row
  if (usage === null) return Object.assign(row, { usage: null })
  return Object.assign(row, {
    usage: {
      status: 'final',
      detail: null,
      event_id: 'ev_1',
      measured_at: new Date(Date.now() - 60_000).toISOString(),
      age_seconds: 60,
      final: true,
      cpu_seconds: 402.311,
      // 1.5 of 2 is exactly 75% in binary floating point; 1.62 is not.
      peak_cpu_cores: 1.5,
      mean_cpu_cores: 0.842,
      cpu_wall_seconds: 477.8,
      cpu_source: 'cgroup',
      cpu_limit_cores: 2,
      cpu_limit_source: 'cgroup',
      peak_rss_bytes: null,
      ...usage,
    },
  })
}

function agentRun(a: AttemptRow): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1 }),
    events: [],
    eventsDetail: null,
    attempts: [a],
    attemptsDetail: null,
    // cpu 4 in the catalogue, 2 in the cgroup: the two must be told apart.
    classes: { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function mount(a: AttemptRow): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={agentRun(a)} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull(), WAIT)
  return container as HTMLElement
}

/** Every `.ctl-util` row whose name's bold key is `cpu`. */
function cpuRows(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>('.ctl-util')].filter(
    (r) => (r.querySelector('.ctl-util-name b')?.textContent ?? '').trim() === 'cpu',
  )
}

function cpuRow(root: HTMLElement, which: 'peak' | 'mean'): HTMLElement {
  const r = cpuRows(root).find((x) => (x.querySelector('.ctl-util-name')?.textContent ?? '').includes(which))
  expect(r, `no cpu ${which} row`).toBeTruthy()
  return r!
}

const by = (r: HTMLElement) => r.querySelector('.ctl-util-by')?.textContent ?? ''
const figure = (r: HTMLElement) => r.querySelector('.ctl-util-figure')?.textContent ?? ''

describe('Details draws CPU as peak and mean cores of the limit', () => {
  it('draws two rows against the cgroup limit, at exit, with the cpu-seconds beside the mean', async () => {
    const root = await mount(withUsage({}))
    const peak = cpuRow(root, 'peak')
    const mean = cpuRow(root, 'mean')
    expect(figure(peak)).toBe('1.5 vCPU / 2 vCPU')
    expect(figure(mean)).toBe('0.84 vCPU / 2 vCPU')
    expect(peak.querySelector<HTMLElement>('.ctl-util-fill')?.style.width, 'the peak is not drawn against the cgroup limit').toBe('75%')
    expect(by(peak)).toBe('at exit · cgroup limit')
    expect(by(mean)).toBe('at exit · 402.3 cpu-s')
    expect(root.textContent, 'the retired row is still drawn').not.toMatch(/not sampled/)
  })

  it('falls back to the class’s cpu, and says so, when the worker could not read the cgroup limit', async () => {
    const root = await mount(withUsage({ cpu_limit_cores: null, cpu_limit_source: null }))
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 4 vCPU')
    expect(by(peak)).toBe('at exit · standard limit')
  })

  // #187/#188 PARITY. The worker sends a limit with `cpu_limit_source:
  // "resource_class"` whenever `cpu.max` says nothing -- the catalogue cpu of
  // the class the container was sized with -- and this row captioned every
  // limit the reading carried `cgroup limit`. MUTATION: label by
  // `cpu_limit_cores !== null` again.
  it('names a limit the worker took from the class as the class’s, never as the cgroup’s', async () => {
    const root = await mount(withUsage({ cpu_limit_cores: 4, cpu_limit_source: 'resource_class' }))
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 4 vCPU')
    expect(by(peak)).toBe('at exit · standard limit')
    expect(root.querySelector('.att-cpu-note')?.textContent, 'the phone strip still calls it the cgroup’s').not.toMatch(/cgroup/)
  })

  it('does not name a class whose cpu is not the limit the worker reported', async () => {
    // The worker sizes by the task's class when the catalogue still has it and
    // by the profile's otherwise, so the class read here (cpu 4) is not the
    // one behind a limit of 2.
    const root = await mount(withUsage({ cpu_limit_cores: 2, cpu_limit_source: 'resource_class' }))
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 2 vCPU')
    expect(by(peak)).toBe('at exit · resource class limit')
  })

  it('reads the route’s usage: null as not served, never as a crash or a zero', async () => {
    const root = await mount(withUsage(null))
    const rows = cpuRows(root)
    expect(rows).toHaveLength(1)
    expect(by(rows[0]!)).toBe('not served')
    expect(rows[0]!.querySelector('.ctl-util-fill'), 'no reading drew a fill').toBeNull()
  })

  it('draws a peak above the limit with the over-ceiling hatch rather than clipping it', async () => {
    const root = await mount(withUsage({ peak_cpu_cores: 2.5 }))
    const track = cpuRow(root, 'peak').querySelector('.ctl-util-track')
    expect(track?.querySelector('.ctl-util-fill.is-bad'), 'an over-limit peak is not a verdict').not.toBeNull()
    expect(track?.querySelector('.ctl-util-over'), 'the excess over the limit is not hatched').not.toBeNull()
  })

  it('says a live reading is the latest heartbeat, aged on the SERVER’s clock, and keeps a null mean an em dash', async () => {
    // THE TWO CLOCKS DISAGREE ON PURPOSE. The server read the heartbeat 20 s
    // after it was written; this browser's clock puts `measured_at` 140 s ago.
    // The contract's age is the server's (`age_seconds`), so a row that reads
    // `2m ago` is aging the reading off a clock the server never checked.
    const root = await mount(
      withUsage(
        { status: 'live', final: false, mean_cpu_cores: null, age_seconds: 20, measured_at: new Date(Date.now() - 140_000).toISOString() },
        { completed_at: null, exit_code: null },
      ),
    )
    expect(by(cpuRow(root, 'peak'))).toMatch(/^latest heartbeat 20s ago/)
    const mean = cpuRow(root, 'mean')
    expect(mean.querySelector('.ctl-util-figure .ctl-em'), 'an unmeasured mean is drawn as a figure').not.toBeNull()
    expect(mean.querySelector('.ctl-util-track')?.classList.contains('is-unknown')).toBe(true)
  })

  it.each([
    [undefined, 'not served'],
    [{ status: 'absent', peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }, 'never measured'],
    [{ status: 'final', peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }, 'never measured'],
    [{ status: 'never_ran', peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }, 'never ran'],
    [{ status: 'beyond_window', peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }, 'off the event window'],
    [{ status: 'unread', peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }, 'read failed'],
  ] as const)('draws no reading (%o) as one hatched row that says %s, and never a zero', async (usage, phrase) => {
    const root = await mount(withUsage(usage === undefined ? undefined : { ...usage }))
    const rows = cpuRows(root)
    expect(rows, 'two identical rows for one absence').toHaveLength(1)
    const r = rows[0]!
    expect(by(r)).toBe(phrase)
    expect(r.querySelector('.ctl-util-figure .ctl-em'), 'no reading is drawn as a figure').not.toBeNull()
    expect(r.querySelector('.ctl-util-track')?.classList.contains('is-unknown')).toBe(true)
    expect(r.querySelector('.ctl-util-fill'), 'no reading drew a fill').toBeNull()
  })
})

// ---------------------------------------------------------------------------
// At phone width
// ---------------------------------------------------------------------------

/**
 * Whether nothing from `el` up to `stop` is `display: none` at `env`, by the
 * shipped sheet's own cascade -- media conditions, specificity and order, as a
 * browser weighs them (cssgate.ts says why jsdom's cannot answer this).
 */
function shownAt(el: Element, env: CascadeEnv, stop: Element): boolean {
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    if (cascade(STYLES, node, 'display', env).winner?.value === 'none') return false
    if (node === stop) return true
  }
  return true
}

/** The innermost elements under `root` whose text matches: where the words actually sit. */
function carriers(root: HTMLElement, pattern: RegExp): HTMLElement[] {
  const all = [root, ...root.querySelectorAll<HTMLElement>('*')]
  const hits = all.filter((el) => pattern.test(el.textContent ?? ''))
  return hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)))
}

const PHONE: CascadeEnv = { width: 390 }
const DESK: CascadeEnv = { width: 1440 }
const RUNNING = { completed_at: null, exit_code: null } as const
/** Read 20 s after it was written, by the server; 140 s ago by this browser's clock. */
const SERVER_AGED = { age_seconds: 20, measured_at: new Date(Date.now() - 140_000).toISOString() }
const NOTHING = { peak_cpu_cores: null, mean_cpu_cores: null, cpu_seconds: null }

describe('the CPU reading reaches a phone, where the by column is hidden', () => {
  // THE DEFECT. `CpuRows` put the reading's age, its source, the cpu-seconds
  // and WHICH kind of "no reading" it is in `CeilingRow`'s `by` text and
  // nowhere else, and `@media (max-width: 560px)` sets `.ctl-util-by
  // { display: none }`. At 390 a live reading 140 s old read `cpu peak 1.62
  // vCPU / 2 vCPU` -- exactly what a final figure reads -- and every absence
  // read `— / 2 vCPU`. The memory row answers the same problem with a strip
  // outside the row (`.att-rss-note`); the CPU rows had none. jsdom applies no
  // stylesheet, so the `.ctl-util-by` text assertions above pass either way;
  // these ask the shipped sheet what a 390px viewport displays.
  it.each([
    ['a live reading', { status: 'live', final: false, ...SERVER_AGED }, RUNNING, [/live heartbeat 20s ago/, /402\.3 cpu-s/, /cgroup limit/]],
    ['a last reading', { status: 'last_reading', final: false, ...SERVER_AGED }, {}, [/last heartbeat 20s ago/, /402\.3 cpu-s/, /cgroup limit/]],
    ['a final reading', {}, {}, [/at exit/, /402\.3 cpu-s/, /cgroup limit/]],
    ['no reading served', undefined, {}, [/not served/]],
    ['an attempt that never ran', { status: 'never_ran', ...NOTHING }, {}, [/never ran/]],
    ['a reading off the event window', { status: 'beyond_window', ...NOTHING }, {}, [/off the event window/]],
    ['a failed read', { status: 'unread', ...NOTHING }, {}, [/read failed/]],
    ['a reading that was never taken', { status: 'absent', ...NOTHING }, {}, [/never measured/]],
  ] as const)('shows %s at 390 in a strip outside the row, in words', async (_case, usage, over, words) => {
    const root = await mount(withUsage(usage === undefined ? undefined : { ...usage }, { ...over }))
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip, 'the CPU reading has no strip outside its rows').not.toBeNull()
    expect(strip!.closest('.ctl-util'), 'the strip sits inside a util row, which a phone hides with its by column').toBeNull()
    expect(shownAt(strip!, PHONE, root), 'the CPU strip is hidden at 390').toBe(true)
    for (const w of words) {
      const at = carriers(strip!, w)
      expect(at.length, `the strip does not say ${w}`).toBeGreaterThan(0)
      expect(at.every((el) => shownAt(el, PHONE, root)), `${w} is in the strip but hidden at 390`).toBe(true)
    }

    // THE CONTROL: the by column really is hidden at 390, so the walk above
    // can answer false -- and at 1440 it is shown, so the words are on screen
    // at both widths.
    const byCell = cpuRows(root)[0]!.querySelector('.ctl-util-by')!
    expect(shownAt(byCell, PHONE, root), 'the by column shows at 390; this case no longer measures its defect').toBe(false)
    expect(shownAt(byCell, DESK, root), 'the by column is hidden at 1440').toBe(true)
  })

  it('names which reading the strip is about, beside the memory strip under it', async () => {
    const root = await mount(withUsage({ status: 'live', final: false, ...SERVER_AGED }, { ...RUNNING }))
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip?.querySelector('b')?.textContent, 'a strip of words under three bars does not say it is the CPU’s').toBe('cpu')
  })
})

describe('the runner log is named for what it is', () => {
  it('titles the Details log panel as the runner’s, not the agent’s', async () => {
    const root = await mount(withUsage({}))
    const titles = [...root.querySelectorAll('section > h2')].map((h) => h.textContent)
    expect(titles).toContain('Runner log (platform)')
    expect(titles).not.toContain('Output, as the agent wrote it')
  })
})
