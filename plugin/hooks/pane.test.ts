// The SwarmCloud pane against the engine itself: `claude plugin test plugin`
// (with CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 while function hooks are early
// access). The test's `on('mcp.call')` stands for the sc plugin's SwarmCloud
// server, so every MCP call the pane makes is counted here; nothing stands for
// the model, so a click that reached it would fail the test.
// tests/unit/mcp/test_sc_browser_pane.py runs this file where the claude CLI is
// installed, and holds the same rules over pane/model.ts under node everywhere.

import { expect, test } from 'claude-code/testing'

type Called = { server: string; tool: string; args: Record<string, unknown> }

const READ_AT = '2026-10-25T10:42:07Z'
const step = (id: string, deps: string[], state: string) => ({ step_id: id, task_id: 'task_' + id, depends_on: deps, state })
const MIDDLE = ['a', 'b', 'c'].map((c) => step('counts-' + c, ['plan'], c === 'b' ? 'FAILED' : 'RUNNING'))
const WORKFLOW = {
  workflow_id: 'wf_1', name: 'COUNTS-REVIEW', state: 'RUNNING', duration_s: 7500, tokens: 7600000,
  steps: [step('plan', [], 'SUCCEEDED'), ...MIDDLE, step('integrate', MIDDLE.map((s) => s.step_id), 'QUEUED')],
}

function answers(called: Called[], broken = false) {
  return async (_$: unknown, e: { server: string; tool: string; args?: Record<string, unknown> }) => {
    called.push({ server: e.server, tool: e.tool, args: e.args || {} })
    if (broken) throw new Error('connect: connection refused')
    const level = e.args && e.args.level
    const body = e.tool === 'swarm_layout'
      ? { layout: (e.args && e.args.layout) || 'rows', source: 'saved for this project', note: 'rows already running keep going' }
      : level === 'workflows' ? { read_at: READ_AT, workflows: [WORKFLOW], single_tasks: [] }
        : level === 'stages' ? { read_at: READ_AT, workflow: WORKFLOW }
          : level === 'agents' ? { read_at: READ_AT, agents: MIDDLE.map((s) => ({ task_id: s.task_id, step_id: s.step_id, state: s.state, note: 'attempt 1 of 2' })) }
            : { read_at: READ_AT, task_id: 'task_counts-b', step_id: 'counts-b', state: 'FAILED', attempts: 2, max_attempts: 2 }
    return { value: { content: [{ type: 'text', text: JSON.stringify(body) }], isError: false } }
  }
}

const PROPS = { title: 'SwarmCloud', isFocused: true, bodyColumns: 53, placement: 'dock' } as never
const VIEWPORT = { columns: 80, rows: 30 } as never

/** Nothing beneath the plugins places panes or tells the time in a test: the engine's answers, stood in for. */
function placesPanes(on: any) {
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('clock.now', async () => ({ value: Date.parse('2026-10-25T10:48:12Z') }))
}

async function mountPane($: any) {
  return await $.ui.mount({ plugin: 'sc', surface: 'terminal', component: 'Pane', requestId: 'swarmcloud', props: PROPS, viewport: VIEWPORT })
}

async function press(ui: any, text: RegExp) {
  const found = await ui.find({ type: 'Button', text })
  expect(found).toBeTruthy()
  await ui.press({ key: found.key })
}

test('opening the pane makes one swarm_browse call; each click makes exactly one more', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', answers(called) as never)
  placesPanes(on)
  await $.command.run({ command: 'sc-pane', args: '' })
  expect(called.length).toBe(1)
  expect(called[0].server).toBe('plugin:sc:swarmcloud')
  expect(called[0].tool).toBe('swarm_browse')
  expect(called[0].args).toEqual({ level: 'workflows' })

  const ui = await mountPane($)
  expect(called.length).toBe(1)
  await press(ui, /COUNTS-REVIEW/)
  expect(called.length).toBe(2)
  expect(called[1].args).toEqual({ level: 'stages', workflow_id: 'wf_1' })

  await press(ui, /Stage 2/)
  expect(called.length).toBe(3)
  expect(called[2].args).toEqual({ level: 'agents', task_ids: ['task_counts-a', 'task_counts-b', 'task_counts-c'] })

  await press(ui, /counts-b/)
  expect(called.length).toBe(4)
  expect(called[3].args).toEqual({ level: 'agent', task_id: 'task_counts-b' })
})

test('drawing again reads nothing: no click, no call', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', answers(called) as never)
  placesPanes(on)
  await $.command.run({ command: 'sc-pane', args: '' })
  const before = called.length
  const first = await mountPane($)
  await first.unmount()
  await mountPane($)
  expect(called.length).toBe(before)
})

test('an unreachable server draws B\'s error state, and nothing retries by itself', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', answers(called, true) as never)
  placesPanes(on)
  await $.command.run({ command: 'sc-pane', args: '' })
  const ui = await mountPane($)
  expect(await ui.find({ type: 'Text', text: /SwarmCloud unreachable/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /SwarmCloud server is/ })).toBeTruthy()
  expect(called.length).toBe(1)
  await press(ui, /Retry/)
  expect(called.length).toBe(2)
})

test('/sc layout pane saves the layout with one swarm_layout call and opens the pane', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', answers(called) as never)
  placesPanes(on)
  const ran = await $.command.run({ command: 'sc', args: 'layout pane' })
  expect(called[0]).toEqual({ server: 'plugin:sc:swarmcloud', tool: 'swarm_layout', args: { layout: 'pane' } })
  expect(String(ran.text)).toContain('Layout set to pane')
  expect(called.length).toBe(2)
  expect(called[1].args).toEqual({ level: 'workflows' })
})

// ---- SC-BROWSER-FOLLOWUP (#2318): BGD's answers of 2026-10-25 ----------------

type Queued = { command: string; args: string }

/** What a run the pane module starts beneath itself reaches: `/sc attach --all` as the engine would run it. */
function runsCommands(on: any, queued: Queued[]) {
  on('command.run', async (_$: unknown, e: { command: string; args: string }) => {
    queued.push({ command: e.command, args: e.args })
    return { text: '' }
  })
}

/**
 * Session start as the REPL raises it. The module's start-up work runs after
 * the hook returns (the first session.start is awaited before the first
 * prompt), so this lets it settle: until `done` holds, then as long again.
 */
async function startSession($: any, on: any, done: () => boolean) {
  on('session.start', async (_$: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('command.register', async () => ({ value: { command: 'sc-pane' } }))
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await settle(done)
}

async function settle(done: () => boolean) {
  let ticks = 0
  while (!done() && ticks < 20000) {
    await Promise.resolve()
    ticks++
  }
  for (let i = 0; i <= ticks; i++) await Promise.resolve()
}

for (const wanted of ['rows', 'pane']) {
  test('/sc layout ' + wanted + ' saves the choice and queues ONE /sc attach --all, and says both', async ($, on) => {
    const called: Called[] = []
    const queued: Queued[] = []
    on('mcp.call', answers(called) as never)
    placesPanes(on)
    runsCommands(on, queued)
    const ran = await $.command.run({ command: 'sc', args: 'layout ' + wanted })
    expect(called[0]).toEqual({ server: 'plugin:sc:swarmcloud', tool: 'swarm_layout', args: { layout: wanted } })
    expect(queued).toEqual([{ command: 'sc', args: 'attach --all' }])
    expect(String(ran.text)).toContain('Layout set to ' + wanted)
    expect(String(ran.text)).toContain('keep going until they end')
    expect(String(ran.text)).toContain('/sc attach --all')
  })
}

test('bare /sc layout saves nothing and attaches nothing', async ($, on) => {
  const called: Called[] = []
  const queued: Queued[] = []
  on('mcp.call', answers(called) as never)
  placesPanes(on)
  runsCommands(on, queued)
  await $.command.run({ command: 'sc', args: 'layout' })
  expect(called).toEqual([{ server: 'plugin:sc:swarmcloud', tool: 'swarm_layout', args: {} }])
  expect(queued).toEqual([])
})

/** The bridge answering swarm_layout {} with `layout`, and the list read as before. */
function layoutIs(layout: string, called: Called[]) {
  const rest = answers(called)
  return async ($: unknown, e: { server: string; tool: string; args?: Record<string, unknown> }) => {
    if (e.tool === 'swarm_layout' && !(e.args && e.args.layout)) {
      called.push({ server: e.server, tool: e.tool, args: e.args || {} })
      return { value: { content: [{ type: 'text', text: JSON.stringify({ layout, source: 'saved for this project' }) }], isError: false } }
    }
    return rest($, e)
  }
}

test('in the pane layout session start opens the pane unread: one swarm_layout call, no list read until Refresh', async ($, on) => {
  const called: Called[] = []
  const opened: string[] = []
  on('mcp.call', layoutIs('pane', called) as never)
  on('ui.open', async (_$: unknown, e: { id: string }) => { opened.push(e.id); return { value: { isPlaced: true } } })
  on('clock.now', async () => ({ value: Date.parse('2026-10-25T10:48:12Z') }))
  await startSession($, on, () => opened.length > 0)
  expect(called).toEqual([{ server: 'plugin:sc:swarmcloud', tool: 'swarm_layout', args: {} }])
  expect(opened).toEqual(['swarmcloud'])

  const ui = await mountPane($)
  expect(await ui.find({ type: 'Text', text: /not read yet/i })).toBeTruthy()
  expect(called.length).toBe(1)
  await press(ui, /Refresh/)
  expect(called.length).toBe(2)
  expect(called[1]).toEqual({ server: 'plugin:sc:swarmcloud', tool: 'swarm_browse', args: { level: 'workflows', project: true } })
})

test('in the rows layout session start opens nothing and reads nothing more', async ($, on) => {
  const called: Called[] = []
  const opened: string[] = []
  on('mcp.call', layoutIs('rows', called) as never)
  on('ui.open', async (_$: unknown, e: { id: string }) => { opened.push(e.id); return { value: { isPlaced: true } } })
  await startSession($, on, () => called.length > 0)
  expect(called.map((c) => c.tool)).toEqual(['swarm_layout'])
  expect(opened).toEqual([])
})

test('below 144 columns the unasked pane waits, and session start says so in one line', async ($, on) => {
  const called: Called[] = []
  const toasts: string[] = []
  on('mcp.call', layoutIs('pane', called) as never)
  on('ui.open', async () => ({ value: { isPlaced: false, reason: 'unasked panes seat from 144 columns; the terminal is 120' } }))
  on('ui.toast', async (_$: unknown, e: { text: string }) => { toasts.push(e.text); return { value: undefined } })
  await startSession($, on, () => toasts.length > 0)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('144')
  expect(toasts[0]).toContain('/sc-pane')
  expect(toasts[0].indexOf('\n')).toBe(-1)
})

test('a second session.start (a reload) asks for the layout no second time', async ($, on) => {
  const called: Called[] = []
  on('mcp.call', layoutIs('rows', called) as never)
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  await startSession($, on, () => called.length > 0)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true } as never)
  await settle(() => called.length > 1)
  expect(called.length).toBe(1)
})
