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
