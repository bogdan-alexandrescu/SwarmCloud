/**
 * THE ACCEPTANCE TEST FOR B7.1.
 *
 * The directive is to take the prose out of the app. The trap is that this
 * app's best property IS prose, so a naive removal deletes it. The line:
 *
 *   THE FACT stays on the surface, always, impossible to miss.
 *   THE EXPLANATION moves into the `?`.
 *
 * And the test, stated by the brief in one sentence: *can a reader tell,
 * without hovering anything, that a number is missing rather than zero?*
 *
 * So every assertion below runs against `renderToStaticMarkup`, which is the
 * surface with EVERY HELP CARD CLOSED -- no hover, no focus, no click, nothing
 * pinned. That is not a convenience of the test harness, it is the condition
 * being tested. If an assertion here can only be satisfied by opening a card,
 * the honesty rule has been deleted rather than moved.
 *
 * The screen is AgentDetail because it is the worst offender by volume and
 * carries the best writing in the product. Getting the split right here is
 * what makes the rest of the app mechanical.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToStaticMarkup } from 'react-dom/server'

import { Run } from '../src/AgentDetail'
import type { AgentRun } from '../src/api'
import type { AttemptRow, Task } from '../src/types'

// ---------------------------------------------------------------------------
// Fixtures: a run that measured some things and did not measure others
// ---------------------------------------------------------------------------

const TASK: Task = {
  id: 'task_b5dc2568713a40158851',
  tenant_id: 'u-bogdan',
  state: 'RUNNING',
  runner_profile: 'claude-code',
  resource_class: 'standard',
  provider: 'anthropic',
  priority: 5,
  created_at: '2026-09-22T09:00:00Z',
  updated_at: '2026-09-22T09:05:00Z',
  started_at: '2026-09-22T09:01:00Z',
  completed_at: null,
  submitted_by: 'bogdan',
  attempt_count: 1,
  max_attempts: 3,
  park_reason: null,
  blocked_by: null,
  workflow_id: null,
  step_id: null,
  depends_on: null,
  cancel_requested: false,
  repository_url: null,
  model: 'claude-opus-5',
  timeout_seconds: 3600,
  next_eligible_at: null,
  metadata: null,
  repository_ref: null,
  input: null,
  last_error: null,
  result_summary: null,
  latest_checkpoint: null,
  current_generation: 1,
  current_lease_id: 'lease_1',
}

/**
 * ONE ATTEMPT THAT MEASURED NOTHING IT COULD HAVE MEASURED.
 *
 * `cost_usd: null` is the case the brief names: no attempt reported a cost. It
 * is an absent measurement and NOT $0.00. `checkpoints: []` is the opposite
 * case sitting right beside it -- the documents were read, and none lists a
 * checkpoint. That zero is real and must render as a digit.
 */
const ATTEMPT: AttemptRow = {
  attempt_id: 'att_1',
  task_id: TASK.id,
  tenant_id: TASK.tenant_id,
  generation: 1,
  lease_id: 'lease_1',
  backend: 'cloudrun',
  execution_name: null,
  created_at: '2026-09-22T09:01:00Z',
  started_at: '2026-09-22T09:01:10Z',
  completed_at: null,
  exit_code: null,
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
}

function run(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: TASK,
    events: [],
    eventsDetail: null,
    attempts: [ATTEMPT],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

function surface(r: AgentRun = run()): string {
  return renderToStaticMarkup(<Run run={r} />)
}

/**
 * One metric tile, as the surface shows it.
 *
 * This is the reader's eye, mechanised: it pulls the label, the rendered value
 * and the tile's tone class out of the markup. Everything below asks the same
 * question of it that a person glancing at the screen asks.
 */
interface Tile {
  label: string
  value: string
  absent: boolean
  unread: boolean
}

function tiles(markup: string): Map<string, Tile> {
  const out = new Map<string, Tile>()
  // Split rather than match across a whole tile: the label span now CONTAINS
  // the `?` button and the card's hidden description, so any regex running
  // `(.*?)</span>` from the label closes on the wrong tag and finds no tiles
  // at all -- which would leave every assertion below quietly vacuous.
  for (const chunk of markup.split('<div class="ctl-metric').slice(1)) {
    const cls = /^([^"]*)"/.exec(chunk)?.[1] ?? ''
    // The label is the text before the first tag inside the label span. That
    // deliberately excludes the help card's hidden short text, which lives in
    // the same span and is NOT part of what a sighted reader sees.
    const label = /class="ctl-metric-label"[^>]*>([^<]*)/.exec(chunk)?.[1]?.trim()
    if (label === undefined || label === '') continue
    const rest = /<span class="ctl-metric-value">([\s\S]*?)<\/div>/.exec(chunk)?.[1] ?? ''
    const value = (rest.split(/<span class="ctl-metric-(?:sub|foot)"/)[0] ?? '')
      .replace(/<[^>]*>/g, '')
      .trim()
    out.set(label, {
      label,
      value,
      absent: cls.includes('is-absent'),
      unread: cls.includes('is-unread'),
    })
  }
  return out
}

/** Would a reader read this rendered value as a quantity? */
function readsAsANumber(value: string): boolean {
  return /^[$]?[\d,]+(\.\d+)?/.test(value.trim())
}

// ---------------------------------------------------------------------------
// The condition every assertion below runs under
// ---------------------------------------------------------------------------

test('the surface under test has every help card closed', () => {
  const markup = surface()
  assert.ok(!markup.includes('role="tooltip"'), 'a card is open; this is not the resting surface')
  assert.ok(!markup.includes('role="dialog"'), 'a card is pinned; this is not the resting surface')
  // ...and the `?` affordances are nonetheless present and reachable.
  assert.ok(markup.includes('aria-expanded="false"'), 'no help affordance rendered at all')
})

// ---------------------------------------------------------------------------
// THE ACCEPTANCE TEST
// ---------------------------------------------------------------------------

test('absent is distinguishable from zero with every help card closed', () => {
  const t = tiles(surface())

  const cost = t.get('Token cost')
  assert.ok(cost, 'the Token cost tile is not on the screen at all')
  const ckpt = t.get('Checkpoints')
  assert.ok(ckpt, 'the Checkpoints tile is not on the screen at all')

  // THE MEASURED ZERO. The attempt documents were read; none lists a
  // checkpoint. That is a result, and it renders as a figure.
  assert.equal(ckpt.value, '0', 'a measured zero must render as a digit')
  assert.equal(ckpt.absent, false, 'a measured zero must not wear the absent treatment')
  assert.ok(readsAsANumber(ckpt.value))

  // THE ABSENT MEASUREMENT. No attempt reported a cost.
  assert.ok(
    !readsAsANumber(cost.value),
    `Token cost renders as "${cost.value}", which reads as a quantity. ` +
      `An unreported cost must not be representable as a number on this surface.`,
  )
  assert.equal(cost.absent, true, 'the absent tile does not carry the absent treatment')
  assert.ok(!/\$?0(\.0+)?$/.test(cost.value), 'an unreported cost rendered as a zero')

  // AND THE TWO MUST NOT LOOK ALIKE. This is the whole test in one line: the
  // shapes differ, on the resting surface, with nothing hovered.
  assert.notEqual(
    readsAsANumber(cost.value),
    readsAsANumber(ckpt.value),
    'an absent figure and a measured zero render as the same kind of thing',
  )
})

test('the named absences still say what they are, in words, on the surface', () => {
  const t = tiles(surface())
  // The brief lists these three by name as writing that must survive VISIBLY.
  assert.equal(t.get('Token cost')?.value, 'not reported')
  assert.equal(t.get('Peak memory')?.value, 'not recorded')
  assert.equal(t.get('Tokens')?.value, 'not reported')
  for (const label of ['Token cost', 'Peak memory', 'Tokens']) {
    assert.equal(t.get(label)?.absent, true, `${label} lost its absent treatment`)
  }
})

test('the explanations moved rather than staying', () => {
  const markup = surface()
  // These sentences were the `sub` under each tile. They are now in help.ts,
  // reachable from the `?`. If one is still printed here the migration did not
  // happen; if the assertion above ever fails at the same time, it was deleted.
  for (const sentence of [
    'This is an absent measurement, not $0.00.',
    'A running attempt has none.',
    'Not the same as a run that used none.',
  ]) {
    assert.ok(!markup.includes(sentence), `still printed on the surface: "${sentence}"`)
  }
})

test('a failed read never produces a figure', () => {
  const t = tiles(surface(run({ attempts: null, attemptsDetail: 'HTTP 503.' })))
  for (const label of ['Peak memory', 'Spend']) {
    const tile = t.get(label)
    assert.ok(tile, `${label} is missing`)
    assert.equal(tile.unread, true, `${label} does not carry the read-failed treatment`)
    assert.ok(!readsAsANumber(tile.value), `${label} rendered "${tile.value}" after a failed read`)
  }
})

// ---------------------------------------------------------------------------
// The panels: a real zero and a failed read must not be told apart by colour
// ---------------------------------------------------------------------------

test('a real zero says so in words, not only in a border colour', () => {
  const parked: Task = { ...TASK, state: 'PARKED', attempt_count: 0, current_lease_id: null }
  const markup = surface(run({ task: parked, attempts: [] }))

  assert.ok(markup.includes('Nothing has been admitted yet'), 'the heading is gone')
  assert.ok(markup.includes('real zero'), 'nothing on the surface says this zero is real')
  // The state it was measured in is a fact about THIS task and stays.
  assert.ok(markup.includes('PARKED'), 'the state the measurement was taken in is gone')
  assert.ok(!markup.includes('role="tooltip"'), 'this surface should be at rest')
})

test('a failed attempt read says so in words, and carries its detail', () => {
  const markup = surface(run({ attempts: null, attemptsDetail: 'HTTP 503 from /attempts.' }))
  assert.ok(markup.includes('read failed'), 'nothing on the surface says the read failed')
  assert.ok(markup.includes('HTTP 503 from /attempts.'), 'the failure detail is gone')
})

test('a partial read is marked apart from both', () => {
  const counted: Task = { ...TASK, attempt_count: 2 }
  const markup = surface(run({ task: counted, attempts: [] }))
  assert.ok(markup.includes('partial'), 'nothing marks this panel as partial')
  assert.ok(markup.includes('Counts 2 attempts'), 'the counts that make it partial are gone')
})

// ---------------------------------------------------------------------------
// The screen-of-prose that left
// ---------------------------------------------------------------------------

test('the standing-rules legend is a footer of links, not a screen of prose', () => {
  const markup = surface()
  assert.ok(!markup.includes('The requested figure is a ceiling, not a target'))
  assert.ok(!markup.includes('What is INSIDE a checkpoint is not recorded'))
  assert.ok(markup.includes('Reading these cards:'), 'the footer link is gone too')
  assert.ok(markup.includes('#help/requests-are-ceilings'), 'the footer links nowhere')
})

test('the `?` sits after a label and never after a value', () => {
  const markup = surface()
  // The glyph is inside `.ctl-metric-label`. A `?` button inside a value span
  // would read as a footnote marker on the figure itself.
  assert.ok(
    !/<span class="ctl-metric-value">[^<]*<button/.test(markup),
    'a help glyph is attached to a value',
  )
  assert.match(markup, /<span class="ctl-metric-label"[^>]*>[^<]*<span[^>]*><button/)
})
