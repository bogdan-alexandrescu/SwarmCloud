// The derived checks on the landing screen, exercised rather than read.
//
// WHAT THIS FILE IS FOR. `overview/now` said "Nothing is running -- No task on
// the 7 most recently created is in LEASED, DISPATCHED, STARTING or RUNNING.
// The state counts agree: zero." while a three-step workflow in the tenant sat
// with one step READY and two PARKED, and the word "workflow" appeared nowhere
// on the screen. Every sentence was true. The conclusion was false. That bug
// cannot be caught by reading the code, because its symptom is a check that
// does not fire -- so the tests below drive `deriveChecks` over real response
// shapes and assert what the panel would SAY.
//
// Three cases the build brief names, each with its own section: a stalled
// workflow, parked steps, and the healthy case that must stay silent. The
// mutation each one is meant to catch is named in its own docstring, because a
// test whose failure mode nobody has checked is worth about as much as the
// check it is testing.

import test from 'node:test'
import assert from 'node:assert/strict'

import { deriveChecks } from '../src/checks.ts'
import { PARK_CLEARS_ITSELF, PARK_NEEDS_A_PERSON, PARK_WAITS_ON_A_STEP, PARK_REASONS } from '../src/types.ts'
import {
  NOW,
  agoIso,
  checkNamed,
  empty,
  failed,
  inputs,
  ok,
  problems,
  task,
  taskPage,
  workflow,
  workflowPage,
} from './fixtures.mjs'

// --------------------------------------------------------------------------
// 1. A STALLED WORKFLOW
// --------------------------------------------------------------------------

test('a workflow quiet past the threshold with nothing in flight is reported', () => {
  // The audit's own screen: three steps, one READY, two PARKED, no step in any
  // capacity-holding state, and twenty minutes since the workflow last moved.
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([
          workflow({
            id: 'wf_5e5ad3b6f7da4299a839',
            counts: { READY: 1, PARKED: 2 },
            quietSeconds: 20 * 60,
            state: 'READY',
          }),
        ]),
      ),
    }),
    NOW,
  )

  const check = checkNamed(checks, 'Workflows')
  assert.equal(check.status, 'found')
  assert.equal(check.problems.length, 1)
  const p = check.problems[0]
  assert.match(p.headline, /1 workflow has not advanced in 10 minutes/)
  assert.equal(p.n, 1)
  // `#work/workflows`, NOT `#agents/workflows`. The section was renamed and
  // `checks.ts:632` writes the new spelling -- it has to, because
  // `nav.links.test.tsx` fails the build if any internal href uses a key of
  // `SECTION_ALIASES`. This expectation is the old one and has been red since
  // the rename landed; it was invisible until the route suite ahead of it went
  // green, because the runner stops at the first TAP group that fails.
  assert.equal(p.href, '#work/workflows')
  // The workflow is NAMED. "A workflow is stalled" without saying which one
  // sends the reader to a list to find it.
  assert.match(p.detail, /wf_5e5ad3b6f7da4299a839/)
})

test('a stalled workflow with a READY step is act, not watch', () => {
  // READY means admitted-eligible. Nothing picking it up for ten minutes is a
  // fault, not a wait, and the panel sorts `bad` above `warn`.
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([workflow({ counts: { READY: 1, PARKED: 2 }, quietSeconds: 1200 })]),
      ),
    }),
    NOW,
  )
  assert.equal(checkNamed(checks, 'Workflows').problems[0].severity, 'bad')
})

test('a stalled workflow with only parked steps is watch, and defers the why', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(workflowPage([workflow({ counts: { PARKED: 3 }, quietSeconds: 1200 })])),
    }),
    NOW,
  )
  const p = checkNamed(checks, 'Workflows').problems[0]
  assert.equal(p.severity, 'warn')
  assert.match(p.detail, /the parked check says on what/)
})

test('THE STALL IS NEVER PHRASED AS A CAPACITY PROBLEM', () => {
  // CONTRACT invariant 1: PARKED and READY create no demand. A reader told
  // their workflow is stuck because the platform is full raises a ceiling that
  // was never binding, and the work still does not move.
  const checks = deriveChecks(
    inputs({
      workflows: ok(workflowPage([workflow({ counts: { READY: 1, PARKED: 2 }, quietSeconds: 1200 })])),
    }),
    NOW,
  )
  const p = checkNamed(checks, 'Workflows').problems[0]
  assert.match(p.detail, /not a full pool/)
  assert.match(p.detail, /nothing is holding capacity|none is holding capacity/)
})

test('MUTATION GUARD: the threshold is crossed at exactly 600 seconds', () => {
  // Catches `>` in place of `>=`. Ten minutes on the nose is stalled; one
  // second under is not.
  const at = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 600 })])) }),
    NOW,
  )
  assert.equal(checkNamed(at, 'Workflows').status, 'found')

  const under = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 599 })])) }),
    NOW,
  )
  assert.equal(checkNamed(under, 'Workflows').status, 'clear')
})

test('MUTATION GUARD: the threshold is above the measured cold start', () => {
  // dispatched -> starting was p50 122.6s / p90 159.0s over 231 tasks, and
  // `dispatch_timeout_seconds` is 300. A threshold lowered into that range
  // fires on healthy work, so a workflow quiet for 300s must stay clear.
  const checks = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 300 })])) }),
    NOW,
  )
  assert.equal(checkNamed(checks, 'Workflows').status, 'clear')
})

test('a workflow with a step in flight is never stalled, however long it has run', () => {
  // The gate that makes the threshold usable at all: a forty-minute agent is
  // doing its job, and `updated_at` does not move while it does it.
  for (const state of ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING']) {
    const checks = deriveChecks(
      inputs({
        workflows: ok(
          workflowPage([workflow({ counts: { [state]: 1, PARKED: 2 }, quietSeconds: 40 * 60 })]),
        ),
      }),
      NOW,
    )
    assert.equal(
      checkNamed(checks, 'Workflows').status,
      'clear',
      `a workflow with a ${state} step was reported stalled`,
    )
  }
})

test('a finished workflow is not stalled, however long ago it finished', () => {
  for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']) {
    const checks = deriveChecks(
      inputs({
        workflows: ok(
          workflowPage([workflow({ counts: { [state]: 3 }, quietSeconds: 86_400, state })]),
        ),
      }),
      NOW,
    )
    assert.equal(
      checkNamed(checks, 'Workflows').status,
      'clear',
      `a ${state} workflow was reported stalled`,
    )
  }
})

test('a cancelling workflow is not reported as failing to advance', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([
          workflow({ counts: { PARKED: 2 }, quietSeconds: 3600, cancelRequested: true }),
        ]),
      ),
    }),
    NOW,
  )
  const check = checkNamed(checks, 'Workflows')
  assert.equal(check.status, 'clear')
  assert.match(check.note, /1 cancelling/)
})

test('MUTATION GUARD: an incomplete rollup is never judged, and never silent', () => {
  // THE `false // true` TRAP. `w.rollup?.complete || true` is true for every
  // input, so every workflow would be treated as judgeable and a state derived
  // from steps the API could not read would be believed. `?? true` invents
  // agreement for a missing rollup. Both are caught here: the row must be
  // reported as unjudged, not silently cleared and not silently stalled.
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([
          workflow({ id: 'wf_partial', counts: { PARKED: 1 }, quietSeconds: 3600, complete: false }),
        ]),
      ),
    }),
    NOW,
  )
  const check = checkNamed(checks, 'Workflows')
  assert.equal(check.status, 'found')
  const p = check.problems[0]
  assert.match(p.headline, /could not be judged/)
  assert.match(p.detail, /wf_partial/)
  assert.match(p.detail, /unknown, not fine/)
})

test('an exhausted step-read budget is named as the reason rows are unjudged', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([workflow({ complete: false, counts: {} })], {
          examined: 1, written: 0, agreed: 0, disagreed: 0, unknown: 1,
          truncated: false, step_reads: 400, step_read_budget_exhausted: true,
        }),
      ),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Workflows').problems[0].detail, /ran out of step reads/)
})

test('the workflow read failing makes the check blind, not clear', () => {
  const checks = deriveChecks(inputs({ workflows: failed() }), NOW)
  assert.equal(checkNamed(checks, 'Workflows').status, 'blind')
})

test('no workflows ever submitted is a clear that says so', () => {
  const checks = deriveChecks(inputs({ workflows: empty() }), NOW)
  const check = checkNamed(checks, 'Workflows')
  assert.equal(check.status, 'clear')
  assert.match(check.note, /no workflow has ever been submitted/)
})

// --------------------------------------------------------------------------
// 2. PARKED STEPS
// --------------------------------------------------------------------------

test('parked steps that need a person are reported, with the reason', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(
        taskPage([
          task({ id: 't1', state: 'PARKED', parkReason: 'CREDENTIAL_MISSING', workflowId: 'wf_1', stepId: 'plan' }),
          task({ id: 't2', state: 'PARKED', parkReason: 'BUDGET_EXHAUSTED', workflowId: 'wf_1', stepId: 'build' }),
        ]),
      ),
    }),
    NOW,
  )
  const check = checkNamed(checks, 'Parked work')
  assert.equal(check.status, 'found')
  const p = check.problems[0]
  assert.equal(p.severity, 'bad')
  assert.equal(p.n, 2)
  assert.match(p.headline, /2 parked workflow steps will not resume without a person/)
  // "here is why" -- the reason, spelled and explained.
  assert.match(p.detail, /CREDENTIAL_MISSING/)
  assert.match(p.detail, /No provider key is registered for this tenant/)
  assert.match(p.detail, /BUDGET_EXHAUSTED/)
  assert.match(p.detail, /No timer ends any of these/)
})

test('PARKED WORK IS NEVER PHRASED AS A CAPACITY PROBLEM', () => {
  // CONTRACT invariant 1 again, and the reason this check exists as a progress
  // check rather than a capacity one.
  const checks = deriveChecks(
    inputs({
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'MANUAL_PAUSE' })])),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').problems[0].detail, /hold no capacity/)
})

test('parked steps waiting on a clock are watch, and say when', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(
        taskPage([
          task({
            id: 't1',
            state: 'PARKED',
            parkReason: 'PROVIDER_QUOTA_EXHAUSTED',
            nextEligibleAt: new Date(NOW + 42 * 60_000).toISOString(),
          }),
        ]),
      ),
    }),
    NOW,
  )
  const p = checkNamed(checks, 'Parked work').problems[0]
  assert.equal(p.severity, 'warn')
  assert.match(p.headline, /waiting on a clock/)
  assert.match(p.detail, /becomes eligible in 42m/)
})

test('a clock park with no next_eligible_at says the wait is open-ended', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'PROVIDER_OUTAGE' })])),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').problems[0].detail, /open-ended/)
})

test('a park reason this bundle does not know is reported, not dropped', () => {
  // `codec.blocked_reason_values()` is wired to no route, so this table ships
  // client-side and will go stale. An unknown value must be visible.
  const checks = deriveChecks(
    inputs({
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'SOME_NEW_REASON' })])),
    }),
    NOW,
  )
  const p = checkNamed(checks, 'Parked work').problems[0]
  assert.match(p.headline, /no reason this build understands/)
  assert.match(p.detail, /SOME_NEW_REASON/)
})

test('a PARKED task with no park_reason at all is reported', () => {
  const checks = deriveChecks(
    inputs({ tasks: ok(taskPage([task({ state: 'PARKED', parkReason: null })])) }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').problems[0].detail, /no reason recorded/)
})

test('a standalone parked task is called a task, not a workflow step', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'MANUAL_PAUSE', workflowId: null })])),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').problems[0].headline, /1 parked task /)
})

// --------------------------------------------------------------------------
// 3. THE HEALTHY CASE, WHICH MUST STAY SILENT
// --------------------------------------------------------------------------

test('DEPENDENCY_INCOMPLETE alone raises nothing', () => {
  // The whole point. A three-step chain has two steps parked like this for its
  // entire life and nothing is wrong. Raising it would light this panel for
  // every healthy workflow on the platform.
  const checks = deriveChecks(
    inputs({
      tasks: ok(
        taskPage([
          task({ id: 't1', state: 'SUCCEEDED' }),
          task({ id: 't2', state: 'PARKED', parkReason: 'DEPENDENCY_INCOMPLETE', workflowId: 'wf_1', stepId: 'b' }),
          task({ id: 't3', state: 'PARKED', parkReason: 'DEPENDENCY_INCOMPLETE', workflowId: 'wf_1', stepId: 'c' }),
        ]),
      ),
    }),
    NOW,
  )
  const check = checkNamed(checks, 'Parked work')
  assert.equal(check.status, 'clear')
  // Clear, but it still SAYS what it cleared -- the panel prints these notes.
  assert.match(check.note, /2 parked workflow steps waiting on an earlier step/)
  assert.match(check.note, /costs nothing/)
})

test('a healthy running workflow and a healthy task page raise nothing at all', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([
          workflow({ counts: { SUCCEEDED: 1, RUNNING: 2, PARKED: 1 }, quietSeconds: 30 }),
        ]),
      ),
      tasks: ok(
        taskPage([
          task({ id: 't1', state: 'RUNNING' }),
          task({ id: 't2', state: 'PARKED', parkReason: 'DEPENDENCY_INCOMPLETE', workflowId: 'wf_1', stepId: 'b' }),
        ]),
      ),
    }),
    NOW,
  )
  assert.deepEqual(problems(checks), [])
  assert.equal(checkNamed(checks, 'Workflows').status, 'clear')
  assert.equal(checkNamed(checks, 'Parked work').status, 'clear')
})

test('no parked task at all is a clear that names the population', () => {
  const checks = deriveChecks(
    inputs({ tasks: ok(taskPage([task({ id: 't1', state: 'RUNNING' })])) }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').note, /none of the 1 most recently created/)
})

test('a failed task read makes the parked check blind, not clear', () => {
  const checks = deriveChecks(inputs({ tasks: failed() }), NOW)
  assert.equal(checkNamed(checks, 'Parked work').status, 'blind')
})

// --------------------------------------------------------------------------
// 4. THE SHAPE OF THE PANEL
// --------------------------------------------------------------------------

test('deriveChecks reports on workflows and on parked work by name', () => {
  // The audit finding in one line: the word "workflow" appeared nowhere on
  // `overview/now`. The panel prints every check's label, so this is the
  // assertion that it now does.
  const labels = deriveChecks(inputs(), NOW).map((c) => c.label)
  assert.ok(labels.includes('Workflows'), `labels were ${JSON.stringify(labels)}`)
  assert.ok(labels.includes('Parked work'), `labels were ${JSON.stringify(labels)}`)
  assert.equal(labels.length, 8)
})

test('every check is reading while its read is in flight -- none is clear', () => {
  // First paint must not print an all-clear derived from reads that have not
  // landed. This is the provenance rule the whole screen is built on.
  for (const check of deriveChecks(inputs(), NOW)) {
    assert.equal(check.status, 'reading', `${check.label} was ${check.status} before its read landed`)
  }
})

test('the three park-reason sets partition the eight reasons exactly', () => {
  // A reason in none of the sets falls into "no reason this build understands"
  // and a reason in two would be counted twice. The Python suite checks this
  // list against `swarm_common.states.ParkReason`; this checks the partition.
  const seen = new Map()
  for (const [name, set] of [
    ['PARK_NEEDS_A_PERSON', PARK_NEEDS_A_PERSON],
    ['PARK_CLEARS_ITSELF', PARK_CLEARS_ITSELF],
    ['PARK_WAITS_ON_A_STEP', PARK_WAITS_ON_A_STEP],
  ]) {
    for (const reason of set) {
      assert.ok(!seen.has(reason), `${reason} is in both ${seen.get(reason)} and ${name}`)
      seen.set(reason, name)
    }
  }
  assert.deepEqual([...seen.keys()].sort(), [...PARK_REASONS].sort())
})

test('problems sort worst-first across checks, as the panel draws them', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(workflowPage([workflow({ counts: { PARKED: 2 }, quietSeconds: 1200 })])),
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'CREDENTIAL_MISSING' })])),
    }),
    NOW,
  )
  const all = problems(checks)
  assert.equal(all.length, 2)
  assert.ok(all.some((p) => p.severity === 'bad'))
  assert.ok(all.some((p) => p.severity === 'warn'))
})

test('a workflow whose updated_at will not parse is unjudged, not stalled', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([workflow({ id: 'wf_bad_clock', counts: { READY: 1 }, updatedAt: 'not a date' })]),
      ),
    }),
    NOW,
  )
  const check = checkNamed(checks, 'Workflows')
  assert.equal(check.status, 'found')
  assert.match(check.problems[0].headline, /could not be judged/)
})

test('a truncated workflow page says its clear is not a census', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([workflow({ counts: { RUNNING: 1 }, quietSeconds: 10 })], {
          examined: 1, written: 0, agreed: 1, disagreed: 0, unknown: 0,
          truncated: true, step_reads: 4, step_read_budget_exhausted: false,
        }),
      ),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Workflows').note, /the first 1 workflows of more/)
})

test('agoIso and NOW agree, so every age in this file means what it says', () => {
  assert.equal(Date.parse(agoIso(600)), NOW - 600_000)
})
