// The admin section's click-only states the fixture build never draws
// (issue 439): each screen mounted on hand-built props, or behind a stubbed
// fetch, so one click opens the state. Copied from the tests that build the
// same reads: adminSchedules (every workspace's schedules), voice.costpill,
// voice.debug.trace and voice.floating.

import type { ReactElement } from 'react'
import { vi } from 'vitest'

import type { CardListState, Delegation, RosterRow } from '../voice/CardList'
import type { VoiceSession } from '../voice/session'
import { serve } from './repofixture'
import { Routed } from './screenroute'

type Json = Record<string, unknown>

const realFetch = globalThis.fetch

/** Undo the mounts' stubs: called after each state. */
export function restoreAdminStubs(): void {
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.resetModules()
}

async function live(): Promise<void> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  await Promise.resolve()
}

const SESSION = 'vs_0001'
const at = (minutesAgo: number): string => new Date(Date.now() - minutesAgo * 60_000).toISOString()

// ---------------------------------------------------------------- Setup: a team refused

/** Setup's Add a team, the write refused by the API: the refusal names its error. */
export async function mountTeamRefused(): Promise<ReactElement> {
  await live()
  serve((m, url) => {
    if (m === 'POST' && url.startsWith('/v1/admin/setup/teams')) return { status: 409, body: { code: 'TEAM_EXISTS', message: 'eng@example.com already has a workspace.' } }
    return null
  })
  const { AdminSetupTeam } = await import('../AdminSetupTeam')
  return <AdminSetupTeam teams={[]} />
}

// ---------------------------------------------------------------- Schedules, every workspace

function scheduleRow(id: string, tenant: string): Json {
  return {
    schedule_id: id, tenant_id: tenant, name: `Schedule ${id}`, type: 'issue-sweep', tier: 'R2', cron: '0 9 * * 1-5',
    timezone: 'Europe/London', words: 'at 09:00, Monday through Friday', next_run_at: at(-180), last_outcome: 'succeeded',
    last_firing_at: at(1200), spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0, state: 'enabled', pause: null, owner: 'operator@example.com',
  }
}

const TICK = { window_minutes: 60, ticks: 60, disabled: 0, errors: 0, truncated: 0, fired: 4, max_lateness_seconds: 12, last: null }

/** Schedules › All workspaces, two schedules: each action asks first, and the API refuses the write. */
export async function mountSchedules(): Promise<ReactElement> {
  await live()
  serve((m, url) => {
    if (m === 'GET' && url.split('?')[0] === '/v1/admin/schedules') {
      return { status: 200, body: { schedules: [scheduleRow('sch_a', 'eng'), scheduleRow('sch_b', 'research')], next_page_token: null, tick: TICK } }
    }
    if (m === 'POST') return { status: 409, body: { code: 'SCHEDULE_BUSY', message: 'Another change to this schedule is in flight.' } }
    return null
  })
  const { SchedulesScreen } = await import('../Schedules')
  return <Routed Screen={SchedulesScreen} section="automate/schedules" start="scope=all" spy={() => undefined} />
}

// ---------------------------------------------------------------- Voice it: the crew and the working card

const ROWS: RosterRow[] = [
  { kind: 'run', id: 'wf-1', title: 'Export page lanes', state: 'running', done: 2, total: 5, seq: 4 },
  { kind: 'agent', id: 'task-77', title: null, state: 'done', done: null, total: null, seq: 3 },
] as RosterRow[]

/** The crew strip, two rows: a chip opens its card. */
export async function mountRoster(): Promise<ReactElement> {
  const { Roster } = await import('../voice/Roster')
  return <Roster rows={ROWS} navigate={() => undefined} />
}

const WORK: Delegation[] = [
  {
    delegation_id: 'd1', at: at(1), folded: 0,
    steps: [
      { seq: 1, at: at(1), phrase: 'Reading running agents', ids: [] },
      { seq: 2, at: at(0.5), phrase: 'Opening the export page', ids: ['task-77'] },
    ],
  },
] as unknown as Delegation[]

/** The working card, one delegation of two steps: its head opens the steps. */
export async function mountWorkingCard(): Promise<ReactElement> {
  const { WorkingCard } = await import('../voice/WorkingCard')
  return <WorkingCard work={WORK} />
}

// ---------------------------------------------------------------- Voice it: the cost pill

function cap(capUsd: number, talk: number, finished: number, reserved: number): Json {
  const spent = talk + finished
  return {
    cap_usd: capUsd, spent_usd: spent, reserved_usd: reserved, total_usd: spent + reserved, remaining_usd: capUsd - spent - reserved,
    lines: [
      { line: 'talk', spent_usd: talk, reserved_usd: 0 },
      { line: 'planner', spent_usd: 0, reserved_usd: 0 },
      { line: 'speech_machine', spent_usd: 0, reserved_usd: 0 },
      { line: 'work_running', spent_usd: 0, reserved_usd: reserved },
      { line: 'work_finished', spent_usd: finished, reserved_usd: 0 },
      { line: 'followups', spent_usd: 0, reserved_usd: 0 },
    ],
  }
}

const LEDGER = {
  session_id: SESSION, date: '2026-11-02', time_zone: 'Europe/London',
  session: cap(100, 1.5, 2, 9), day: cap(300, 4.25, 30, 60),
  not_counted: ['pod compute for lanes', 'CI runner minutes', 'deploys'],
}

/** A session's cost pill on a served ledger: the pill opens the panel. */
export async function mountCostPill(): Promise<ReactElement> {
  await live()
  serve((m, url) => (m === 'GET' && url.includes(`/v1/voice/sessions/${SESSION}/ledger`) ? { status: 200, body: LEDGER } : null))
  const { CostPill } = await import('../voice/CostPill')
  return <CostPill sessionId={SESSION} live={false} />
}

// ---------------------------------------------------------------- Voice it: the debug trace

const TURNS = [
  {
    n: 1, at: '2026-11-10T09:00:04+00:00', secs: 3.25,
    lines: [{ who: 'person', text: ['Draw the export page'] }, { who: 'voice', text: ['On it'] }],
    handoff: ['the person wants the export page drawn'],
    read: ['read_file: apps/web/export.tsx', 'list_runs'],
    drawn: [{ kind: 'mockup', title: 'Export page', frames: ['Empty', 'Filled'] }, { kind: 'work', title: 'Export lane', refused: 'the budget is spent' }],
    none: null, none_code: null, told: ['A mock-up is on screen'],
    events: { pieces: 4, finals: 3, empty_finals: 2, handoffs: 1, barge_ins: 0 }, tier: 'api_key',
  },
]

const TRACE = {
  session_id: SESSION, project_id: 'vp_0001', engine: 'Own speech', engine_code: 'own', started: '2026-11-10T09:00:00+00:00', minutes: 1.5,
  turns: TURNS, events: { pieces: 4, finals: 3, empty_finals: 2, handoffs: 1, barge_ins: 0, replies: 1 }, scope: 'platform',
}

const TRACES = {
  sessions: [{ session_id: SESSION, project_id: 'vp_0001', engine: 'Own speech', engine_code: 'own', started: '2026-11-10T09:00:00+00:00', turns: 1, cards: 1, refused: 1 }],
  scope: 'platform',
}

/** The debug trace of one session of one turn: a turn's row opens what the planner was given and did. */
export async function mountDebugTrace(): Promise<ReactElement> {
  await live()
  serve((m, url) => {
    if (m !== 'GET') return null
    if (url.includes('/v1/voice/trace/sessions/')) return { status: 200, body: TRACE }
    if (url.includes('/v1/voice/trace/sessions')) return { status: 200, body: TRACES }
    return null
  })
  const { DebugTrace } = await import('../voice/DebugTrace')
  return <DebugTrace sessionId={SESSION} />
}

// ---------------------------------------------------------------- Voice it: the floating view, folded

function heldSession(): VoiceSession {
  const cards: CardListState = { lastSeq: 0, cards: [] }
  return {
    phase: 'live', problem: null, problemTip: null, lines: [], debug: false, turn: null, voice: null, micStream: null, voicePlaying: false,
    sessionId: SESSION, projectId: 'vp_0001', talkMode: 'live', boot: [], speechState: null, fallback: null, dismissFallback: () => undefined,
    tierLine: null, onPlan: false, level: null, typed: null, draft: '', setDraft: () => undefined, cards, meter: null,
    mic: { muted: false, mode: 'open', held: false, permission: 'granted' },
    setMuted: () => undefined, setMode: () => undefined, hold: () => undefined, start: () => Promise.resolve(), end: () => Promise.resolve(),
    say: () => undefined, quiet: () => undefined, attach: () => () => undefined, navigate: () => undefined,
  } as unknown as VoiceSession
}

/** The floating view of a held session, folded to its orb: a press unfolds it. */
export async function mountFloating(): Promise<ReactElement> {
  const { FloatingView, PLACE_KEY, forgetPlace } = await import('../voice/Floating')
  const { stageFeed } = await import('../voice/Stage')
  const { MIC_START } = await import('../voice/mic')
  window.sessionStorage.setItem(PLACE_KEY, JSON.stringify({ corner: 'br', collapsed: true }))
  forgetPlace()
  const held: VoiceSession = { ...heldSession(), feed: stageFeed(), mic: MIC_START }
  return <FloatingView held={held} go={() => undefined} />
}

// ---------------------------------------------------------------- Voice it: projects and a session's record

/** Voice it › Projects, one project: Delete asks for its title typed back. */
export async function mountProjects(): Promise<ReactElement> {
  await live()
  serve((m, url) => {
    if (m === 'GET' && url.split('?')[0] === '/v1/voice/projects') {
      return { status: 200, body: { projects: [{ project_id: 'vp_0001', title: 'Export page', updated_at: at(30), last_session_at: at(40), open_cards: 2 }] } }
    }
    return null
  })
  const { VoiceProjects } = await import('../voice/Projects')
  return <VoiceProjects />
}

/** A session's record, the transcript refused: Download names that it was not downloaded. */
export async function mountSessionRecord(): Promise<ReactElement> {
  await live()
  serve((_m, url) => {
    if (url.includes('/summary')) return { status: 200, body: { session_id: SESSION, state: 'written', text: 'Planned the export page.', at: at(60) } }
    if (url.includes('/transcript')) return { status: 403, body: { code: 'TRANSCRIPT_REFUSED', message: 'The transcript is held for its owner.' } }
    return null
  })
  const { SessionRecord } = await import('../voice/SessionRecord')
  return <SessionRecord sessionId={SESSION} />
}
