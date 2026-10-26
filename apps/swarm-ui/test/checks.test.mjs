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
  // THE AGE, NOT THE THRESHOLD (live QA OV-9, 2026-10-11): twenty minutes
  // quiet, past a twelve-minute threshold, says twenty.
  assert.match(p.headline, /1 workflow has not advanced for 20 minutes/)
  assert.equal(p.n, 1)
  // `#work/workflows`, NOT `#agents/workflows`.
  //
  // This has been failing on this branch since the rename moved the section to
  // `work`. SECTION_ALIASES keeps the old href WORKING, which is why nothing in
  // the app looked broken, and this assertion is the only thing that noticed --
  // it is the one place in this file that names a route.
  //
  // IT WAS ALSO INVISIBLE. It was the second red in the node runner, behind
  // route.test.ts, and that runner stops at the first failing file: the TAP
  // output ended at `# fail 1` there and never reached this line. Two greps of
  // the same rename found both; one CI run found one.
  //
  // ASSERTED AS EXACT EQUALITY rather than as a pattern. The help-density lane
  // reached this file independently and proposed `/^#[a-z-]+\/workflows$/` plus
  // a "not #agents/" guard, to avoid pinning a spelling. Equality is stronger
  // here and the reason is that the pattern admits `#capacity/workflows` -- a
  // link to a pane that does not exist in that section, which is the mistake
  // most likely to be made. The generic rule the pattern was reaching for is
  // already enforced globally: `nav.links.test.tsx` reads every internal href in
  // the app and fails on any that uses a retired id or names a pane that does
  // not exist. One exact value here, one general rule there.
  //
  // ONE STALLED WORKFLOW OPENS THAT WORKFLOW (live QA OV-9): the unfiltered
  // list left the reader to find it. Several open the list (next test).
  assert.equal(p.href, '#work/workflows?wf=wf_5e5ad3b6f7da4299a839')
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

test('MUTATION GUARD: the threshold is crossed at exactly 720 seconds', () => {
  // Catches `>` in place of `>=`. Twelve minutes on the nose is stalled; one
  // second under is not. (#404 raised the dispatch deadline to 480s, which
  // raised this threshold from 600 to 720 so it still clears deadline + p90
  // cold start; contract request 37.)
  const at = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 720 })])) }),
    NOW,
  )
  assert.equal(checkNamed(at, 'Workflows').status, 'found')

  const under = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 719 })])) }),
    NOW,
  )
  assert.equal(checkNamed(under, 'Workflows').status, 'clear')
})

test('MUTATION GUARD: the threshold is above the measured cold start', () => {
  // dispatched -> starting was p50 122.6s / p90 159.0s over 231 tasks, and
  // `dispatch_timeout_seconds` is 480 (contract request 37; it was 300). A
  // threshold lowered into that range fires on healthy work, so a workflow
  // quiet for 480s must stay clear.
  const checks = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 480 })])) }),
    NOW,
  )
  assert.equal(checkNamed(checks, 'Workflows').status, 'clear')
})

test('MUTATION GUARD: the threshold clears the dispatch deadline plus the p90 cold start', () => {
  // #404 moved `dispatch_timeout_seconds` from 300 to 480. 480 + the measured
  // p90 cold start of 159.0s is about 639s -- a threshold at or below that sum
  // can fire on a task that is merely still inside its deadline and its
  // ordinary cold start, before anything could have fenced and redispatched
  // it. 720 must stay clear at that sum.
  const checks = deriveChecks(
    inputs({ workflows: ok(workflowPage([workflow({ counts: { READY: 1 }, quietSeconds: 480 + 159 })])) }),
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

test('a standalone parked agent is called an agent, not a workflow step', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(taskPage([task({ state: 'PARKED', parkReason: 'MANUAL_PAUSE', workflowId: null })])),
    }),
    NOW,
  )
  assert.match(checkNamed(checks, 'Parked work').problems[0].headline, /1 parked agent /)
})

/**
 * VOCAB-AGENTS (QA-109, part of #1680; owner decision 2026-10-20): the checks
 * count agents, the word the Agents page they link to uses, never tasks --
 * one agent, N agents.
 */
test('VOCAB-AGENTS: the failures and parked checks count agents, singular and plural', () => {
  const one = checkNamed(
    deriveChecks(inputs({ tasks: ok(taskPage([task({ state: 'FAILED' })])) }), NOW),
    'Failures',
  ).problems[0]
  // MUTATION: put back `failed task${...}` in failedProblem's headline.
  assert.match(one.headline, /^1 failed agent among/)
  const two = checkNamed(
    deriveChecks(
      inputs({
        tasks: ok(taskPage([
          task({ state: 'PARKED', parkReason: 'MANUAL_PAUSE', workflowId: null }),
          task({ state: 'PARKED', parkReason: 'MANUAL_PAUSE', workflowId: null }),
        ])),
      }),
      NOW,
    ),
    'Parked work',
  ).problems[0]
  // MUTATION: put back `'task' : 'tasks'` in unitFor.
  assert.match(two.headline, /^2 parked agents /)
  assert.doesNotMatch(`${one.headline} ${two.headline}`, /\btasks?\b/)
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

// --------------------------------------------------------------------------
// OV-10: THE LINK OPENS THE LIST IT NAMES
// --------------------------------------------------------------------------
//
// The failed-tasks item linked to `#work/running` and said "agents · Recent
// tab" -- a how-to, because the tab could not be carried in the hash -- and the
// list then opened on Live whenever anything ran. The three parked items had
// the same pattern with "Waiting tab". The tab and the Recent state are
// addresses now (App.tsx, `agentlist.ts`), so each link opens exactly the list
// it names and its word names the destination, as OV-11 set for card heads.
//
// MUTATION: point either back at `#work/running`, or label it by the tab.

// QA-109 (visual QA 2026-10-20): the word is the page's own title, Agents,
// and the state the item counts -- not "failed agents", a fourth name for
// the page that nothing else on it carries. The state is a word, not the
// enum in capitals (visual QA VQ-003, 2026-10-26).
test('OV-10: the failures item opens the failed agents, and says so', () => {
  const checks = deriveChecks(
    inputs({ tasks: ok(taskPage([task({ id: 't1', state: 'FAILED', lastError: 'exit 1' })])) }),
    NOW,
  )
  const [p] = checkNamed(checks, 'Failures').problems
  assert.equal(p.href, '#work/running/recent/failed')
  assert.equal(p.linkLabel, 'Agents · failed')
})

test('OV-10: every parked item opens the waiting agents, and says so', () => {
  const checks = deriveChecks(
    inputs({
      tasks: ok(
        taskPage([
          task({ id: 't1', state: 'PARKED', parkReason: 'CREDENTIAL_MISSING' }),
          task({ id: 't2', state: 'PARKED', parkReason: 'PROVIDER_OUTAGE' }),
          task({ id: 't3', state: 'PARKED', parkReason: 'SOME_NEW_REASON' }),
        ]),
      ),
    }),
    NOW,
  )
  const parked = checkNamed(checks, 'Parked work').problems
  // All three kinds: a person, a clock, and a reason this build does not know.
  assert.equal(parked.length, 3)
  for (const p of parked) {
    assert.equal(p.href, '#work/running/waiting', p.headline)
    assert.equal(p.linkLabel, 'waiting agents', p.headline)
  }
})

// --------------------------------------------------------------------------
// LIVE QA, OVERVIEW (2026-10-11): OV-2, OV-3, OV-8, OV-9
// --------------------------------------------------------------------------

/** A row as the checks read it, created and ended at the given ages. */
function ended(id, state, { createdMinAgo, endedMinAgo, lastError = null }) {
  return {
    ...task({ id, state, lastError }),
    created_at: agoIso(createdMinAgo * 60),
    updated_at: agoIso(endedMinAgo * 60),
    completed_at: agoIso(endedMinAgo * 60),
  }
}

/**
 * The live phone: 50 newest-created rows, none FAILED, with more behind
 * them, and 11 tasks that failed in the last 24h -- created earlier.
 */
function phonePage({ withFailedRead = true } = {}) {
  const rows = Array.from({ length: 50 }, (_, i) => ended(`ok_${i}`, 'SUCCEEDED', { createdMinAgo: 10 + i, endedMinAgo: 5 + i }))
  const failed = Array.from({ length: 11 }, (_, i) =>
    ended(`bad_${i}`, 'FAILED', { createdMinAgo: 300 + i, endedMinAgo: 20 + i, lastError: `error ${i}` }),
  )
  return {
    tasks: rows,
    next_page_token: 'more',
    tenant_id: 'u-test',
    ...(withFailedRead ? { byState: [{ state: 'FAILED', tasks: failed, complete: true }] } : {}),
  }
}

/**
 * OV-2: the phone dropped the failed-tasks check ("8/8 · 2 found something"
 * against 3 on the desktop) because none of its 50 newest was FAILED.
 *
 * MUTATION: count the page alone (drop the `byState` branch).
 */
test('OV-2: a phone page with no failure on it still finds the 11 the FAILED read holds', () => {
  const checks = deriveChecks(inputs({ tasks: ok(phonePage()), stats: loading_() }), NOW)
  const check = checkNamed(checks, 'Failures')
  assert.equal(check.status, 'found')
  const [p] = check.problems
  assert.equal(p.n, 11)
  assert.match(p.headline, /^11 failed agents in the last 24 hours/)
  // Every FAILED task was read, so the figure is not a lower bound.
  assert.doesNotMatch(p.headline, /≥/)
})

/**
 * A FAILED read with more behind it is a lower bound, and says from when, in
 * the words and over the sample Recent failures uses (visual QA VQ-005,
 * 2026-10-26: "FAILED agents created since 06:04 UTC read" above "partial:
 * agents created since 09:06 UTC read").
 */
test('OV-2: a FAILED read with more behind it is ≥N, and says how far back it reaches', () => {
  const page = phonePage()
  page.byState[0].complete = false
  const [p] = checkNamed(deriveChecks(inputs({ tasks: ok(page) }), NOW), 'Failures').problems
  assert.match(p.headline, /^≥11 failed agents in the last 24 hours/)
  assert.match(p.headline, / · partial: agents created since \d\d:\d\d UTC read/)
  assert.doesNotMatch(p.headline, /FAILED/)
})

/**
 * OV-2, the fallback: the FAILED read did not land. A partial page with no
 * failure on it is clear only when the state counts say no task ever FAILED.
 *
 * MUTATION: return `clear` for an empty partial page whatever stats say.
 */
test('OV-2: without the FAILED read, a partial page is blind while stats count failures', () => {
  const stats = (FAILED) => ok({ tenant_id: 'u-test', tasks_by_state: { FAILED }, dispatch_paused: false, limits: {}, generated_at: agoIso(0) })
  const blind = checkNamed(deriveChecks(inputs({ tasks: ok(phonePage({ withFailedRead: false })), stats: stats(83) }), NOW), 'Failures')
  assert.equal(blind.status, 'blind')
  assert.match(blind.why, /83 failed in all/)
  // The control: an exact zero clears it.
  const clear = checkNamed(deriveChecks(inputs({ tasks: ok(phonePage({ withFailedRead: false })), stats: stats(0) }), NOW), 'Failures')
  assert.equal(clear.status, 'clear')
})

/**
 * OV-3: "newest 15m ago" was the newest END, and "Newest:" the first FAILED
 * row in creation order -- another failure's error.
 *
 * MUTATION: take `failed[0]` without sorting by `completed_at`.
 */
test('OV-3: the Newest error is the failure that ended last, not the one created last', () => {
  const rows = [
    // Created last, ended earlier.
    ended('created_last', 'FAILED', { createdMinAgo: 30, endedMinAgo: 26, lastError: 'merge_conflict: pull request #1084' }),
    // Created earlier, ended last.
    ended('ended_last', 'FAILED', { createdMinAgo: 90, endedMinAgo: 15, lastError: 'checks_failed on LB-L6' }),
  ]
  const [p] = checkNamed(deriveChecks(inputs({ tasks: ok(taskPage(rows)) }), NOW), 'Failures').problems
  assert.match(p.headline, /newest 15m ago/)
  assert.match(p.detail, /Newest: checks_failed on LB-L6$/)
})

/**
 * OV-8: `reasonCopy` ends in a full stop and the caller adds ". ", so the
 * parked item read "Holds no capacity.. The first becomes eligible".
 *
 * MUTATION: drop the trailing-stop strip in `whyEach`.
 */
test('OV-8: a parked item never prints a double full stop', () => {
  const clock = [...PARK_CLEARS_ITSELF][0]
  const person = [...PARK_NEEDS_A_PERSON][0]
  const checks = deriveChecks(
    inputs({
      tasks: ok(
        taskPage([
          task({ id: 'p1', state: 'PARKED', parkReason: clock, nextEligibleAt: agoIso(-300) }),
          task({ id: 'p2', state: 'PARKED', parkReason: person }),
        ]),
      ),
    }),
    NOW,
  )
  const parked = checkNamed(checks, 'Parked work').problems
  assert.equal(parked.length, 2)
  for (const p of parked) assert.doesNotMatch(p.detail, /\.\./, p.detail)
})

/**
 * OV-9: two stalled workflows -- the headline gives the longest quiet, the
 * detail names both, and the link is the list (one workflow opens itself,
 * above).
 */
test('OV-9: several stalled workflows are all named, with the longest quiet', () => {
  const checks = deriveChecks(
    inputs({
      workflows: ok(
        workflowPage([
          workflow({ id: 'wf_older', counts: { PARKED: 2 }, quietSeconds: 30 * 60 }),
          workflow({ id: 'wf_newer', counts: { PARKED: 1 }, quietSeconds: 15 * 60 }),
        ]),
      ),
    }),
    NOW,
  )
  const [p] = checkNamed(checks, 'Workflows').problems
  assert.match(p.headline, /^2 workflows have not advanced for 12 minutes or more, the longest 30 minutes/)
  assert.match(p.detail, /wf_older/)
  assert.match(p.detail, /wf_newer/)
  assert.equal(p.href, '#work/workflows')
})

function loading_() {
  return { status: 'loading', since: NOW }
}
