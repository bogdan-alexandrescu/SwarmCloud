// #184: CPU IN DETAILS, AS PEAK AND MEAN CORES AGAINST THE LIMIT -- read off
// the attempt's own typed fields (contract request #15).
//
// THE HISTORY. Details drew `cpu not sampled` on every attempt while the
// worker measured cpu-seconds, peak cores and mean cores all along. #188 put
// the figures on HEARTBEAT events and `attempts?include=usage` read them back,
// because the frozen `Attempt` had nowhere to put them. The owner ACCEPTED
// request #15 on #184 (2026-09-25): `Attempt` now carries `cpu_seconds`,
// `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`, served on every
// attempt row, and they replace the interim path. Details keeps two rows,
// peak and mean, "as built".
//
// WHAT A ROW CAN SAY, now that the reading is the attempt document itself:
//
//   at exit          the attempt's finish is recorded, so its runner was
//                    reaped and wrote its figures first;
//   live reading     the attempt is running; the worker rewrites the figures
//                    with each periodic reading. Since request #26 the API
//                    serves the reading's age, and the strip says it; an
//                    attempt from before #26 records no time, so no age is
//                    claimed -- never a browser-clock guess (the section at
//                    the end);
//   last written     the attempt ended without a recorded finish (a kill, a
//                    reclaim), so these are the figures it last wrote;
//   heartbeat event  the attempt's typed fields are empty and a HEARTBEAT of
//                    it on this page carries a figure -- the legacy reader.
//                    From the #188 window, peak, mean and cpu-seconds; from
//                    before #188, the cpu-seconds only, and the rows draw the
//                    cores as em dashes (`cpu-seconds only` in the strip);
//   not served       an API older than the typed fields sends no key at all;
//   never ran / not yet written / never measured -- three kinds of nothing,
//                    one hatched row each, never a zero.
//
// MUTATIONS: draw `peak_cpu_cores ?? 0`; take the class cpu over the worker's
// limit; drop the over-ceiling excess; say `never measured` for a row the API
// did not serve; read the interim `usage` block again; claim an age for a live
// reading; carry the reading's kind ONLY in `.ctl-util-by`, which the sheet
// hides below 560px; take only a HEARTBEAT with `peak_cpu_cores` as a reading,
// so a pre-#188 attempt's cpu-seconds read `never measured`.

import STYLES from '../styles.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { EVENT_PAGE_LIMIT, type AgentRun } from '../api'
import type { AttemptRow, Task, TaskEvent } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { attempt, ev, task } from './runfixture'

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

/** The four typed fields as the attempts route serves them. */
const MEASURED = {
  cpu_seconds: 402.311,
  // 1.5 of 2 is exactly 75% in binary floating point; 1.62 is not.
  peak_cpu_cores: 1.5,
  mean_cpu_cores: 0.842,
  cpu_limit_cores: 2,
}
const NOTHING = { cpu_seconds: null, peak_cpu_cores: null, mean_cpu_cores: null, cpu_limit_cores: null }

/**
 * An attempt row with its typed CPU fields. `undefined` is a row from an API
 * older than the fields, which sends none of the four keys.
 */
function withCpu(cpu: Record<string, unknown> | undefined, over: Partial<AttemptRow> = {}): AttemptRow {
  const row = attempt(1, over)
  return cpu === undefined ? row : Object.assign(row, { ...MEASURED, ...cpu })
}

const RUNNING_TASK: Partial<Task> = { state: 'RUNNING', current_lease_id: 'lse_1' }
const RUNNING_ATTEMPT: Partial<AttemptRow> = { completed_at: null, exit_code: null }
/** The task ended and this attempt's finish was never recorded: a kill or a reclaim. */
const UNRECORDED: Partial<AttemptRow> = { completed_at: null, exit_code: null }

function agentRun(a: AttemptRow, t: Partial<Task> = {}, events: TaskEvent[] | null = []): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1, ...t }),
    events,
    eventsDetail: events === null ? 'The event read failed: 503.' : null,
    attempts: [a],
    attemptsDetail: null,
    // cpu 4 in the catalogue, 2 reported by the worker: the two must be told apart.
    classes: { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function mount(a: AttemptRow, t: Partial<Task> = {}, events: TaskEvent[] | null = []): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={agentRun(a, t, events)} />)
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

describe('Details draws CPU as peak and mean cores of the limit, from the attempt’s typed fields', () => {
  it('draws two rows against the limit the worker wrote, at exit, with the cpu-seconds beside the mean', async () => {
    const root = await mount(withCpu({}))
    const peak = cpuRow(root, 'peak')
    const mean = cpuRow(root, 'mean')
    expect(figure(peak)).toBe('1.5 vCPU / 2 vCPU')
    expect(figure(mean)).toBe('0.84 vCPU / 2 vCPU')
    expect(peak.querySelector<HTMLElement>('.ctl-util-fill')?.style.width, 'the peak is not drawn against the worker’s limit').toBe('75%')
    // The class read here has 4 vCPU, so the limit of 2 is not its; the
    // worker wrote it without saying where from.
    expect(by(peak)).toBe('at exit · reported limit')
    expect(by(mean)).toBe('at exit · 402.3 cpu-s')
    expect(root.textContent, 'the retired row is still drawn').not.toMatch(/not sampled/)
  })

  it('falls back to the class’s cpu, and says so, when the worker wrote no limit', async () => {
    const root = await mount(withCpu({ cpu_limit_cores: null }))
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 4 vCPU')
    expect(by(peak)).toBe('at exit · standard limit')
  })

  it('names the class when the limit the worker wrote is that class’s cpu', async () => {
    const root = await mount(withCpu({ cpu_limit_cores: 4 }))
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 4 vCPU')
    expect(by(peak)).toBe('at exit · standard limit')
  })

  it('draws a peak above the limit with the over-ceiling hatch rather than clipping it', async () => {
    const root = await mount(withCpu({ peak_cpu_cores: 2.5 }))
    const track = cpuRow(root, 'peak').querySelector('.ctl-util-track')
    expect(track?.querySelector('.ctl-util-fill.is-bad'), 'an over-limit peak is not a verdict').not.toBeNull()
    expect(track?.querySelector('.ctl-util-over'), 'the excess over the limit is not hatched').not.toBeNull()
  })

  it('calls a running attempt’s figures a live reading, claims no age for it, and keeps a null mean an em dash', async () => {
    const root = await mount(withCpu({ mean_cpu_cores: null }, RUNNING_ATTEMPT), RUNNING_TASK)
    const peak = cpuRow(root, 'peak')
    expect(by(peak)).toMatch(/^live reading/)
    // The document records no time for the reading. An age computed here
    // would be a guess on this browser's clock, drawn as a measurement.
    expect(by(peak), 'a live reading claims an age').not.toMatch(/\bago\b/)
    const mean = cpuRow(root, 'mean')
    expect(mean.querySelector('.ctl-util-figure .ctl-em'), 'an unmeasured mean is drawn as a figure').not.toBeNull()
    expect(mean.querySelector('.ctl-util-track')?.classList.contains('is-unknown')).toBe(true)
  })

  it('says an attempt that ended without a recorded finish left its last written figures, not figures at exit', async () => {
    const root = await mount(withCpu({}, UNRECORDED), { state: 'FAILED' })
    expect(by(cpuRow(root, 'peak'))).toMatch(/^last written/)
  })

  it('does not read the interim usage block, even when a row still carries one', async () => {
    // A row from the #188 API: no typed fields, and the heartbeat reading in
    // `usage`. The block's route is gone; drawing it would keep the path the
    // typed fields replaced.
    const row = Object.assign(attempt(1), {
      usage: { status: 'final', final: true, peak_cpu_cores: 1.5, mean_cpu_cores: 0.8, cpu_seconds: 40, cpu_limit_cores: 2 },
    })
    const root = await mount(row)
    const rows = cpuRows(root)
    expect(rows).toHaveLength(1)
    expect(by(rows[0]!)).toBe('not served')
  })

  it('reads an attempt that ran before the typed fields from its interim heartbeat event on the page', async () => {
    // THE LEGACY READER. Attempts that ran between #188's deploy and this
    // change carry their CPU on HEARTBEAT events only (one on dev, read-only
    // Firestore, 2026-09-25 23:16 UTC). Their typed fields are null; the
    // drawer's own event page has the reading.
    const legacy = ev('heartbeat', new Date(Date.now() - 60_000).toISOString(), 'att_1', {
      elapsed_seconds: 60, checkpoints: 0, peak_rss_bytes: null, final: true,
      cpu_seconds: 40.5, peak_cpu_cores: 1.5, mean_cpu_cores: 0.75, cpu_wall_seconds: 54,
      cpu_source: 'cgroup', cpu_limit_cores: 2, cpu_limit_source: 'cgroup',
    })
    const root = await mount(withCpu(NOTHING), {}, [legacy])
    const peak = cpuRow(root, 'peak')
    expect(figure(peak)).toBe('1.5 vCPU / 2 vCPU')
    expect(by(peak)).toMatch(/^heartbeat event/)
    expect(by(cpuRow(root, 'mean'))).toMatch(/40\.5 cpu-s/)
  })

  it('reads an attempt from before #188 from the cpu-seconds its heartbeat events carry, and draws no cores for it', async () => {
    // PR #210 REVIEW. Every HEARTBEAT before #188 carried `cpu_seconds` and
    // `cpu_source` and no cores (5262b74^ lifecycle.py `_heartbeat`). #188's
    // reader on main took those as a reading and drew the cpu-seconds; a
    // legacy reader that asks for `peak_cpu_cores` only drew `never measured`
    // on them, and said no heartbeat on the page carried a figure while the
    // page held it.
    const older = ev('heartbeat', new Date(Date.now() - 600_000).toISOString(), 'att_1', {
      elapsed_seconds: 60, peak_rss_bytes: 1024, checkpoints: 0, cpu_seconds: 31.2, cpu_source: 'cgroup',
    })
    const newest = ev('heartbeat', new Date(Date.now() - 120_000).toISOString(), 'att_1', {
      elapsed_seconds: 600, peak_rss_bytes: 734003200, checkpoints: 2, cpu_seconds: 402.3, cpu_source: 'cgroup',
    })
    const root = await mount(withCpu(NOTHING), {}, [older, newest])
    expect(root.textContent, 'a measured attempt reads never measured').not.toMatch(/never measured/)
    const peak = cpuRow(root, 'peak')
    const mean = cpuRow(root, 'mean')
    // No cores were recorded: em dashes, never zeros, and no fill.
    for (const r of [peak, mean]) {
      expect(r.querySelector('.ctl-util-figure .ctl-em'), 'an unrecorded figure is drawn as one').not.toBeNull()
      expect(r.querySelector('.ctl-util-fill'), 'an unrecorded figure drew a fill').toBeNull()
    }
    expect(by(peak)).toMatch(/^heartbeat event/)
    // The newest reading on the page, not the first one.
    expect(by(mean)).toBe('heartbeat event · 402.3 cpu-s')
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip?.textContent ?? '', 'the strip does not say only the cpu-seconds were read').toMatch(/cpu-seconds only/)
  })

  it('says never measured when the attempt’s own heartbeats carried the key and no figure', async () => {
    // THE CONTROL for the case above: the reader takes a figure, not a key,
    // and only this attempt's.
    const empty = ev('heartbeat', new Date(Date.now() - 60_000).toISOString(), 'att_1', {
      elapsed_seconds: 60, peak_rss_bytes: null, checkpoints: 0, cpu_seconds: null, cpu_source: null,
    })
    const another = ev('heartbeat', new Date(Date.now() - 30_000).toISOString(), 'att_2', {
      elapsed_seconds: 30, peak_rss_bytes: null, checkpoints: 0, cpu_seconds: 12.5, cpu_source: 'cgroup',
    })
    const root = await mount(withCpu(NOTHING), {}, [empty, another])
    const rows = cpuRows(root)
    expect(rows).toHaveLength(1)
    expect(by(rows[0]!)).toBe('never measured')
  })

  it('says events not read, not never measured, when the event read failed on an attempt with no typed figures', async () => {
    // PR #210 RE-REVIEW. `loadAgentRun` sets `events: null` when `/events`
    // fails, and the legacy reader iterated nothing and answered `never
    // measured` -- "none of its heartbeat events on this page carries one" --
    // about a page nobody read. Every attempt from before the typed fields
    // reads that way at deploy, whenever the event read fails.
    const root = await mount(withCpu(NOTHING), {}, null)
    const rows = cpuRows(root)
    expect(rows, 'two identical rows for one absence').toHaveLength(1)
    expect(by(rows[0]!)).toBe('events not read')
    expect(root.textContent, 'a page that was not read is said to hold no reading').not.toMatch(/never measured/)
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip?.querySelector('.ctl-mark.is-unread'), 'the strip does not mark the reading as not read').not.toBeNull()
    expect(rows[0]!.querySelector('.ctl-util-fill'), 'no reading drew a fill').toBeNull()
  })

  it('reads an attempt with typed figures the same whether or not the event read failed', async () => {
    // THE CONTROL: the typed fields need no page, so a failed event read
    // changes nothing about an attempt that has them.
    const root = await mount(withCpu({}), {}, null)
    expect(by(cpuRow(root, 'peak'))).toBe('at exit · reported limit')
  })

  it('says a full page may hold newer readings than the heartbeat it read, and a short page does not', async () => {
    // PR #210 REVIEW. The page is the task's first `EVENT_PAGE_LIMIT` events.
    // When it is full the route has more, and on a long attempt the newest
    // heartbeat on the page is an early one; the strip says so.
    const beats = (n: number) =>
      Array.from({ length: n }, (_, i) =>
        ev('heartbeat', new Date(Date.now() - (n - i) * 150_000).toISOString(), 'att_1', {
          elapsed_seconds: 150 * (i + 1), peak_rss_bytes: null, checkpoints: 0,
          cpu_seconds: Number((10 + i * 0.5).toFixed(1)), cpu_source: 'cgroup',
        }),
      )
    const full = await mount(withCpu(NOTHING), {}, beats(EVENT_PAGE_LIMIT))
    const fullStrip = full.querySelector<HTMLElement>('.att-cpu-note')?.textContent ?? ''
    expect(fullStrip, 'a full page does not say newer readings may exist').toMatch(/page full; newer readings may exist/)
    full.remove()

    const short = await mount(withCpu(NOTHING), {}, beats(3))
    const shortStrip = short.querySelector<HTMLElement>('.att-cpu-note')?.textContent ?? ''
    expect(shortStrip, 'a short page claims to be full').not.toMatch(/page full/)
    expect(shortStrip).toMatch(/cpu-seconds only/)
  })

  it.each([
    ['a row from an API older than the typed fields', undefined, {}, {}, 'not served'],
    ['an attempt that never started', NOTHING, { started_at: null, completed_at: null, exit_code: null }, {}, 'never ran'],
    ['a running attempt with nothing written yet', NOTHING, RUNNING_ATTEMPT, RUNNING_TASK, 'not yet written'],
    ['an ended attempt with nothing written', NOTHING, {}, {}, 'never measured'],
  ] as const)('draws %s as one hatched row that says so, and never a zero', async (_case, cpu, over, t, phrase) => {
    const root = await mount(withCpu(cpu === undefined ? undefined : { ...cpu }, { ...over }), { ...t })
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

describe('the CPU reading reaches a phone, where the by column is hidden', () => {
  // `@media (max-width: 560px)` sets `.ctl-util-by { display: none }`, so a
  // reading's kind carried only there is invisible at 390: a live reading
  // reads exactly like a final one, and every absence reads `— / 2 vCPU`. The
  // strip outside the rows (`.att-cpu-note`) carries the same words. jsdom
  // applies no stylesheet, so these ask the shipped sheet what 390 displays.
  it.each([
    ['a live reading', {}, RUNNING_ATTEMPT, RUNNING_TASK, [/live reading/, /402\.3 cpu-s/, /reported limit/]],
    ['figures last written', {}, UNRECORDED, { state: 'FAILED' }, [/last written/, /402\.3 cpu-s/, /reported limit/]],
    ['figures at exit', {}, {}, {}, [/at exit/, /402\.3 cpu-s/, /reported limit/]],
    ['no figures served', undefined, {}, {}, [/not served/]],
    ['an attempt that never ran', NOTHING, { started_at: null, completed_at: null, exit_code: null }, {}, [/never ran/]],
    ['a running attempt with nothing written yet', NOTHING, RUNNING_ATTEMPT, RUNNING_TASK, [/not yet written/]],
    ['an ended attempt with nothing written', NOTHING, {}, {}, [/never measured/]],
  ] as const)('shows %s at 390 in a strip outside the row, in words', async (_case, cpu, over, t, words) => {
    const root = await mount(withCpu(cpu === undefined ? undefined : { ...cpu }, { ...over }), { ...t })
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

  it('shows a cpu-seconds-only heartbeat reading at 390, with its cpu-seconds, in the strip', async () => {
    const newest = ev('heartbeat', new Date(Date.now() - 120_000).toISOString(), 'att_1', {
      elapsed_seconds: 600, peak_rss_bytes: 734003200, checkpoints: 2, cpu_seconds: 402.3, cpu_source: 'cgroup',
    })
    const root = await mount(withCpu(NOTHING), {}, [newest])
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip, 'the CPU reading has no strip outside its rows').not.toBeNull()
    for (const w of [/cpu-seconds only/, /402\.3 cpu-s/]) {
      const at = carriers(strip!, w)
      expect(at.length, `the strip does not say ${w}`).toBeGreaterThan(0)
      expect(at.every((el) => shownAt(el, PHONE, root)), `${w} is in the strip but hidden at 390`).toBe(true)
    }
  })

  it('names which reading the strip is about, beside the memory strip under it', async () => {
    const root = await mount(withCpu({}, RUNNING_ATTEMPT), RUNNING_TASK)
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')
    expect(strip?.querySelector('b')?.textContent, 'a strip of words under three bars does not say it is the CPU’s').toBe('cpu')
  })
})

// ---------------------------------------------------------------------------
// Contract request #26: the reading's age and the limit's source
// ---------------------------------------------------------------------------
//
// The owner ACCEPTED request #26 on #184 (2026-09-26): the attempt carries
// `cpu_measured_at` and `cpu_limit_source`, and the API serves the reading's
// age against its own clock (`cpu_reading_age_seconds`). Details said `live
// reading · age not recorded` and `reported limit`; it now says how old a live
// reading is, and `cgroup limit` when the kernel gave the limit. An attempt
// from before #26 keeps the legacy words (the owner kept the legacy reader).
//
// MUTATIONS: age the reading off this browser's clock (`cpu_measured_at`
// against `Date.now()`) instead of the served age; drop the age from the
// strip, which is the only place a phone sees it; name the class for a
// `cgroup` source; call a pre-#26 attempt's limit anything but what it was.

describe('Details dates a live CPU reading and names where its limit came from (request #26)', () => {
  it('says how old a live reading is, by the API’s measure, in the strip a phone sees', async () => {
    // A measured time far from the served age: the words must follow the
    // server's age, not this browser's clock.
    const root = await mount(
      withCpu(
        { cpu_reading_age_seconds: 20, cpu_measured_at: new Date(Date.now() - 3_600_000).toISOString(), cpu_limit_source: 'cgroup' },
        RUNNING_ATTEMPT,
      ),
      RUNNING_TASK,
    )
    const strip = root.querySelector<HTMLElement>('.att-cpu-note')!
    expect(strip.textContent).toMatch(/live reading · 20\s?s ago/)
    expect(strip.textContent, 'the age was taken off this browser’s clock').not.toMatch(/1\s?h ago|60\s?m ago/)
    expect(strip.textContent, 'a dated reading still says its age was not recorded').not.toMatch(/age not recorded/)
    for (const w of [/20\s?s ago/]) {
      const at = carriers(strip, w)
      expect(at.length).toBeGreaterThan(0)
      expect(at.every((el) => shownAt(el, PHONE, root)), 'the age is hidden at 390').toBe(true)
    }
  })

  it('names a limit the kernel gave as the cgroup’s, even where the class has another cpu', async () => {
    const root = await mount(withCpu({ cpu_limit_source: 'cgroup', cpu_reading_age_seconds: 5 }))
    expect(by(cpuRow(root, 'peak'))).toBe('at exit · cgroup limit')
    expect(root.querySelector('.att-cpu-note')!.textContent, 'a sourced limit is still called reported').not.toMatch(/reported limit/)
  })

  it('names a limit the catalogue gave by its class when the class has that cpu', async () => {
    // The catalogue here has standard at 4 vCPU; the worker took 4 from it.
    const root = await mount(withCpu({ cpu_limit_cores: 4, cpu_limit_source: 'resource_class', cpu_reading_age_seconds: 5 }))
    expect(by(cpuRow(root, 'peak'))).toBe('at exit · standard limit')
  })

  it('says how long ago an ended attempt that recorded no finish last wrote its figures', async () => {
    const root = await mount(withCpu({ cpu_reading_age_seconds: 180, cpu_limit_source: 'cgroup' }, UNRECORDED), { state: 'FAILED' })
    expect(root.querySelector<HTMLElement>('.att-cpu-note')!.textContent).toMatch(/last written · 3\s?m ago/)
  })

  it('keeps the legacy words for an attempt from before request #26', async () => {
    const root = await mount(withCpu({}, RUNNING_ATTEMPT), RUNNING_TASK)
    expect(root.querySelector<HTMLElement>('.att-cpu-note')!.textContent).toMatch(/live reading · age not recorded/)
    expect(by(cpuRow(root, 'peak'))).toMatch(/reported limit$/)
  })
})
