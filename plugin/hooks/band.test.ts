// The SwarmCloud band above the prompt against the engine itself (SC-BAND,
// part of #2318): `claude plugin test plugin`, with
// CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 while function hooks are early access.
// The test's `on('mcp.call')` stands for the sc plugin's SwarmCloud server,
// so every read the band makes is counted here; `on('command.run')` beneath
// the plugin is where a queued chat turn lands; `on('clock.every')` hands the
// test the band's minute, one tick per resolve. Nothing stands for the model.
// tests/unit/mcp/test_sc_band.py runs this file where the claude CLI is
// installed, and holds the same rules over pane/model.ts under node everywhere.

import { expect, test } from 'claude-code/testing'

type Called = { server: string; tool: string; args: Record<string, unknown> }

const READ_AT = '2026-10-26T16:42:07Z'
const step = (id: string, deps: string[], state: string, extra: Record<string, unknown> = {}) => ({ step_id: id, task_id: 'task_' + id, depends_on: deps, state, ...extra })
const MIDDLE = ['a', 'b', 'c'].map((c) => step('counts-' + c, ['plan'], c === 'b' ? 'FAILED' : 'RUNNING'))
const WORKFLOW = {
  workflow_id: 'wf_1', name: 'COUNTS-REVIEW', state: 'RUNNING', duration_s: 7500, tokens: 7600000,
  steps: [step('plan', [], 'SUCCEEDED'), ...MIDDLE, step('integrate', MIDDLE.map((s) => s.step_id), 'QUEUED')],
}
const WAITING = {
  workflow_id: 'wf_2', name: 'ASK-OWNER', state: 'RUNNING', duration_s: 600, tokens: null,
  steps: [step('ask', [], 'PARKED', { park_reason: 'AWAITING_INPUT' })],
}

/** The sc plugin's server: swarm_layout answers `layout`, and the list is whatever `list()` says now. */
function server(called: Called[], layout: string, list: () => unknown[]) {
  return async (_$: unknown, e: { server: string; tool: string; args?: Record<string, unknown> }) => {
    called.push({ server: e.server, tool: e.tool, args: e.args || {} })
    const level = e.args && e.args.level
    const body = e.tool === 'swarm_layout'
      ? { layout: (e.args && e.args.layout) || layout, source: 'saved for this project', note: 'rows already running keep going' }
      : level === 'workflows' ? { read_at: READ_AT, workflows: list(), single_tasks: [] }
        : level === 'stages' ? { read_at: READ_AT, workflow: WORKFLOW }
          : level === 'agents' ? { read_at: READ_AT, agents: MIDDLE.map((s) => ({ task_id: s.task_id, step_id: s.step_id, state: s.state, note: 'attempt 1 of 2' })) }
            : { read_at: READ_AT, task_id: 'task_counts-b', step_id: 'counts-b', state: 'FAILED', attempts: 2, max_attempts: 2 }
    return { value: { content: [{ type: 'text', text: JSON.stringify(body) }], isError: false } }
  }
}

type Engine = { statuses: (string | null)[]; queued: string[]; ticks: Array<() => void>; drawnBeneath: number }

/** What lies beneath the plugin: the status line, queued turns, the minute, the clock, and another band. */
function beneath(on: any): Engine {
  const engine: Engine = { statuses: [], queued: [], ticks: [], drawnBeneath: 0 }
  on('ui.status', async (_$: unknown, e: { text?: string }) => {
    engine.statuses.push(e.text === undefined ? null : e.text)
    return { value: undefined }
  })
  on('command.run', async (_$: unknown, e: { command: string; args: string }) => {
    engine.queued.push('/' + e.command + ' ' + e.args)
    return { text: '' }
  })
  on('clock.every', async () => new Promise((resolve) => engine.ticks.push(() => resolve({ value: undefined }))))
  on('clock.now', async () => ({ value: Date.parse('2026-10-26T16:48:12Z') }))
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.render', async () => {
    engine.drawnBeneath++
    return { type: 'Text', children: ['beneath'] }
  })
  return engine
}

function props(cols: number, maxRows = 30, hasSurvey = false) {
  return { hasSurvey, isWorking: false, maxRows, bodyColumns: cols, scroll: { offset: 0, bodyRows: maxRows - 1 } }
}

async function mountBand($: any, cols = 120, maxRows = 30, hasSurvey = false) {
  return await $.ui.mount({ plugin: 'sc', surface: 'terminal', component: 'AbovePrompt', requestId: 'above-prompt', props: props(cols, maxRows, hasSurvey), viewport: { columns: cols, rows: 60 } })
}

/** Each drawn row as its text: Texts and Buttons' labels, left to right. */
async function rows(ui: any): Promise<string[]> {
  const drawn = await ui.drawn()
  if (!drawn || typeof drawn === 'string') return []
  const text = (node: any): string => {
    if (typeof node === 'string') return node
    if (!node) return ''
    if (node.type === 'Button') return String(node.props && node.props.label)
    return (node.children || []).map(text).join('')
  }
  return (drawn.children || []).map(text)
}

async function press(ui: any, label: RegExp) {
  const found = await ui.find({ type: 'Button', text: label })
  expect(found).toBeTruthy()
  await ui.press({ key: found.key })
}

/** Until `done` holds -- the engine re-arms the minute on a later macrotask, so this waits on timers too -- then a little longer. */
async function settle(done: () => boolean) {
  for (let i = 0; i < 200 && !done(); i++) await new Promise((resolve) => setTimeout(resolve, 1))
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 1))
}

async function tick(engine: Engine, called: Called[]) {
  const before = called.length
  const fire = engine.ticks.shift()
  expect(fire).toBeTruthy()
  ;(fire as () => void)()
  await settle(() => called.length > before)
}

test('in the band layout session start opens the band with ONE list read of this session and project, and the line under the prompt', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', server(called, 'band', () => [WORKFLOW, WAITING]) as never)
  const engine = beneath(on)
  on('session.start', async (_$: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('command.register', async () => ({ value: { command: 'sc-pane' } }))
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true } as never)
  await settle(() => engine.statuses.length > 0)
  expect(called.map((c) => c.tool)).toEqual(['swarm_layout', 'swarm_browse'])
  expect(called[1].args).toEqual({ level: 'workflows', project: true })
  expect(engine.statuses).toEqual(['SC 2 running · 1 waits on you'])
  expect(engine.queued).toEqual([])
  const drawn = await rows(await mountBand($))
  expect(drawn[0]).toContain('SwarmCloud · 2 running · 1 waits on you')
  expect(drawn.some((r) => r.indexOf('COUNTS-REVIEW') >= 0)).toBe(true)
})

test('the band draws each level, one swarm_browse call per press', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', server(called, 'band', () => [WORKFLOW]) as never)
  beneath(on)
  const ran = await $.command.run({ command: 'sc', args: 'layout band' })
  expect(String(ran.text)).toContain('Layout set to band')
  expect(called.map((c) => c.tool)).toEqual(['swarm_layout', 'swarm_browse'])
  const ui = await mountBand($)
  await press(ui, /COUNTS-REVIEW/)
  expect(called[2].args).toEqual({ level: 'stages', workflow_id: 'wf_1' })
  expect((await rows(ui)).some((r) => r.indexOf('Stage 2') >= 0)).toBe(true)
  await press(ui, /Stage 2/)
  expect(called[3].args).toEqual({ level: 'agents', task_ids: ['task_counts-a', 'task_counts-b', 'task_counts-c'] })
  await press(ui, /counts-b/)
  expect(called[4].args).toEqual({ level: 'agent', task_id: 'task_counts-b' })
  expect((await rows(ui)).some((r) => r.indexOf('2 of 2') >= 0)).toBe(true)
  await press(ui, /‹ Workflows/)
  expect(called.length).toBe(5)
})

test('Fold leaves one line, the summary, and reads nothing; so does a band one row tall', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', server(called, 'band', () => [WORKFLOW, WAITING]) as never)
  beneath(on)
  await $.command.run({ command: 'sc', args: 'layout band' })
  const ui = await mountBand($)
  const reads = called.length
  await press(ui, /Fold/)
  await settle(() => false)
  const folded = await rows(ui)
  expect(folded.length).toBe(1)
  expect(folded[0]).toContain('SwarmCloud · 2 running · 1 waits on you')
  expect(folded[0]).toContain('[Open]')
  expect(called.length).toBe(reads)
  await press(ui, /Open/)
  await settle(() => false)
  expect((await rows(ui)).length).toBeGreaterThan(1)
  await ui.unmount()
  expect((await rows(await mountBand($, 120, 1))).length).toBe(1)
})

for (const cols of [60, 160]) {
  test('the band fits ' + cols + ' columns at every level', async ($, on) => {
    const called: Called[] = []
    on('mcp.call', server(called, 'band', () => [WORKFLOW]) as never)
    beneath(on)
    await $.command.run({ command: 'sc', args: 'layout band' })
    const ui = await mountBand($, cols)
    const fits = async () => {
      for (const row of await rows(ui)) expect([...row].length).toBeLessThanOrEqual(cols)
    }
    await fits()
    await press(ui, /COUNTS-REVIEW/)
    await fits()
    await press(ui, /Stage 2/)
    await fits()
    // Two columns where there is room, one at a time on a narrow terminal.
    expect((await rows(ui)).some((r) => r.indexOf('│') >= 0)).toBe(cols > 70)
  })
}

test('a survey holds the band: it yields, and so does any layout but band', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', server(called, 'band', () => [WORKFLOW]) as never)
  const engine = beneath(on)
  await (await mountBand($)).unmount()
  expect(engine.drawnBeneath).toBe(1)
  await $.command.run({ command: 'sc', args: 'layout band' })
  const reads = called.length
  const ui = await mountBand($, 120, 30, true)
  expect(engine.drawnBeneath).toBe(2)
  expect(JSON.stringify(await ui.drawn())).not.toContain('SwarmCloud')
  expect(called.length).toBe(reads)
})

test('the line under the prompt follows each read and is cleared when nothing runs or the layout leaves band', async ($, on) => {
  const called: Called[] = []
  let list: unknown[] = [WORKFLOW, WAITING]
  on('mcp.call', server(called, 'band', () => list) as never)
  const engine = beneath(on)
  await $.command.run({ command: 'sc', args: 'layout band' })
  expect(engine.statuses).toEqual(['SC 2 running · 1 waits on you'])
  const ui = await mountBand($)
  list = [{ ...WORKFLOW, state: 'SUCCEEDED' }, { ...WAITING, state: 'FAILED' }]
  await press(ui, /Refresh/)
  await settle(() => engine.statuses.length > 1)
  expect(engine.statuses).toEqual(['SC 2 running · 1 waits on you', null])
  list = [WORKFLOW]
  await press(ui, /Refresh/)
  await settle(() => engine.statuses.length > 2)
  expect(engine.statuses[2]).toBe('SC 1 running')
  await ui.unmount()
  await $.command.run({ command: 'sc', args: 'layout rows' })
  await settle(() => engine.statuses.length > 3)
  expect(engine.statuses[3]).toBe(null)
  expect(engine.drawnBeneath).toBe(0)
  await mountBand($)
  expect(engine.drawnBeneath).toBe(1)
})

test('the minute queues exactly one turn for a finished workflow, and none for an unchanged read', async ($, on) => {
  const called: Called[] = []
  let list: unknown[] = [WORKFLOW]
  on('mcp.call', server(called, 'band', () => list) as never)
  const engine = beneath(on)
  await $.command.run({ command: 'sc', args: 'layout band' })
  await settle(() => engine.ticks.length > 0)
  expect(engine.queued).toEqual([])
  expect(engine.ticks.length).toBe(1)

  await tick(engine, called)
  expect(called[called.length - 1].args).toEqual({ level: 'workflows', project: true })
  await settle(() => engine.ticks.length > 0)
  expect(engine.queued).toEqual([])

  list = [{ ...WORKFLOW, state: 'SUCCEEDED', pull_requests: ['https://github.com/acme/app/pull/2463'] }]
  await tick(engine, called)
  await settle(() => engine.queued.length > 0)
  expect(engine.queued).toEqual(['/sc status wf_1 · finished: succeeded · PR #2463'])
  // Nothing runs now: the minute stopped, and the line under the prompt is cleared.
  expect(engine.statuses[engine.statuses.length - 1]).toBe(null)
  const reads = called.length
  await settle(() => false)
  expect(engine.ticks.length).toBe(0)
  expect(called.length).toBe(reads)
})

test('a step that starts waiting for a person is one turn; a read that only moves a step is none', async ($, on) => {
  const called: Called[] = []
  let list: unknown[] = [WORKFLOW]
  on('mcp.call', server(called, 'band', () => list) as never)
  const engine = beneath(on)
  await $.command.run({ command: 'sc', args: 'layout band' })
  await settle(() => engine.ticks.length > 0)
  list = [{ ...WORKFLOW, steps: WORKFLOW.steps.map((s) => (s.step_id === 'counts-a' ? { ...s, state: 'SUCCEEDED' } : s)) }]
  await tick(engine, called)
  await settle(() => engine.ticks.length > 0)
  expect(engine.queued).toEqual([])
  list = [{ ...WORKFLOW, steps: WORKFLOW.steps.map((s) => (s.step_id === 'integrate' ? { ...s, state: 'PARKED', park_reason: 'AWAITING_INPUT' } : s)) }]
  await tick(engine, called)
  await settle(() => engine.queued.length > 0)
  expect(engine.queued).toEqual(['/sc status wf_1 · integrate waits for a person'])
})
