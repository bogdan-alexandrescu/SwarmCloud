// MOCK-HOOK FILE
/**
 * VOICE IT FIXTURES (mock-ups only, lane VOICE-IT-MOCKUPS). Every scenario's
 * every state, as data: the project, the caption lines, the oscilloscope, the
 * cards, the budget and what needs Bogdan. Nothing here calls a service; all
 * names, repositories and addresses are invented.
 *
 * ONE STORY PER SCENARIO. A frame is the next moment of the same conversation:
 * its captions are every line said so far (the last one still partial while
 * someone is speaking), and its cards are the cards as they stand at that
 * moment, so a plan proposed at `decide` is the same card running at `result`.
 *
 * HONEST VALUES: a figure that is not known is `null` and is drawn as not
 * known, never as 0.
 */

export const SCENARIOS = ['new-app', 'rescue', 'feature', 'approvals', 'flex'] as const
export type Scenario = (typeof SCENARIOS)[number]
export const STAGES = ['rest', 'talk', 'cards', 'decide', 'result'] as const
export type Stage = (typeof STAGES)[number]

/** Shown once, quietly: who hears and speaks, and who thinks. */
export const ENGINES = { voice: 'GPT-Live', planner: 'Claude Opus' } as const

export const SESSION_CAP = 100
export const DAY_CAP = 300

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ScopeState = 'speaking' | 'listening' | 'thinking' | 'idle'

export interface Scope {
  state: ScopeState
  /** Thinking only: what the planner is doing, in a few plain words. */
  label?: string
  /** Seed for the deterministic sample data, so each frame has its own wave. */
  seed: number
}

export type Speaker = 'bogdan' | 'agent' | 'note'

export interface CaptionLine {
  id: string
  /** `note` is a line the console writes (a secure-form entry), not speech. */
  who: Speaker
  text: string
  /** Wall-clock time the line started, HH:MM:SS. */
  at: string
  /** Still being spoken: drawn muted and may change. */
  partial?: boolean
}

export interface Budget {
  /** Dollars spent this session; null when the spend could not be read. */
  session: number | null
  /** Dollars spent today, every session and every lane. */
  day: number | null
  sessionCap: number
  dayCap: number
}

export interface Project {
  id: string
  name: string
  /** What kind of work it is, in plain words. */
  kind: 'New app' | 'Rescue' | 'Feature' | 'Approvals' | 'Mixed'
  /** When the conversation started (HH:MM) and how long it has run. */
  startedAt: string
  length: string
  /** A saved project Bogdan can come back to. */
  resumable: boolean
  /** What opened it: a "Talk it through" on a workflow, a repository, ... */
  openedFrom?: { what: string; name: string; state: string; detail?: string }
}

export interface MicState {
  /** The microphone is open (open mic with barge-in). */
  on: boolean
  muted: boolean
  /** Push-to-talk instead of an open mic. */
  pushToTalk: boolean
  /** Bogdan's input level 0..1 while listening; null when the mic is off. */
  level: number | null
}

export type CardTone = 'neutral' | 'live' | 'waiting' | 'bad' | 'done'

interface CardBase {
  id: string
  /** Time the card appeared, HH:MM. */
  at: string
  title: string
  /** The card's state in plain words ("running", "waiting on you", "merged"). */
  state: string
  tone: CardTone
  /** What Bogdan has to do, in a few words; absent when nothing. */
  needs?: string
}

export interface TaskCard extends CardBase { kind: 'task'; what: string; when: string | null; repo?: string }

export interface Lane {
  name: string
  step: string
  /** 0..100; null while the lane has not reported progress. */
  pct: number | null
  /** Dollars so far; null when not yet metered. */
  cost: number | null
  state: 'queued' | 'running' | 'waiting on you' | 'done' | 'failed'
}
export interface RunningCard extends CardBase { kind: 'running'; lanes: Lane[]; phase?: string }

export interface Checks { passed: number; failed: number; pending: number }
export interface ReviewCard extends CardBase {
  kind: 'review'
  repo: string
  number: number
  files: number
  added: number
  removed: number
  checks: Checks
}

export interface DesignOption { key: string; name: string; summary: string; good: string[]; cost: string[] }
export interface DesignCard extends CardBase { kind: 'design'; question: string; options: DesignOption[]; recommended: string; picked: string | null }

export interface DiagramNode { id: string; label: string; sub?: string; col: number; row: number; tone?: 'new' | 'existing' | 'outside' }
export interface DiagramEdge { from: string; to: string; label?: string }
export interface Diagram { kind: 'architecture' | 'data flow' | 'user flow' | 'data model'; nodes: DiagramNode[]; edges: DiagramEdge[] }
export interface DiagramCard extends CardBase { kind: 'diagram'; diagram: Diagram }

export type Sketch = 'list' | 'calendar' | 'form' | 'detail' | 'grid'
export interface MockupCard extends CardBase { kind: 'mockup'; frames: { label: string; sketch: Sketch }[]; picked?: string | null }

export interface PlanPhase { name: string; lanes: string[]; cost: number; minutes: number; state?: 'next' | 'running' | 'done' | 'later' }
export interface PlanCard extends CardBase { kind: 'plan'; phases: PlanPhase[]; totalCost: number; totalMinutes: number; goLabel: string }

export interface DecisionRow { k: string; v: string }
export interface DecisionCardData extends CardBase {
  kind: 'decision'
  question: string
  rows: DecisionRow[]
  /** What Bogdan said, as heard. */
  spoken?: string
  /** A spoken yes is not enough: confirm with a click (an IAM hold). */
  confirmByClick?: boolean
  options?: string[]
}

export interface InputCard extends CardBase { kind: 'input'; question: string; options?: string[]; value: string | null; placeholder?: string }

export interface CredentialCard extends CardBase { kind: 'credential'; asks: string; storedAs: string; entered: boolean }

export interface FailureCard extends CardBase { kind: 'failure'; where: string; broke: string; log: string[]; fix: string }

export interface ShippedCard extends CardBase { kind: 'shipped'; url: string; changed: string[]; cost: number | null; took: string }

export interface CapCard extends CardBase { kind: 'cap'; which: 'session' | 'day'; spent: number; cap: number; stopped: string[] }

export type VoiceCard =
  | TaskCard | RunningCard | ReviewCard | DesignCard | DiagramCard | MockupCard | PlanCard
  | DecisionCardData | InputCard | CredentialCard | FailureCard | ShippedCard | CapCard

export type CardKind = VoiceCard['kind']

/** One thing waiting on Bogdan: a card on screen, or an inbox item not yet opened. */
export interface Waiting { id: string; text: string; cardId?: string; kind: string }

export interface Frame {
  scenario: Scenario
  stage: Stage
  project: Project
  scope: Scope
  mic: MicState
  captions: CaptionLine[]
  cards: VoiceCard[]
  budget: Budget
  waiting: Waiting[]
  /** Bogdan's standing preferences, kept across projects; he can edit them. */
  preferences: string[]
}

// ---------------------------------------------------------------------------
// Shared data
// ---------------------------------------------------------------------------

export const PREFERENCES: string[] = [
  'TypeScript and React for front ends; Python with FastAPI for services',
  'Postgres unless there is a reason not to',
  'Show me a plan before anything over $10',
  'Pull requests small: one concern each',
  'Plain words in every screen, no codes',
]

const MIC_OPEN: MicState = { on: true, muted: false, pushToTalk: false, level: 0.18 }
const MIC_TALKING: MicState = { on: true, muted: false, pushToTalk: false, level: 0.62 }

// ---------------------------------------------------------------------------
// Oscilloscope sample data (deterministic)
// ---------------------------------------------------------------------------

/** A small seeded generator, so a frame draws the same wave on every render. */
function rand(seed: number): () => number {
  let s = seed >>> 0 || 1
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 4294967296
  }
}

/**
 * `n` samples in -1..1 for a state: speaking is a full voice-like wave (an
 * envelope of syllables over a carrier), listening is Bogdan's mic level
 * (small), thinking is a slow low shimmer, idle is a flat line with a breath.
 */
export function scopeSamples(state: ScopeState, seed: number, n = 160, level = 0.5): number[] {
  const r = rand(seed)
  const out: number[] = []
  const syll = 5 + Math.floor(r() * 4)
  const phase = r() * Math.PI * 2
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1)
    let v = 0
    if (state === 'speaking') {
      const env = Math.pow(Math.abs(Math.sin(t * Math.PI * syll + phase)), 0.6) * (0.55 + 0.45 * Math.sin(t * Math.PI))
      v = env * (Math.sin(i * 0.9 + phase) * 0.7 + Math.sin(i * 2.3) * 0.3) + (r() - 0.5) * 0.08
    } else if (state === 'listening') {
      const env = 0.25 + 0.75 * Math.abs(Math.sin(t * Math.PI * 3 + phase))
      v = level * 0.45 * env * Math.sin(i * 1.4 + phase) + (r() - 0.5) * 0.05
    } else if (state === 'thinking') {
      v = 0.12 * Math.sin(t * Math.PI * 4 + phase) + 0.05 * Math.sin(t * Math.PI * 11)
    } else {
      v = 0.02 * Math.sin(t * Math.PI * 2 + phase)
    }
    out.push(Math.max(-1, Math.min(1, v)))
  }
  return out
}

// ---------------------------------------------------------------------------
// Building frames
// ---------------------------------------------------------------------------

type Said = [who: Speaker, at: string, text: string]

interface StageDef {
  scope: Scope
  mic?: MicState
  lines: Said[]
  /** The last line of this stage is still being spoken. */
  partial?: boolean
  cards: VoiceCard[]
  budget: [session: number | null, day: number | null]
  waiting?: Waiting[]
  length: string
}

interface ScenarioDef {
  project: Omit<Project, 'length'>
  stages: Record<Stage, StageDef>
}

function waitingFrom(cards: VoiceCard[]): Waiting[] {
  return cards.filter((c) => c.needs !== undefined).map((c) => ({ id: `w-${c.id}`, text: c.needs as string, cardId: c.id, kind: c.kind }))
}

function build(scenario: Scenario, def: ScenarioDef, stage: Stage): Frame {
  const upto = STAGES.indexOf(stage)
  const lines: CaptionLine[] = []
  STAGES.slice(0, upto + 1).forEach((st) => {
    const s = def.stages[st]
    s.lines.forEach(([who, at, text], i) => {
      lines.push({ id: `${st}-${i}`, who, at, text, partial: st === stage && s.partial === true && i === s.lines.length - 1 ? true : undefined })
    })
  })
  const s = def.stages[stage]
  return {
    scenario,
    stage,
    project: { ...def.project, length: s.length },
    scope: s.scope,
    mic: s.mic ?? MIC_OPEN,
    captions: lines,
    cards: s.cards,
    budget: { session: s.budget[0], day: s.budget[1], sessionCap: SESSION_CAP, dayCap: DAY_CAP },
    waiting: s.waiting ?? waitingFrom(s.cards),
    preferences: PREFERENCES,
  }
}

// ---------------------------------------------------------------------------
// new-app: "a tool for my team to book meeting rooms"
// ---------------------------------------------------------------------------

const ROOMBOOK_ARCH: Diagram = {
  kind: 'architecture',
  nodes: [
    { id: 'web', label: 'Roombook web', sub: 'React', col: 0, row: 0, tone: 'new' },
    { id: 'slack', label: 'Slack command', sub: '/room', col: 0, row: 1, tone: 'new' },
    { id: 'api', label: 'Booking service', sub: 'FastAPI', col: 1, row: 0, tone: 'new' },
    { id: 'db', label: 'Postgres', sub: 'rooms, bookings', col: 2, row: 0, tone: 'new' },
    { id: 'cal', label: 'Google Calendar', sub: 'room resources', col: 2, row: 1, tone: 'outside' },
    { id: 'sso', label: 'Company sign-in', sub: 'Google Workspace', col: 1, row: 1, tone: 'outside' },
  ],
  edges: [
    { from: 'web', to: 'api', label: 'book, list' },
    { from: 'slack', to: 'api' },
    { from: 'api', to: 'db' },
    { from: 'api', to: 'cal', label: 'sync' },
    { from: 'web', to: 'sso', label: 'sign in' },
  ],
}

const ROOMBOOK_FLOW: Diagram = {
  kind: 'user flow',
  nodes: [
    { id: 'open', label: 'Open Roombook', col: 0, row: 0 },
    { id: 'pick', label: 'Pick a time', col: 1, row: 0 },
    { id: 'free', label: 'See free rooms', col: 2, row: 0 },
    { id: 'book', label: 'Book', col: 2, row: 1 },
    { id: 'invite', label: 'Invite sent', sub: 'calendar', col: 1, row: 1, tone: 'outside' },
  ],
  edges: [
    { from: 'open', to: 'pick' },
    { from: 'pick', to: 'free' },
    { from: 'free', to: 'book' },
    { from: 'book', to: 'invite' },
  ],
}

const ROOMBOOK_PLAN = (state: 'proposed' | 'approved' | 'done'): PlanCard => ({
  id: 'rb-plan',
  kind: 'plan',
  at: '10:14',
  title: 'Plan: Roombook, first version',
  state: state === 'proposed' ? 'waiting on you' : state === 'approved' ? 'running' : 'done',
  tone: state === 'proposed' ? 'waiting' : state === 'approved' ? 'live' : 'done',
  needs: state === 'proposed' ? 'Go or change the Roombook plan' : undefined,
  phases: [
    { name: 'Foundation', lanes: ['Repository and CI', 'Database and booking service'], cost: 6, minutes: 40, state: state === 'proposed' ? 'next' : 'done' },
    { name: 'Product', lanes: ['Booking web app', 'Calendar sync', 'Slack command'], cost: 9, minutes: 55, state: state === 'proposed' ? 'later' : 'done' },
    { name: 'Ship', lanes: ['Deploy and smoke test'], cost: 3, minutes: 25, state: state === 'proposed' ? 'later' : 'done' },
  ],
  totalCost: 18,
  totalMinutes: 120,
  goLabel: 'Go',
})

const NEW_APP: ScenarioDef = {
  project: { id: 'p-roombook', name: 'Roombook', kind: 'New app', startedAt: '10:02', resumable: true },
  stages: {
    rest: {
      scope: { state: 'speaking', seed: 11 },
      lines: [['agent', '10:02:04', 'Hi Bogdan. What are we building today?']],
      partial: false,
      cards: [],
      budget: [0, 37.6],
      length: 'just started',
    },
    talk: {
      scope: { state: 'speaking', seed: 23 },
      lines: [
        ['bogdan', '10:02:11', 'A tool for my team to book meeting rooms.'],
        ['agent', '10:02:15', 'Nice. Who books: just your team of about twenty, or the whole office?'],
        ['bogdan', '10:02:22', 'My team, twelve people. Maybe the office later.'],
        ['agent', '10:02:27', 'And the rooms already live in Google Calendar as room resources, right? Then I would sync with that rather than keep a second calendar'],
      ],
      partial: true,
      cards: [],
      budget: [0.4, 38],
      length: '1 min',
    },
    cards: {
      scope: { state: 'thinking', label: 'drawing the architecture', seed: 31 },
      lines: [
        ['bogdan', '10:03:02', 'Yes, Google Calendar. And a Slack command would be great.'],
        ['agent', '10:03:08', 'Got it. Here is how I would shape it, and two ways the booking screen could look.'],
      ],
      cards: [
        { id: 'rb-arch', kind: 'diagram', at: '10:03', title: 'Architecture', state: 'draft', tone: 'neutral', diagram: ROOMBOOK_ARCH },
        { id: 'rb-flow', kind: 'diagram', at: '10:04', title: 'Booking a room', state: 'draft', tone: 'neutral', diagram: ROOMBOOK_FLOW },
        {
          id: 'rb-mock', kind: 'mockup', at: '10:05', title: 'Booking screen', state: 'pick one', tone: 'waiting',
          needs: 'Pick a booking screen', picked: null,
          frames: [{ label: 'Day by room', sketch: 'calendar' }, { label: 'Free rooms list', sketch: 'list' }],
        },
      ],
      budget: [1.9, 39.5],
      length: '3 min',
    },
    decide: {
      scope: { state: 'listening', seed: 41 },
      mic: MIC_TALKING,
      lines: [
        ['bogdan', '10:12:40', 'The day by room one. Go on.'],
        ['agent', '10:14:02', 'Here is the plan: three phases, six lanes, about eighteen dollars and two hours. Say go when you are happy.'],
        ['bogdan', '10:14:20', 'Looks right, let me read the phases'],
      ],
      partial: true,
      cards: [
        { id: 'rb-arch', kind: 'diagram', at: '10:03', title: 'Architecture', state: 'agreed', tone: 'done', diagram: ROOMBOOK_ARCH },
        { id: 'rb-mock', kind: 'mockup', at: '10:05', title: 'Booking screen', state: 'picked', tone: 'done', picked: 'Day by room',
          frames: [{ label: 'Day by room', sketch: 'calendar' }, { label: 'Free rooms list', sketch: 'list' }] },
        ROOMBOOK_PLAN('proposed'),
      ],
      budget: [3.2, 40.8],
      length: '12 min',
    },
    result: {
      scope: { state: 'speaking', seed: 53 },
      lines: [
        ['bogdan', '10:14:31', 'Go.'],
        ['agent', '10:14:33', 'Starting phase one now.'],
        ['agent', '12:09:50', 'Roombook is live. Twelve seats, synced with your room calendars, and /room works in Slack. It came in at seventeen forty.'],
      ],
      partial: true,
      cards: [
        ROOMBOOK_PLAN('done'),
        {
          id: 'rb-run', kind: 'running', at: '10:14', title: 'Lanes', state: 'all done', tone: 'done', phase: 'Phases 1 to 3',
          lanes: [
            { name: 'Repository and CI', step: 'merged', pct: 100, cost: 1.8, state: 'done' },
            { name: 'Database and booking service', step: 'merged', pct: 100, cost: 3.9, state: 'done' },
            { name: 'Booking web app', step: 'merged', pct: 100, cost: 4.6, state: 'done' },
            { name: 'Calendar sync', step: 'merged', pct: 100, cost: 2.7, state: 'done' },
            { name: 'Slack command', step: 'merged', pct: 100, cost: 1.5, state: 'done' },
            { name: 'Deploy and smoke test', step: 'deployed', pct: 100, cost: 2.9, state: 'done' },
          ],
        },
        {
          id: 'rb-ship', kind: 'shipped', at: '12:09', title: 'Roombook shipped', state: 'live', tone: 'done',
          url: 'https://roombook.example.com', took: '1 h 55 min', cost: 17.4,
          changed: ['New repository roombook with CI', 'Booking web app and service', 'Google Calendar room sync', 'Slack /room command'],
        },
      ],
      budget: [20.6, 58.2],
      length: '2 h 8 min',
    },
  },
}

// ---------------------------------------------------------------------------
// rescue: "Talk it through" on a failed workflow
// ---------------------------------------------------------------------------

const RESCUE_LOG = [
  'tests/booking/test_overlap.py::test_back_to_back FAILED',
  'AssertionError: expected 2 bookings, found 1',
  'shard 3 of 4: 212 passed, 1 failed in 3 min 41 s',
  'same test passed on shard 3 in the two runs before',
]

const RESCUE_FIX = (state: 'proposed' | 'running' | 'done'): PlanCard => ({
  id: 'rs-fix', kind: 'plan', at: '09:41', title: 'Fix: wait for the booking write, then re-run',
  state: state === 'proposed' ? 'waiting on you' : state === 'running' ? 'running' : 'done',
  tone: state === 'proposed' ? 'waiting' : state === 'running' ? 'live' : 'done',
  needs: state === 'proposed' ? 'Approve the re-run and fix' : undefined,
  phases: [
    { name: 'Fix the test', lanes: ['Await the write in test_back_to_back'], cost: 1.2, minutes: 8, state: state === 'proposed' ? 'next' : 'done' },
    { name: 'Re-run', lanes: ['Re-run checks and merge'], cost: 0.8, minutes: 6, state: state === 'done' ? 'done' : state === 'running' ? 'running' : 'later' },
  ],
  totalCost: 2, totalMinutes: 14, goLabel: 'Re-run and fix',
})

const RESCUE: ScenarioDef = {
  project: {
    id: 'p-rescue', name: 'Rescue: roombook nightly release', kind: 'Rescue', startedAt: '09:38', resumable: true,
    openedFrom: { what: 'Workflow', name: 'roombook nightly release', state: 'failed', detail: '3 of 5 steps done; the merge was refused on a red check' },
  },
  stages: {
    rest: {
      scope: { state: 'speaking', seed: 61 },
      lines: [['agent', '09:38:02', 'I have the nightly release open. Three of five steps finished; the merge was refused because one check is red. Want me to walk you through it?']],
      cards: [
        {
          id: 'rs-wf', kind: 'failure', at: '09:38', title: 'roombook nightly release', state: 'failed', tone: 'bad',
          where: 'Step 4 of 5: merge', broke: 'The merge was refused: the "unit tests" check is red.',
          log: [], fix: 'Not looked at yet',
        },
      ],
      waiting: [],
      budget: [0, 12.3],
      length: 'just started',
    },
    talk: {
      scope: { state: 'speaking', seed: 67 },
      lines: [
        ['bogdan', '09:38:20', 'Yes, what broke?'],
        ['agent', '09:38:24', 'One test, back to back bookings, failed on shard three. It passed on the same shard the two runs before, so it looks flaky: the test reads before the booking write lands'],
      ],
      partial: true,
      cards: [],
      waiting: [],
      budget: [0.3, 12.6],
      length: '1 min',
    },
    cards: {
      scope: { state: 'thinking', label: 'checking the test history', seed: 71 },
      lines: [
        ['bogdan', '09:39:10', 'Show me the log.'],
        ['agent', '09:39:13', 'Here are the lines that matter, and the fix I would make.'],
      ],
      cards: [
        {
          id: 'rs-fail', kind: 'failure', at: '09:39', title: 'Unit tests, shard 3', state: 'failed', tone: 'bad',
          where: 'Step 4 of 5: merge, check "unit tests"', broke: 'test_back_to_back reads before the write lands, so it fails about one run in ten.',
          log: RESCUE_LOG, fix: 'Await the booking write in the test, then re-run the checks and merge.',
        },
        RESCUE_FIX('proposed'),
      ],
      budget: [0.9, 13.2],
      length: '2 min',
    },
    decide: {
      scope: { state: 'listening', seed: 79 },
      mic: MIC_TALKING,
      lines: [
        ['agent', '09:40:30', 'It is about two dollars and fifteen minutes. Shall I go?'],
        ['bogdan', '09:41:02', 'Yes, re-run and fix it'],
      ],
      partial: true,
      cards: [
        {
          id: 'rs-fail', kind: 'failure', at: '09:39', title: 'Unit tests, shard 3', state: 'failed', tone: 'bad',
          where: 'Step 4 of 5: merge, check "unit tests"', broke: 'test_back_to_back reads before the write lands, so it fails about one run in ten.',
          log: RESCUE_LOG, fix: 'Await the booking write in the test, then re-run the checks and merge.',
        },
        RESCUE_FIX('proposed'),
      ],
      budget: [1.1, 13.4],
      length: '3 min',
    },
    result: {
      scope: { state: 'idle', seed: 83 },
      mic: { on: true, muted: true, pushToTalk: false, level: null },
      lines: [
        ['agent', '09:41:05', 'On it.'],
        ['agent', '09:55:40', 'All green. The release merged at 09:55. It cost one ninety.'],
      ],
      cards: [
        RESCUE_FIX('done'),
        {
          id: 'rs-ship', kind: 'shipped', at: '09:55', title: 'roombook nightly release merged', state: 'merged', tone: 'done',
          url: 'https://github.example.com/bogdan/roombook/pull/88', took: '14 min', cost: 1.9,
          changed: ['test_back_to_back awaits the booking write', 'Checks re-run: 4 of 4 shards green', 'Steps 4 and 5 finished'],
        },
      ],
      budget: [3.1, 15.4],
      length: '17 min',
    },
  },
}

// ---------------------------------------------------------------------------
// feature: "add team billing to northwind-crm"
// ---------------------------------------------------------------------------

const BILLING_MODEL: Diagram = {
  kind: 'data model',
  nodes: [
    { id: 'team', label: 'Team', sub: 'existing', col: 0, row: 0, tone: 'existing' },
    { id: 'user', label: 'User', sub: 'existing', col: 0, row: 1, tone: 'existing' },
    { id: 'acct', label: 'BillingAccount', sub: 'new: plan, seats', col: 1, row: 0, tone: 'new' },
    { id: 'inv', label: 'Invoice', sub: 'new', col: 2, row: 0, tone: 'new' },
    { id: 'stripe', label: 'Stripe customer', sub: 'outside', col: 2, row: 1, tone: 'outside' },
  ],
  edges: [
    { from: 'user', to: 'team', label: 'member of' },
    { from: 'team', to: 'acct', label: 'has one' },
    { from: 'acct', to: 'inv', label: 'has many' },
    { from: 'acct', to: 'stripe', label: 'mirrors' },
  ],
}

const BILLING_DESIGN = (picked: string | null): DesignCard => ({
  id: 'nw-design', kind: 'design', at: '14:08', title: 'How seats are counted',
  state: picked === null ? 'pick one' : `picked ${picked}`, tone: picked === null ? 'waiting' : 'done',
  needs: picked === null ? 'Pick design A or B' : undefined,
  question: 'Bill per seat on the fly, or per seat at each month start?',
  recommended: 'B', picked,
  options: [
    { key: 'A', name: 'Live seats', summary: 'Every seat change updates Stripe at once, prorated.', good: ['Exact to the day'], cost: ['Many small charges', 'More Stripe calls'] },
    { key: 'B', name: 'Monthly seats', summary: 'Count seats on the 1st; changes settle next month.', good: ['One invoice a month', 'Simpler code'], cost: ['A new seat is free until the 1st'] },
  ],
})

const FEATURE: ScenarioDef = {
  project: {
    id: 'p-billing', name: 'Team billing for northwind-crm', kind: 'Feature', startedAt: '14:02', resumable: true,
    openedFrom: { what: 'Repository', name: 'northwind-crm', state: 'main, read 2 min ago' },
  },
  stages: {
    rest: {
      scope: { state: 'speaking', seed: 91 },
      lines: [['agent', '14:02:03', 'northwind-crm is loaded: 412 files, the code graph is fresh. What should we add?']],
      cards: [],
      budget: [0, 44.1],
      length: 'just started',
    },
    talk: {
      scope: { state: 'speaking', seed: 97 },
      lines: [
        ['bogdan', '14:02:15', 'Team billing. Teams pay for seats with Stripe.'],
        ['agent', '14:02:21', 'I have read how teams and users fit together. Two questions. Does an admin pay for the whole team, or each member for themselves? And monthly only, or yearly too'],
      ],
      partial: true,
      cards: [],
      budget: [0.6, 44.7],
      length: '1 min',
    },
    cards: {
      scope: { state: 'thinking', label: 'mapping the modules', seed: 101 },
      lines: [
        ['bogdan', '14:03:05', 'The admin pays. Monthly for now.'],
        ['agent', '14:03:12', 'Then four modules change. Here is the data model, and two ways to count seats.'],
      ],
      cards: [
        {
          id: 'nw-mods', kind: 'task', at: '14:04', title: 'Modules touched', state: 'read from the code graph', tone: 'neutral',
          what: 'teams/ (models, admin), billing/ (new), api/routes/teams.py, web/src/settings/', when: null, repo: 'northwind-crm',
        },
        { id: 'nw-model', kind: 'diagram', at: '14:06', title: 'Data model', state: 'draft', tone: 'neutral', diagram: BILLING_MODEL },
        BILLING_DESIGN(null),
      ],
      budget: [2.4, 46.5],
      length: '6 min',
    },
    decide: {
      scope: { state: 'speaking', seed: 107 },
      lines: [
        ['bogdan', '14:09:30', 'B. Go.'],
        ['agent', '14:09:34', 'Going with monthly seats. I need the Stripe test key: I have opened the secure form, type it there, never out loud.'],
        ['note', '14:10:02', 'Bogdan entered the Stripe key in the secure form'],
        ['agent', '14:10:05', 'Thanks, it is stored. Starting two lanes'],
      ],
      partial: true,
      cards: [
        BILLING_DESIGN('B'),
        {
          id: 'nw-key', kind: 'credential', at: '14:09', title: 'Stripe test key', state: 'stored', tone: 'done',
          asks: 'Stripe test key', storedAs: 'northwind-crm, test only', entered: true,
        },
        {
          id: 'nw-run', kind: 'running', at: '14:10', title: 'Lanes', state: 'running', tone: 'live',
          lanes: [
            { name: 'Billing model and Stripe sync', step: 'reading the code', pct: 4, cost: null, state: 'running' },
            { name: 'Team settings: billing page', step: 'queued', pct: null, cost: null, state: 'queued' },
          ],
        },
      ],
      budget: [3.0, 47.1],
      length: '8 min',
    },
    result: {
      scope: { state: 'speaking', seed: 113 },
      lines: [
        ['agent', '15:21:40', 'Both pull requests are green. Want to look, or shall I merge and deploy?'],
        ['bogdan', '15:22:02', 'Merge and deploy.'],
        ['agent', '15:31:15', 'Deployed to staging and production. Team admins see Billing in settings now'],
      ],
      partial: true,
      cards: [
        {
          id: 'nw-pr1', kind: 'review', at: '15:20', title: 'Team billing: model, Stripe sync and invoices', state: 'merged', tone: 'done',
          repo: 'northwind-crm', number: 214, files: 14, added: 612, removed: 38, checks: { passed: 9, failed: 0, pending: 0 },
        },
        {
          id: 'nw-pr2', kind: 'review', at: '15:21', title: 'Team settings: billing page', state: 'merged', tone: 'done',
          repo: 'northwind-crm', number: 215, files: 7, added: 301, removed: 12, checks: { passed: 7, failed: 0, pending: 0 },
        },
        {
          id: 'nw-ship', kind: 'shipped', at: '15:31', title: 'Team billing deployed', state: 'live', tone: 'done',
          url: 'https://crm.northwind.example.com/settings/billing', took: '1 h 21 min', cost: 11.8,
          changed: ['Teams have a billing account and monthly seat invoices', 'Admins manage billing in team settings', 'Stripe test mode until you switch the key'],
        },
      ],
      budget: [14.8, 58.9],
      length: '1 h 29 min',
    },
  },
}

// ---------------------------------------------------------------------------
// approvals: "what's waiting on me?"
// ---------------------------------------------------------------------------

const IN_IAM: Waiting = { id: 'ap-iam', kind: 'decision', text: 'IAM hold: northwind-crm deploy wants storage access' }
const IN_Q: Waiting = { id: 'ap-q', kind: 'input', text: 'Owner question: which region for the roombook database' }
const IN_MERGE: Waiting = { id: 'ap-merge', kind: 'review', text: 'Merge approval: roombook #91' }
const IN_MOCK: Waiting = { id: 'ap-mock', kind: 'mockup', text: 'Pick a mock-up: northwind-crm invoice page' }
const IN_COST: Waiting = { id: 'ap-cost', kind: 'cap', text: 'Cost raise: the analytics lane wants $12 more' }
const INBOX: Waiting[] = [IN_IAM, IN_Q, IN_MERGE, IN_MOCK, IN_COST]

const IAM_ROWS: DecisionRow[] = [
  { k: 'Who', v: 'the northwind-crm deploy account' },
  { k: 'Wants', v: 'read and write objects' },
  { k: 'On', v: 'the invoices bucket, northwind-crm prefix only' },
  { k: 'Why', v: 'invoice PDFs from the team billing feature' },
  { k: 'Asked by', v: 'lane "Team billing: invoices", 10 min ago' },
]

const IAM_CARD = (spoken: string | undefined, state: 'waiting' | 'heard' | 'approved'): DecisionCardData => ({
  id: 'ap-iam-c', kind: 'decision', at: '08:31', title: 'IAM hold', question: 'Give the deploy account access to the invoices bucket?',
  state: state === 'waiting' ? 'waiting on you' : state === 'heard' ? 'confirm by click' : 'approved',
  tone: state === 'approved' ? 'done' : 'waiting',
  needs: state === 'approved' ? undefined : state === 'heard' ? 'Confirm the IAM approval with a click' : 'Approve or hold the IAM request',
  rows: IAM_ROWS, spoken, confirmByClick: true,
})

const APPROVALS: ScenarioDef = {
  project: { id: 'p-inbox', name: 'What is waiting on me', kind: 'Approvals', startedAt: '08:30', resumable: false },
  stages: {
    rest: {
      scope: { state: 'speaking', seed: 121 },
      lines: [['agent', '08:30:04', 'Good morning. Five things are waiting on you. Shall I go through them?']],
      cards: [],
      waiting: INBOX,
      budget: [0, 0],
      length: 'just started',
    },
    talk: {
      scope: { state: 'speaking', seed: 127 },
      lines: [
        ['bogdan', '08:30:12', 'What is waiting on me?'],
        ['agent', '08:30:15', 'First, an IAM hold. The northwind-crm deploy account wants to read and write the invoices bucket, only under its own prefix'],
      ],
      partial: true,
      cards: [IAM_CARD(undefined, 'waiting')],
      waiting: INBOX,
      budget: [0.2, 0.2],
      length: '1 min',
    },
    cards: {
      scope: { state: 'listening', seed: 131 },
      mic: MIC_TALKING,
      lines: [
        ['agent', '08:31:02', 'Then three more after it: an owner question, a merge, and a mock-up to pick.'],
        ['bogdan', '08:31:20', 'Go one by one'],
      ],
      partial: true,
      cards: [
        IAM_CARD(undefined, 'waiting'),
        {
          id: 'ap-q-c', kind: 'input', at: '08:31', title: 'Owner question', state: 'waiting on you', tone: 'waiting',
          needs: 'Pick a region for the roombook database',
          question: 'Which region should the roombook database live in?', options: ['Europe (Belgium)', 'Europe (Frankfurt)', 'United States (Iowa)'], value: null,
        },
        {
          id: 'ap-merge-c', kind: 'review', at: '08:31', title: 'Room capacity limits', state: 'waiting on you', tone: 'waiting',
          needs: 'Approve the merge of roombook #91', repo: 'roombook', number: 91, files: 4, added: 88, removed: 9, checks: { passed: 6, failed: 0, pending: 0 },
        },
        {
          id: 'ap-mock-c', kind: 'mockup', at: '08:31', title: 'Invoice page', state: 'pick one', tone: 'waiting', needs: 'Pick an invoice page',
          frames: [{ label: 'Table', sketch: 'grid' }, { label: 'Cards', sketch: 'detail' }], picked: null,
        },
      ],
      waiting: [
        ...waitingFrom([IAM_CARD(undefined, 'waiting')]),
        { id: 'w-ap-q-c', cardId: 'ap-q-c', kind: 'input', text: 'Pick a region for the roombook database' },
        { id: 'w-ap-merge-c', cardId: 'ap-merge-c', kind: 'review', text: 'Approve the merge of roombook #91' },
        { id: 'w-ap-mock-c', cardId: 'ap-mock-c', kind: 'mockup', text: 'Pick an invoice page' },
        IN_COST,
      ],
      budget: [0.5, 0.5],
      length: '2 min',
    },
    decide: {
      scope: { state: 'speaking', seed: 137 },
      lines: [
        ['bogdan', '08:32:05', 'Approve the IAM one.'],
        ['agent', '08:32:08', 'I heard approve. Because it is access, confirm it with a click on the card.'],
      ],
      cards: [IAM_CARD('Approve the IAM one.', 'heard')],
      waiting: [
        { id: 'w-ap-iam-c', cardId: 'ap-iam-c', kind: 'decision', text: 'Confirm the IAM approval with a click' },
        IN_Q, IN_MERGE, IN_MOCK, IN_COST,
      ],
      budget: [0.7, 0.7],
      length: '2 min',
    },
    result: {
      scope: { state: 'idle', seed: 139 },
      lines: [
        ['note', '08:32:15', 'Bogdan confirmed the IAM approval with a click'],
        ['bogdan', '08:32:40', 'Frankfurt. Merge it. Table. And no to the extra twelve dollars.'],
        ['agent', '08:32:46', 'All clear. Four approved, one sent back with your no.'],
      ],
      cards: [
        {
          id: 'ap-done', kind: 'decision', at: '08:32', title: 'All clear', state: 'nothing waiting', tone: 'done',
          question: 'What you decided',
          rows: [
            { k: 'Approved', v: 'IAM: deploy account on the invoices bucket (confirmed by click)' },
            { k: 'Answered', v: 'roombook database in Europe (Frankfurt)' },
            { k: 'Approved', v: 'merge of roombook #91' },
            { k: 'Picked', v: 'invoice page: Table' },
            { k: 'Sent back', v: 'analytics lane cost raise: no' },
          ],
        },
      ],
      waiting: [],
      budget: [1.1, 1.1],
      length: '3 min',
    },
  },
}

// ---------------------------------------------------------------------------
// flex: one tiny task and one big project on the same page
// ---------------------------------------------------------------------------

const BIG_PHASES = (stage: 'decide' | 'result'): PlanPhase[] => [
  { name: 'Phase 1: Foundations', lanes: ['Tenant model', 'Auth and roles', 'Audit log', 'Shared UI kit', 'CI pipeline', 'Data migration', 'Event bus', 'Search index'], cost: 22, minutes: 90, state: stage === 'decide' ? 'next' : 'running' },
  { name: 'Phase 2: Core features', lanes: ['Contacts', 'Deals', 'Pipelines', 'Tasks', 'Email sync', 'Reports'], cost: 26, minutes: 120, state: 'later' },
  { name: 'Phase 3: Integrations', lanes: ['Stripe', 'Slack', 'Calendar', 'Webhooks'], cost: 14, minutes: 80, state: 'later' },
  { name: 'Phase 4: Ship', lanes: ['Load test', 'Deploy'], cost: 8, minutes: 40, state: 'later' },
]

const BIG_PLAN = (stage: 'decide' | 'result'): PlanCard => ({
  id: 'fx-plan', kind: 'plan', at: '16:20', title: 'Plan: northwind-crm version 2',
  state: stage === 'decide' ? 'waiting on you' : 'phase 1 running', tone: stage === 'decide' ? 'waiting' : 'live',
  needs: stage === 'decide' ? 'Go on phase 1 (about $22)' : undefined,
  phases: BIG_PHASES(stage), totalCost: 70, totalMinutes: 330, goLabel: 'Go on phase 1',
})

const COPY_FIX: TaskCard = {
  id: 'fx-copy', kind: 'task', at: '16:01', title: 'Copy fix: "Sign in" button on roombook', state: 'merged', tone: 'done',
  what: '"Log in" became "Sign in" on the roombook home page. roombook #93, merged.', when: 'done in 2 min', repo: 'roombook',
}

const FLEX: ScenarioDef = {
  project: { id: 'p-flex', name: 'northwind-crm version 2', kind: 'Mixed', startedAt: '16:00', resumable: true },
  stages: {
    rest: {
      scope: { state: 'listening', seed: 151 },
      mic: MIC_TALKING,
      lines: [
        ['agent', '16:00:03', 'Hi Bogdan.'],
        ['bogdan', '16:00:08', 'Quick one first: the roombook home page says Log in, make it Sign in'],
      ],
      partial: true,
      cards: [],
      budget: [38.2, 182.5],
      length: 'just started',
    },
    talk: {
      scope: { state: 'speaking', seed: 157 },
      lines: [
        ['agent', '16:00:12', 'Done in a minute, no plan needed.'],
        ['agent', '16:01:58', 'Merged: it says Sign in now. Ten cents.'],
      ],
      cards: [COPY_FIX],
      budget: [38.3, 182.6],
      length: '2 min',
    },
    cards: {
      scope: { state: 'thinking', label: 'planning the lanes', seed: 163 },
      lines: [
        ['bogdan', '16:03:10', 'Now the big one: northwind-crm version 2. Multi-tenant, new pipelines, integrations, the lot.'],
        ['agent', '16:03:18', 'That is a big one. I am sorting it into phases; about twenty lanes.'],
      ],
      cards: [
        COPY_FIX,
        { id: 'fx-arch', kind: 'diagram', at: '16:12', title: 'Version 2 architecture', state: 'draft', tone: 'neutral', diagram: {
          kind: 'architecture',
          nodes: [
            { id: 'web', label: 'CRM web', col: 0, row: 0, tone: 'existing' },
            { id: 'api', label: 'CRM API', sub: 'tenant aware', col: 1, row: 0, tone: 'existing' },
            { id: 'bus', label: 'Event bus', col: 1, row: 1, tone: 'new' },
            { id: 'db', label: 'Postgres', sub: 'per tenant', col: 2, row: 0, tone: 'new' },
            { id: 'search', label: 'Search index', col: 2, row: 1, tone: 'new' },
          ],
          edges: [{ from: 'web', to: 'api' }, { from: 'api', to: 'db' }, { from: 'api', to: 'bus', label: 'events' }, { from: 'bus', to: 'search' }],
        } },
        { id: 'fx-mods', kind: 'task', at: '16:14', title: '20 lanes in 4 phases', state: 'drafting', tone: 'neutral', what: 'Lanes drafted from the code graph: 8 foundations, 6 features, 4 integrations, 2 ship.', when: null, repo: 'northwind-crm' },
      ],
      budget: [44.0, 188.3],
      length: '14 min',
    },
    decide: {
      scope: { state: 'speaking', seed: 167 },
      lines: [
        ['agent', '16:20:04', 'Four phases, twenty lanes, about seventy dollars all in. This session has thirty six left, so I suggest phase one now, twenty two dollars, and the rest when you raise the cap'],
      ],
      partial: true,
      cards: [COPY_FIX, BIG_PLAN('decide')],
      budget: [64, 208.3],
      length: '20 min',
    },
    result: {
      scope: { state: 'speaking', seed: 173 },
      lines: [
        ['bogdan', '16:20:30', 'Go on phase one.'],
        ['agent', '16:58:12', 'Phase one: eight lanes in flight, two need you, and the session is near its cap'],
      ],
      partial: true,
      cards: [
        BIG_PLAN('result'),
        {
          id: 'fx-run', kind: 'running', at: '16:20', title: 'Phase 1 lanes', state: '8 running', tone: 'live', phase: 'Phase 1: Foundations',
          lanes: [
            { name: 'Tenant model', step: 'writing tests', pct: 70, cost: 2.9, state: 'running' },
            { name: 'Auth and roles', step: 'waiting on you: pick a role model', pct: 45, cost: 2.1, state: 'waiting on you' },
            { name: 'Audit log', step: 'review', pct: 85, cost: 2.6, state: 'running' },
            { name: 'Shared UI kit', step: 'building', pct: 55, cost: 3.0, state: 'running' },
            { name: 'CI pipeline', step: 'checks', pct: 90, cost: 1.4, state: 'running' },
            { name: 'Data migration', step: 'waiting on you: Postgres admin key', pct: 30, cost: 1.8, state: 'waiting on you' },
            { name: 'Event bus', step: 'reading the code', pct: 15, cost: 0.9, state: 'running' },
            { name: 'Search index', step: 'starting', pct: null, cost: null, state: 'running' },
          ],
        },
        {
          id: 'fx-role', kind: 'input', at: '16:41', title: 'Auth and roles: role model', state: 'waiting on you', tone: 'waiting',
          needs: 'Pick a role model for Auth and roles', question: 'How should roles work?', options: ['Owner, admin, member', 'Custom roles per tenant'], value: null,
        },
        {
          id: 'fx-key', kind: 'credential', at: '16:49', title: 'Postgres admin key', state: 'waiting on you', tone: 'waiting',
          needs: 'Enter the Postgres admin key in the secure form', asks: 'Postgres admin key', storedAs: 'northwind-crm, production', entered: false,
        },
        {
          id: 'fx-cap', kind: 'cap', at: '16:58', title: 'Session budget nearly used', state: 'near the cap', tone: 'waiting',
          needs: 'Raise the session cap or let phase 1 finish and stop', which: 'session', spent: 93.6, cap: 100,
          stopped: ['Phases 2 to 4 will not start until you raise the cap'],
        },
      ],
      budget: [93.6, 237.9],
      length: '58 min',
    },
  },
}

const DEFS: Record<Scenario, ScenarioDef> = { 'new-app': NEW_APP, rescue: RESCUE, feature: FEATURE, approvals: APPROVALS, flex: FLEX }

/** The frame for a scenario at a stage: the one read every layout makes. */
export function frameFor(scenario: Scenario, stage: Stage): Frame {
  return build(scenario, DEFS[scenario], stage)
}

/** A short line naming the scenario, for a picker or a page title. */
export const SCENARIO_LABEL: Record<Scenario, string> = {
  'new-app': 'New app: room booking',
  rescue: 'Rescue a failed workflow',
  feature: 'Feature: team billing',
  approvals: 'What is waiting on me',
  flex: 'A tiny task and a big project',
}

export const STAGE_LABEL: Record<Stage, string> = { rest: 'Start', talk: 'Talking', cards: 'Cards', decide: 'Decide', result: 'Result' }

/** Every saved project, for a "Projects" list (memory across conversations). */
export const SAVED_PROJECTS: { id: string; name: string; when: string; state: string }[] = [
  { id: 'p-roombook', name: 'Roombook', when: 'today 10:02', state: 'shipped' },
  { id: 'p-billing', name: 'Team billing for northwind-crm', when: 'yesterday 14:02', state: 'deployed' },
  { id: 'p-flex', name: 'northwind-crm version 2', when: 'yesterday 16:00', state: 'phase 1 running' },
  { id: 'p-rescue', name: 'Rescue: roombook nightly release', when: '30 Oct 09:38', state: 'merged' },
]
