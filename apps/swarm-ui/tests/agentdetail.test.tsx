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

import { DRAWER_POLL_MS, Run, drawerPoll } from '../src/AgentDetail'
import { HELP } from '../src/help'
import type { AgentRun } from '../src/api'
import type { AttemptRow, Task, TaskEvent } from '../src/types'

// ---------------------------------------------------------------------------
// Fixtures: a run that measured some things and did not measure others
// ---------------------------------------------------------------------------

/**
 * A RUNNING task and its open attempt.
 *
 * THIS USED TO BE THE B7.1 FIXTURE, AND THAT WAS THE DEFECT AG-4 NAMES. The
 * acceptance test asserted `not recorded` and `not reported` -- the ABSENT
 * encoding -- on an attempt that had not finished, when peak memory, tokens and
 * cost are all written at the END of an attempt: on a running agent "nothing
 * yet" is the expected state, not an absence. The acceptance test now runs on
 * the ended run below, and this pair is the running case, which asserts the
 * pending tone instead.
 */
const RUNNING_TASK: Task = {
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

const RUNNING_ATTEMPT: AttemptRow = {
  attempt_id: 'att_1',
  task_id: RUNNING_TASK.id,
  tenant_id: RUNNING_TASK.tenant_id,
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

/**
 * THE B7.1 FIXTURE: a run that is OVER and measured nothing it could have.
 *
 * `cost_usd: null` is the case the brief names: no attempt reported a cost. It
 * is an absent measurement and NOT $0.00 -- and it is only an absence because
 * the attempt ENDED without writing one; the running pair above is the case
 * where the same null is still due. `checkpoints: []` is the opposite case
 * sitting right beside it -- the documents were read, and none lists a
 * checkpoint. That zero is real and must render as a digit.
 */
const TASK: Task = {
  ...RUNNING_TASK,
  state: 'SUCCEEDED',
  updated_at: '2026-09-22T09:40:00Z',
  completed_at: '2026-09-22T09:40:00Z',
  current_lease_id: null,
}

const ATTEMPT: AttemptRow = {
  ...RUNNING_ATTEMPT,
  completed_at: '2026-09-22T09:40:00Z',
  exit_code: 0,
}

function event(over: Partial<TaskEvent> & Pick<TaskEvent, 'type' | 'at'>): TaskEvent {
  return {
    event_id: `ev_${over.type}_${over.at}`,
    task_id: RUNNING_TASK.id,
    attempt_id: null,
    lease_id: null,
    generation: null,
    detail: null,
    ...over,
  }
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
 * One cell of the stat strip, as the surface shows it (agent-details-v3.html
 * A replaced the six tiles with one five-cell strip: Elapsed · Attempt · Peak
 * memory · CPU · Cost, tokens as Cost's sub-line, checkpoints in the leading
 * card).
 *
 * This is the reader's eye, mechanised: it pulls the label, the rendered value
 * and the cell's tone class out of the markup. Everything below asks the same
 * question of it that a person glancing at the screen asks.
 */
interface Tile {
  label: string
  value: string
  /** What the cell says under the figure, tags stripped. */
  sub: string
  absent: boolean
  unread: boolean
  /** The pending tone: a figure that is due, not missing. */
  reading: boolean
  /** The live tone: a real reading of a process still running, not its final figure. */
  live: boolean
}

function text(html: string): string {
  return html
    .replace(/<span id="[^"]*" data-help-description=""[^>]*>[\s\S]*?<\/span>/g, '')
    .replace(/<[^>]*>/g, '')
    .replace(/<!-- -->/g, '')
    .trim()
}

function tiles(markup: string): Map<string, Tile> {
  const out = new Map<string, Tile>()
  // Split rather than match across a whole cell: the label span CONTAINS the
  // cell's hidden help description, so a regex running `(.*?)</span>` from the
  // label would close on the wrong tag and find no cells at all -- which would
  // leave every assertion below quietly vacuous.
  for (const chunk of markup.split('<div class="dt-sc').slice(1)) {
    const cls = /^([^"]*)"/.exec(chunk)?.[1] ?? ''
    // The label is the text before the first tag inside the label span. That
    // deliberately excludes the help copy, which lives in the same span and is
    // NOT part of what a sighted reader sees.
    const label = /class="dt-sc-l"[^>]*>([^<]*)/.exec(chunk)?.[1]?.trim()
    if (label === undefined || label === '') continue
    const value = text(/<span class="dt-sc-v">([\s\S]*?)<\/span>(?=<span class="dt-bar|<span class="dt-sc-s|<\/div>)/.exec(chunk)?.[1] ?? '')
    const sub = text(/<span class="dt-sc-s">([\s\S]*?)<\/span><\/div>/.exec(chunk)?.[1] ?? '')
    out.set(label, {
      label,
      value,
      sub,
      absent: cls.includes('is-absent'),
      unread: cls.includes('is-unread'),
      reading: cls.includes('is-reading'),
      live: cls.includes('is-live'),
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

  const cost = t.get('Cost')
  assert.ok(cost, 'the Cost cell is not on the screen at all')
  // THE MEASURED ZERO, which moved into the leading card with the
  // checkpoints (agent-details-v3.html A). The attempt documents were read;
  // none lists a checkpoint. That is a result, and it renders as a figure.
  const ck = /<span class="dt-nf">Checkpoints <b>([^<]*)<\/b>/.exec(plain(surface()))
  assert.ok(ck, 'the checkpoint fact is not on the screen at all')
  const ckpt = { value: ck[1]! }
  assert.equal(ckpt.value, '0', 'a measured zero must render as a digit')
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
  // The brief lists these three by name as writing that must survive VISIBLY:
  // a dash and the word, never a figure. Tokens are Cost's sub-line now.
  assert.equal(t.get('Cost')?.value, '— not reported')
  assert.equal(t.get('Peak memory')?.value, '— not recorded')
  assert.equal(t.get('Cost')?.sub, 'tokens not reported')
  for (const label of ['Cost', 'Peak memory']) {
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
  for (const label of ['Peak memory', 'CPU', 'Cost']) {
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

  // THE FACT, NOT THE SENTENCE. The runs redesign shortened the heading to
  // `no attempt yet - <state>` and moved the explanation into the panel's own
  // copy, where it still says nothing has been admitted FOR THIS TASK yet.
  // What must hold is that the surface states nothing was admitted and calls
  // the zero real; which words carry it is the redesign's to choose.
  assert.ok(/no attempt yet/i.test(markup), 'the surface no longer says no attempt has been made')
  assert.ok(/nothing has been admitted/i.test(markup), 'the surface no longer says nothing was admitted')
  assert.ok(markup.includes('real zero'), 'nothing on the surface says this zero is real')
  // The state it was measured in is a fact about THIS task and stays -- IN THE
  // HEADING, spelled as the chip beside it reads (AG-28). This asserted only
  // that `PARKED` was somewhere in the markup, which the state chip satisfies
  // on its own; the heading printing `no attempt yet · PARKED` in capitals
  // beside a chip reading `parked` passed it. It now pins the heading.
  assert.ok(markup.includes('no attempt yet · parked'), 'the heading lost the state, or shouts it')
  assert.ok(!markup.includes('no attempt yet · PARKED'), 'the heading prints the API’s uppercase state')
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
  // The count is what MAKES it partial, so the count has to be on the surface.
  // It is; the redesign lowercased it and put it in two places -- the heading
  // reads `counts 2 - returned 0` and the copy reads `counts 2 attempts`. The
  // assertion pins the number beside the word, case-insensitively, rather than
  // one capitalisation of one of the two.
  assert.ok(/counts 2\b/i.test(markup), 'the counts that make it partial are gone')
  assert.ok(/returned (0|none)/i.test(markup), 'the surface no longer says the query returned nothing')
})

// ---------------------------------------------------------------------------
// The screen-of-prose that left
// ---------------------------------------------------------------------------

test('the standing rules are behind one `?` per card, not a screen of prose or a block of links', () => {
  const markup = surface()
  assert.ok(!markup.includes('The requested figure is a ceiling, not a target'))
  assert.ok(!markup.includes('What is INSIDE a checkpoint is not recorded'))
  // THE LINK BLOCK IS GONE (agent-details-v3.html A: "a block of 10
  // underlined mono help links"); each card carries its own `?` instead, so
  // the topics are still one click away, on the card they explain.
  assert.ok(!/reading (this|these) (screen|card)/i.test(markup), 'the help-link block is back')
  for (const topic of ['requests-are-ceilings', 'attempt-documents'] as const) {
    assert.ok(markup.includes(`aria-label="Help: ${HELP[topic].title}"`), `no card opens ${topic}`)
  }
})

test('the `?` sits after a label and never after a value', () => {
  const markup = surface()
  // A `?` button inside a value span would read as a footnote marker on the
  // figure itself, which is how a measured number becomes one nobody trusts.
  // This half of the rule is unchanged and unconditional.
  assert.ok(!/<span class="dt-sc-v">(?:(?!<\/span>)[\s\S])*<button/.test(markup), 'a help glyph is attached to a strip value')
  assert.ok(!/<span class="dt-rr-v">(?:(?!<\/span>)[\s\S])*<button/.test(markup), 'a help glyph is attached to a resource value')
  // THE STRIP'S LABELS PUBLISH THEIR EXPLANATION AND DRAW NO BUTTON (B7.4):
  // the strip is one card, and the cards' `?`s are below. Pinned on EVERY
  // explained label rather than on the first one a regex finds.
  const labels = [...markup.matchAll(/<span class="dt-sc-l"([^>]*)>(.*?)<\/span><span class="dt-sc-v"/gs)]
  assert.ok(labels.length >= 4, `only ${labels.length} strip labels were examined`)
  let explained = 0
  for (const [, attrs, body] of labels) {
    const described = /aria-describedby="([^"]+)"/.exec(attrs ?? '')
    if (described === null) continue
    explained++
    assert.ok(
      new RegExp(`<span id="${described[1]!.replace(/[$.*+?^{}()|[\]\\]/g, '\\$&')}" data-help-description=""`).test(body ?? ''),
      'a strip label points aria-describedby at nothing it contains',
    )
    assert.ok(!(body ?? '').includes('<button'), 'a strip label draws a help glyph')
  }
  assert.ok(explained >= 3, `only ${explained} strip labels publish an explanation`)
  // AND EVERY GLYPH FOLLOWS A CARD'S HEADING (AH-24: a `?` goes after the
  // label or heading it explains, never after a value). Each card head is its
  // title, then its links, then its one `?`.
  // MUTATION: put a `?` in a strip value, or a second one in a card head.
  const glyphs = markup.split('aria-label="Help: ').length - 1
  const heads = [...markup.matchAll(/<div class="dt-card-head"><b>[^<]+<\/b>([\s\S]*?)<\/div>/g)]
  const inHeads = heads.reduce((n, [, body]) => n + ((body ?? '').split('aria-label="Help: ').length - 1), 0)
  assert.ok(glyphs >= 4, `only ${glyphs} help glyphs on the surface`)
  assert.equal(inHeads, glyphs, 'a help glyph sits somewhere other than after a card heading')
  for (const [, body] of heads) {
    assert.ok((body ?? '').split('aria-label="Help: ').length - 1 <= 1, 'a card carries more than one `?`')
  }
})

// ---------------------------------------------------------------------------
// A running agent: pending, not absent (AG-4)
// ---------------------------------------------------------------------------

/** Static markup with React's text separators taken out, for phrase matching. */
function plain(markup: string): string {
  return markup.replace(/<!-- -->/g, '')
}

test('a running agent draws its end-of-attempt figures as pending, never as absent', () => {
  // BREAK IT: drop the `open !== null` branches from RunMetrics. The three
  // tiles go back to `not recorded` / `not reported` on the dashed absent
  // tile, beside an attempt that is still running.
  const t = tiles(surface(run({ task: RUNNING_TASK, attempts: [RUNNING_ATTEMPT] })))
  for (const label of ['Peak memory', 'Cost']) {
    const tile = t.get(label)
    assert.ok(tile, `${label} is not on the screen`)
    assert.equal(tile.absent, false, `${label} draws the absent encoding while its attempt is open`)
    assert.equal(tile.reading, true, `${label} is not on the pending tone while its attempt is open`)
    // Pending is still not a number: nothing has been written.
    assert.ok(!readsAsANumber(tile.value), `${label} rendered "${tile.value}" before anything was written`)
  }
  // Small, muted words -- `at exit` -- never full size (agent-details-v3.html A).
  assert.equal(t.get('Cost')?.value, 'at exit')
  assert.equal(t.get('Cost')?.sub, 'with tokens, when the attempt ends')
  assert.equal(t.get('Peak memory')?.value, 'at exit')
})

test('a running agent shows its newest heartbeat peak as a live figure, with its age', () => {
  const beat = event({
    type: 'heartbeat',
    at: '2026-09-22T09:10:00Z',
    attempt_id: 'att_1',
    detail: { peak_rss_bytes: 19_500_000, elapsed_seconds: 530, checkpoints: 0 },
  })
  const t = tiles(surface(run({ task: RUNNING_TASK, attempts: [RUNNING_ATTEMPT], events: [beat] })))
  const peak = t.get('Peak memory')
  assert.ok(peak, 'Peak memory is not on the screen')
  // A heartbeat reading IS a measurement of the live process, so it is a figure.
  assert.equal(peak.value, '18.6 MiB')
  assert.match(peak.sub, / · so far · \S+ ago$/, `the live figure is not labelled live with its age: "${peak.sub}"`)
  assert.equal(peak.absent, false)
  assert.equal(peak.live, true, 'the live high-water mark is presented as the final figure')
})

test('a runner that never reports spend stays absent while it runs', () => {
  // The control: `mock`, `generic` and `browser` never write tokens, so for
  // them "not reported" is true while running as well as after. A pending
  // tile there would promise a figure that is never coming.
  const mock: Task = { ...RUNNING_TASK, runner_profile: 'mock' }
  const t = tiles(surface(run({ task: mock, attempts: [RUNNING_ATTEMPT] })))
  // Tokens: no model is called, so there is no figure to promise (owner
  // decision 2026-09-29, #322); it reads `no model call`, never a pending
  // `written at exit` and never a number. Token cost stays absent.
  assert.equal(t.get('Cost')?.value, 'no model call', 'Cost promises a figure a mock run never writes')
  assert.ok(!/at exit|when the attempt ends/.test(t.get('Cost')?.sub ?? ''), 'a mock run is promised tokens at exit')
  assert.equal(t.get('Cost')?.absent, true)
})

// ---------------------------------------------------------------------------
// A task that never ran (AG-3, AG-6, AG-8)
// ---------------------------------------------------------------------------

const UPSTREAM = 'an upstream workflow step did not succeed'

/**
 * A workflow step the scheduler cascade-cancelled after its parent failed. It
 * sat READY for 27m 57s and never ran: no attempt, no `started_at`, and the
 * `last_error` and the `cancelled` event's `reason` are the scheduler's own
 * words (scheduler/store.py `cancel`).
 */
const CASCADE: Task = {
  ...TASK,
  id: 'task_cascade000000000000',
  state: 'CANCELLED',
  created_at: '2026-09-22T09:00:00Z',
  started_at: null,
  completed_at: '2026-09-22T09:27:57Z',
  updated_at: '2026-09-22T09:27:57Z',
  attempt_count: 0,
  current_generation: 0,
  current_lease_id: null,
  workflow_id: 'wf_0123456789',
  step_id: 'synthesise',
  depends_on: ['task_parent0000000000000'],
  last_error: UPSTREAM,
}

function cascade(): AgentRun {
  return run({
    task: CASCADE,
    attempts: [],
    events: [
      event({ type: 'submitted', at: '2026-09-22T09:00:00Z' }),
      event({
        type: 'cancelled',
        at: '2026-09-22T09:27:57Z',
        detail: { reason: UPSTREAM, failed_parents: ['task_parent0000000000000'] },
      }),
    ],
  })
}

test('a task that never ran does not print its wait as its run', () => {
  // BREAK IT: print `el.text` under `run` whatever the task's start. This
  // step then reads `run 27m 57s` beside `never ran`.
  const markup = plain(surface(cascade()))
  assert.ok(!markup.includes('<b>run</b>27m 57s'), 'the wait is printed under the run key')
  // #102: THE HEADLINE CARRIES NO `run` AT ALL. It was the Elapsed tile's
  // figure a second time -- `never ran` here, `never ran` one row down -- so
  // the tile below is its one home. BREAK IT: put the `run` fact back.
  assert.ok(!markup.includes('<b>run</b>'), 'the Headline repeats the Elapsed tile under `run`')
  // The Elapsed tile says it too, in its figure or in its note -- whichever
  // `elapsed()` leaves it to. The figure is `elapsed()`'s to choose (the
  // shared-types lane makes it `never ran` outright); what this screen must
  // never do is show a duration there with nothing saying no run happened.
  const tile = tiles(surface(cascade())).get('Elapsed')
  assert.ok(tile, 'the Elapsed tile is gone')
  assert.ok(
    /never ran/.test(tile.value) || /^never ran · cancelled/.test(tile.sub),
    `the Elapsed tile reads "${tile.value}" / "${tile.sub}" and never says this never ran`,
  )
})

/**
 * AG-3, THE NOTE UNDER A FIGURE THAT ALREADY SAYS IT. Since #145, `elapsed()`
 * gives a task that finished without starting the figure `never ran`. The
 * note under it still opened `never ran · cancelled 3d ago`: the same two
 * words twice, one line apart, with the one fact the figure cannot carry --
 * how the task ended, and when -- pushed behind them.
 *
 * BREAK IT: prefix the never-ran note with `never ran · ` again.
 */
test('a task that never ran says so once on its Elapsed tile, and the note names how it ended', () => {
  const tile = tiles(surface(cascade())).get('Elapsed')
  assert.ok(tile, 'the Elapsed tile is gone')
  assert.equal(tile.value, 'never ran')
  assert.ok(!/never ran/.test(tile.sub), `the note repeats the figure: "${tile.sub}"`)
  assert.match(tile.sub, /^cancelled \S/, `the note does not say how the task ended: "${tile.sub}"`)

  // With no end recorded, the ending is still named, and so is the absence.
  const unended = tiles(surface(run({ ...cascade(), task: { ...CASCADE, completed_at: null } }))).get('Elapsed')
  assert.ok(unended, 'the Elapsed tile is gone')
  assert.equal(unended.value, 'never ran')
  assert.ok(!/never ran/.test(unended.sub), `the note repeats the figure: "${unended.sub}"`)
  assert.match(unended.sub, /^cancelled\b.*no finish recorded/, `the note hides the missing end: "${unended.sub}"`)
})

/**
 * WHERE THE WAIT AND THE AGE ARE SAID NOW (agent-details-v3.html A): a wait
 * in the Now card's phase line, the age in Progress's `submitted` line. The
 * head's facts strip that said both is gone.
 */
function nowCard(markup: string): string {
  const at = markup.indexOf('data-lead="now"')
  assert.ok(at >= 0, 'the Now card is gone; every assertion below would be vacuous')
  return markup.slice(at, markup.indexOf('</section>', at))
}

function submittedLine(markup: string): string {
  const m = /<p class="dt-note"[^>]*>submitted [^<]*<\/p>/.exec(markup)
  assert.ok(m, 'the submitted line is gone')
  return m[0]
}

/**
 * A PARKED RETRY. Submitted at 09:00; attempt 1 reached STARTING at 09:09 and
 * later parked on quota. `started_at` survives the park -- nothing on the
 * platform clears it -- and the task document records no time for the park.
 *
 * The first fix for this keyed the strip on `elapsed()`'s phase and printed
 * the task's age under `wait`: `wait waiting 50m 0s` beside `age 50m ago`,
 * fifty minutes of waiting for a task that ran for most of them. On the
 * retry's lease it read `wait leased 1h 30m` for a lease taken seconds ago.
 *
 * BREAK IT: key the fact `wait` on `phase === 'waiting'` alone, and let
 * `elapsed()` print `now - created_at` for a task that has a start.
 */
test('a task between attempts shows no wait and no figure the task document does not hold', () => {
  for (const state of ['PARKED', 'READY', 'LEASED', 'DISPATCHED'] as const) {
    const between: Task = {
      ...TASK,
      state,
      started_at: '2026-09-22T09:09:00Z',
      completed_at: null,
      current_lease_id: state === 'LEASED' || state === 'DISPATCHED' ? 'lease_2' : null,
      attempt_count: state === 'LEASED' || state === 'DISPATCHED' ? 2 : 1,
    }
    const markup = plain(surface(run({ task: between, attempts: [{ ...ATTEMPT, completed_at: null, exit_code: null }] })))
    const facts = nowCard(markup)
    assert.ok(!/\bwait(ing)?\b/.test(facts), `${state}: a task that already ran is shown a wait`)
    assert.match(submittedLine(markup), / ago<\/p>$/, `${state}: the age is gone`)
    assert.ok(
      !new RegExp(`${state.toLowerCase()} \\d`).test(facts),
      `${state}: the strip prints the task's age after its state word, which reads as time in that state`,
    )

    const tile = tiles(markup).get('Elapsed')
    assert.ok(tile, `${state}: the Elapsed tile is gone`)
    assert.ok(!/^waiting\b/.test(tile.value), `${state}: the Elapsed tile calls the age a wait: "${tile.value}"`)
    assert.ok(!/\d/.test(tile.value), `${state}: the Elapsed tile prints a figure: "${tile.value}"`)
    // Why there is no figure is on the surface, not only in a `?`.
    assert.ok(/earlier attempt ran/.test(tile.sub), `${state}: the tile does not say an attempt ran: "${tile.sub}"`)
  }
})

test('an unstarted waiting task keeps its wait, because its whole age is one', () => {
  // THE CONTROL, so the test above cannot pass by never printing `wait`.
  const ready: Task = { ...TASK, state: 'READY', started_at: null, completed_at: null, current_lease_id: null }
  const facts = nowCard(plain(surface(run({ task: ready, attempts: [] }))))
  assert.ok(/no attempt yet · waiting \d/.test(facts), `an unstarted READY task lost its wait: ${facts}`)
})

test('the error banner names the scheduler for a cascade cancel, not the agent', () => {
  // BREAK IT: fall back to `agent, at finish` for any text the prefixes do
  // not recognise -- the old rule. No agent existed to write this.
  const markup = plain(surface(cascade()))
  assert.ok(markup.includes('Error written by <b>scheduler · cancel</b>'), 'the cascade cancel is not attributed to the scheduler')
  assert.ok(!markup.includes('agent, at finish'), 'a task that never ran blames its agent')
})

test('the error banner reads a lowercase dispatch code as the scheduler’s', () => {
  // BREAK IT: make DISPATCH_CODE case-sensitive again (`^[A-Z][A-Z0-9_]+`).
  // Every real code is lowercase, so every dispatch failure became the agent's.
  const failed: Task = {
    ...TASK,
    state: 'FAILED',
    attempt_count: 3,
    started_at: null,
    last_error: 'gke_create_job_failed (attempt att_3)',
  }
  const unstarted: AttemptRow = { ...ATTEMPT, started_at: null, completed_at: null, exit_code: null }
  const markup = plain(surface(run({ task: failed, attempts: [unstarted] })))
  assert.ok(markup.includes('Error written by <b>scheduler · dispatch</b>'), 'a lowercase dispatch code is not read as a dispatch failure')
})

test('the error banner still names the agent when the agent ran and failed', () => {
  // THE CONTROL, so the two tests above cannot pass by never saying `agent`.
  const failed: Task = { ...TASK, state: 'FAILED', last_error: 'claude exited 1: tool call failed' }
  const ran: AttemptRow = { ...ATTEMPT, exit_code: 1, error: 'claude exited 1: tool call failed' }
  const markup = plain(surface(run({ task: failed, attempts: [ran] })))
  assert.ok(markup.includes('Error written by <b>agent, at finish</b>'), 'an agent’s own failure is no longer attributed to it')
})

test('a finished task that never ran has nothing-ran as its output, a real zero', () => {
  // BREAK IT: remove the `!anythingRan` branch from Output. The step then says
  // `finished with no summary`, partial -- a missing record -- for the one
  // result that was never going to be written.
  const markup = plain(surface(cascade()))
  assert.ok(markup.includes('nothing ran · cancelled'), 'the output panel does not say nothing ran')
  assert.ok(!markup.includes('finished with no summary'), 'a task that never ran is drawn as a missing summary')
})

// ---------------------------------------------------------------------------
// The reason, once (AG-7)
// ---------------------------------------------------------------------------

test('a failed agent’s reason is printed once, and an earlier attempt’s own error still shows', () => {
  // BREAK IT: render `Why` and the attempt card's `pre.err` unconditionally.
  // The last error is then printed three times.
  const reason = 'claude exited 1: the repository has no main branch'
  const failed: Task = { ...TASK, state: 'FAILED', attempt_count: 2, last_error: reason }
  const first: AttemptRow = {
    ...ATTEMPT,
    attempt_id: 'att_0',
    lease_id: 'lease_0',
    created_at: '2026-09-22T09:00:30Z',
    exit_code: 1,
    error: 'first attempt: timed out cloning',
  }
  const last: AttemptRow = { ...ATTEMPT, exit_code: 1, error: reason }
  const markup = surface(run({ task: failed, attempts: [first, last] }))
  const count = markup.split(reason).length - 1
  assert.equal(count, 1, `the reason is printed ${count} times`)
  assert.ok(markup.includes('first attempt: timed out cloning'), 'an earlier attempt’s different error was dropped')
})

// ---------------------------------------------------------------------------
// Marks that are not in flight, and one name per attempt (AG-9, AG-21)
// ---------------------------------------------------------------------------

/** The Progress card's event list and its paging note, as markup. */
function timelineToolbar(markup: string): string {
  const at = markup.indexOf('<b>Progress</b>')
  assert.ok(at >= 0, 'the Progress card is not on the screen')
  return markup.slice(at, markup.indexOf('</section>', at))
}

test('the timeline does not draw a read in flight once its read has landed', () => {
  // BREAK IT: put `kind={endMissing ? 'partial' : 'pending'}` back.
  const events = [event({ type: 'submitted', at: '2026-09-22T09:00:00Z' })]
  const unproven = timelineToolbar(surface(run({ task: RUNNING_TASK, attempts: [RUNNING_ATTEMPT], events })))
  assert.ok(!unproven.includes('is-pending'), 'the landed timeline still says `reading`')
  assert.ok(unproven.includes('one page, cap unknown'), 'the paging caveat is gone from the event list')
  // THE PROVEN CASE KEEPS ITS MARK: a SUCCEEDED task with no terminal event
  // on the page proves the page ends early.
  const proven = timelineToolbar(surface(run({ events })))
  assert.ok(proven.includes('ctl-mark is-partial'), 'a provably short page is no longer marked partial')
})

test('one attempt has one name on its card and over its events', () => {
  // BREAK IT: title the card `Attempt {n}` with a `gen` chip, or label the
  // event group `generation`. The same attempt then has two spellings.
  const events = [
    event({ type: 'submitted', at: '2026-09-22T09:00:00Z' }),
    event({ type: 'starting', at: '2026-09-22T09:01:10Z', attempt_id: 'att_1' }),
  ]
  const markup = plain(surface(run({ events })))
  const names = markup.split('Attempt 1 · gen 1').length - 1
  assert.ok(names >= 2, `the card and its event group do not share one name (${names} found)`)
  assert.ok(!markup.includes('Attempt 1 · generation'), 'the event group spells the attempt differently')
})

// ---------------------------------------------------------------------------
// The drawer re-reads while there is something to learn (AG-2)
// ---------------------------------------------------------------------------

test('the drawer re-reads an unfinished run, and stops once it is finished', () => {
  // BREAK IT: return DRAWER_POLL_MS for a finished task. A finished run's
  // documents are final once its finish is whole; an unfinished one's
  // liveness is only as fresh as its last read.
  //
  // WHAT THIS DOES NOT COVER, said so nobody reads it as more: it calls
  // `drawerPoll` and checks its answers. Whether AgentDetailScreen hands it to
  // `Screen` at all -- the `pollMs={drawerPoll}` line -- is asserted by
  // rendering the screen under fake timers, in
  // src/__tests__/drawer.reread.test.tsx; dropping that line passes this test.
  // So is the half-written finish (a terminal state whose attempt end and
  // terminal event are not in yet), which keeps the drawer polling.
  //
  // `now` is an hour after TASK's `completed_at`, well past DRAWER_SETTLE_MS,
  // so a finish with no terminal event on this page is not waited for.
  const later = Date.parse('2026-09-22T10:40:00Z')
  assert.equal(drawerPoll(run({ task: RUNNING_TASK, attempts: [RUNNING_ATTEMPT] }), later), DRAWER_POLL_MS)
  assert.equal(drawerPoll(run({ task: { ...RUNNING_TASK, state: 'PARKED' } }), later), DRAWER_POLL_MS)
  for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED'] as const) {
    assert.equal(drawerPoll(run({ task: { ...TASK, state } }), later), null, `a ${state} run is still polled`)
  }
  // Before the first read there is nothing to say it is finished.
  assert.equal(drawerPoll(null), DRAWER_POLL_MS)
})
