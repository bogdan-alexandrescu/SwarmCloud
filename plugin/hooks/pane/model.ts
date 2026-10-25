// The SwarmCloud pane's model: what it holds, what one click reads, and the
// lines it draws -- everything but the engine calls, so a unit test runs it
// under plain node (tests/unit/mcp/test_sc_browser_pane.py) and the hooks
// module (../pane.tsx) only turns these lines into elements.
//
// PROPOSAL B, TWO COLUMNS, FOR EVERY FRAME (BGD, 2026-10-25, #2318; the
// mock-ups are docs/design/mockups/sc-browser-b.html in PR 2328): the open
// workflow's stages on the left, the selected stage's agents -- then one
// agent's details -- on the right; Level 1 a full-width list of this
// session's workflows (and, after session start, this project's running
// work); one column at a time on a narrow terminal.
//
// NOTHING IS READ UNTIL SOMEBODY ASKS. `act` is the only place a read is
// made: one `swarm_browse` call per click (open, a workflow, a stage, an
// agent, Refresh, Retry), none for moving, none on a timer, and none while
// the pane is closed. Each read is a direct MCP call over the engine's own
// connection, so no click reaches the model.
//
// STAGES ARE THE CONSOLE'S. A step's stage is its dependency level plus one,
// `levelsOf` in apps/swarm-ui/src/dag.ts, named `Stage N` as the workflow page
// labels each band; a stage's census is that band's (`stageCensus`: failures,
// then cancellations, then unread, then the biggest count first). The unit
// test runs dag.ts's own `levelsOf` over a fixture and holds `stagesOf` equal
// to it, by name and count.

export type Seg = {
  text: string
  /** The action a press runs (`act`); a pressable segment is drawn as a Button. */
  press?: string
  hotkey?: string
  dim?: boolean
  bold?: boolean
  color?: 'green' | 'red' | 'yellow' | 'cyan'
}
export type Line = Seg[]

import type {
  ScAgentDetail as AgentDetail,
  ScAgentRow as AgentRow,
  ScFailure as Failure,
  ScLayout as Layout,
  ScPaneState as PaneState,
  ScSingleTask as SingleTask,
  ScStep as Step,
  ScWorkflowRow as WorkflowRow,
} from '../../types/index.d.ts'

export type { AgentDetail, AgentRow, Failure, Layout, PaneState, SingleTask, Step, WorkflowRow }

export const SERVER = 'plugin:sc:swarmcloud'
export const BROWSE = 'swarm_browse'
export const LAYOUT = 'swarm_layout'

export function initialState(): PaneState {
  return {
    level: 'workflows', workflows: null, singles: [], listReadAt: null,
    workflowId: null, workflow: null, workflowReadAt: null,
    stage: null, agents: null, agentsReadAt: null,
    taskId: null, agent: null, agentReadAt: null, agentFrom: 'agents',
    layout: null, layoutNote: null, failure: null, calls: 0,
    project: false, started: false,
  }
}

// --------------------------------------------------------------------------
// The console's stages
// --------------------------------------------------------------------------

/** dag.ts's `levelsOf`, ported line for line: each inner list is one Stage. */
export function levelsOf(steps: readonly Step[]): Step[][] {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const depth = new Map<string, number>()
  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id)
    if (cached !== undefined) return cached
    if (seen.has(id)) return 0
    const step = byId.get(id)
    const parents = step ? step.depends_on || [] : []
    if (!step || parents.length === 0) {
      depth.set(id, 0)
      return 0
    }
    seen.add(id)
    const d = 1 + Math.max(...parents.map((p) => resolve(p, seen)))
    seen.delete(id)
    depth.set(id, d)
    return d
  }
  steps.forEach((s) => resolve(s.step_id, new Set()))
  if (steps.length === 0) return []
  const max = Math.max(0, ...steps.map((s) => depth.get(s.step_id) ?? 0))
  return Array.from({ length: max + 1 }, (_, lvl) => steps.filter((s) => (depth.get(s.step_id) ?? 0) === lvl))
}

const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']
const FAILED = ['FAILED', 'DEAD_LETTERED']
const RUNNING = ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING']

export type Count = { word: string; n: number }
export type Stage = { number: number; name: string; steps: Step[]; census: Count[] }

/** dag.ts's `censusWord`: `not started` with no task, `not read` with no state, else the state. */
export function censusWord(step: Step): string {
  if (!step.task_id) return 'not started'
  if (!step.state) return 'not read'
  return step.state.toLowerCase()
}

function rank(step: Step): number {
  if (!step.task_id) return 3
  if (!step.state) return 2
  if (FAILED.indexOf(step.state) >= 0) return 0
  return step.state === 'CANCELLED' ? 1 : 3
}

/** dag.ts's `stageCensus` order: failures, cancellations, unread, then biggest first. */
export function censusOf(steps: readonly Step[]): Count[] {
  const buckets = new Map<string, { n: number; rank: number }>()
  for (const step of steps) {
    const word = censusWord(step)
    const seen = buckets.get(word)
    if (seen) seen.n += 1
    else buckets.set(word, { n: 1, rank: rank(step) })
  }
  return [...buckets.entries()]
    .map(([word, b]) => ({ word, n: b.n, rank: b.rank }))
    .sort((a, b) => a.rank - b.rank || b.n - a.n || a.word.localeCompare(b.word))
    .map(({ word, n }) => ({ word, n }))
}

export function stagesOf(steps: readonly Step[]): Stage[] {
  return levelsOf(steps).map((level, index) => ({
    number: index + 1, name: 'Stage ' + (index + 1), steps: level, census: censusOf(level),
  }))
}

/** The console's page-head shape line: `1 → 13 → 1`. */
export function shapeText(stages: readonly Stage[]): string {
  return stages.map((s) => s.steps.length).join(' → ')
}

export function stageDone(stage: Stage): boolean {
  return stage.steps.every((s) => !!s.state && TERMINAL.indexOf(s.state) >= 0)
}

/** The stage the right column opens on: the first unfinished one, else the last. */
export function currentStage(stages: readonly Stage[]): number | null {
  if (stages.length === 0) return null
  const open = stages.find((s) => !stageDone(s))
  return (open || stages[stages.length - 1]).number
}

export function stageGlyph(stage: Stage): Seg {
  const states = stage.steps.map((s) => s.state || '')
  if (states.some((s) => FAILED.indexOf(s) >= 0 || s === 'CANCELLED')) return { text: '✗', color: 'red' }
  if (states.every((s) => s === 'SUCCEEDED')) return { text: '✓', color: 'green' }
  if (states.some((s) => RUNNING.indexOf(s) >= 0)) return { text: '●', color: 'cyan' }
  // A step parked only until its parents finish is waiting its turn, not held.
  if (stage.steps.some((s) => s.state === 'PARKED' && s.park_reason !== 'DEPENDENCY_INCOMPLETE')) return { text: '‖', color: 'yellow' }
  return { text: '○', dim: true }
}

export function stateGlyph(state: string | null | undefined): Seg {
  if (state === 'SUCCEEDED') return { text: '✓', color: 'green' }
  if (state && (FAILED.indexOf(state) >= 0 || state === 'CANCELLED')) return { text: '✗', color: 'red' }
  if (state && RUNNING.indexOf(state) >= 0) return { text: '●', color: 'cyan' }
  if (state === 'PARKED') return { text: '‖', color: 'yellow' }
  return { text: '○', dim: true }
}

/** A workflow's state as the list says it: `running, 1 failed`. */
export function workflowStateText(row: WorkflowRow): string {
  if (!row.state) return 'state not read'
  const failed = row.steps.filter((s) => s.state && FAILED.indexOf(s.state) >= 0).length
  const word = row.state.toLowerCase()
  return failed && row.state !== 'FAILED' ? word + ', ' + failed + ' failed' : word
}

export function workflowGlyph(row: WorkflowRow): Seg {
  if (row.state === 'SUCCEEDED') return { text: '✓', color: 'green' }
  if (row.state === 'FAILED' || row.state === 'CANCELLED' || row.state === 'DEAD_LETTERED') return { text: '✗', color: 'red' }
  if (!row.state || row.state === 'UNKNOWN') return { text: '?', dim: true }
  return { text: '●', color: 'cyan' }
}

export function stagesDone(row: WorkflowRow): string {
  const stages = stagesOf(row.steps)
  return stages.filter(stageDone).length + '/' + stages.length
}

// --------------------------------------------------------------------------
// Figures: an unknown figure is a dash, never 0
// --------------------------------------------------------------------------

export function duration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '—'
  const s = Math.floor(seconds)
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm'
  return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm'
}

export function tokens(count: number | null | undefined): string {
  if (typeof count !== 'number' || !isFinite(count) || count < 0) return '—'
  if (count >= 1_000_000) return (count / 1_000_000).toFixed(1) + 'M'
  if (count >= 1_000) return Math.round(count / 1_000) + 'k'
  return String(count)
}

export function money(usd: number | null | undefined): string {
  return typeof usd === 'number' && isFinite(usd) ? '$' + usd.toFixed(2) : '—'
}

/** `10:42:07Z` from an ISO time. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const at = new Date(iso)
  if (isNaN(at.getTime())) return '—'
  return at.toISOString().slice(11, 19) + 'Z'
}

export function ago(fromIso: string, toIso: string): string {
  const ms = new Date(toIso).getTime() - new Date(fromIso).getTime()
  if (!isFinite(ms) || ms < 60_000) return 'under a minute ago'
  const minutes = Math.floor(ms / 60_000)
  return minutes < 60 ? minutes + ' min ago' : Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min ago'
}

function prText(url: string | null | undefined): string | null {
  if (!url) return null
  const at = url.lastIndexOf('/pull/')
  return at >= 0 ? '#' + url.slice(at + 6).split('/')[0] : url
}

// --------------------------------------------------------------------------
// Text in cells
// --------------------------------------------------------------------------

export function width(text: string): number {
  return [...text].length
}

export function clip(text: string, room: number): string {
  if (room <= 0) return ''
  const chars = [...text]
  return chars.length <= room ? text : chars.slice(0, Math.max(0, room - 1)).join('') + '…'
}

function padEnd(text: string, room: number): string {
  const t = clip(text, room)
  return t + ' '.repeat(Math.max(0, room - width(t)))
}

function padStart(text: string, room: number): string {
  const t = clip(text, room)
  return ' '.repeat(Math.max(0, room - width(t))) + t
}

/** Greedy word wrap at `room` cells; a word longer than the room breaks after its last `/` that fits, else hard. */
export function wrap(text: string, room: number): string[] {
  const out: string[] = []
  let line = ''
  const words = text.split(' ').filter((w) => w.length > 0)
  for (let word of words) {
    while (width(word) > room) {
      const head = [...word].slice(0, room).join('')
      const slash = head.lastIndexOf('/')
      const cut = slash > 0 ? slash + 1 : room
      const piece = [...word].slice(0, cut).join('')
      if (line) {
        out.push(line)
        line = ''
      }
      out.push(piece)
      word = [...word].slice(cut).join('')
    }
    if (!word) continue
    if (!line) line = word
    else if (width(line) + 1 + width(word) <= room) line += ' ' + word
    else {
      out.push(line)
      line = word
    }
  }
  if (line) out.push(line)
  return out.length ? out : ['']
}

export function lineWidth(line: Line): number {
  return line.reduce((n, seg) => n + width(seg.text), 0)
}

/** `line` padded (or clipped) to exactly `room` cells. */
export function fit(line: Line, room: number): Line {
  const out: Line = []
  let used = 0
  for (const seg of line) {
    if (used >= room) break
    const left = room - used
    const text = width(seg.text) > left ? clip(seg.text, left) : seg.text
    out.push({ ...seg, text })
    used += width(text)
  }
  if (used < room) out.push({ text: ' '.repeat(room - used) })
  return out
}

function plain(text: string, extra: Partial<Seg> = {}): Seg {
  return { text, ...extra }
}

function rule(cols: number): Line {
  return [{ text: '─'.repeat(cols), dim: true }]
}

/** ` <left>` and `<right>` flush to the last column. */
function ends(left: Line, right: Line, cols: number): Line {
  const gap = cols - lineWidth(left) - lineWidth(right)
  return gap >= 1 ? [...left, plain(' '.repeat(gap)), ...right] : fit([...left, plain(' '), ...right], cols)
}

/** A paragraph wrapped into full-width lines: one space in, one cell spare. */
function paragraph(text: string, cols: number, extra: Partial<Seg> = {}): Line[] {
  return wrap(text, cols - 2).map((t) => [plain(' '), plain(t, extra)])
}

// The two columns: ` ` + 14 cells + `│`, then the right side.
const LEFT = 15

function columns(left: Line[], right: Line[], cols: number): Line[] {
  const rows = Math.max(left.length, right.length)
  const out: Line[] = []
  for (let i = 0; i < rows; i++) {
    out.push([...fit(left[i] || [], LEFT), plain('│', { dim: true }), ...fit(right[i] || [], cols - LEFT - 1)])
  }
  return out
}

/** Right-column text: one space in, wrapped with one cell spare and `indent` more. */
function rightText(text: string, cols: number, indent = 0, extra: Partial<Seg> = {}): Line[] {
  const room = cols - LEFT - 1 - 2 - indent
  return wrap(text, room).map((t) => [plain(' ' + ' '.repeat(indent)), plain(t, extra)])
}

// --------------------------------------------------------------------------
// The frames
// --------------------------------------------------------------------------

export type View = { cols: number; narrow: boolean; now: string }

/** Whether the terminal is too narrow for two columns beside the chat (B's 60-column frame). */
export function isNarrow(terminalColumns: number | undefined, bodyColumns: number): boolean {
  return (terminalColumns ?? bodyColumns) < 70 || bodyColumns < 40
}

function button(label: string, press: string, hotkey?: string): Seg {
  return { text: '[' + label + ']', press, hotkey }
}

function refreshButton(): Seg {
  return button('Refresh', 'refresh', 'r')
}

export function frame(state: PaneState, view: View): Line[] {
  if (state.level === 'settings') return settingsFrame(state, view)
  if (state.level === 'workflows' || !state.workflow) return listFrame(state, view)
  return workflowFrame(state, view)
}

// ---- Level 1 --------------------------------------------------------------

function listFrame(state: PaneState, view: View): Line[] {
  const { cols } = view
  const failure = state.failure
  const head: Line = [plain(state.project ? ' SwarmCloud · this project' : ' SwarmCloud · this session')]
  const out: Line[] = [failure ? fit(head, cols) : ends(head, [plain(clock(state.listReadAt) + ' '), refreshButton()], cols)]
  if (failure) out.push(fit([plain(' failed ' + clock(failure.at) + ' · ' + (state.listReadAt ? 'last good read ' + clock(state.listReadAt) : 'nothing read yet'))], cols))
  out.push(rule(cols))
  const rows = state.workflows || []
  if (!failure && state.workflows === null) {
    // UNREAD (BGD, 2026-10-25: "open it with no read"): the pane the session
    // start opened, before anyone pressed Refresh. Never the empty state: no
    // read said there is nothing.
    const left: Line[] = [[plain(' Stages')], [plain(' none: no')], [plain(' workflow open')]]
    const right: Line[] = [
      ...rightText('Not read yet.', cols),
      [],
      ...rightText('Refresh lists ' + (state.project ? 'this project’s running SwarmCloud work and this session’s' : 'this session’s SwarmCloud work') +
        ': one call, no model tokens.', cols),
    ]
    out.push(...columns(left, right, cols))
    out.push(rule(cols))
    out.push(fit([plain(' not read yet · ' + state.calls + ' call' + (state.calls === 1 ? '' : 's') + ' made, 0 model tokens')], cols))
    return out
  }
  if (failure || (rows.length === 0 && state.singles.length === 0)) {
    const left: Line[] = [[plain(' Stages')], [plain(' none: no')], [plain(' workflow open')]]
    const right: Line[] = failure ? errorBlock(failure, cols, state.listReadAt ? 'The list was last read at ' + clock(state.listReadAt) + ', ' + ago(state.listReadAt, failure.at) + ': not current.' : null) : [
      ...rightText('No SwarmCloud work', cols),
      ...rightText(state.project ? 'in this session or running in this project.' : 'in this session yet.', cols),
      [],
      ...rightText('Start one with /sc run <spec>, or attach running ones with /sc attach --all.', cols),
    ]
    out.push(...columns(left, right, cols))
    out.push(rule(cols))
    if (failure) out.push(fit([plain(' r retries · nothing retries by itself')], cols))
    else out.push(fit([plain(' ' + rows.length + ' workflow' + (rows.length === 1 ? '' : 's') + ' · ' + state.calls + ' call' + (state.calls === 1 ? '' : 's') + ' made, 0 model tokens')], cols))
    return out
  }
  const wide = cols >= 60
  const nameW = wide ? 16 : 14
  const stateW = wide ? 19 : 18
  const stgW = 4
  const timeW = wide ? 10 : 7
  const tokW = wide ? 7 : 6
  const lead = wide ? ' › ' : '› '
  const headCells = (wide ? '     ' : '    ') + padEnd('workflow', nameW) + padEnd('state', stateW) +
    (wide ? 'stages' + padStart('time', timeW - 2) + padStart('tokens', tokW) : padEnd('stg', stgW) + padStart('time', timeW) + padStart('tok', tokW))
  out.push(fit([plain(headCells, { dim: true })], cols))
  for (const row of rows) {
    out.push(fit([
      plain(lead),
      workflowGlyph(row),
      plain(' '),
      { text: padEnd(row.name, nameW) + padEnd(workflowStateText(row), stateW) + padEnd(stagesDone(row), stgW) +
        padStart(duration(row.duration_s), timeW) + padStart(tokens(row.tokens), tokW), press: 'open:' + row.workflow_id },
    ], cols))
  }
  for (const task of state.singles) {
    const stateText = task.state ? task.state.toLowerCase() : 'state not read'
    const done = task.state && TERMINAL.indexOf(task.state) >= 0 ? '1/1' : '0/1'
    out.push(fit([
      plain(lead),
      stateGlyph(task.state),
      plain(' '),
      { text: padEnd(task.label || task.task_id, nameW) + padEnd(stateText, stateW) + padEnd(done, stgW) +
        padStart(duration(task.age_seconds), timeW) + padStart('—', tokW), press: 'task:' + task.task_id },
    ], cols))
  }
  out.push(rule(cols))
  const all = rows.length + state.singles.length
  const running = rows.filter((r) => r.state && TERMINAL.indexOf(r.state) < 0).length + state.singles.length
  const succeeded = rows.filter((r) => r.state === 'SUCCEEDED').length
  const failed = rows.filter((r) => r.state && r.state !== 'SUCCEEDED' && TERMINAL.indexOf(r.state) >= 0).length
  const known = rows.map((r) => r.tokens).filter((t): t is number => typeof t === 'number')
  const costs = rows.map((r) => r.cost_usd).filter((c): c is number => typeof c === 'number')
  const parts = [all + (state.project ? ' in this session and project' : ' in this session')]
  if (running) parts.push(running + ' running')
  if (succeeded) parts.push(succeeded + ' succeeded')
  if (failed) parts.push(failed + ' failed')
  if (known.length) parts.push(tokens(known.reduce((a, b) => a + b, 0)) + ' tokens')
  if (costs.length) parts.push(money(costs.reduce((a, b) => a + b, 0)))
  out.push(...paragraph(parts.join(' · '), cols).map((l) => fit(l, cols)))
  out.push(fit([], cols))
  out.push(fit([plain(' Tab moves · Enter opens the stages · '), button('Layout', 'layout')], cols))
  return out
}

// ---- Levels 2-4 -----------------------------------------------------------

function stageCount(stage: Stage, selected: boolean): Line {
  return [
    plain(' '),
    stageGlyph(stage),
    { text: ' ' + padEnd(stage.name, 8) + padStart(String(stage.steps.length), 3), press: 'stage:' + stage.number, bold: selected },
  ]
}

function leftColumn(row: WorkflowRow, selected: number | null): Line[] {
  const stages = stagesOf(row.steps)
  const out: Line[] = [[plain(' Stages · ' + stages.length)]]
  for (const stage of stages) out.push(stageCount(stage, stage.number === selected))
  out.push([])
  out.push([plain(' ' + row.steps.length + ' step' + (row.steps.length === 1 ? '' : 's') + ' in')])
  out.push([plain(' ' + stages.length + ' stage' + (stages.length === 1 ? '' : 's'))])
  for (const piece of wrap('(' + shapeText(stages) + ')', LEFT - 2)) out.push([plain(' ' + piece)])
  return out
}

function censusLines(census: Count[]): string[] {
  const words = census.map((c) => c.n + ' ' + c.word)
  const out: string[] = []
  for (let i = 0; i < words.length; i += 2) out.push(words.slice(i, i + 2).join(', '))
  return out
}

function stepNames(stage: Stage): string {
  const names = stage.steps.map((s) => s.step_id)
  return names.length <= 2 ? names.join(', ') : names[0] + ' … ' + names[names.length - 1]
}

function errorBlock(failure: Failure, cols: number, lastGood: string | null): Line[] {
  const out: Line[] = [[plain(' '), plain('✗', { color: 'red' }), plain(' SwarmCloud unreachable', { bold: true })]]
  out.push(...rightText(failure.tool + ' did not answer: ' + failure.reason, cols, 2))
  out.push([plain('   '), button('Retry', 'retry', 'r'), plain('  no automatic retries', { dim: true })])
  if (lastGood) {
    out.push([])
    out.push(...rightText(lastGood, cols))
  }
  return out
}

function headerLine(state: PaneState, row: WorkflowRow, view: View, back: Seg): Line {
  const left: Line = [plain(' '), back, plain(' '), { text: '[' + row.name + ' ▾]', press: 'back:workflows' }]
  if (state.failure) return fit(left, view.cols)
  return ends(left, [plain(clock(state.workflowReadAt) + ' '), refreshButton()], view.cols)
}

function statusLine(state: PaneState, row: WorkflowRow, view: View): Line {
  if (state.failure) {
    const good = state.level === 'agent' ? state.agentReadAt : state.level === 'agents' ? state.agentsReadAt : state.workflowReadAt
    return fit([plain(' failed ' + clock(state.failure.at) + ' · ' + (good ? 'last good read ' + clock(good) : 'nothing read yet'))], view.cols)
  }
  return fit([
    plain(' '), workflowGlyph(row),
    plain(' ' + workflowStateText(row) + ' · stages ' + stagesDone(row) + ' · ' + duration(row.duration_s) + ' · ' + tokens(row.tokens)),
  ], view.cols)
}

function stageRight(state: PaneState, row: WorkflowRow, stage: Stage, view: View): Line[] {
  const { cols } = view
  if (state.level === 'stages' || !state.agents) {
    return [
      ...rightText(stage.name + ' · ' + stage.steps.length + ' step' + (stage.steps.length === 1 ? '' : 's'), cols),
      ...rightText(stepNames(stage), cols),
      [],
      ...censusLines(stage.census).flatMap((t) => rightText(t, cols)),
      [],
      ...rightText('Its agents are not loaded yet. Enter loads them: one call.', cols),
    ]
  }
  return agentLines(state, stage, cols - LEFT - 1, true)
}

/** Level 3's agents: one line each where every one fits, else two lines each. */
function agentLines(state: PaneState, stage: Stage, room: number, header: boolean): Line[] {
  const agents = state.agents || []
  const out: Line[] = []
  if (header) out.push(ends([plain(' ' + stage.name + ' · ' + agents.length + ' agent' + (agents.length === 1 ? '' : 's'))], [refreshButton()], room))
  const name = (a: AgentRow) => a.step_id || a.task_id
  const stateWord = (a: AgentRow) => (a.state ? a.state.toLowerCase() : 'not read')
  const one = (a: AgentRow) => name(a) + ' ' + stateWord(a) + (a.note ? ' · ' + a.note : '')
  const split = agents.some((a) => 3 + width(one(a)) > room - 1)
  for (const a of agents) {
    if (!split) {
      out.push([plain(' '), stateGlyph(a.state), { text: ' ' + one(a), press: 'agent:' + a.task_id }])
      continue
    }
    out.push([plain(' '), stateGlyph(a.state), { text: ' ' + name(a) + ' ' + stateWord(a), press: 'agent:' + a.task_id }])
    if (a.note) out.push([plain('   ' + clip(a.note, room - 4), { dim: true })])
  }
  return out
}

function detailRows(agent: AgentDetail, stage: Stage | null, room: number): Line[] {
  const labelW = 11
  const values: [string, string][] = []
  const state = agent.state ? agent.state.toLowerCase() : 'not read'
  values.push(['state', agent.state && TERMINAL.indexOf(agent.state) >= 0 && agent.state !== 'SUCCEEDED' && agent.attempts
    ? state + ' on attempt ' + agent.attempts
    : agent.state === 'PARKED' && agent.park_reason ? state + ' · ' + agent.park_reason.toLowerCase().replace(/_/g, ' ') : state])
  values.push(['attempts', typeof agent.attempts === 'number' && typeof agent.max_attempts === 'number'
    ? agent.attempts + ' of ' + agent.max_attempts + ' allowed' : '—'])
  values.push(['duration', duration(agent.duration_s) + (agent.duration_basis === 'so far' ? ' so far' : '')])
  values.push(['cost', money(agent.cost_usd)])
  values.push(['tokens', typeof agent.tokens === 'number' ? tokens(agent.tokens) + ' (' + agent.tokens.toLocaleString('en-US') + ')' : '—'])
  values.push(['last error', agent.last_error || 'none'])
  values.push(['PR', prText(agent.pr_url) || 'none'])
  // A name never breaks, nor parts from the `·` after it (no-break spaces, as B draws them).
  const names = (agent.artifacts || []).map((name) => name.replace(/ /g, '\u00a0'))
  values.push(['artifacts', names.length ? names.map((name, i) => (i < names.length - 1 ? name + '\u00a0·' : name)).join(' ') : 'none'])
  values.push(['console', agent.console ? agent.console.replace(/^https?:\/\//, '') : 'none'])
  const out: Line[] = [
    [plain(' '), button('‹ ' + (stage ? stage.name : 'Workflows'), 'back:agents')],
    [plain(' '), stateGlyph(agent.state), plain(' ' + (agent.step_id || agent.task_id) + (stage ? ' · ' + stage.name : ''))],
    [],
  ]
  for (const [label, value] of values) {
    const pieces = wrap(value, room - 1 - labelW)
    pieces.forEach((piece, i) => out.push([plain(' ' + (i === 0 ? padEnd(label, labelW) : ' '.repeat(labelW))), plain(piece)]))
  }
  out.push([])
  out.push([plain(' '), refreshButton(), plain(' '), button('Console ↗', 'console')])
  return out
}

function workflowFrame(state: PaneState, view: View): Line[] {
  const row = state.workflow as WorkflowRow
  const { cols } = view
  const stages = stagesOf(row.steps)
  const selected = state.stage ?? currentStage(stages)
  const stage = stages.find((s) => s.number === selected) || null
  if (view.narrow) return narrowFrame(state, row, stage, view)
  const out: Line[] = [
    headerLine(state, row, view, button('‹ Workflows', 'back:workflows')),
    statusLine(state, row, view),
    rule(cols),
  ]
  let right: Line[]
  if (state.failure) {
    right = errorBlock(state.failure, cols, state.workflowReadAt
      ? 'Stages on the left are as last read at ' + clock(state.workflowReadAt) + ', ' + ago(state.workflowReadAt, state.failure.at) + ': not current.'
      : null)
  } else if (state.level === 'agent' && state.agent) {
    right = detailRows(state.agent, stage, cols - LEFT - 1)
  } else if (stage) {
    right = stageRight(state, row, stage, view)
  } else {
    right = rightText('This workflow has no steps.', cols)
  }
  out.push(...columns(leftColumn(row, selected), right, cols))
  out.push(rule(cols))
  const hint = state.failure
    ? ' r retries · nothing retries by itself'
    : state.level === 'agent'
      ? ' r refreshes this agent only · ‹ back to the agents'
      : state.level === 'agents'
        ? ' Tab moves · Enter opens it · ‹ Workflows'
        : ' Tab moves · Enter loads its agents · ‹ Workflows'
  out.push(fit([plain(hint)], cols))
  return out
}

/** One column at a time (B's 60-column frame): the pane has the whole terminal. */
function narrowFrame(state: PaneState, row: WorkflowRow, stage: Stage | null, view: View): Line[] {
  const { cols } = view
  const out: Line[] = []
  if (state.level === 'agent' && state.agent) {
    out.push(ends([plain(' '), button('‹ ' + (stage ? stage.name : 'Stages'), 'back:agents'), plain(' ' + (state.agent.step_id || state.agent.task_id))], [refreshButton()], cols))
    out.push(rule(cols))
    out.push(...detailRows(state.agent, stage, cols).slice(3).map((l) => fit(l, cols)))
  } else if (state.level === 'agents' && stage && state.agents) {
    out.push(ends([plain(' '), button('‹ Stages', 'back:stages'), plain(' ' + row.name + ' › ' + stage.name)], [refreshButton()], cols))
    out.push(rule(cols))
    censusLines(stage.census).forEach((t, i) => out.push(fit(i === 0 ? [plain(' '), stageGlyph(stage), plain(' ' + t)] : [plain('   ' + t)], cols)))
    out.push(rule(cols))
    out.push(...agentLines(state, stage, cols, false).map((l) => fit(l, cols)))
  } else {
    out.push(ends([plain(' '), button('‹ Workflows', 'back:workflows'), plain(' ' + row.name)], [refreshButton()], cols))
    out.push(statusLine(state, row, view))
    out.push(rule(cols))
    out.push(...leftColumn(row, stage ? stage.number : null).map((l) => fit(l, cols)))
  }
  if (state.failure) {
    out.push(rule(cols))
    out.push(...errorBlock(state.failure, cols + LEFT + 1, null).map((l) => fit(l, cols)))
  }
  out.push(rule(cols))
  out.push(fit([plain(state.failure ? ' r retries · nothing retries by itself' : state.level === 'agents'
    ? ' Tab moves · Enter opens it · Esc back to chat'
    : ' Tab moves · Enter opens · Esc back to chat')], cols))
  return out
}

// ---- The toggle -----------------------------------------------------------

function settingsFrame(state: PaneState, view: View): Line[] {
  const { cols } = view
  const out: Line[] = [ends([plain(' SwarmCloud · settings')], [button('‹ Workflows', 'back:workflows')], cols), rule(cols)]
  const name = (layout: Layout | null) => (layout === 'pane' ? 'Pane + 1 row each' : layout === 'rows' ? 'Steps (today)' : 'not read')
  out.push(fit([plain(' Layout  [' + name(state.layout) + ' ▾]')], cols))
  out.push(fit([], cols))
  const option = (layout: Layout, note: string): Line[] => [
    fit([plain('         '), { text: (state.layout === layout ? '● ' : '○ ') + name(layout), press: 'layout:' + layout }], cols),
    fit([plain('           ' + note, { dim: true })], cols),
  ]
  out.push(...option('pane', 'one quiet row per workflow'))
  out.push(...option('rows', 'one row per unfinished step, as the rows setting says'))
  out.push(fit([], cols))
  if (state.failure) out.push(...paragraph(state.failure.tool + ' did not answer: ' + state.failure.reason, cols))
  out.push(...paragraph(state.layoutNote || 'Enter switches and saves it for this project. The session keeps running and SwarmCloud is not touched.', cols))
  return out.map((l) => fit(l, cols))
}

// --------------------------------------------------------------------------
// Reading: one call per click
// --------------------------------------------------------------------------

export type Reply = { ok: true; data: any } | { ok: false; reason: string }
/** One direct MCP call to the sc plugin's server: `$.mcp.call(SERVER, tool, args)`, parsed. */
export type Call = (tool: string, args: Record<string, unknown>) => Promise<Reply>

/** An MCP tool result as `$.mcp.call` resolves it, parsed: JSON text, or the reason it failed. */
export function parseResult(result: { content?: Array<{ type: string; text?: string }>; isError?: boolean } | null | undefined): Reply {
  const text = (result && Array.isArray(result.content) ? result.content : [])
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text as string)
    .join('\n')
  if (!result || result.isError) return { ok: false, reason: clip(text.replace(/\s+/g, ' ').trim() || 'the tool reported an error and said nothing more', 300) }
  try {
    return { ok: true, data: JSON.parse(text) }
  } catch {
    return { ok: false, reason: 'its reply was not JSON: ' + clip(text.replace(/\s+/g, ' '), 120) }
  }
}

/** A call that threw: the engine could not reach the server at all. */
export function unreachable(error: unknown): Reply {
  const message = error instanceof Error ? error.message : String(error)
  return { ok: false, reason: "the sc plugin's SwarmCloud server is not connected in this session (" + clip(message.replace(/\s+/g, ' '), 160) + ').' }
}

type Read = { tool: string; args: Record<string, unknown>; take: (state: PaneState, data: any, at: string) => PaneState }

function stageTasks(state: PaneState, number: number): string[] {
  const stage = stagesOf(state.workflow ? state.workflow.steps : []).find((s) => s.number === number)
  return stage ? stage.steps.map((s) => s.task_id).filter((t): t is string => typeof t === 'string' && t.length > 0) : []
}

/** Level 1's read: this session's work, and with `project` (set at session start) this project's running work too. */
function readList(state: PaneState): Read {
  return {
    tool: BROWSE, args: state.project ? { level: 'workflows', project: true } : { level: 'workflows' },
    take: (s, d, at) => ({ ...s, level: 'workflows', workflows: Array.isArray(d.workflows) ? d.workflows : [], singles: Array.isArray(d.single_tasks) ? d.single_tasks : [], listReadAt: d.read_at || at }),
  }
}

function readWorkflow(id: string): Read {
  return {
    tool: BROWSE, args: { level: 'stages', workflow_id: id },
    take: (s, d, at) => {
      const same = s.workflowId === id
      return { ...s, level: same && s.level !== 'workflows' ? s.level : 'stages', workflowId: id, workflow: d.workflow, workflowReadAt: d.read_at || at,
        stage: same ? s.stage : null, agents: same ? s.agents : null, agentsReadAt: same ? s.agentsReadAt : null }
    },
  }
}

function readStage(state: PaneState, number: number): Read | null {
  const ids = stageTasks(state, number)
  if (ids.length === 0) return null
  return {
    tool: BROWSE, args: { level: 'agents', task_ids: ids },
    take: (s, d, at) => ({ ...s, level: 'agents', stage: number, agents: Array.isArray(d.agents) ? d.agents : [], agentsReadAt: d.read_at || at }),
  }
}

function readAgent(taskId: string, from: 'agents' | 'workflows'): Read {
  return {
    tool: BROWSE, args: { level: 'agent', task_id: taskId },
    take: (s, d, at) => ({ ...s, level: 'agent', taskId, agent: d, agentReadAt: d.read_at || at, agentFrom: from }),
  }
}

const READ_LAYOUT: Read = {
  tool: LAYOUT, args: {},
  take: (s, d) => ({ ...s, level: 'settings', layout: d.layout === 'pane' || d.layout === 'rows' ? d.layout : null }),
}

function saveLayout(layout: Layout): Read {
  return {
    tool: LAYOUT, args: { layout },
    take: (s, d) => ({ ...s, level: 'settings', layout: d.layout === 'pane' || d.layout === 'rows' ? d.layout : layout,
      layoutNote: 'Saved for this project: ' + (layout === 'pane' ? 'Pane + 1 row each' : 'Steps (today)') + '. ' + (d.note || '') }),
  }
}

/** The read the current level's Refresh (or Retry) makes. */
function refreshRead(state: PaneState): Read | null {
  if (state.level === 'settings') return READ_LAYOUT
  if (state.level === 'agent' && state.taskId) return readAgent(state.taskId, state.agentFrom)
  if (state.level === 'agents' && state.stage !== null) return readStage(state, state.stage)
  if (state.level === 'stages' && state.workflowId) return readWorkflow(state.workflowId)
  return readList(state)
}

/** What `action` reads, or how it moves without reading. */
export function plan(state: PaneState, action: string): { read: Read | null; move: PaneState | null } {
  const [verb, ...rest] = action.split(':')
  const arg = rest.join(':')
  if (verb === 'open') return { read: action === 'open' ? readList(state) : readWorkflow(arg), move: null }
  if (verb === 'retry' && state.failure && state.failure.action !== 'retry') return plan({ ...state, failure: null }, state.failure.action)
  if (verb === 'refresh' || verb === 'retry') return { read: refreshRead(state), move: null }
  if (verb === 'stage') {
    const read = readStage(state, Number(arg))
    return read ? { read, move: null } : { read: null, move: { ...state, stage: Number(arg), level: 'stages', agents: null } }
  }
  if (verb === 'agent') return { read: readAgent(arg, 'agents'), move: null }
  if (verb === 'task') return { read: readAgent(arg, 'workflows'), move: null }
  if (verb === 'layout') return { read: arg === 'pane' || arg === 'rows' ? saveLayout(arg) : READ_LAYOUT, move: null }
  if (verb === 'back') {
    const failure = null
    if (arg === 'workflows') return { read: null, move: { ...state, level: 'workflows', failure, layoutNote: null } }
    if (arg === 'stages') return { read: null, move: { ...state, level: 'stages', failure } }
    if (arg === 'agents') {
      if (state.agentFrom === 'workflows') return { read: null, move: { ...state, level: 'workflows', failure } }
      return { read: null, move: { ...state, level: state.agents ? 'agents' : 'stages', failure } }
    }
  }
  return { read: null, move: null }
}

/** Run `action`: at most ONE `call`, and none for a move. Never throws. */
export async function act(state: PaneState, action: string, call: Call, now: () => string): Promise<PaneState> {
  const { read, move } = plan(state, action)
  if (!read) return move || state
  let reply: Reply
  try {
    reply = await call(read.tool, read.args)
  } catch (error) {
    reply = unreachable(error)
  }
  const at = now()
  const counted = { ...state, calls: state.calls + 1 }
  if (!reply.ok) {
    const again = action === 'retry' && state.failure ? state.failure.action : action
    return { ...counted, failure: { tool: read.tool, reason: reply.reason, at, action: again } }
  }
  return { ...read.take(counted, reply.data || {}, at), failure: null }
}
