// THE LANES MODEL (timeline.html pick A): what a lane draws from an attempt
// page, a task document and one events page -- as data, before any pixel.
//
// Each case names the rule from the picked frame it holds:
//
//   * a solid bar is an attempt that HELD capacity (invariant 1);
//   * a hatched band is a park, which holds nothing;
//   * a thin grey line is waiting, which holds nothing;
//   * a fenced generation is its own cut bar, labelled "gen N fenced";
//   * a cancel request is a mark, never an end;
//   * one lane per agent, grouped under its workflow, standalone last;
//   * the filters run here, in the browser, over the rows read.

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_LANES_VIEW,
  buildLane,
  filterLanes,
  groupLanes,
  lanesWindow,
  madeOnOutcomes,
  parseLanesView,
  serializeLanesView,
  spanDays,
  zoomedTo,
  type Lane,
} from '../lanes'
import type { AttemptRow, Task, TaskEvent } from '../types'

const T0 = Date.parse('2026-10-02T08:00:00Z')
const at = (min: number) => new Date(T0 + min * 60_000).toISOString()
const NOW = T0 + 360 * 60_000

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: null,
    priority: 0,
    created_at: at(0),
    updated_at: at(100),
    started_at: at(5),
    completed_at: at(100),
    submitted_by: 'operator@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    current_generation: 1,
    current_lease_id: null,
    ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number | null, extra: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`,
    task_id: taskId,
    tenant_id: 'eng',
    generation: gen,
    lease_id: `lease_${gen}`,
    backend: 'cloud_run',
    execution_name: null,
    created_at: at(from),
    started_at: at(from + 1),
    completed_at: to === null ? null : at(to),
    exit_code: to === null ? null : 0,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...extra,
  } as AttemptRow
}

function ev(taskId: string, type: string, min: number, gen: number | null, detail: Record<string, unknown> | null = null): TaskEvent {
  return { event_id: `${taskId}-${type}-${min}`, task_id: taskId, type, at: at(min), attempt_id: null, lease_id: null, generation: gen, detail }
}

describe('a lane, from its attempts and events', () => {
  it('draws an attempt that held capacity as a solid bar from its start to its end', () => {
    const lane = buildLane('t1', task('t1'), [attempt('t1', 1, 5, 100)], [], NOW)
    const holds = lane.segs.filter((s) => s.kind === 'hold')
    expect(holds).toHaveLength(1)
    expect(holds[0]!.from).toBe(Date.parse(at(5)))
    expect(holds[0]!.to).toBe(Date.parse(at(100)))
    expect(holds[0]!.gen).toBe(1)
  })

  it('draws the time before the first attempt as a wait line, not a bar', () => {
    const lane = buildLane('t1', task('t1'), [attempt('t1', 1, 5, 100)], [], NOW)
    const waits = lane.segs.filter((s) => s.kind === 'wait')
    expect(waits.length).toBeGreaterThan(0)
    expect(waits[0]!.from).toBe(Date.parse(at(0)))
  })

  it('draws a park as a hatched band between the attempt that parked and the next one, and holds nothing in it', () => {
    const t = task('t2', { attempt_count: 2, current_generation: 2, completed_at: at(200) })
    const lane = buildLane(
      't2',
      t,
      [attempt('t2', 1, 10, 60), attempt('t2', 2, 135, 200)],
      [ev('t2', 'parked', 60, 1, { reason: 'PROVIDER_QUOTA_EXHAUSTED' })],
      NOW,
    )
    const park = lane.segs.find((s) => s.kind === 'park')
    expect(park, 'no park band was drawn').toBeDefined()
    expect(park!.from).toBe(Date.parse(at(60)))
    expect(park!.to).toBe(Date.parse(at(135)))
    // No hold bar overlaps the park: a park holds no capacity (invariant 1).
    for (const h of lane.segs.filter((s) => s.kind === 'hold')) {
      expect(h.to <= park!.from || h.from >= park!.to).toBe(true)
    }
  })

  it('runs a park that is still parked to now, and its expected resume past now as a future band', () => {
    const t = task('t3', { state: 'PARKED', completed_at: null, next_eligible_at: new Date(NOW + 30 * 60_000).toISOString() })
    const lane = buildLane('t3', t, [attempt('t3', 1, 10, 60)], [ev('t3', 'parked', 60, 1)], NOW)
    const parks = lane.segs.filter((s) => s.kind === 'park')
    expect(parks.find((p) => !p.future)?.to).toBe(NOW)
    expect(parks.find((p) => p.future)?.to).toBe(NOW + 30 * 60_000)
  })

  it('cuts a fenced generation: its own bar, labelled "gen N fenced", from the exit code alone', () => {
    const t = task('t4', { attempt_count: 2, current_generation: 2 })
    const lane = buildLane('t4', t, [attempt('t4', 1, 10, 75, { exit_code: 70 }), attempt('t4', 2, 76, 100)], null, NOW)
    const cut = lane.segs.find((s) => s.kind === 'cut')
    expect(cut, 'the fenced generation was drawn as an ordinary hold').toBeDefined()
    expect(cut!.gen).toBe(1)
    expect(cut!.label).toBe('gen 1 fenced')
    expect(lane.segs.filter((s) => s.kind === 'hold').map((s) => s.gen)).toEqual([2])
  })

  it('cuts a fenced generation named by a generation_fenced event, too', () => {
    const t = task('t5', { attempt_count: 2, current_generation: 2 })
    const lane = buildLane(
      't5',
      t,
      [attempt('t5', 1, 10, 75, { exit_code: 1 }), attempt('t5', 2, 76, 100)],
      [ev('t5', 'generation_fenced', 75, 2, { expected_generation: 1, observed_generation: 2 })],
      NOW,
    )
    expect(lane.segs.find((s) => s.kind === 'cut')?.label).toBe('gen 1 fenced')
  })

  it('marks a cancel request and does not end the lane there', () => {
    const t = task('t6', { state: 'RUNNING', completed_at: null, current_generation: 1 })
    const lane = buildLane('t6', t, [attempt('t6', 1, 10, null)], [ev('t6', 'cancel_requested', 40, 1)], NOW)
    const mark = lane.marks.find((m) => m.kind === 'cancel_requested')
    expect(mark?.at).toBe(Date.parse(at(40)))
    // The legacy flag-only cancel reads the same way.
    const legacy = buildLane('t6', t, [attempt('t6', 1, 10, null)], [ev('t6', 'cancelled', 40, 1, { phase: 'cancel_requested' })], NOW)
    expect(legacy.marks.map((m) => m.kind)).toContain('cancel_requested')
    expect(legacy.marks.map((m) => m.kind)).not.toContain('cancelled')
    // The attempt is still holding: its bar runs on to now, open.
    const hold = lane.segs.find((s) => s.kind === 'hold')!
    expect(hold.to).toBe(NOW)
    expect(hold.open).toBe(true)
  })

  it('does not invent an end for an attempt with no completed_at whose task moved on', () => {
    const t = task('t7', { attempt_count: 2, current_generation: 2 })
    const lane = buildLane('t7', t, [attempt('t7', 1, 10, null), attempt('t7', 2, 50, 100)], [], NOW)
    const first = lane.segs.find((s) => s.gen === 1 && s.kind === 'hold')!
    expect(first.endKnown).toBe(false)
    expect(first.open).toBe(false)
  })

  it('ends a terminal task with its state mark', () => {
    const lane = buildLane('t8', task('t8', { state: 'FAILED' }), [attempt('t8', 1, 5, 100, { exit_code: 1 })], [], NOW)
    expect(lane.marks.map((m) => m.kind)).toContain('failed')
  })

  it('says whether its marks could be read: null events is "not read", never "none"', () => {
    expect(buildLane('t1', task('t1'), [attempt('t1', 1, 5, 100)], null, NOW).eventsRead).toBe(false)
    expect(buildLane('t1', task('t1'), [attempt('t1', 1, 5, 100)], [], NOW).eventsRead).toBe(true)
  })
})

describe('lanes, grouped and filtered in the browser', () => {
  const lanes: Lane[] = [
    buildLane('s1', task('s1'), [attempt('s1', 1, 5, 30)], [], NOW),
    buildLane('w1', task('w1', { workflow_id: 'wf_a', step_id: 'plan' }), [attempt('w1', 1, 10, 40)], [], NOW),
    buildLane('w2', task('w2', { workflow_id: 'wf_a', step_id: 'build', runner_profile: 'codex', state: 'FAILED' }), [attempt('w2', 1, 40, 90)], [], NOW),
    buildLane('s2', task('s2', { runner_profile: 'browser' }), [attempt('s2', 1, 20, 50)], [], NOW),
  ]

  it('groups by workflow, with standalone agents last', () => {
    const groups = groupLanes(lanes, 'workflow')
    expect(groups.map((g) => g.key)).toEqual(['wf_a', 'standalone'])
    expect(groups[0]!.lanes.map((l) => l.taskId)).toEqual(['w1', 'w2'])
    expect(groups[1]!.lanes.map((l) => l.taskId)).toEqual(['s1', 's2'])
  })

  it('groups by profile, and not at all', () => {
    expect(groupLanes(lanes, 'profile').map((g) => g.key)).toEqual(['browser', 'claude-code', 'codex'])
    expect(groupLanes(lanes, 'none')).toHaveLength(1)
  })

  it('filters state, profile, workflow and kind over the lanes read', () => {
    const v = DEFAULT_LANES_VIEW
    expect(filterLanes(lanes, { ...v, state: ['FAILED'] }).map((l) => l.taskId)).toEqual(['w2'])
    expect(filterLanes(lanes, { ...v, profile: ['browser'] }).map((l) => l.taskId)).toEqual(['s2'])
    expect(filterLanes(lanes, { ...v, wf: 'wf_a' }).map((l) => l.taskId)).toEqual(['w1', 'w2'])
    expect(filterLanes(lanes, { ...v, kind: 'standalone' }).map((l) => l.taskId)).toEqual(['s1', 's2'])
    expect(filterLanes(lanes, { ...v, kind: 'steps' }).map((l) => l.taskId)).toEqual(['w1', 'w2'])
  })

  it('cannot place a lane whose task was not read under a state or profile filter', () => {
    const unknown = buildLane('x', null, [attempt('x', 1, 5, 30)], null, NOW)
    expect(filterLanes([unknown], DEFAULT_LANES_VIEW)).toHaveLength(1)
    expect(filterLanes([unknown], { ...DEFAULT_LANES_VIEW, state: ['FAILED'] })).toHaveLength(0)
  })
})

describe('the view in the address', () => {
  it('round-trips every filter, the zoom and its way back, and the open lane', () => {
    const v = {
      ...DEFAULT_LANES_VIEW,
      span: null,
      since: at(0),
      until: at(360),
      back: 'span=7d',
      state: ['FAILED' as const, 'RUNNING' as const],
      profile: ['codex'],
      wf: 'wf_a',
      kind: 'steps' as const,
      by: 'profile' as const,
      lane: 'task_1',
    }
    expect(parseLanesView(serializeLanesView(v))).toEqual(v)
    expect(serializeLanesView(DEFAULT_LANES_VIEW)).toBe('')
  })

  it('opens on 24h, the span a lane is readable at', () => {
    expect(parseLanesView('').span).toBe('24h')
    expect(parseLanesView('span=bogus').span).toBe('24h')
  })

  it('knows a link made on the Outcomes page', () => {
    expect(madeOnOutcomes('span=7d&table=1')).toBe(true)
    expect(madeOnOutcomes('span=7d&bucket=day')).toBe(true)
    expect(madeOnOutcomes('span=7d')).toBe(false)
  })

  it('turns a drag on the axis into since/until with a way back', () => {
    const v = parseLanesView('span=24h')
    const z = zoomedTo(v, T0, T0 + 120 * 60_000)
    expect(z.span).toBeNull()
    expect(z.since).toBe(at(0))
    expect(z.until).toBe(at(120))
    expect(z.back).toBe('')
    expect(lanesWindow(z, NOW)).toEqual({ since: T0, until: T0 + 120 * 60_000 })
  })

  it('measures the span in days, so the page can suggest Outcomes past 7', () => {
    expect(spanDays(parseLanesView('span=30d'), NOW)).toBe(30)
    expect(spanDays(parseLanesView(''), NOW)).toBe(1)
  })
})
