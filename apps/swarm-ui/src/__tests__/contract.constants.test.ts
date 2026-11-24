// THE CONSOLE'S OWN LISTS AGAINST THE SERVER'S (part of #72).
//
// `scripts/gen_api_types.py` writes `src/generated/constants/*.json` from the
// live Python objects, and `--check` (CI, and the worker's pre-publish gate)
// fails while a file differs from them. So these tests compare the console
// with the JSON and never open a Python file: a value changed in Python
// reaches the JSON on regeneration, and the console value that did not follow
// fails here by name, whichever side moved.
//
// A console value is read as the value it exports, or, where the module that
// holds it exports none, through what it does: the check it derives, the read
// the page makes, the fixture the loader answers.
//
// Two cases read this package's own TypeScript as text (`?raw`, through vite's
// module graph): a reader that has not decided about a reason, and a list of
// reasons written out in a screen, are facts about the source, and nothing a
// screen draws tells them apart.

import { createElement } from 'react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import consoleConstants from '../generated/constants/console.json'
import statesConstants from '../generated/constants/states.json'
import { deriveChecks, type Check, type CheckInputs } from '../checks'
import { PARK_WORD } from '../components/StatePill'
import { TERMINAL_EVENTS, isTerminalEvent } from '../events'
import type { Result } from '../fetch'
import { handoffOf, ownerQuestionsHold } from '../Handoff'
import { ledgerFixture } from '../outcomes.fixture'
import { FIXED } from '../paths'
import {
  CONCURRENCY_STATES,
  PARK_CLEARS_ITSELF,
  PARK_NEEDS_A_PERSON,
  PARK_REASONS,
  PARK_WAITS_ON_A_STEP,
  REASON_COPY,
  TERMINAL_STATES,
  blockerGroup,
  canBeStopped,
  headroomFor,
  type ParkReason,
  type Task,
  type TaskState,
  type WorkflowPage,
} from '../types'
import { agentMaxWorkers } from '../vitestWorkers'
import {
  CARD_CONTENTS,
  CARD_CREATORS,
  CARD_FIELDS,
  CARD_KINDS,
  CARD_REFERENCES,
  CARD_STATES,
  COMMON_ACTIONS,
  DECIDING_ACTIONS,
} from '../voice/cards'

const api = vi.hoisted(() => ({
  loadOutcomes: vi.fn(),
  loadMe: vi.fn(),
  loadRunnerProfiles: vi.fn(),
  loadStats: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { OUTCOME_SECTIONS, loadCapacity } = await import('../api')
const { ActivityScreen, LEDGER_LATE_SECTIONS, LEDGER_SECTIONS } = await import('../Activity')

const sorted = (values: Iterable<string>): string[] => [...values].sort()

const PARK: readonly string[] = statesConstants.park_reason
const BLOCKED: readonly string[] = statesConstants.blocked_reason
const STATES = statesConstants.task_state as TaskState[]

afterEach(() => cleanup())

// ---------------------------------------------------------------------------
// States and events
// ---------------------------------------------------------------------------

describe('task states and events', () => {
  it('TERMINAL_STATES is the server\'s terminal states', () => {
    expect(sorted(TERMINAL_STATES)).toEqual(statesConstants.terminal_states)
  })

  it('CONCURRENCY_STATES is the server\'s capacity-holding states', () => {
    expect(sorted(CONCURRENCY_STATES)).toEqual(statesConstants.concurrency_states)
  })

  it('canBeStopped offers the stop on exactly the states the cancel route accepts', () => {
    const offered = STATES.filter((state) => canBeStopped({ state }))
    expect(sorted(offered)).toEqual(sorted(STATES.filter((s) => !statesConstants.terminal_states.includes(s))))
  })

  it('TERMINAL_EVENTS is the event each terminal state is announced with', () => {
    expect(sorted(TERMINAL_EVENTS)).toEqual(statesConstants.terminal_events)
  })

  it('a cancel request is an event the server writes and not an ending', () => {
    expect(statesConstants.event_type).toContain('cancel_requested')
    expect(TERMINAL_EVENTS.has('cancel_requested')).toBe(false)
    expect(isTerminalEvent({ type: 'cancel_requested', detail: {} })).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Park reasons
// ---------------------------------------------------------------------------

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false

describe('park reasons', () => {
  it('PARK_REASONS is the server\'s park reasons', () => {
    expect(sorted(PARK_REASONS)).toEqual(sorted(PARK))
  })

  it('the ParkReason union is PARK_REASONS', () => {
    // Erased at run time: `tsc` refuses this line when the union and the list differ.
    const same: Same<ParkReason, (typeof PARK_REASONS)[number]> = true
    expect(same).toBe(true)
  })

  it('the three park groups hold every park reason exactly once', () => {
    const grouped = [...PARK_NEEDS_A_PERSON, ...PARK_CLEARS_ITSELF, ...PARK_WAITS_ON_A_STEP]
    expect(sorted(grouped)).toEqual(sorted(PARK))
  })

  it('the group that raises nothing is DEPENDENCY_INCOMPLETE alone', () => {
    expect(PARK).toContain('DEPENDENCY_INCOMPLETE')
    expect([...PARK_WAITS_ON_A_STEP]).toEqual(['DEPENDENCY_INCOMPLETE'])
  })

  it('HUMAN_REQUIRED and AWAITING_INPUT both need a person', () => {
    for (const reason of ['HUMAN_REQUIRED', 'AWAITING_INPUT']) {
      expect(PARK).toContain(reason)
      expect(PARK_NEEDS_A_PERSON.has(reason), reason).toBe(true)
    }
  })

  it('PARK_WORD has the pill\'s words for exactly the park reasons', () => {
    expect(sorted(Object.keys(PARK_WORD))).toEqual(sorted(PARK))
    for (const reason of PARK) expect(PARK_WORD[reason as ParkReason].trim(), reason).not.toBe('')
  })

  it('REASON_COPY has a sentence for every park reason', () => {
    expect(PARK.filter((reason) => !(REASON_COPY[reason] ?? '').trim())).toEqual([])
  })

  it('an AWAITING_INPUT park is no browser hand-off and no merge hold', () => {
    const parked = (park_reason: string) =>
      ({
        id: 't', state: 'PARKED', park_reason, human_wait: null,
        next_eligible_at: '2026-10-17T12:00:00Z', updated_at: '2026-10-17T12:00:00Z',
        blocked_by: [{ reason: park_reason, park_phase: 'owner_questions', owner_questions: { questions: [], unreadable: 1 } }],
      }) as unknown as Task
    expect(ownerQuestionsHold(parked('HUMAN_REQUIRED')), 'the control: a HUMAN_REQUIRED hold is read').not.toBeNull()
    expect(ownerQuestionsHold(parked('AWAITING_INPUT'))).toBeNull()
    const bare = (park_reason: string) => ({ ...parked(park_reason), blocked_by: [] }) as Task
    expect(handoffOf(bare('HUMAN_REQUIRED'))?.phase, 'the control: a HUMAN_REQUIRED park is a hand-off').toBe('parked')
    expect(handoffOf(bare('AWAITING_INPUT'))).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Every console reader of HUMAN_REQUIRED decides about AWAITING_INPUT
// ---------------------------------------------------------------------------

const SOURCES: Readonly<Record<string, string>> = {
  ...import.meta.glob<string>('../**/*.{ts,tsx,js,mjs}', { query: '?raw', import: 'default', eager: true }),
  ...import.meta.glob<string>('../../*.{ts,js,mjs}', { query: '?raw', import: 'default', eager: true }),
}

/** `../Handoff.tsx`, or `./x.test.ts` for a file of this directory: vite names each relative to here. */
const isTestSource = (path: string): boolean =>
  path.startsWith('./') || path.includes('__tests__/') || /\.(test|spec)\./.test(path)

/**
 * The console modules that name HUMAN_REQUIRED and must IGNORE AWAITING_INPUT,
 * each with why. Every one is about the live-browser hand-off or a merge hold
 * (contract request 68), which an await-input step never has: no container
 * runs it (request 93), so it has no worker, no browser and no attempt. The
 * readers outside the console are the Python scan's.
 */
const MUST_IGNORE: Readonly<Record<string, string>> = {
  '../AgentSplit.tsx':
    'a merge held by the owner\'s questions has no Browser tab (#2313), read through ownerQuestionsHold; an await-input step is no browser either',
  '../QuestionsCard.tsx':
    'a merge held by the owner\'s questions (#2313) and an agent\'s blocking questions; an await-input step is answered on its own card',
  '../AgentDetail.tsx': 'the live-browser hand-off banner; the await-input card is its own',
  '../Handoff.tsx': 'the live-browser hand-off view; the await-input card is its own',
  '../LiveBrowser.tsx': 'the live browser; an await-input step has none',
}

function undecided(sources: Readonly<Record<string, string>>, ignore: Readonly<Record<string, string>>): string[] {
  return Object.keys(sources)
    .filter((path) => !isTestSource(path) && !(path in ignore))
    .filter((path) => sources[path]!.includes('HUMAN_REQUIRED') && !sources[path]!.includes('AWAITING_INPUT'))
    .sort()
}

describe('every console reader of HUMAN_REQUIRED decides about AWAITING_INPUT', () => {
  it('names both park reasons, or is on MUST_IGNORE with its reason', () => {
    const readers = Object.keys(SOURCES).filter((p) => !isTestSource(p) && SOURCES[p]!.includes('HUMAN_REQUIRED'))
    // Fewer means the glob is reading the wrong tree, not that the readers went away.
    expect(readers.length, readers.join(' ')).toBeGreaterThanOrEqual(10)
    expect(undecided(SOURCES, MUST_IGNORE)).toEqual([])
  })

  it('the tables that restate the reasons are deciders, not ignorers', () => {
    for (const path of ['../types.ts', '../components/StatePill.tsx']) {
      expect(SOURCES[path], path).toContain('HUMAN_REQUIRED')
      expect(SOURCES[path], path).toContain('AWAITING_INPUT')
      expect(path in MUST_IGNORE, path).toBe(false)
    }
  })

  it('every ignored module exists and says why', () => {
    expect(Object.keys(MUST_IGNORE).filter((path) => !(path in SOURCES))).toEqual([])
    expect(Object.values(MUST_IGNORE).every((why) => why.trim() !== '')).toBe(true)
  })

  it('refuses a reader that has not decided', () => {
    const tree = {
      '../reader.ts': 'if (t.park_reason === \'HUMAN_REQUIRED\') promote(t)',
      '../decided.ts': 'const a = \'HUMAN_REQUIRED\'; const b = \'AWAITING_INPUT\'',
      '../listed.ts': 'const a = \'HUMAN_REQUIRED\'',
      './reader.test.ts': 'expect(\'HUMAN_REQUIRED\')',
    }
    expect(undecided(tree, { '../listed.ts': 'a reason' })).toEqual(['../reader.ts'])
    expect(undecided(tree, {})).toEqual(['../listed.ts', '../reader.ts'])
  })
})

// ---------------------------------------------------------------------------
// Blocked reasons, their groups, and admission
// ---------------------------------------------------------------------------

/**
 * Members of the enum nothing writes as a blocker, so REASON_COPY leaves them
 * out on purpose (its own comment says why). SCHEDULED_RETRY is the sixth, and
 * has a sentence because a park reason spells the same.
 */
const NEVER_WRITTEN_AS_A_BLOCKER = ['BUDGET_LIMIT', 'COOLDOWN', 'DEPENDENCY', 'QUOTA_EXHAUSTED']

/** Reasons the console tests for on their own: the two whose ceiling decides who acts. */
const BY_CEILING = ['MANUAL_PAUSE', 'POOL_LIMIT_UNSET']

async function fixtureCapacity() {
  const r = await loadCapacity()
  expect(r.status, 'the fixture build answers the capacity read').toBe('ok')
  if (r.status !== 'ok') throw new Error('unreachable')
  return r.data
}

describe('blocked reasons and admission', () => {
  it('REASON_COPY names park and blocked reasons and nothing else, and leaves out only what is never written', () => {
    const known = new Set([...PARK, ...BLOCKED])
    expect(Object.keys(REASON_COPY).filter((reason) => !known.has(reason))).toEqual([])
    expect(sorted(BLOCKED.filter((reason) => !(reason in REASON_COPY)))).toEqual(NEVER_WRITTEN_AS_A_BLOCKER)
  })

  it('the served groups hold every blocked reason exactly once', () => {
    const { needs_action, no_room } = consoleConstants.blocked_reason_groups
    expect(sorted([...needs_action, ...no_room])).toEqual(sorted(BLOCKED))
  })

  it('the fixture\'s blocked_reason_groups are the served ones', async () => {
    expect((await fixtureCapacity()).blocked_reason_groups).toEqual(consoleConstants.blocked_reason_groups)
  })

  it('blockerGroup files each reason where the served grouping puts it, and holds no grouping of its own', () => {
    const swapped = {
      needs_action: consoleConstants.blocked_reason_groups.no_room,
      no_room: consoleConstants.blocked_reason_groups.needs_action,
    }
    for (const reason of BLOCKED) {
      const blocker = { reason, pool: 'resource:standard', limit: 8, active: 8 }
      if (BY_CEILING.includes(reason)) {
        expect(blockerGroup(blocker, swapped), reason).toBe('needs_action')
        continue
      }
      const served = consoleConstants.blocked_reason_groups.needs_action.includes(reason) ? 'needs_action' : 'no_room'
      const other = served === 'needs_action' ? 'no_room' : 'needs_action'
      expect(blockerGroup(blocker, consoleConstants.blocked_reason_groups), reason).toBe(served)
      expect(blockerGroup(blocker, swapped), `${reason}, with the served groups swapped`).toBe(other)
      expect(blockerGroup({ ...blocker, group: other }, consoleConstants.blocked_reason_groups), reason).toBe(other)
      expect(blockerGroup(blocker, undefined), `${reason}, with no grouping served`).toBeNull()
    }
  })

  it('no screen lists blocked reasons together as a grouping of its own', () => {
    // What is refused is a line carrying two or more of the reasons, the shape
    // a grouping takes; a reason named on its own (REASON_COPY's keys, the
    // paused tag) is not one, and neither is a ternary that PICKS one
    // (`poolGroup`). The ParkReason union is a different enum that spells two
    // members the same way, so it is cut out; comments are prose.
    const whole = (reason: string, before = '') => new RegExp(`${before}(?<![A-Z_])${reason}(?![A-Z_])`)
    const groupings = (text: string): string[] =>
      text
        .replace(/export type ParkReason =(?:\n\s*\|[^\n]*)+/, '')
        .split('\n')
        .filter((line) => !/^\s*(\*|\/\/|\/\*)/.test(line))
        .filter((line) => {
          const hits = BLOCKED.filter((reason) => whole(reason).test(line))
          return hits.length > 1 && !hits.every((reason) => whole(reason, '\\?\\s*\'?').test(line))
        })
    for (const path of ['../types.ts', '../Blockers.tsx', '../Capacity.tsx', '../Profiles.tsx']) {
      const text = SOURCES[path]
      expect(text, `${path} is not read`).toBeTruthy()
      expect(groupings(text!), path).toEqual([])
      for (const literal of ['needs_action: [', 'no_room: [', 'needs_action = [', 'no_room = [']) {
        expect(text!.includes(literal), `${path} declares its own ${literal}`).toBe(false)
      }
    }
    // The control: a list the scan must refuse.
    expect(groupings('const FULL = [\'TENANT_LIMIT\', \'RUNNER_LIMIT\']')).toHaveLength(1)
  })

  it('the fixture\'s pools are the ones the admission example was computed over', async () => {
    const pools = new Map((await fixtureCapacity()).pools.map((p) => [p.name, p]))
    const example = consoleConstants.admission_example
    for (const want of example.pools) {
      const { name, hard_limit, quota_derived_limit, adaptive_target, active, enabled, effective_limit, available } = {
        ...pools.get(want.name)!,
      }
      expect({ name, hard_limit, quota_derived_limit, adaptive_target, active, enabled, effective_limit, available }).toEqual(want)
    }
    // And no pool a fixture profile needs is served without being in the example.
    const needed = new Set(Object.values(example.profiles).flatMap((p) => p.pools))
    const named = new Set(example.pools.map((p) => p.name))
    expect([...pools.keys()].filter((n) => needed.has(n) && !named.has(n))).toEqual([])
  })

  it('the fixture\'s admission blocks are what the server\'s analyser answers over those pools', async () => {
    const served = (await fixtureCapacity()).runner_profiles
    const example = consoleConstants.admission_example.profiles
    expect(sorted(Object.keys(served))).toEqual(sorted(Object.keys(example)))
    for (const [name, want] of Object.entries(example)) {
      const got = served[name]!
      expect({ units: got.units, pools: got.pools, admission: got.admission }, name).toEqual(want)
    }
  })

  it('headroomFor answers the served admission block, never a figure of its own', async () => {
    for (const [name, profile] of Object.entries((await fixtureCapacity()).runner_profiles)) {
      const want = profile.admission!
      const got = headroomFor(profile)
      expect([got.agents, got.basis, got.blockers, got.counterfactual, got.complete], name).toEqual([
        want.headroom, want.basis, want.blockers, want.counterfactual, want.complete,
      ])
    }
  })
})

// ---------------------------------------------------------------------------
// Overview's stalled-workflow check
// ---------------------------------------------------------------------------

/**
 * The measured DISPATCHED -> STARTING p90 over 231 tasks: a threshold below it
 * fires on an ordinary cold start.
 */
const MEASURED_COLD_START_P90_SECONDS = 159

const NOW = Date.parse('2026-10-05T12:00:00Z')

/** Overview's Workflows check over one open workflow, `quiet` seconds after it last moved. */
function workflowsCheck(quiet: number, counts: Record<string, number>): Check {
  const empty = { status: 'empty', fetchedAt: NOW } as const
  const page = {
    workflows: [{
      workflow_id: 'wf_one', tenant_id: 'eng', name: 'one', cancel_requested: false,
      created_at: new Date(NOW - 3_600_000 * 24).toISOString(),
      updated_at: new Date(NOW - quiet * 1000).toISOString(),
      rollup: { state: 'PARKED', complete: true, counts, total: 1 },
      steps: [],
    }],
    next_page_token: null,
    tenant_id: 'eng',
  } as unknown as WorkflowPage
  const inputs = {
    capacity: empty, tasks: empty, leases: empty, providers: empty, accounts: empty, stats: empty,
    workflows: { status: 'ok', data: page, fetchedAt: NOW } as Result<WorkflowPage>,
  } as unknown as CheckInputs
  return deriveChecks(inputs, NOW).find((c) => c.label === 'Workflows')!
}

describe('the stalled-workflow check', () => {
  const A_DAY = 86_400

  it('the stall threshold is above the server\'s dispatch_timeout_seconds', () => {
    expect(workflowsCheck(A_DAY, { PARKED: 1 }).status, 'the control: a day\'s quiet is a stall').toBe('found')
    expect(workflowsCheck(consoleConstants.dispatch_timeout_seconds, { PARKED: 1 }).status).toBe('clear')
  })

  it('the stall threshold is above the measured cold start', () => {
    expect(workflowsCheck(MEASURED_COLD_START_P90_SECONDS, { PARKED: 1 }).status).toBe('clear')
  })

  it('a workflow is in flight on exactly the server\'s concurrency_states', () => {
    const inFlight = STATES.filter((state) => workflowsCheck(A_DAY, { [state]: 1 }).status === 'clear')
    expect(sorted(inFlight)).toEqual(statesConstants.concurrency_states)
  })
})

// ---------------------------------------------------------------------------
// The outcome ledger's sections
// ---------------------------------------------------------------------------

/** An IntersectionObserver that reports nothing until a test says what is in view. */
class FakeObserver {
  static all: FakeObserver[] = []
  readonly targets = new Set<Element>()
  constructor(readonly callback: IntersectionObserverCallback) {
    FakeObserver.all.push(this)
  }
  observe(el: Element): void {
    this.targets.add(el)
  }
  unobserve(el: Element): void {
    this.targets.delete(el)
  }
  disconnect(): void {
    this.targets.clear()
  }
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

function scrollTo(el: Element): void {
  act(() => {
    for (const o of FakeObserver.all) {
      if (!o.targets.has(el)) continue
      const entry = { target: el, isIntersecting: true, intersectionRatio: 1 } as unknown as IntersectionObserverEntry
      o.callback([entry], o as unknown as IntersectionObserver)
    }
  })
}

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

/** The Timeline's outcomes page, its headline landed and no card in view. */
async function outcomesPage(): Promise<HTMLElement> {
  FakeObserver.all = []
  vi.stubGlobal('IntersectionObserver', FakeObserver)
  api.loadOutcomes.mockReset()
  api.loadOutcomes.mockImplementation(() => Promise.resolve(ok(ledgerFixture())))
  api.loadMe.mockResolvedValue({ status: 'loading' })
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code']))
  api.loadStats.mockResolvedValue({ status: 'loading' })
  api.loadTasksInState.mockResolvedValue(ok({ tasks: [], next_page_token: null }))
  api.loadTenants.mockResolvedValue(ok({ tenants: [] }))
  const { container } = render(createElement(ActivityScreen))
  await waitFor(() => expect(container.querySelectorAll('[data-card]').length).toBeGreaterThan(0))
  await waitFor(() => expect(asked()).toHaveLength(2))
  return container as HTMLElement
}

/** Every distinct ask made so far, as one string each. */
function asked(): string[] {
  return sorted(new Set(api.loadOutcomes.mock.calls.map((c) => ((c[1] ?? []) as string[]).join(','))))
}

describe('the outcome sections', () => {
  const asks = consoleConstants.outcome_page_asks

  it('OUTCOME_SECTIONS is the route\'s sections, in its order', () => {
    expect([...OUTCOME_SECTIONS]).toEqual(consoleConstants.outcome_sections)
  })

  it('the headline and its second read ask for the sections the server prices the page by', () => {
    expect([...LEDGER_SECTIONS]).toEqual(asks.headline)
    expect([...LEDGER_LATE_SECTIONS]).toEqual(asks.late)
  })

  it('the page draws exactly the cards the server prices it by', async () => {
    const page = await outcomesPage()
    const cards = [...page.querySelectorAll<HTMLElement>('[data-card]')].map((el) => el.dataset.card!)
    expect(sorted(cards)).toEqual(sorted(Object.keys(asks.cards)))
    expect(asked()).toEqual(sorted([asks.headline.join(','), asks.late.join(',')]))
  })

  it.each(Object.entries(asks.cards))('the %s card asks for the sections the server prices it by', async (id, want) => {
    const page = await outcomesPage()
    scrollTo(page.querySelector(`[data-card="${id}"]`)!)
    const expected = sorted(new Set([asks.headline, asks.late, want].map((s) => s.join(','))))
    await waitFor(() => expect(asked()).toEqual(expected))
  })
})

// ---------------------------------------------------------------------------
// Voice it's cards and destinations
// ---------------------------------------------------------------------------

describe('voice cards', () => {
  const want = consoleConstants.voice

  it('CARD_KINDS names the catalogue\'s kinds, in its order', () => {
    expect(CARD_KINDS.map((k) => k.kind)).toEqual(want.card_kinds.map((k) => k.kind))
  })

  it('CARD_KINDS gives each kind the catalogue\'s label, content, fields, states and actions', () => {
    expect(
      CARD_KINDS.map(({ kind, label, content, fields, states, actions }) => ({
        kind, label, content, fields: [...fields], states: [...states], actions: [...actions],
      })),
    ).toEqual(want.card_kinds)
  })

  it('CARD_FIELDS is the envelope every card carries', () => {
    expect([...CARD_FIELDS]).toEqual(want.card_fields)
  })

  it('CARD_STATES is the catalogue\'s states', () => {
    expect([...CARD_STATES]).toEqual(want.card_states)
  })

  it('CARD_CREATORS is the catalogue\'s creators', () => {
    expect([...CARD_CREATORS]).toEqual(want.card_creators)
  })

  it('CARD_REFERENCES is the catalogue\'s references', () => {
    expect([...CARD_REFERENCES]).toEqual(want.card_references)
  })

  it('CARD_CONTENTS is the catalogue\'s contents and renderers', () => {
    expect(CARD_CONTENTS.map(({ content, renderer }) => ({ content, renderer }))).toEqual(want.card_contents)
  })

  it('COMMON_ACTIONS is the catalogue\'s common actions', () => {
    expect([...COMMON_ACTIONS]).toEqual(want.common_actions)
  })

  it('DECIDING_ACTIONS is the actions that decide a card', () => {
    expect([...DECIDING_ACTIONS]).toEqual(want.deciding_actions)
  })

  it('NAVIGATE_DESTINATIONS is FIXED\'s reading pages, in its order', () => {
    // The pages a person reads: never an admin page, a submit form or a
    // callback (docs/design/voice-it.md §3.12.3).
    const reading = Object.entries(FIXED)
      .filter(([address, path]) =>
        !address.startsWith('admin/') && !path.startsWith('/admin')
        && !path.startsWith('/submit') && !address.includes('callback'))
      .map(([address]) => address)
    expect(want.navigate_destinations).toEqual(reading)
  })
})

// ---------------------------------------------------------------------------
// The agent container's environment
// ---------------------------------------------------------------------------

describe('the agent container\'s environment', () => {
  it('agentMaxWorkers is gated on the variable the worker hands the agent', () => {
    const memory = 16 * 2 ** 30
    expect(agentMaxWorkers({ [consoleConstants.task_id_env]: 'task_one' }, memory)).toBeGreaterThanOrEqual(1)
    expect(agentMaxWorkers({ CI: 'true', SWARM_MEMORY_LIMIT_BYTES: String(memory) }, memory)).toBeUndefined()
  })
})
